import datetime as dt
import unittest
from unittest import mock

import gcp_costtree as g
from tests.helpers import q1_row, q2_row, table_meta

DET = g.TableInfo("p.ds.r", "p", "ds", "r", "US", "detailed", "_PARTITIONTIME", "TIMESTAMP")
STD = g.TableInfo("p.ds.t", "p", "ds", "t", "US", "standard", "_PARTITIONTIME", "TIMESTAMP")
W = g.compute_window(dt.date(2026, 9, 28), 30, 2)  # prior 07-29..08-27, period 08-28..09-26
SNAP = "2026-09-28 10:00:00.000000+00"


def build(rows1, rows2=(), info=DET, labels=()):
    return g.build_cache(info, W, list(labels), 50, 25, SNAP, list(rows1), list(rows2), "2026-09-28T10:00:00Z")


class BuildCacheTest(unittest.TestCase):
    def test_encodes_dims_rows_and_meta(self):
        c = build([q1_row(day="2026-07-29", labels=("prod",)), q1_row(day="2026-09-01", labels=("dev",))],
                  [q2_row("resource", cur=("10", "0", "11"), prior=("10", "0", "11")),
                   q2_row("total", cur=("10", "0", "11"), prior=("10", "0", "11"))], labels=["env"])
        self.assertEqual(c["version"], 1)
        self.assertEqual(c["daily_columns"], ["service", "sku", "unit", "project", "region", "cost_type",
                                              "label:env", "day", "cost", "credits", "list", "usage"])
        self.assertEqual(c["dims"]["service"], [["S1", "svc S1"]])
        self.assertEqual(c["dims"]["label:env"], ["prod", "dev"])
        self.assertEqual(c["daily"][1], [0, 0, 0, 0, 0, 0, 1, "2026-09-01", 10.0, 0.0, 11.0, 1.0])
        m = c["meta"]
        self.assertEqual(m["period"], ["2026-08-28", "2026-09-26"])
        self.assertEqual(m["prior"], ["2026-07-29", "2026-08-27"])
        self.assertEqual(m["provisional_day"], "2026-09-26")
        self.assertTrue(m["prior_covered"] and m["period_covered"])
        self.assertEqual(m["list_complete"], {"period": True, "prior": True})
        self.assertEqual(m["table_hash"], g.hashlib.sha256(b"p.ds.r").hexdigest()[:16])
        self.assertEqual((m["export"], m["currency"], m["snapshot_time"]), ("detailed", "USD", SNAP))
        self.assertEqual(c["resources"], [[0, 0, 0, 0, "r1", 10.0, 0.0, 11.0, 10.0, 0.0, 11.0]])

    def test_description_change_keeps_one_dim(self):
        c = build([q1_row(day="2026-09-01", sku="Old name"), q1_row(day="2026-09-02", sku="New name")],
                  [q2_row("resource", cur=("20", "0", "22")), q2_row("total", cur=("20", "0", "22"))])
        self.assertEqual(c["dims"]["sku"], [["K1", "Old name"]])

    def test_description_is_earliest_day_whatever_the_row_order(self):
        c = build([q1_row(day="2026-09-02", sku="New name"), q1_row(day="2026-09-01", sku="Old name")],
                  [q2_row("resource", cur=("20", "0", "22")), q2_row("total", cur=("20", "0", "22"))])
        self.assertEqual(c["dims"]["sku"], [["K1", "Old name"]])

    def test_tax_rows_have_no_resources(self):
        c = build([q1_row(day="2026-09-01"),
                   q1_row(service_id="_tax", sku_id="tax", cost_type="tax", day="2026-09-01", cost="3", list_cost="0")],
                  [q2_row("resource"), q2_row("total")])
        self.assertEqual(len(c["resources"]), 1)

    def test_other_bucket_is_signed(self):
        rows1 = [q1_row(day="2026-09-01", cost="10", credits="-4", list_cost="11")]
        rows2 = [q2_row("resource", resource="big", cur=("7", "-1", "8")),
                 q2_row("total", cur=("10", "-4", "11"))]
        c = build(rows1, rows2)
        self.assertEqual(c["resources"][1], [0, 0, 0, 0, "(other)", 3.0, -3.0, 3.0, 0.0, 0.0, 0.0])

    def test_totals_mismatch_fails(self):
        with self.assertRaisesRegex(g.CostTreeError, "disagree"):
            build([q1_row(day="2026-09-01")], [q2_row("resource"), q2_row("total", cur=("10.000000001", "0", "11"))])

    def test_missing_key_fails(self):
        with self.assertRaisesRegex(g.CostTreeError, "disagree"):
            build([q1_row(day="2026-09-01"), q1_row(day="2026-09-01", project="p2")],
                  [q2_row("resource"), q2_row("total")])

    def test_coverage_heuristic(self):
        c = build([q1_row(day="2026-08-10")], info=STD)
        self.assertFalse(c["meta"]["prior_covered"])
        self.assertTrue(c["meta"]["period_covered"])
        c = build([q1_row(day="2026-09-10")], info=STD)
        self.assertFalse(c["meta"]["period_covered"])

    def test_data_ending_before_period_end_is_flagged(self):
        c = build([q1_row(day="2026-07-29"), q1_row(day="2026-09-10")], info=STD)
        self.assertEqual((c["meta"]["data_through"], c["meta"]["period_end_covered"]), ("2026-09-10", False))
        c = build([q1_row(day="2026-07-29"), q1_row(day="2026-09-25")], info=STD)
        self.assertEqual((c["meta"]["data_through"], c["meta"]["period_end_covered"]), ("2026-09-25", True))

    def test_list_nulls_per_period(self):
        c = build([q1_row(day="2026-08-01", list_nulls=2), q1_row(day="2026-09-01")], info=STD)
        self.assertEqual(c["meta"]["list_complete"], {"period": True, "prior": False})

    def test_multiple_currencies(self):
        with self.assertRaisesRegex(g.CostTreeError, "currency"):
            build([q1_row(day="2026-09-01"), q1_row(day="2026-09-02", currency="EUR")], info=STD)

    def test_row_cap(self):
        with mock.patch.object(g, "MAX_DAILY_ROWS", 1):
            with self.assertRaisesRegex(g.CostTreeError, "--label-top"):
                build([q1_row(day="2026-09-01"), q1_row(day="2026-09-02")], info=STD)

    def test_standard_has_no_resources(self):
        c = build([q1_row(day="2026-09-01")], info=STD)
        self.assertEqual((c["meta"]["export"], c["resources"]), ("standard", []))


