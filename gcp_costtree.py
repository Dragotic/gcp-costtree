#!/usr/bin/env python3
"""gcp-costtree: where did my GCP money go?

Reads a Cloud Billing BigQuery export (standard or detailed) and writes a
self-contained HTML treemap. Standard library only.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import http.client
import json
import math
import os
import random
import re
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

VERSION = "0.1.0"
CACHE_VERSION = 1
MAX_LABELS = 6
MAX_DAILY_ROWS = 500_000
OTHER = "(other)"
NONE_LABEL = "(none)"


class CostTreeError(Exception):
    """User-facing error: one line that says what to do next."""


class UsageError(CostTreeError):
    """Bad flags or config; nothing was queried."""


# ---------------------------------------------------------------- window & table id

def utc_today() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()


@dataclasses.dataclass(frozen=True)
class Window:
    prior_start: dt.date
    period_start: dt.date
    period_end: dt.date  # exclusive
    today: dt.date

    @property
    def last_day(self) -> dt.date:
        return self.period_end - dt.timedelta(days=1)

    @property
    def partition_end(self) -> dt.date:  # exclusive
        return self.today + dt.timedelta(days=1)


def compute_window(today: dt.date, days: int, lag_days: int, end: dt.date | None = None) -> Window:
    if not 1 <= days <= 90:
        raise UsageError("--days must be between 1 and 90")
    if not 1 <= lag_days <= 7:
        raise UsageError("--lag-days must be between 1 and 7")
    if end is not None and end >= today:
        raise UsageError(f"--end must be before today ({today.isoformat()}, UTC)")
    # --end picks the last day of the period; otherwise the last day is today minus --lag-days.
    try:
        period_end = end + dt.timedelta(days=1) if end is not None else today - dt.timedelta(days=lag_days - 1)
        period_start = period_end - dt.timedelta(days=days)
        prior_start = period_start - dt.timedelta(days=days)
        partition_start = prior_start - dt.timedelta(days=1)  # computed only to reject dates it can't reach
        del partition_start
    except OverflowError:
        raise UsageError("--end is too far in the past") from None
    return Window(prior_start, period_start, period_end, today)


_TABLE_RE = re.compile(r"^([A-Za-z0-9.:_-]+)\.([A-Za-z0-9_]+)\.([A-Za-z0-9_-]+)$")


def parse_table_id(s: str) -> tuple[str, str, str]:
    m = _TABLE_RE.match(s or "")
    if not m or s.endswith("\n"):
        raise UsageError(f"invalid table id {s!r}; expected PROJECT.DATASET.TABLE")
    return m.group(1), m.group(2), m.group(3)


# ---------------------------------------------------------------- CLI & config

DEFAULT_OUT = "out/gcp-costtree.html"
DEFAULT_EXPORT = "out/gcp-costtree-export.json"
METRICS = ("net", "cost", "list")

# key -> (CLI flag, accepted types, default)
_FETCH = {
    "table": ("--table", (str,), None),
    "job_project": ("--job-project", (str,), None),
    "days": ("--days", (int,), 30),
    "lag_days": ("--lag-days", (int,), 2),
    "end": ("--end", (str, dt.date), None),
    "labels": ("--label", (list,), []),
    "label_top": ("--label-top", (int,), 50),
    "top_resources": ("--top-resources", (int,), 25),
    "max_gb": ("--max-gb", (int, float), 50.0),
}
_RENDER = {
    "metric": ("--metric", (str,), "net"),
    "export": ("--export", (str,), None),
    "out": ("--out", (str,), DEFAULT_OUT),
    "no_open": ("--no-open", (bool,), False),
}
_LABEL_RE = re.compile(r"^(project:)?[^\s:]{1,63}$")


@dataclasses.dataclass
class Options:
    mode: str
    from_file: str | None
    table: str | None
    job_project: str | None
    days: int
    lag_days: int
    end: dt.date | None
    labels: list[str]
    label_top: int
    top_resources: int
    max_gb: float
    metric: str
    export: str | None
    out: str
    no_open: bool
    rules: dict
    categories: dict


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="gcp-costtree",
        description="Treemap of where your GCP money went, from the Cloud Billing BigQuery export.")
    p.add_argument("--table", help="export table PROJECT.DATASET.TABLE (standard or detailed)")
    p.add_argument("--job-project", dest="job_project", help="project that runs and pays for the queries")
    p.add_argument("--days", type=int, help="period length in days (default 30)")
    p.add_argument("--lag-days", dest="lag_days", type=int, help="recent days to skip as incomplete (default 2)")
    p.add_argument("--end", metavar="YYYY-MM-DD", help="last day of the period (UTC, before today); overrides --lag-days")
    p.add_argument("--label", dest="labels", action="append", help="group by a label key; project:KEY for project labels")
    p.add_argument("--label-top", dest="label_top", type=int, help="top values kept per label (default 50)")
    p.add_argument("--top-resources", dest="top_resources", type=int, help="top resources per SKU (default 25)")
    p.add_argument("--max-gb", dest="max_gb", type=float, help="per-query scan cap in GB (default 50)")
    p.add_argument("--dry-run", dest="dry_run", action="store_true", help="print bytes to scan and exit")
    p.add_argument("--from", dest="from_file", metavar="FILE", help="render from a cache file; no queries")
    p.add_argument("--demo", action="store_true", help="fake data; no GCP access")
    p.add_argument("--metric", choices=METRICS, help="initial viewer metric (default net)")
    p.add_argument("--export", nargs="?", const=DEFAULT_EXPORT, help="write the AI summary JSON")
    p.add_argument("--out", help=f"viewer output path (default {DEFAULT_OUT})")
    p.add_argument("--no-open", dest="no_open", action="store_const", const=True, help="don't open the browser")
    p.add_argument("--config", help="TOML config (default ./gcp-costtree.toml if present)")
    p.add_argument("--version", action="version", version=f"gcp-costtree {VERSION}")
    return p


def _load_config(path: Path, required: bool) -> dict:
    if not path.exists():
        if required:
            raise UsageError(f"config file not found: {path}")
        return {}
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise UsageError(f"invalid TOML in {path}: {e}") from None


def _check_type(key: str, value, types: tuple) -> None:
    ok = isinstance(value, types) and (bool in types or not isinstance(value, bool))
    if key == "labels" and ok:
        ok = all(isinstance(v, str) for v in value)
    if not ok:
        raise UsageError(f"config key {key!r} has the wrong type")


def _parse_end(value) -> dt.date:
    # TOML may hand over a date literal (end = 2026-08-25); a datetime is not a day.
    if isinstance(value, dt.datetime):
        raise UsageError("end must be a date without a time, like 2026-08-25")
    if type(value) is dt.date:
        day = value
    else:
        try:
            if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ValueError
            day = dt.date.fromisoformat(value)
        except ValueError:
            raise UsageError(f"--end must be a date like 2026-08-25, not {value!r}") from None
    if day < dt.date(2000, 1, 1):
        raise UsageError("--end must be 2000-01-01 or later")
    return day


def parse_args(argv: list[str], cwd: Path) -> Options:
    a = _parser().parse_args(argv)
    chosen = [k for k in ("dry_run", "from_file", "demo") if getattr(a, k)]
    if len(chosen) > 1:
        raise UsageError("--dry-run, --from and --demo cannot be combined")
    mode = {"dry_run": "dry-run", "from_file": "cache", "demo": "demo"}[chosen[0]] if chosen else "live"

    cfg = _load_config(Path(a.config) if a.config else cwd / "gcp-costtree.toml", required=bool(a.config))
    rules = cfg.pop("rules", {})
    categories = cfg.pop("categories", {})
    if not isinstance(rules, dict) or not isinstance(categories, dict):
        raise UsageError("[rules] and [categories] must be tables")
    for key, value in cfg.items():
        spec = _FETCH.get(key) or _RENDER.get(key)
        if spec is None:
            raise UsageError(f"unknown config key {key!r}")
        _check_type(key, value, spec[1])

    querying = mode in ("live", "dry-run")
    if not querying:
        passed = [flag for key, (flag, _, _) in _FETCH.items() if getattr(a, key) is not None]
        if passed:
            verb = "applies" if len(passed) == 1 else "apply"
            raise UsageError(f"{', '.join(passed)} only {verb} when querying BigQuery, not with --from or --demo")

    def pick(key, table):
        cli = getattr(a, key)
        if cli is not None:
            return cli
        if table is _FETCH and not querying:
            return table[key][2]
        return cfg.get(key, table[key][2])

    o = Options(mode=mode, from_file=a.from_file,
                **{k: pick(k, _FETCH) for k in _FETCH},
                **{k: pick(k, _RENDER) for k in _RENDER},
                rules=rules, categories=categories)
    o.labels = list(o.labels)
    o.max_gb = float(o.max_gb)
    if o.metric not in METRICS:
        raise UsageError(f"metric must be one of {', '.join(METRICS)}")
    if not 1 <= o.days <= 90:
        raise UsageError("--days must be between 1 and 90")
    if not 1 <= o.lag_days <= 7:
        raise UsageError("--lag-days must be between 1 and 7")
    if o.end is not None:
        o.end = _parse_end(o.end)
    if not 1 <= o.label_top <= 1000 or not 1 <= o.top_resources <= 1000:
        raise UsageError("--label-top and --top-resources must be between 1 and 1000")
    if not math.isfinite(o.max_gb) or o.max_gb <= 0:
        raise UsageError("--max-gb must be a finite number greater than 0")
    if len(o.labels) > MAX_LABELS:
        raise UsageError(f"at most {MAX_LABELS} labels")
    if len(set(o.labels)) != len(o.labels):
        raise UsageError("duplicate --label")
    for label in o.labels:
        if not _LABEL_RE.match(label):
            raise UsageError(f"invalid label key {label!r}")
    if querying:
        if not o.table:
            raise UsageError("--table is required (or set table in gcp-costtree.toml)")
        parse_table_id(o.table)
    return o


# ---------------------------------------------------------------- SQL

@dataclasses.dataclass(frozen=True)
class TableInfo:
    table_id: str
    project: str
    dataset: str
    table: str
    location: str
    export: str  # "standard" | "detailed"
    partition_column: str | None
    partition_type: str  # "TIMESTAMP" | "DATE" | "DATETIME"


SNAPSHOT_SQL = "SELECT FORMAT_TIMESTAMP('%Y-%m-%d %H:%M:%E6S+00', CURRENT_TIMESTAMP()) AS ts"

_BASE_COLUMNS = [
    "IF(cost_type = 'regular', IFNULL(service.id, '(unknown)'), '_tax') AS service_id",
    "IF(cost_type = 'regular', IFNULL(service.description, '(unknown)'), 'Tax & adjustments') AS service",
    "IF(cost_type = 'regular', IFNULL(sku.id, '(unknown)'), cost_type) AS sku_id",
    "IF(cost_type = 'regular', IFNULL(sku.description, '(unknown)'), cost_type) AS sku",
    "IFNULL(usage.pricing_unit, '') AS unit",
    "IFNULL(project.id, '(no project)') AS project",
    "COALESCE(location.region, location.location, '(unspecified)') AS region",
    "cost_type",
    "DATE(usage_start_time) AS usage_day",
    "currency",
    "export_time",
    "CAST(cost AS NUMERIC) AS cost",
    "IFNULL((SELECT SUM(CAST(c.amount AS NUMERIC)) FROM UNNEST(credits) c), 0) AS credits",
    "CAST(cost_at_list AS NUMERIC) AS list_cost",
    "IFNULL(usage.amount_in_pricing_units, 0) AS usage_amount",
]
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _partition_ref(column: str) -> str:
    if column == "_PARTITIONTIME":
        return column
    if not _IDENT_RE.match(column):
        raise CostTreeError(f"unexpected partition column name {column!r}")
    return f"`{column}`"


def _label_expr(i: int, spec: str) -> str:
    source = "project.labels" if spec.startswith("project:") else "labels"
    return (f"IFNULL((SELECT l.value FROM UNNEST({source}) l WHERE l.key = @label_key_{i} LIMIT 1), "
            f"'{NONE_LABEL}') AS label_{i}")


def _base_cte(info: TableInfo, labels: list[str], *, resource: bool) -> str:
    cols = _BASE_COLUMNS + [_label_expr(i, s) for i, s in enumerate(labels)]
    if resource:
        cols = cols + ["COALESCE(resource.global_name, resource.name, '(no resource)') AS resource"]
    where = []
    if info.partition_column:
        ref = _partition_ref(info.partition_column)
        where += [f"{ref} >= @part_start", f"{ref} < @part_end"]
    where += ["usage_start_time >= TIMESTAMP(@prior_start)", "usage_start_time < TIMESTAMP(@period_end)"]
    if resource:
        where.append("cost_type = 'regular'")
    select = ",\n    ".join(cols)
    conditions = "\n    AND ".join(where)
    return (f"base AS (\n  SELECT\n    {select}\n"
            f"  FROM `{info.table_id}` FOR SYSTEM_TIME AS OF @snapshot_ts\n"
            f"  WHERE {conditions}\n)")


def build_q1(info: TableInfo, labels: list[str]) -> str:
    ctes = [_base_cte(info, labels, resource=False)]
    label_cols = [f"label_{i}" for i in range(len(labels))]
    source = "base"
    if labels:
        for c in label_cols:
            ctes.append(f"top_{c} AS (\n  SELECT {c} AS v FROM base WHERE usage_day >= @period_start\n"
                        f"  GROUP BY v\n  ORDER BY SUM(cost + credits) DESC, v\n  LIMIT @label_top\n)")
        replace = ", ".join(f"IF({c} IN (SELECT v FROM top_{c}), {c}, '{OTHER}') AS {c}" for c in label_cols)
        ctes.append(f"folded AS (\n  SELECT * REPLACE ({replace}) FROM base\n)")
        source = "folded"
    keys = ", ".join(["service_id", "sku_id", "unit", "project", "region", "cost_type",
                      *label_cols, "usage_day", "currency"])
    aggs = ", ".join([
        "ANY_VALUE(service) AS service", "ANY_VALUE(sku) AS sku", "SUM(cost) AS cost",
        "SUM(credits) AS credits", "IFNULL(SUM(list_cost), 0) AS list_cost",
        "SUM(usage_amount) AS usage_amount", "COUNTIF(list_cost IS NULL AND cost_type = 'regular') AS list_nulls",
        "MAX(export_time) AS latest_export"])
    with_block = ",\n".join(ctes)
    return f"WITH {with_block}\nSELECT {keys},\n  {aggs}\nFROM {source}\nGROUP BY {keys}"


_MONEY6 = ["cur_cost", "cur_credits", "cur_list", "prior_cost", "prior_credits", "prior_list"]


def build_q2(info: TableInfo) -> str:
    base = _base_cte(info, [], resource=True)
    sums = []
    for prefix, cond in (("cur", "usage_day >= @period_start"), ("prior", "usage_day < @period_start")):
        sums += [f"SUM(IF({cond}, cost, 0)) AS {prefix}_cost",
                 f"SUM(IF({cond}, credits, 0)) AS {prefix}_credits",
                 f"IFNULL(SUM(IF({cond}, list_cost, NULL)), 0) AS {prefix}_list"]
    sums_sql = ",\n    ".join(sums)
    agg = ("agg AS (\n  SELECT service_id, sku_id, project, region, resource,\n    "
           f"{sums_sql}\n  FROM base\n  GROUP BY service_id, sku_id, project, region, resource\n)")
    m6 = ", ".join(_MONEY6)
    top = ("SELECT 'resource' AS row_kind, service_id, sku_id, project, region, resource, " + m6 + "\n"
           "  FROM agg\n  WHERE TRUE\n  QUALIFY ROW_NUMBER() OVER (\n    PARTITION BY service_id, sku_id\n"
           "    ORDER BY GREATEST(cur_cost + cur_credits, prior_cost + prior_credits) DESC, project, region, resource\n"
           "  ) <= @top")
    totals_sums = ", ".join(f"SUM({c}) AS {c}" for c in _MONEY6)
    totals = ("SELECT 'total' AS row_kind, service_id, sku_id, project, region, CAST(NULL AS STRING) AS resource, "
              f"{totals_sums}\nFROM agg\nGROUP BY service_id, sku_id, project, region")
    return f"WITH {base},\n{agg}\n(\n  {top}\n)\nUNION ALL\n{totals}"


def _param(name: str, type_: str, value) -> dict:
    return {"name": name, "parameterType": {"type": type_}, "parameterValue": {"value": str(value)}}


def _partition_value(d: dt.date, ptype: str) -> str:
    if ptype == "DATE":
        return d.isoformat()
    if ptype == "DATETIME":
        return f"{d.isoformat()} 00:00:00"
    return f"{d.isoformat()} 00:00:00+00"


def query_params(info: TableInfo, window: Window, snapshot_ts: str, *,
                 labels=(), label_top=None, top=None) -> list[dict]:
    ps = [_param("prior_start", "DATE", window.prior_start.isoformat()),
          _param("period_end", "DATE", window.period_end.isoformat()),
          _param("snapshot_ts", "TIMESTAMP", snapshot_ts)]
    if labels or top is not None:
        ps.append(_param("period_start", "DATE", window.period_start.isoformat()))
    if info.partition_column:
        # One day of slack: the partition date is not guaranteed to be the UTC usage day.
        ps.append(_param("part_start", info.partition_type,
                         _partition_value(window.prior_start - dt.timedelta(days=1), info.partition_type)))
        ps.append(_param("part_end", info.partition_type, _partition_value(window.partition_end, info.partition_type)))
    for i, spec in enumerate(labels):
        ps.append(_param(f"label_key_{i}", "STRING", spec.split(":", 1)[1] if spec.startswith("project:") else spec))
    if labels:
        ps.append(_param("label_top", "INT64", label_top))
    if top is not None:
        ps.append(_param("top", "INT64", top))
    return ps


# ---------------------------------------------------------------- BigQuery client

API = "https://bigquery.googleapis.com/bigquery/v2"
_RETRY_STATUS = {429, 500, 503}
_RETRY_REASONS = {"backendError", "rateLimitExceeded"}
_REQUIRED_PERMS = ("bigquery.jobs.create on the job project, and bigquery.tables.get + "
                   "bigquery.tables.getData on the export table")


def _checked_token(token: str, source: str) -> str:
    # Never echo the value: a bad token would otherwise surface in an HTTP header error.
    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in token):
        raise CostTreeError(f"the token from {source} contains whitespace or control characters; check its value")
    return token


def get_token(env=None, run=subprocess.run) -> str:
    env = os.environ if env is None else env
    token = (env.get("GCP_COSTTREE_TOKEN") or "").strip()
    if token:
        return _checked_token(token, "GCP_COSTTREE_TOKEN")
    try:
        p = run(["gcloud", "auth", "print-access-token"], capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        raise CostTreeError("gcloud not found; install the Google Cloud SDK or set GCP_COSTTREE_TOKEN") from None
    except subprocess.TimeoutExpired:
        raise CostTreeError("gcloud auth print-access-token timed out; run: gcloud auth login") from None
    token = (p.stdout or "").strip()
    if p.returncode != 0 or not token:
        raise CostTreeError("no valid gcloud credentials; run: gcloud auth login")
    return _checked_token(token, "gcloud auth print-access-token")


class UrllibTransport:
    def request(self, method: str, url: str, body, headers: dict) -> tuple[int, dict]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except ValueError:
                # No truncation here: the client redacts the token first, then map_error truncates.
                return e.code, {"error": {"message": raw.decode("utf-8", "replace")}}
        except urllib.error.URLError as e:
            raise CostTreeError(f"cannot reach BigQuery: {e.reason}") from None
        except (OSError, http.client.HTTPException) as e:
            raise CostTreeError(f"cannot reach BigQuery: {type(e).__name__}: {e}") from None


def _reason(payload) -> str:
    try:
        return payload["error"]["errors"][0]["reason"]
    except (KeyError, IndexError, TypeError):
        return ""


def map_error(status: int, payload) -> CostTreeError:
    err = payload.get("error", {}) if isinstance(payload, dict) else {}
    msg = str(err.get("message", "")).strip()[:300] or f"HTTP {status}"
    reason = _reason(payload)
    if status == 401 or reason == "authError":
        return CostTreeError("credentials rejected or expired; run: gcloud auth login")
    if reason == "accessDenied" or (status == 403 and reason in ("", "forbidden")):
        return CostTreeError(f"access denied: {msg}. Needs {_REQUIRED_PERMS}")
    if reason == "notFound" or status == 404:
        return CostTreeError(f"not found: {msg}. Check the name with: bq ls <project>:<dataset>")
    if reason == "bytesBilledLimitExceeded":
        return CostTreeError(f"query hit the --max-gb cap: {msg}. Raise --max-gb or lower --days")
    if reason in ("quotaExceeded", "rateLimitExceeded"):
        return CostTreeError(f"BigQuery quota or rate limit: {msg}")
    return CostTreeError(f"BigQuery error ({reason or status}): {msg}")


def _parse_value(v, field: dict):
    if v is None:
        return None
    if field.get("mode") == "REPEATED":
        return [_parse_value(x["v"], {**field, "mode": "NULLABLE"}) for x in v]
    t = field.get("type")
    if t in ("NUMERIC", "BIGNUMERIC"):
        return Decimal(v)
    if t in ("INTEGER", "INT64"):
        return int(v)
    if t in ("FLOAT", "FLOAT64"):
        return float(v)
    if t in ("BOOLEAN", "BOOL"):
        return v == "true"
    if t == "TIMESTAMP":
        return dt.datetime.fromtimestamp(float(v), dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    if t in ("RECORD", "STRUCT"):
        return parse_row(v, field["fields"])
    return v


def parse_row(row: dict, fields: list[dict]) -> dict:
    return {f["name"]: _parse_value(c.get("v"), f) for f, c in zip(fields, row["f"])}


def _q(s: str) -> str:
    return urllib.parse.quote(s, safe="")


class BigQuery:
    def __init__(self, token: str, transport, *, sleep=time.sleep, clock=time.monotonic,
                 poll_timeout: float = 600.0, retries: int = 3):
        self._token = token
        self._transport = transport
        self._sleep = sleep
        self._clock = clock
        self.poll_timeout = poll_timeout
        self.retries = retries

    def _call(self, method: str, path: str, body=None, *, retry: bool = True) -> dict:
        headers = {"Authorization": "Bearer " + self._token, "Content-Type": "application/json"}
        attempts = self.retries + 1 if retry else 1
        delay = 1.0
        for attempt in range(attempts):
            status, payload = self._transport.request(method, API + path, body, headers)
            if status < 400:
                return payload
            if attempt < attempts - 1 and (status in _RETRY_STATUS or _reason(payload) in _RETRY_REASONS):
                self._sleep(delay)
                delay *= 2
                continue
            # Redact before map_error truncates, so no fragment of the token can survive the cut.
            payload = json.loads(json.dumps(payload).replace(self._token, "[redacted]"))
            raise CostTreeError(" ".join(str(map_error(status, payload)).split())) from None
        raise AssertionError("unreachable")

    def get_table(self, project: str, dataset: str, table: str) -> dict:
        return self._call("GET", f"/projects/{_q(project)}/datasets/{_q(dataset)}/tables/{_q(table)}")

    def _results_path(self, job: dict, page_token) -> str:
        qs = {"location": job.get("location", ""), "timeoutMs": "10000"}
        if page_token:
            qs["pageToken"] = page_token
        return (f"/projects/{_q(job['projectId'])}/queries/{_q(job['jobId'])}?"
                + urllib.parse.urlencode(qs))

    def query(self, project: str, sql: str, params: list, location: str, max_bytes: int, *, dry_run=False,
              max_rows: int | None = None):
        body = {"query": sql, "useLegacySql": False, "parameterMode": "NAMED", "queryParameters": params,
                "location": location, "timeoutMs": 10000, "maximumBytesBilled": str(int(max_bytes)),
                "requestId": str(uuid.uuid4()), "dryRun": dry_run}
        # Never retried: requestId does not deduplicate read-only queries, so a retry could bill twice.
        resp = self._call("POST", f"/projects/{_q(project)}/queries", body, retry=False)
        if dry_run:
            return int(resp.get("totalBytesProcessed", 0))
        job = resp.get("jobReference", {})
        deadline = self._clock() + self.poll_timeout
        delay = 0.5
        while not resp.get("jobComplete"):
            if self._clock() > deadline:
                raise CostTreeError("query did not finish within 10 minutes; try fewer --days or labels")
            self._sleep(delay)
            delay = min(delay * 2, 8.0)
            resp = self._call("GET", self._results_path(job, None))
        fields = resp.get("schema", {}).get("fields", [])
        out = [parse_row(r, fields) for r in resp.get("rows", [])]
        token = resp.get("pageToken")
        while True:
            if max_rows is not None and len(out) > max_rows:
                raise CostTreeError(f"more than {max_rows:,} rows, over the limit; use fewer --label keys, "
                                    "a smaller --label-top, or fewer --days")
            if not token:
                return out
            resp = self._call("GET", self._results_path(job, token))
            out += [parse_row(r, fields) for r in resp.get("rows", [])]
            token = resp.get("pageToken")


# ---------------------------------------------------------------- fetch & cache model

DIM_COLUMNS = ["service", "sku", "unit", "project", "region", "cost_type"]
RESOURCE_COLUMNS = ["service", "sku", "project", "region", "resource"] + _MONEY6
_REQUIRED_COLUMNS = {"service", "sku", "usage_start_time", "project", "labels", "location", "export_time",
                     "cost", "currency", "usage", "credits", "cost_type", "cost_at_list"}
NO_ROWS_MESSAGE = ("no rows in the window; if the export was enabled recently, data can take hours to "
                   "arrive, and while it backfills set --end to the last day with data. Or try --demo")


def daily_columns_for(labels: list[str]) -> list[str]:
    return DIM_COLUMNS + [f"label:{l}" for l in labels] + ["day", "cost", "credits", "list", "usage"]


def table_info_from_metadata(table_id: str, meta: dict) -> TableInfo:
    project, dataset, table = parse_table_id(table_id)
    fields = {f["name"]: f for f in meta.get("schema", {}).get("fields", [])}
    missing = sorted(_REQUIRED_COLUMNS - set(fields))
    if missing:
        raise CostTreeError(f"{table_id} does not look like a Cloud Billing export (missing {', '.join(missing)})")
    tp = meta.get("timePartitioning")
    if tp and tp.get("field"):
        column, ptype = tp["field"], fields.get(tp["field"], {}).get("type", "TIMESTAMP")
    elif tp:
        column, ptype = "_PARTITIONTIME", "TIMESTAMP"
    else:
        column, ptype = None, "TIMESTAMP"
    if ptype not in ("TIMESTAMP", "DATE", "DATETIME"):
        raise CostTreeError(f"unsupported partition column type {ptype}")
    export = "detailed" if "resource" in fields else "standard"
    return TableInfo(table_id, project, dataset, table, meta.get("location", "US"), export, column, ptype)


def inspect_table(bq, table_id: str) -> TableInfo:
    return table_info_from_metadata(table_id, bq.get_table(*parse_table_id(table_id)))


class _Dim:
    def __init__(self, pairs: bool = False):
        self.values: list = []
        self._index: dict = {}
        self._pairs = pairs

    def add(self, key, desc=None) -> int:
        i = self._index.get(key)
        if i is None:
            i = self._index[key] = len(self.values)
            self.values.append([key, desc] if self._pairs else key)
        return i

    def get(self, key) -> int:
        return self._index[key]


def _dec(v) -> Decimal:
    return v if isinstance(v, Decimal) else Decimal(str(v))


def _money(v) -> float:
    return float(_dec(v).quantize(Decimal("0.000001")))


def _resource_rows(dims: dict, key_sums: dict, rows2: list) -> list:
    totals, kept, found = {}, {}, []
    for r in rows2:
        key = (r["service_id"], r["sku_id"], r["project"], r["region"])
        vals = [_dec(r[c]) for c in _MONEY6]
        if r["row_kind"] == "total":
            totals[key] = vals
            continue
        acc = kept.setdefault(key, [Decimal(0)] * 6)
        for j in range(6):
            acc[j] += vals[j]
        found.append((key, r["resource"], vals))
    diff = set(key_sums) ^ set(totals)
    if diff:
        raise CostTreeError(f"daily and resource queries disagree on {len(diff)} key(s), e.g. {sorted(diff)[0]}; "
                            "re-run, and report a bug if it persists")
    for key, tot in totals.items():
        if tot != key_sums[key]:
            raise CostTreeError(f"daily and resource totals disagree for {key}; re-run, and report a bug if it persists")
    for key in sorted(totals):
        other = [t - k for t, k in zip(totals[key], kept.get(key, [Decimal(0)] * 6))]
        if any(other):
            found.append((key, OTHER, other))
    out = []
    for (sid, kid, project, region), name, vals in found:
        out.append([dims["service"].get(sid), dims["sku"].get(kid), dims["project"].get(project),
                    dims["region"].get(region), name] + [_money(v) for v in vals])
    return out


def _check_currency(rows1: list) -> list:
    currencies = sorted({r["currency"] for r in rows1})
    if len(currencies) > 1:
        raise CostTreeError(f"the export has more than one currency ({', '.join(currencies)}); not supported")
    return currencies


def build_cache(info: TableInfo, window: Window, labels: list[str], label_top: int, top_resources: int,
                snapshot: str, rows1: list, rows2: list, fetched_at: str) -> dict:
    if len(rows1) > MAX_DAILY_ROWS:
        raise CostTreeError(f"{len(rows1):,} daily rows is over the {MAX_DAILY_ROWS:,} limit; use fewer --label "
                            "keys, a smaller --label-top, or fewer --days")
    currencies = _check_currency(rows1)
    dims = {"service": _Dim(True), "sku": _Dim(True), **{c: _Dim() for c in DIM_COLUMNS[2:]},
            **{f"label:{l}": _Dim() for l in labels}}
    period_start = window.period_start.isoformat()
    daily, key_sums = [], {}
    nulls = {"period": 0, "prior": 0}
    earliest = latest = last_seen = None
    # Earliest day first, so a renamed SKU keeps its first-seen description whatever order BigQuery returns.
    for r in sorted(rows1, key=lambda r: r["usage_day"]):
        day = r["usage_day"]
        current = day >= period_start
        cost, credits, list_cost = _dec(r["cost"]), _dec(r["credits"]), _dec(r["list_cost"])
        row = [dims["service"].add(r["service_id"], r["service"]), dims["sku"].add(r["sku_id"], r["sku"]),
               dims["unit"].add(r["unit"]), dims["project"].add(r["project"]), dims["region"].add(r["region"]),
               dims["cost_type"].add(r["cost_type"])]
        row += [dims[f"label:{l}"].add(r[f"label_{i}"]) for i, l in enumerate(labels)]
        row += [day, _money(cost), _money(credits), _money(list_cost), float(r["usage_amount"] or 0)]
        daily.append(row)
        nulls["period" if current else "prior"] += int(r["list_nulls"] or 0)
        if last_seen is None or day > last_seen:
            last_seen = day
        if earliest is None or day < earliest:
            earliest = day
        if r["latest_export"] and (latest is None or r["latest_export"] > latest):
            latest = r["latest_export"]
        if r["cost_type"] == "regular":
            acc = key_sums.setdefault((r["service_id"], r["sku_id"], r["project"], r["region"]), [Decimal(0)] * 6)
            off = 0 if current else 3
            acc[off] += cost
            acc[off + 1] += credits
            acc[off + 2] += list_cost
    resources = _resource_rows(dims, key_sums, rows2) if info.export == "detailed" else []
    last = window.last_day.isoformat()
    meta = {
        "table": info.table_id,
        "table_hash": hashlib.sha256(info.table_id.encode()).hexdigest()[:16],
        "export": info.export,
        "currency": currencies[0] if currencies else "USD",
        "period": [period_start, last],
        "prior": [window.prior_start.isoformat(), (window.period_start - dt.timedelta(days=1)).isoformat()],
        "provisional_day": last,
        "prior_covered": earliest is not None and earliest <= window.prior_start.isoformat(),
        "period_covered": earliest is not None and earliest <= period_start,
        # A backfilling export can stop before the period ends; one missing day is normal export lag.
        "data_through": last_seen,
        "period_end_covered": last_seen is not None and last_seen >= (window.last_day - dt.timedelta(days=1)).isoformat(),
        "list_complete": {"period": nulls["period"] == 0, "prior": nulls["prior"] == 0},
        "labels": list(labels),
        "label_top": label_top,
        "top_resources": top_resources,
        "fetched_at": fetched_at,
        "snapshot_time": snapshot,
        "latest_export_seen": latest,
    }
    return {"version": CACHE_VERSION, "meta": meta, "dims": {k: d.values for k, d in dims.items()},
            "daily_columns": daily_columns_for(labels), "daily": daily,
            "resource_columns": list(RESOURCE_COLUMNS), "resources": resources}


def _plan(info: TableInfo, opts: Options, window: Window, snapshot: str) -> list:
    plan = [("daily", build_q1(info, opts.labels),
             query_params(info, window, snapshot, labels=opts.labels, label_top=opts.label_top))]
    if info.export == "detailed":
        plan.append(("resources", build_q2(info), query_params(info, window, snapshot, top=opts.top_resources)))
    return plan


def _cap(opts: Options) -> int:
    return int(opts.max_gb * 10**9)


def _format_ts(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f+00")


def estimate(bq, opts: Options, today: dt.date, now: dt.datetime | None = None) -> dict:
    info = inspect_table(bq, opts.table)
    window = compute_window(today, opts.days, opts.lag_days, opts.end)
    # A dry run reads nothing, so a local "a minute ago" snapshot is enough.
    snapshot = _format_ts((now or dt.datetime.now(dt.timezone.utc)) - dt.timedelta(minutes=1))
    job_project = opts.job_project or info.project
    return {name: bq.query(job_project, sql, params, info.location, _cap(opts), dry_run=True)
            for name, sql, params in _plan(info, opts, window, snapshot)}


def fetch(bq, opts: Options, today: dt.date, now: dt.datetime | None = None) -> dict:
    info = inspect_table(bq, opts.table)
    window = compute_window(today, opts.days, opts.lag_days, opts.end)
    job_project = opts.job_project or info.project
    cap = _cap(opts)
    snapshot = bq.query(job_project, SNAPSHOT_SQL, [], info.location, cap)[0]["ts"]
    plan = _plan(info, opts, window, snapshot)
    for name, sql, params in plan:
        scanned = bq.query(job_project, sql, params, info.location, cap, dry_run=True)
        if scanned > cap:
            raise CostTreeError(f"the {name} query would scan {scanned / 1e9:.2f} GB, over --max-gb "
                                f"{opts.max_gb:g}; raise --max-gb or lower --days")
    results = {}
    for name, sql, params in plan:
        results[name] = bq.query(job_project, sql, params, info.location, cap,
                                 max_rows=MAX_DAILY_ROWS if name == "daily" else None)
        if name == "daily":
            if not results["daily"]:
                raise CostTreeError(NO_ROWS_MESSAGE)
            _check_currency(results["daily"])  # fail before paying for the resource query
    fetched_at = (now or dt.datetime.now(dt.timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return build_cache(info, window, opts.labels, opts.label_top, opts.top_resources, snapshot,
                       results["daily"], results.get("resources", []), fetched_at)


# ---------------------------------------------------------------- cache I/O

_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_META_TYPES = {"table": str, "table_hash": str, "export": str, "currency": str, "period": list, "prior": list,
               "provisional_day": str, "prior_covered": bool, "period_covered": bool, "list_complete": dict,
               "labels": list, "label_top": int, "top_resources": int, "fetched_at": str}


def save_cache(cache: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")


def load_cache(path: Path) -> dict:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise CostTreeError(f"cache file not found: {path}") from None
    except ValueError as e:
        raise CostTreeError(f"invalid cache: not JSON ({e})") from None
    validate_cache(d)
    return d


def _is_num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def validate_cache(d: dict) -> None:
    def bad(field: str):
        raise CostTreeError(f"invalid cache: {field}")

    if not isinstance(d, dict):
        bad("top level")
    if d.get("version") != CACHE_VERSION:
        bad(f"version {d.get('version')!r} (expected {CACHE_VERSION}); re-fetch without --from")
    meta = d.get("meta")
    if not isinstance(meta, dict):
        bad("meta")
    for key, typ in _META_TYPES.items():
        if not isinstance(meta.get(key), typ) or (typ is int and isinstance(meta.get(key), bool)):
            bad(f"meta.{key}")
    labels = meta["labels"]
    if not all(isinstance(l, str) for l in labels):
        bad("meta.labels")
    if meta["export"] not in ("standard", "detailed"):
        bad("meta.export")
    def day(v, field):
        try:
            if isinstance(v, str) and _DAY_RE.match(v):
                return dt.date.fromisoformat(v)
        except ValueError:
            pass
        bad(field)

    dates = {}
    for key in ("period", "prior"):
        v = meta[key]
        if len(v) != 2:
            bad(f"meta.{key}")
        dates[key] = [day(x, f"meta.{key}") for x in v]
        if dates[key][0] > dates[key][1]:
            bad(f"meta.{key}")
    if dates["prior"][1] + dt.timedelta(days=1) != dates["period"][0]:
        bad("meta.prior")
    if day(meta["provisional_day"], "meta.provisional_day") != dates["period"][1]:
        bad("meta.provisional_day")
    if len(labels) > MAX_LABELS or len(set(labels)) != len(labels) or not all(_LABEL_RE.match(l) for l in labels):
        bad("meta.labels")
    if (dates["period"][1] - dates["period"][0]) != (dates["prior"][1] - dates["prior"][0]):
        bad("meta.prior")
    for key in ("fetched_at", "snapshot_time", "latest_export_seen", "table", "table_hash"):
        v = meta.get(key)
        if v is not None and (not isinstance(v, str) or len(v) > 300):
            bad(f"meta.{key}")
    first_day, last_day = meta["prior"][0], meta["period"][1]
    if not re.fullmatch(r"[A-Z]{3}", meta["currency"]):
        bad("meta.currency")
    if "demo" in meta and not isinstance(meta["demo"], bool):
        bad("meta.demo")
    if "period_end_covered" in meta and not isinstance(meta["period_end_covered"], bool):
        bad("meta.period_end_covered")
    if meta.get("data_through") is not None:
        day(meta["data_through"], "meta.data_through")
    lc = meta["list_complete"]
    if set(lc) != {"period", "prior"} or not all(isinstance(v, bool) for v in lc.values()):
        bad("meta.list_complete")
    columns = daily_columns_for(labels)
    if d.get("daily_columns") != columns:
        bad("daily_columns")
    if d.get("resource_columns") != RESOURCE_COLUMNS:
        bad("resource_columns")
    dims = d.get("dims")
    if not isinstance(dims, dict):
        bad("dims")
    dim_names = columns[: len(columns) - 5]
    for name in dim_names:
        values = dims.get(name)
        if not isinstance(values, list):
            bad(f"dims.{name}")
        for v in values:
            ok = (isinstance(v, list) and len(v) == 2 and all(isinstance(x, str) for x in v)) \
                if name in ("service", "sku") else isinstance(v, str)
            if not ok:
                bad(f"dims.{name}")
    sizes = {name: len(dims[name]) for name in dim_names}

    def check_index(where: str, name: str, value) -> None:
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value < sizes[name]:
            bad(f"{where}.{name}")

    daily = d.get("daily")
    if not isinstance(daily, list):
        bad("daily")
    for i, row in enumerate(daily):
        where = f"daily[{i}]"
        if not isinstance(row, list) or len(row) != len(columns):
            bad(f"{where} length")
        for j, name in enumerate(dim_names):
            check_index(where, name, row[j])
        n = len(dim_names)
        if not isinstance(row[n], str) or not _DAY_RE.match(row[n]):
            bad(f"{where}.day")
        try:
            dt.date.fromisoformat(row[n])
        except ValueError:
            bad(f"{where}.day")
        if not first_day <= row[n] <= last_day:
            bad(f"{where}.day")
        for j, name in enumerate(("cost", "credits", "list", "usage")):
            if not _is_num(row[n + 1 + j]):
                bad(f"{where}.{name}")
    resources = d.get("resources")
    if not isinstance(resources, list):
        bad("resources")
    for i, row in enumerate(resources):
        where = f"resources[{i}]"
        if not isinstance(row, list) or len(row) != len(RESOURCE_COLUMNS):
            bad(f"{where} length")
        for j, name in enumerate(("service", "sku", "project", "region")):
            check_index(where, name, row[j])
        if not isinstance(row[4], str):
            bad(f"{where}.resource")
        for j, name in enumerate(_MONEY6):
            if not _is_num(row[5 + j]):
                bad(f"{where}.{name}")


# ---------------------------------------------------------------- categories & rules

CATEGORIES = ("Compute", "Storage", "Network", "Databases", "Data & Analytics", "Logging & Ops",
              "Tax & Support", "Other")
DEFAULT_CATEGORIES = {
    **dict.fromkeys(["Compute Engine", "Cloud Run", "Cloud Run Functions", "Cloud Functions", "Kubernetes Engine",
                     "App Engine", "VM Manager", "Cloud Build", "Batch"], "Compute"),
    **dict.fromkeys(["Cloud Storage", "Artifact Registry", "Container Registry", "Filestore", "Backup and DR Service",
                     "Firebase Hosting"], "Storage"),
    **dict.fromkeys(["Networking", "Cloud Load Balancing", "Cloud DNS", "Cloud CDN", "Cloud Armor", "Cloud IDS",
                     "Network Intelligence Center", "Cloud Interconnect", "Cloud VPN"], "Network"),
    **dict.fromkeys(["Cloud SQL", "Cloud Memorystore for Redis", "Cloud Memorystore", "Cloud Spanner", "Cloud Bigtable",
                     "Cloud Firestore", "AlloyDB for PostgreSQL", "Elastic Cloud (Elasticsearch Service)"], "Databases"),
    **dict.fromkeys(["BigQuery", "BigQuery Reservation API", "BigQuery Storage API", "Cloud Dataflow", "Datastream",
                     "Cloud Composer", "Cloud Pub/Sub", "Dataplex", "Cloud Dataproc", "Managed Service for Apache Kafka",
                     "Looker"], "Data & Analytics"),
    **dict.fromkeys(["Cloud Logging", "Cloud Monitoring", "Cloud Trace", "Error Reporting", "Cloud Scheduler",
                     "Secret Manager", "Cloud Key Management Service (KMS)", "Container Registry Vulnerability Scanning",
                     "Security Command Center"], "Logging & Ops"),
    **dict.fromkeys(["Support", "Invoice", "Tax & adjustments"], "Tax & Support"),
}


def check_categories(overrides: dict) -> None:
    for name, cat in overrides.items():
        if cat not in CATEGORIES:
            raise UsageError(f"[categories] {name!r}: unknown category {cat!r}; use one of {', '.join(CATEGORIES)}")


def categorize(cache: dict, overrides: dict) -> dict:
    check_categories(overrides)
    out = {}
    for sid, desc in cache["dims"]["service"]:
        out[sid] = overrides.get(desc) or DEFAULT_CATEGORIES.get(desc) or ("Tax & Support" if sid == "_tax" else "Other")
    return out


def aggregate(cache: dict, dims: tuple) -> dict:
    cols = cache["daily_columns"]
    pos = [cols.index(d) for d in dims]
    iday = cols.index("day")
    start = cache["meta"]["period"][0]
    out: dict = {}
    for r in cache["daily"]:
        key = tuple(r[p] for p in pos)
        acc = out.get(key)
        if acc is None:
            acc = out[key] = [0] * 6
        off = 0 if r[iday] >= start else 3
        # Cache money has 6 decimals, so integer micro-units sum exactly (and fast) at any row count.
        acc[off] += round(r[iday + 1] * 1_000_000)
        acc[off + 1] += round(r[iday + 2] * 1_000_000)
        acc[off + 2] += round(r[iday + 3] * 1_000_000)
    return {k: [v / 1_000_000 for v in acc] for k, acc in out.items()}


BUILTIN_RULES = [
    {"id": "nat-processing", "type": "sku", "service": r"^Networking$", "sku": r"(?i)\bnat\b.*data processing",
     "message": "NAT data processing is billed per GB. Check which workloads send traffic through NAT; "
                "Google API traffic can use Private Google Access instead."},
    {"id": "psc-processing", "type": "sku", "service": r".", "sku": r"(?i)private service connect.*data processing",
     "message": "PSC endpoints bill per GB processed. Check which consumers drive the volume."},
    {"id": "vended-network-logs", "type": "sku", "service": r".", "sku": r"(?i)vended logs|flow logs|firewall logs",
     "message": "Check whether these logs are used; lower sampling or disable where unused."},
    {"id": "logging-ingestion", "type": "sku", "service": r"^Cloud Logging$", "sku": r"(?i)^log storage cost$",
     "message": "Check the largest log names; exclusion filters can drop unused logs."},
    {"id": "unused-static-ip", "type": "sku", "service": r"^Compute Engine$", "sku": r"(?i)^static ip charge",
     "message": "Check whether the address is still attached or needed before releasing it."},
    {"id": "legacy-machine-family", "type": "sku", "service": r"^Compute Engine$",
     "sku": r"(?i)^n1 predefined instance (core|ram)",
     "message": "N1 is an older machine family. Check newer families and commitment eligibility before migrating."},
    {"id": "snapshot-storage", "type": "sku", "service": r".", "sku": r"(?i)pd snapshot",
     "message": "Check snapshot schedules and retention."},
    {"id": "internet-egress", "type": "sku", "service": r".",
     "sku": r"(?i)internet data transfer out|data transfer out internet|internet egress",
     "message": "Check the largest sources; CDN and compression reduce internet egress."},
    {"id": "inter-region-egress", "type": "sku", "service": r".", "sku": r"(?i)^(?!.*transfer in from).*inter[ -]region",
     "message": "Check which services talk across regions."},
    {"id": "extended-support", "type": "sku", "service": r".", "sku": r"(?i)extended support",
     "message": "Extended support fees apply to old major versions. Check the upgrade path."},
    {"id": "growth", "type": "growth", "factor": 1.5, "min_delta": 100.0},
]
_RULE_KEYS = {"sku": {"enabled", "service", "sku", "min_cost", "message"},
              "growth": {"enabled", "factor", "min_delta", "message"}}


@dataclasses.dataclass
class Rule:
    id: str
    type: str
    message: str
    service: re.Pattern | None = None
    sku: re.Pattern | None = None
    min_cost: float | None = None
    factor: float = 1.5
    min_delta: float = 100.0


def _number(rule_id: str, key: str, value) -> float:
    if not _is_num(value):
        raise UsageError(f"[rules.{rule_id}] {key} must be a number")
    return float(value)


def load_rules(overrides: dict) -> list[Rule]:
    specs = {r["id"]: dict(r) for r in BUILTIN_RULES}
    for rid, ov in overrides.items():
        if not isinstance(ov, dict):
            raise UsageError(f"[rules.{rid}] must be a table")
        spec = specs.get(rid)
        if spec is None:
            missing = {"service", "sku", "message"} - set(ov)
            if missing:
                raise UsageError(f"custom rule {rid!r} needs {', '.join(sorted(missing))}")
            spec = specs[rid] = {"id": rid, "type": "sku"}
        unknown = set(ov) - _RULE_KEYS[spec["type"]]
        if unknown:
            raise UsageError(f"unknown key(s) in [rules.{rid}]: {', '.join(sorted(unknown))}")
        if "enabled" in ov and not isinstance(ov["enabled"], bool):
            raise UsageError(f"[rules.{rid}] enabled must be true or false")
        spec.update(ov)
    rules = []
    for s in specs.values():
        if s.get("enabled", True) is False:
            continue
        rid = s["id"]
        if s["type"] == "growth":
            factor = _number(rid, "factor", s["factor"])
            min_delta = _number(rid, "min_delta", s["min_delta"])
            message = s.get("message") or (f"Grew {round((factor - 1) * 100)}% or more and by {min_delta:g} or more "
                                           "vs the prior period. Review the cause.")
            rules.append(Rule(rid, "growth", message, factor=factor, min_delta=min_delta))
            continue
        try:
            service, sku = re.compile(s["service"]), re.compile(s["sku"])
        except (re.error, TypeError) as e:
            raise UsageError(f"bad regex in rule {rid!r}: {e}") from None
        min_cost = _number(rid, "min_cost", s["min_cost"]) if "min_cost" in s else None
        rules.append(Rule(rid, "sku", str(s["message"]), service=service, sku=sku, min_cost=min_cost))
    return rules


def _d(x: float) -> Decimal:
    # aggregate() returns floats rounded to 6 decimals; repr() gives that exact decimal back.
    return Decimal(repr(x))


def evaluate_rules(cache: dict, rules: list[Rule]) -> list[dict]:
    sums = aggregate(cache, ("service", "sku"))
    nets = {k: (_d(a[0]) + _d(a[1]), _d(a[3]) + _d(a[4])) for k, a in sums.items()}
    total = sum((cur for cur, _ in nets.values()), Decimal(0))
    floor = max(Decimal(50), total * Decimal("0.005"))
    prior_ok = cache["meta"]["prior_covered"]
    flags = []
    for (si, ki), (cur, prior) in nets.items():
        sid, sdesc = cache["dims"]["service"][si]
        kid, kdesc = cache["dims"]["sku"][ki]
        for rule in rules:
            if rule.type == "sku":
                threshold = _d(rule.min_cost) if rule.min_cost is not None else floor
                hit = rule.service.search(sdesc) and rule.sku.search(kdesc) and cur >= threshold
            else:
                hit = (prior_ok and prior > 0 and cur >= prior * _d(rule.factor)
                       and cur - prior >= _d(rule.min_delta))
            if hit:
                flags.append({"rule": rule.id, "service_id": sid, "sku_id": kid, "service": sdesc, "sku": kdesc,
                              "net": float(round(cur, 2)), "prior_net": float(round(prior, 2)),
                              "message": rule.message})
    flags.sort(key=lambda f: (-f["net"], f["rule"], f["sku_id"]))
    return flags


# ---------------------------------------------------------------- demo

@dataclasses.dataclass(frozen=True)
class _DemoLine:
    service_id: str
    service: str
    sku_id: str
    sku: str
    unit: str
    project: str
    region: str
    env: str
    resources: tuple
    daily: float
    prior_mult: float = 1.0
    credit_ratio: float = 0.0
    cost_type: str = "regular"


_DEMO_TEAMS = {"acme-prod": "platform", "acme-staging": "platform", "acme-data": "data",
               "acme-shared": NONE_LABEL, "(no project)": NONE_LABEL}
# The demo's main compute is GKE Autopilot. Other SKU names and ids come from a real detailed export; the
# Kubernetes Engine SKU ids below are illustrative.
_GKE, _GCE, _NET, _LOG, _SQL = ("CCD8-9BF1-090E", "Kubernetes Engine"), ("6F81-5844-456A", "Compute Engine"), \
    ("E505-1604-58F8", "Networking"), ("5490-F7B7-8DF6", "Cloud Logging"), ("9662-B51E-5089", "Cloud SQL")
_DEMO_LINES = (
    _DemoLine(*_GKE, "C1A7-4E2D-9B30", "Autopilot Pod mCPU Requests (us-central1)", "vCPU hour",
              "acme-prod", "us-central1", "prod", ("clusters/prod-api", "clusters/prod-workers", "clusters/prod-web"), 420,
              credit_ratio=-0.25),
    _DemoLine(*_GKE, "5B3E-8F61-2AC4", "Autopilot Pod Memory Requests (us-central1)", "gibibyte hour",
              "acme-prod", "us-central1", "prod", ("clusters/prod-api", "clusters/prod-workers", "clusters/prod-web"), 90),
    _DemoLine(*_GKE, "C1A7-4E2D-9B30", "Autopilot Pod mCPU Requests (us-central1)", "vCPU hour",
              "acme-staging", "us-central1", "staging", ("clusters/staging",), 60),
    _DemoLine(*_GCE, "F274-1692-F213", "Network Internet Data Transfer Out from Americas to Americas",
              "gibibyte", "acme-prod", "us-central1", "prod", ("gke-prod-api-nodes", "gke-prod-web-nodes"), 35, prior_mult=0.8),
    _DemoLine(*_LOG, "143F-A1B0-E0BE", "Log Storage cost", "gibibyte", "acme-prod", "global", "prod",
              ("_Default",), 55),
    _DemoLine(*_LOG, "376D-A4B0-82E4", "Vended Logs Storage", "gibibyte", "acme-shared", "global", NONE_LABEL,
              ("vpc-flow-logs",), 18),
    _DemoLine(*_NET, "C40A-084F-09B4", "Networking Private Service Connect Consumer Data Processing", "gibibyte",
              "acme-prod", "us-central1", "prod", ("redis-psc",), 30),
    _DemoLine(*_NET, "015F-5732-FFF0", "Networking Cloud Nat Data Processing", "gibibyte", "acme-prod",
              "us-central1", "prod", ("nat-central",), 20),
    _DemoLine(*_NET, "32E2-4EFC-EF9F", "Networking Cloud Nat Gateway Uptime", "hour", "acme-prod", "us-central1",
              "prod", ("nat-central",), 3),
    _DemoLine(*_GCE, "2E27-4F75-95CD", "N1 Predefined Instance Core running in Americas", "hour", "acme-data",
              "us-central1", "prod", ("etl-1", "etl-2"), 25),
    _DemoLine(*_GCE, "6C71-E844-38BC", "N1 Predefined Instance Ram running in Americas", "gibibyte hour",
              "acme-data", "us-central1", "prod", ("etl-1", "etl-2"), 8),
    _DemoLine(*_GCE, "BB77-5FDA-69D9", "N2 Instance Core running in Americas", "hour", "acme-prod", "us-central1",
              "prod", ("bastion",), 6),
    _DemoLine(*_GCE, "B188-61DD-52E4", "SSD backed PD Capacity", "gibibyte month", "acme-prod", "us-central1",
              "prod", ("disk-a", "disk-b"), 12),
    _DemoLine(*_GCE, "66A2-68EA-56BE", "Static Ip Charge", "hour", "acme-staging", "us-central1", "staging",
              ("ip-old-lb",), 17),
    _DemoLine(*_GCE, "4CCF-185C-4E98", "Network Inter Region Data Transfer Out from Americas to Dallas", "gibibyte",
              "acme-prod", "us-central1", "prod", ("api-vm",), 22, prior_mult=0.9),
    _DemoLine(*_SQL, "66AB-BA17-351C", "Storage PD Snapshot", "gibibyte month", "acme-prod", "us-central1", "prod",
              ("main-db",), 22),
    _DemoLine(*_SQL, "3C63-4C9C-8AF9",
              "Cloud SQL for PostgreSQL: Regional - Enterprise Plus Extended support vCPU v13 in Iowa", "hour",
              "acme-prod", "us-central1", "prod", ("legacy-db",), 20),
    _DemoLine(*_SQL, "903C-B42D-4287", "Cloud SQL for PostgreSQL: Zonal - Enterprise Plus Standard Storage in Iowa",
              "gibibyte month", "acme-prod", "us-central1", "prod", ("main-db",), 60),
    _DemoLine("5AF5-2C11-D467", "Cloud Memorystore for Redis", "52EE-553D-0B96",
              "Redis Cluster Node Standard Small Dallas", "hour", "acme-prod", "us-south1", "prod", ("cache",), 80),
    _DemoLine("95FF-2EF5-5EA1", "Cloud Storage", "E5F0-6A5D-7BAD", "Standard Storage US Regional", "gibibyte month",
              "acme-data", "us-central1", NONE_LABEL, ("raw-events", "exports"), 70),
    _DemoLine("16B8-3DDA-9F10", "BigQuery Reservation API", "5DE7-AD37-6FAB",
              "BigQuery Standard Edition for US (multi-region)", "slot hour", "acme-data", "us", NONE_LABEL,
              ("reservation",), 110, prior_mult=0.45),
    _DemoLine("7EC6-CE53-9E39", "Datastream", "8BA9-429D-C5E8", "CDC Bytes Processed Iowa", "gibibyte", "acme-data",
              "us-central1", "prod", ("stream-1",), 15),
    _DemoLine("149C-F9EC-3994", "Artifact Registry", "8502-299A-ABAF", "Artifact Registry Storage", "gibibyte month",
              "acme-shared", "us-central1", NONE_LABEL, tuple(f"repo-{i:02d}" for i in range(1, 31)), 30),
    _DemoLine("2062-016F-44A2", "Support", "346F-C447-D7DB", "GCP  Support Variable Fee for Enhanced Support",
              "count", "acme-shared", "(unspecified)", NONE_LABEL, ("(no resource)",), 35),
    _DemoLine("_tax", "Tax & adjustments", "tax", "tax", "", "(no project)", "(unspecified)", NONE_LABEL,
              ("(no resource)",), 12, cost_type="tax"),
)


# Real API hosts, so demo resource names look like the ones in an actual detailed export.
_DEMO_HOSTS = {"Kubernetes Engine": "container", "Compute Engine": "compute", "Cloud SQL": "sqladmin",
               "Cloud Logging": "logging", "Cloud Storage": "storage", "BigQuery Reservation API": "bigqueryreservation",
               "Cloud Memorystore for Redis": "redis", "Datastream": "datastream", "Artifact Registry": "artifactregistry",
               "Networking": "compute"}


def _demo_resource(line: _DemoLine, name: str) -> str:
    if name == "(no resource)":
        return name
    host = _DEMO_HOSTS[line.service]
    return f"//{host}.googleapis.com/projects/{line.project}/locations/{line.region}/{name}"


def _demo_q2(kind: str, key: tuple, values: list) -> dict:
    return {"row_kind": kind, "service_id": key[0], "sku_id": key[1], "project": key[2], "region": key[3],
            "resource": key[4], **dict(zip(_MONEY6, values))}


def make_demo(today: dt.date) -> dict:
    window = compute_window(today, 30, 2)
    rng = random.Random(42)
    q = Decimal("0.000001")
    facts = []
    day = window.prior_start
    while day < window.period_end:
        current = day >= window.period_start
        for line in _DEMO_LINES:
            weights = [len(line.resources) - k for k in range(len(line.resources))]
            for name, weight in zip(line.resources, weights):
                base = line.daily * (1.0 if current else line.prior_mult) * weight / sum(weights)
                cost = Decimal(str(round(base * rng.uniform(0.9, 1.1), 6)))
                credits = (cost * Decimal(str(line.credit_ratio))).quantize(q)
                list_cost = (cost * Decimal("1.1")).quantize(q)
                facts.append((line, _demo_resource(line, name), day.isoformat(), cost, credits, list_cost,
                              float(cost) * 3))
        day += dt.timedelta(days=1)

    latest = f"{window.last_day.isoformat()}T23:00:00.000000Z"
    rows1: dict = {}
    for line, _res, d, cost, credits, list_cost, usage in facts:
        team = _DEMO_TEAMS[line.project]
        key = (line.service_id, line.sku_id, line.unit, line.project, line.region, line.cost_type, line.env, team, d)
        r = rows1.get(key)
        if r is None:
            r = rows1[key] = {
                "service_id": line.service_id, "service": line.service, "sku_id": line.sku_id, "sku": line.sku,
                "unit": line.unit, "project": line.project, "region": line.region, "cost_type": line.cost_type,
                "label_0": line.env, "label_1": team, "usage_day": d, "currency": "USD", "cost": Decimal(0),
                "credits": Decimal(0), "list_cost": Decimal(0), "usage_amount": 0.0, "list_nulls": 0,
                "latest_export": latest}
        r["cost"] += cost
        r["credits"] += credits
        r["list_cost"] += list_cost
        r["usage_amount"] += usage

    period_start = window.period_start.isoformat()
    per_resource: dict = {}
    for line, res, d, cost, credits, list_cost, _usage in facts:
        if line.cost_type != "regular":
            continue
        acc = per_resource.setdefault((line.service_id, line.sku_id, line.project, line.region, res), [Decimal(0)] * 6)
        off = 0 if d >= period_start else 3
        acc[off] += cost
        acc[off + 1] += credits
        acc[off + 2] += list_cost
    by_sku = defaultdict(list)
    for key, values in per_resource.items():
        by_sku[key[:2]].append((key, values))
    top = 25
    rows2, totals = [], {}
    for items in by_sku.values():
        items.sort(key=lambda kv: (-max(kv[1][0] + kv[1][1], kv[1][3] + kv[1][4]), kv[0][2], kv[0][3], kv[0][4]))
        rows2 += [_demo_q2("resource", key, values) for key, values in items[:top]]
        for key, values in items:
            t = totals.setdefault(key[:4], [Decimal(0)] * 6)
            for j in range(6):
                t[j] += values[j]
    rows2 += [_demo_q2("total", key + (None,), values) for key, values in totals.items()]

    info = TableInfo("demo-project.billing.gcp_billing_export_resource_v1_DEMO", "demo-project", "billing",
                     "gcp_billing_export_resource_v1_DEMO", "US", "detailed", "_PARTITIONTIME", "TIMESTAMP")
    cache = build_cache(info, window, ["env", "project:team"], 50, top, f"{today.isoformat()} 00:00:00.000000+00",
                        list(rows1.values()), rows2, f"{today.isoformat()}T00:00:00Z")
    cache["meta"]["demo"] = True
    return cache


# ---------------------------------------------------------------- AI export

EXPORT_CAP = 64 * 1024
EMBED_EXPORT_CAP = 48 * 1024  # the viewer adds up to 100 marks to the embedded copy
_TRIM_ORDER = ["top_resources", "by_project", "by_label", "top_skus", "marks", "flags", "growers", "new_spend",
               "by_region", "by_service", "by_category"]


def _t(s) -> str:
    s = str(s)
    return s if len(s) <= 200 else s[:199] + "…"


def _metrics(a: list, meta: dict) -> dict:
    prior_ok = meta["prior_covered"]
    list_ok = meta["list_complete"]["period"] and meta["list_complete"]["prior"]
    out = {}
    for name, (cur, prior) in (("net", (a[0] + a[1], a[3] + a[4])), ("cost", (a[0], a[3])), ("list", (a[2], a[5]))):
        show = prior_ok and (name != "list" or list_ok)
        out[name] = {"cur": round(cur, 2), "prior": round(prior, 2) if show else None,
                     "delta": round(cur - prior, 2) if show else None}
    return out


def _item_net(item: dict) -> float:
    return item["metrics"]["net"]["cur"] if "metrics" in item else item.get("net", 0.0)


def _coll(items: list, limit: int) -> dict:
    rest = items[limit:]
    return {"items": items[:limit], "omitted": len(rest), "omitted_net": round(sum(_item_net(i) for i in rest), 2)}


def _by_net(items: list) -> list:
    return sorted(items, key=lambda i: (-_item_net(i), str(i.get("name", i.get("sku", "")))))


def _json_size(d) -> int:
    return len(json.dumps(d, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def _halve(coll: dict) -> bool:
    items = coll["items"]
    if not items:
        return False
    keep = len(items) // 2
    rest = items[keep:]
    coll["items"] = items[:keep]
    coll["omitted"] += len(rest)
    coll["omitted_net"] = round(coll["omitted_net"] + sum(_item_net(i) for i in rest), 2)
    return True


def _trim(out: dict, cap: int) -> dict:
    while _json_size(out) > cap:
        progressed = False
        for name in _TRIM_ORDER:
            if name not in out:
                continue
            colls = list(out[name].values()) if name == "by_label" else [out[name]]
            for coll in colls:
                if _halve(coll):
                    progressed = True
                    out["truncated"] = True
            if _json_size(out) <= cap:
                return out
        if not progressed:
            raise CostTreeError(f"the export cannot fit in {cap:,} bytes even with every list emptied; "
                                "check for very long label keys or metadata")
    return out


def build_export(cache: dict, flags: list, categories: dict, cap: int = EXPORT_CAP) -> dict:
    meta, dims = cache["meta"], cache["dims"]

    def simple(dim: str) -> list:
        return _by_net([{"name": _t(dims[dim][k[0]]), "metrics": _metrics(a, meta)}
                        for k, a in aggregate(cache, (dim,)).items()])

    services = aggregate(cache, ("service",))
    by_service, per_category = [], {}
    for (si,), a in services.items():
        sid, desc = dims["service"][si]
        cat = categories.get(sid, "Other")
        by_service.append({"id": sid, "name": _t(desc), "category": cat, "metrics": _metrics(a, meta)})
        acc = per_category.setdefault(cat, [0.0] * 6)
        for j in range(6):
            acc[j] += a[j]
    totals_raw = [sum(a[j] for a in services.values()) for j in range(6)]

    skus, growers, new_spend = [], [], []
    for (si, ki), a in aggregate(cache, ("service", "sku")).items():
        sid, sdesc = dims["service"][si]
        kid, kdesc = dims["sku"][ki]
        item = {"service_id": sid, "service": _t(sdesc), "sku_id": kid, "sku": _t(kdesc),
                "category": categories.get(sid, "Other"), "metrics": _metrics(a, meta)}
        skus.append(item)
        cur, prior = a[0] + a[1], a[3] + a[4]
        if meta["prior_covered"]:
            if prior > 0 and cur > prior:
                growers.append((cur - prior, item))
            elif prior <= 0 and cur > 0:
                new_spend.append(item)
    growers.sort(key=lambda x: (-x[0], x[1]["sku"]))

    period_days = (dt.date.fromisoformat(meta["period"][1]) - dt.date.fromisoformat(meta["period"][0])).days + 1
    totals = _metrics(totals_raw, meta)
    totals["projection_30d"] = round((totals_raw[0] + totals_raw[1]) / period_days * 30, 2)
    out = {
        "schema": "gcp-costtree/export/v1",
        "meta": {k: meta[k] for k in ("period", "prior", "currency", "export", "prior_covered", "period_covered", "data_through", "period_end_covered",
                                      "list_complete", "provisional_day", "latest_export_seen", "snapshot_time",
                                      "labels") if k in meta},
        "totals": totals,
        "by_service": _coll(_by_net(by_service), 30),
        "by_category": _coll(_by_net([{"name": c, "metrics": _metrics(a, meta)} for c, a in per_category.items()]), 30),
        "by_region": _coll(simple("region"), 30),
        "by_project": _coll(simple("project"), 50),
        "by_label": {spec: _coll(simple(f"label:{spec}"), 20) for spec in meta["labels"]},
        "top_skus": _coll(_by_net(skus), 50),
        "growers": _coll([item for _, item in growers], 10),
        "new_spend": _coll(_by_net(new_spend), 10),
        "flags": _coll([{"rule": f["rule"], "service": _t(f["service"]), "sku": _t(f["sku"]), "net": f["net"],
                         "message": str(f["message"])[:300]} for f in flags], 100),
        "truncated": False,
    }
    if meta["export"] == "detailed":
        res = []
        for r in cache["resources"]:
            if r[4] == OTHER:
                continue
            res.append({"service": _t(dims["service"][r[0]][1]), "sku": _t(dims["sku"][r[1]][1]),
                        "project": _t(dims["project"][r[2]]), "region": _t(dims["region"][r[3]]),
                        "resource": _t(r[4]), "metrics": _metrics(r[5:11], meta)})
        out["top_resources"] = _coll(_by_net(res), 25)
    return _trim(out, cap)


# ---------------------------------------------------------------- HTML render

TEMPLATE_PATH = Path(__file__).parent / "viewer.html"
_PLACEHOLDER = "__COSTTREE_DATA__"
_HTML_UNSAFE = {c: "\\u%04x" % ord(c) for c in "<>&"}


def embed_json(obj) -> str:
    text = json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
    for ch, esc in _HTML_UNSAFE.items():
        text = text.replace(ch, esc)
    return text


def render_html(cache: dict, flags: list, categories: dict, metric: str, template: str) -> str:
    if template.count(_PLACEHOLDER) != 1:
        raise CostTreeError("viewer.html is damaged (data placeholder missing); reinstall gcp-costtree")
    validate_cache(cache)  # the viewer trusts its embedded data; this is the only gate
    data = dict(cache)
    data["meta"] = {k: v for k, v in cache["meta"].items() if k != "table"}
    data["flags"] = flags
    data["categories"] = categories
    data["view"] = {"metric": metric}
    data["export"] = build_export(cache, flags, categories, EMBED_EXPORT_CAP)
    return template.replace(_PLACEHOLDER, embed_json(data))


# ---------------------------------------------------------------- main

def _print_summary(cache: dict, flags: list, out: Path, stdout) -> None:
    meta = cache["meta"]
    a = aggregate(cache, ()).get((), [0.0] * 6)
    print(f"gcp-costtree: {meta['period'][0]} to {meta['period'][1]} (UTC), {meta['export']} export, "
          f"net {a[0] + a[1]:,.2f} {meta['currency']}", file=stdout)
    if flags:
        print(f"  {len(flags)} money-pit flag(s); largest: {flags[0]['sku']} ({flags[0]['net']:,.2f})", file=stdout)
    if not meta["prior_covered"]:
        print("  note: the export does not cover the prior period; comparisons are off", file=stdout)
    if not meta.get("period_covered", True):
        print("  note: the export does not cover the whole period; totals are partial", file=stdout)
    if meta.get("period_end_covered") is False:
        print(f"  note: data ends on {meta['data_through']}, before the period end {meta['period'][1]}; "
              "the export may still be backfilling, so totals are partial", file=stdout)
    if not all(meta["list_complete"].values()):
        print("  note: some rows have no list price; list totals are partial", file=stdout)
    if meta.get("latest_export_seen"):
        print(f"  latest export seen: {meta['latest_export_seen']}", file=stdout)
    print(f"  wrote {out}", file=stdout)


def main(argv=None, *, today=None, now=None, env=None, transport=None, stdout=None, stderr=None,
         open_browser=webbrowser.open, cwd=None) -> int:
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    try:
        opts = parse_args(sys.argv[1:] if argv is None else argv, cwd or Path.cwd())
        today = today or utc_today()
        out = Path(opts.out)
        # Local config mistakes must fail before authentication or any (paid) query.
        rules = load_rules(opts.rules)
        check_categories(opts.categories)
        if opts.mode in ("live", "dry-run"):
            compute_window(today, opts.days, opts.lag_days, opts.end)  # a bad window fails before any query
        if opts.mode == "demo":
            cache = make_demo(today)
        elif opts.mode == "cache":
            cache = load_cache(Path(opts.from_file))
        else:
            bq = BigQuery(get_token(env), transport or UrllibTransport())
            if opts.mode == "dry-run":
                for name, scanned in estimate(bq, opts, today, now).items():
                    print(f"{name}: {scanned / 1e9:.2f} GB, about ${scanned / 2**40 * 6.25:.2f} at on-demand list "
                          "price (US multi-region); actual cost depends on your pricing model", file=stdout)
                return 0
            cache = fetch(bq, opts, today, now)
            save_cache(cache, out.parent / "gcp-costtree-data.json")
        categories = categorize(cache, opts.categories)
        flags = evaluate_rules(cache, rules)
        try:
            template = TEMPLATE_PATH.read_text(encoding="utf-8")
        except OSError:
            raise CostTreeError(f"viewer.html not found next to {Path(__file__).name}; reinstall gcp-costtree") from None
        html = render_html(cache, flags, categories, opts.metric, template)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html, encoding="utf-8")
        if opts.export:
            export_path = Path(opts.export)
            export_path.parent.mkdir(parents=True, exist_ok=True)
            # Compact on purpose: the 64 KB cap is measured on this exact serialization.
            export_path.write_text(json.dumps(build_export(cache, flags, categories), separators=(",", ":"),
                                              ensure_ascii=False), encoding="utf-8")
        _print_summary(cache, flags, out, stdout)
        if not opts.no_open:
            open_browser(out.resolve().as_uri())
        return 0
    except UsageError as e:
        print(f"gcp-costtree: {e}", file=stderr)
        return 2
    except CostTreeError as e:
        print(f"gcp-costtree: {e}", file=stderr)
        return 1
    except OSError as e:
        print(f"gcp-costtree: {e}", file=stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