class TableInfoTest(unittest.TestCase):
    def test_detailed_ingestion_partitioned(self):
        i = g.table_info_from_metadata("p.d.r", table_meta(detailed=True))
        self.assertEqual((i.export, i.partition_column, i.partition_type, i.location),
                         ("detailed", "_PARTITIONTIME", "TIMESTAMP", "US"))

    def test_field_partitioned(self):
        i = g.table_info_from_metadata("p.d.t", table_meta(detailed=False, field="export_day", ftype="DATE"))
        self.assertEqual((i.export, i.partition_column, i.partition_type), ("standard", "export_day", "DATE"))

    def test_not_a_billing_export(self):
        with self.assertRaisesRegex(g.CostTreeError, "Cloud Billing export"):
            g.table_info_from_metadata("p.d.t", {"schema": {"fields": [{"name": "x", "type": "STRING"}]}})


class StubBQ:
    def __init__(self, meta, rows1, rows2=(), bytes_=1000):
        self.meta, self.rows1, self.rows2, self.bytes = meta, list(rows1), list(rows2), bytes_
        self.calls = []

    def get_table(self, p, d, t):
        self.calls.append(("get_table", p, d, t))
        return self.meta

    def query(self, project, sql, params, location, max_bytes, *, dry_run=False, max_rows=None):
        kind = "snapshot" if sql == g.SNAPSHOT_SQL else ("q2" if "row_kind" in sql else "q1")
        self.params = getattr(self, "params", {})
        self.params[("dry" if dry_run else "run", kind)] = {p["name"]: p["parameterValue"]["value"] for p in params}
        self.calls.append(("dry" if dry_run else "run", kind, project, location, max_bytes))
        if dry_run:
            return self.bytes
        return {"snapshot": [{"ts": SNAP}], "q1": self.rows1, "q2": self.rows2}[kind]


def opts(**kw):
    base = dict(mode="live", from_file=None, table="p.ds.r", job_project=None, days=30, lag_days=2, end=None, labels=[],
                label_top=50, top_resources=25, max_gb=1.0, metric="net", export=None, out="out/x.html",
                no_open=True, rules={}, categories={})
    base.update(kw)
    return g.Options(**base)


class FetchTest(unittest.TestCase):
    TODAY = dt.date(2026, 9, 28)

    def test_detailed_flow_order(self):
        bq = StubBQ(table_meta(), [q1_row(day="2026-09-01")], [q2_row("resource"), q2_row("total")])
        c = g.fetch(bq, opts(), self.TODAY)
        kinds = [c_[:2] for c_ in bq.calls[1:]]
        self.assertEqual(kinds, [("run", "snapshot"), ("dry", "q1"), ("dry", "q2"), ("run", "q1"), ("run", "q2")])
        self.assertEqual(c["meta"]["snapshot_time"], SNAP)
        self.assertTrue(all(call[4] == 10**9 for call in bq.calls[1:]))

    def test_standard_skips_q2(self):
        bq = StubBQ(table_meta(detailed=False), [q1_row(day="2026-09-01")])
        g.fetch(bq, opts(table="p.ds.t"), self.TODAY)
        self.assertNotIn("q2", [c_[1] for c_ in bq.calls])

    def test_dry_run_over_cap_aborts_before_running(self):
        bq = StubBQ(table_meta(), [q1_row()], bytes_=2 * 10**9)
        with self.assertRaisesRegex(g.CostTreeError, "--max-gb"):
            g.fetch(bq, opts(), self.TODAY)
        self.assertNotIn(("run", "q1"), [c_[:2] for c_ in bq.calls])

    def test_bad_daily_result_stops_before_resource_query(self):
        bq = StubBQ(table_meta(), [q1_row(day="2026-09-01"), q1_row(day="2026-09-02", currency="EUR")])
        with self.assertRaisesRegex(g.CostTreeError, "currency"):
            g.fetch(bq, opts(), self.TODAY)
        self.assertNotIn(("run", "q2"), [c_[:2] for c_ in bq.calls])

    def test_no_rows(self):
        bq = StubBQ(table_meta(), [], [])
        with self.assertRaisesRegex(g.CostTreeError, "--end"):
            g.fetch(bq, opts(), self.TODAY)

    def test_end_moves_the_window(self):
        bq = StubBQ(table_meta(), [q1_row(day="2026-08-20")], [q2_row("resource"), q2_row("total")])
        c = g.fetch(bq, opts(end=dt.date(2026, 8, 25), days=14), self.TODAY)
        self.assertEqual(c["meta"]["period"], ["2026-08-12", "2026-08-25"])
        self.assertEqual(c["meta"]["provisional_day"], "2026-08-25")
        for key in (("run", "q1"), ("run", "q2"), ("dry", "q1"), ("dry", "q2")):
            p = bq.params[key]
            self.assertEqual((p["prior_start"], p["period_end"]), ("2026-07-29", "2026-08-26"), key)
            self.assertEqual((p["part_start"], p["part_end"]), ("2026-07-28 00:00:00+00", "2026-09-29 00:00:00+00"), key)
        est = StubBQ(table_meta(), [])
        g.estimate(est, opts(mode="dry-run", end=dt.date(2026, 8, 25), days=14), self.TODAY,
                   dt.datetime(2026, 9, 28, 10, tzinfo=dt.timezone.utc))
        for key in (("dry", "q1"), ("dry", "q2")):
            p = est.params[key]
            self.assertEqual((p["prior_start"], p["period_end"]), ("2026-07-29", "2026-08-26"), key)
            self.assertEqual((p["part_start"], p["part_end"]), ("2026-07-28 00:00:00+00", "2026-09-29 00:00:00+00"), key)
        self.assertEqual(bq.params[("run", "q2")]["period_start"], "2026-08-12")
        # Coverage follows the shifted window: data on 08-20 covers neither the prior start nor the period end.
        self.assertEqual((c["meta"]["prior_covered"], c["meta"]["period_end_covered"], c["meta"]["data_through"]),
                         (False, False, "2026-08-20"))

    def test_job_project_used(self):
        bq = StubBQ(table_meta(), [q1_row(day="2026-09-01")], [q2_row("resource"), q2_row("total")])
        g.fetch(bq, opts(job_project="billing-jobs"), self.TODAY)
        self.assertTrue(all(c_[2] == "billing-jobs" for c_ in bq.calls[1:]))

    def test_estimate_uses_no_real_queries(self):
        bq = StubBQ(table_meta(), [], bytes_=123)
        now = dt.datetime(2026, 9, 28, 10, tzinfo=dt.timezone.utc)
        self.assertEqual(g.estimate(bq, opts(mode="dry-run"), self.TODAY, now), {"daily": 123, "resources": 123})
        self.assertEqual({c_[0] for c_ in bq.calls[1:]}, {"dry"})


import copy
import json
import tempfile
from pathlib import Path


def sample_cache():
    return build([q1_row(day="2026-09-01", labels=("prod",))],
                 [q2_row("resource"), q2_row("total")], labels=["env"])


class CacheIoTest(unittest.TestCase):
    def test_round_trip(self):
        c = sample_cache()
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "sub" / "c.json"
            g.save_cache(c, p)
            self.assertEqual(g.load_cache(p), json.loads(json.dumps(c)))

    def test_each_corruption_is_named(self):
        cases = {
            "version": lambda c: c.__setitem__("version", 2),
            "meta.labels": lambda c: c["meta"].__setitem__("labels", "env"),
            "meta.period": lambda c: c["meta"].__setitem__("period", ["x", "y"]),
            "meta.period ": lambda c: c["meta"].__setitem__("period", ["2026-99-99", "2026-09-26"]),
            "meta.period  ": lambda c: c["meta"].__setitem__("period", []),
            "meta.labels ": lambda c: c["meta"].__setitem__("labels", ["x" * 70000]),
            "meta.prior ": lambda c: c["meta"].__setitem__("prior", ["2026-08-20", "2026-08-27"]),
            "meta.fetched_at": lambda c: c["meta"].__setitem__("fetched_at", "x" * 5000),
            "daily[0].day ": lambda c: c["daily"][0].__setitem__(7, "2020-01-01"),
            "meta.prior": lambda c: c["meta"].__setitem__("prior", ["2026-07-29"]),
            "meta.provisional_day": lambda c: c["meta"].__setitem__("provisional_day", "2026/09/26"),
            "meta.currency": lambda c: c["meta"].__setitem__("currency", "US$"),
            "meta.currency ": lambda c: c["meta"].__setitem__("currency", "USD\n"),
            "meta.data_through": lambda c: c["meta"].__setitem__("data_through", "x" * 5000),
            "meta.data_through ": lambda c: c["meta"].__setitem__("data_through", "2026-99-01"),
            "meta.period_end_covered": lambda c: c["meta"].__setitem__("period_end_covered", "yes"),
            "meta.list_complete": lambda c: c["meta"].__setitem__("list_complete", {"period": True}),
            "daily_columns": lambda c: c["daily_columns"].pop(),
            "dims.label:env": lambda c: c["dims"].pop("label:env"),
            "dims.service": lambda c: c["dims"].__setitem__("service", ["S1"]),
            "daily[0] length": lambda c: c["daily"][0].pop(),
            "daily[0].sku": lambda c: c["daily"][0].__setitem__(1, 9),
            "daily[0].day": lambda c: c["daily"][0].__setitem__(7, "2026-9-1"),
            "daily[0].cost": lambda c: c["daily"][0].__setitem__(8, "10"),
            "resources[0].region": lambda c: c["resources"][0].__setitem__(3, -1),
            "resources[0].resource": lambda c: c["resources"][0].__setitem__(4, 5),
            "resources[0].cur_cost": lambda c: c["resources"][0].__setitem__(5, True),
        }
        for field, corrupt in cases.items():
            c = copy.deepcopy(sample_cache())
            corrupt(c)
            with self.assertRaises(g.CostTreeError, msg=field) as cm:
                g.validate_cache(c)
            self.assertIn(field.strip(), str(cm.exception))

    def test_demo_flag_optional_bool(self):
        for ok in (None, True, False):
            c = copy.deepcopy(sample_cache())
            if ok is not None:
                c["meta"]["demo"] = ok
            g.validate_cache(c)
        for bad in ("false", 1, [], None):
            c = copy.deepcopy(sample_cache())
            c["meta"]["demo"] = bad
            with self.assertRaisesRegex(g.CostTreeError, "meta.demo", msg=repr(bad)):
                g.validate_cache(c)

    def test_optional_coverage_fields_may_be_absent(self):
        c = copy.deepcopy(sample_cache())
        del c["meta"]["data_through"], c["meta"]["period_end_covered"]
        g.validate_cache(c)

    def test_non_json_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.json"
            p.write_text("{nope")
            with self.assertRaisesRegex(g.CostTreeError, "invalid cache"):
                g.load_cache(p)


if __name__ == "__main__":
    unittest.main()
