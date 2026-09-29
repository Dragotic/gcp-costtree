import datetime as dt
import io
import json
import re
import tempfile
import unittest
from pathlib import Path

import gcp_costtree as g
from tests.helpers import FakeTransport, err, q1_row, table_meta

TODAY = dt.date(2026, 9, 28)
NOW = dt.datetime(2026, 9, 28, 10, tzinfo=dt.timezone.utc)
TOKEN = "ya29.SECRET"
T = "p.ds.gcp_billing_export_resource_v1_X"


def result(fields, rows):
    return {"jobComplete": True, "jobReference": {"projectId": "p", "jobId": "j", "location": "US"},
            "schema": {"fields": [{"name": n, "type": t} for n, t in fields]},
            "rows": [{"f": [{"v": v} for v in r]} for r in rows]}


Q1_FIELDS = [("service_id", "STRING"), ("sku_id", "STRING"), ("unit", "STRING"), ("project", "STRING"),
             ("region", "STRING"), ("cost_type", "STRING"), ("usage_day", "DATE"), ("currency", "STRING"),
             ("service", "STRING"), ("sku", "STRING"), ("cost", "NUMERIC"), ("credits", "NUMERIC"),
             ("list_cost", "NUMERIC"), ("usage_amount", "FLOAT64"), ("list_nulls", "INT64"),
             ("latest_export", "TIMESTAMP")]
Q2_FIELDS = [("row_kind", "STRING"), ("service_id", "STRING"), ("sku_id", "STRING"), ("project", "STRING"),
             ("region", "STRING"), ("resource", "STRING")] + [(c, "NUMERIC") for c in g.RESOURCE_COLUMNS[5:]]


class MainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.out, self.err, self.opened = io.StringIO(), io.StringIO(), []

    def tearDown(self):
        self.tmp.cleanup()

    def run_main(self, *argv, transport=None):
        return g.main(list(argv), today=TODAY, now=NOW, env={"GCP_COSTTREE_TOKEN": TOKEN}, transport=transport,
                      stdout=self.out, stderr=self.err, open_browser=self.opened.append, cwd=self.dir)

    def html_payload(self, path):
        html = Path(path).read_text()
        return json.loads(re.search(r'id="data">(.*?)</script>', html, re.S).group(1))

    def test_demo_writes_html_export_and_opens(self):
        html, exp = self.dir / "o" / "v.html", self.dir / "e.json"
        self.assertEqual(self.run_main("--demo", "--out", str(html), "--export", str(exp)), 0)
        self.assertTrue(html.exists())
        self.assertEqual(json.loads(exp.read_text())["schema"], "gcp-costtree/export/v1")
        self.assertEqual(self.opened, [html.resolve().as_uri()])
        self.assertIn("wrote", self.out.getvalue())

    def test_from_cache_recomputes_rules_with_config(self):
        cache = self.dir / "c.json"
        g.save_cache(g.make_demo(TODAY), cache)
        disable = "\n".join(f"[rules.{r['id']}]\nenabled = false" for r in g.BUILTIN_RULES)
        (self.dir / "gcp-costtree.toml").write_text(disable + "\n")
        html = self.dir / "v.html"
        self.assertEqual(self.run_main("--from", str(cache), "--out", str(html), "--no-open"), 0)
        self.assertEqual(self.html_payload(html)["flags"], [])
        self.assertEqual(self.opened, [])

    def test_live_run_with_fake_bigquery(self):
        q1 = result(Q1_FIELDS, [["S", "K", "h", "p", "r", "regular", "2026-09-01", "USD", "Svc", "Sku",
                                 "10", "-1", "12", "3", "0", "1790585945.0"]])
        q2 = result(Q2_FIELDS, [["resource", "S", "K", "p", "r", "//x/a", "10", "-1", "12", "0", "0", "0"],
                                ["total", "S", "K", "p", "r", None, "10", "-1", "12", "0", "0", "0"]])
        t = FakeTransport([table_meta(),
                           result([("ts", "STRING")], [["2026-09-28 10:00:00.000000+00"]]),
                           {"totalBytesProcessed": "100"}, {"totalBytesProcessed": "100"}, q1, q2])
        html = self.dir / "out" / "v.html"
        self.assertEqual(self.run_main("--table", T, "--out", str(html), "--no-open", transport=t), 0)
        cache = g.load_cache(self.dir / "out" / "gcp-costtree-data.json")
        self.assertEqual(cache["resources"][0][4], "//x/a")
        self.assertNotIn(TOKEN, html.read_text() + self.out.getvalue() + self.err.getvalue())

    def test_export_file_within_cap(self):
        info = g.TableInfo("p.ds.t", "p", "ds", "t", "US", "standard", "_PARTITIONTIME", "TIMESTAMP")
        rows = [q1_row(day="2026-09-01", sku_id=f"K{i}", sku=f"sku {i} " + "y" * 190, project=f"p{i % 80}-" + "z" * 150)
                for i in range(400)]
        cache = self.dir / "big.json"
        g.save_cache(g.build_cache(info, g.compute_window(TODAY, 30, 2), [], 50, 25, "s", rows, [], "f"), cache)
        exp = self.dir / "e.json"
        self.assertEqual(self.run_main("--from", str(cache), "--out", str(self.dir / "v.html"), "--no-open",
                                       "--export", str(exp)), 0)
        self.assertLessEqual(exp.stat().st_size, g.EXPORT_CAP)

    def test_dry_run_prints_estimate(self):
        t = FakeTransport([table_meta(), {"totalBytesProcessed": "2000000000"}, {"totalBytesProcessed": "1000"}])
        self.assertEqual(self.run_main("--table", T, "--dry-run", transport=t), 0)
        self.assertIn("daily: 2.00 GB", self.out.getvalue())
        self.assertIn("actual cost depends on your pricing model", self.out.getvalue())

    def test_bad_render_config_fails_before_any_query(self):
        for cfg in ("[rules.nat-processing]\nbogus = 1\n", '[categories]\n"Cloud Run" = "Weird"\n'):
            (self.dir / "gcp-costtree.toml").write_text(cfg)
            t = FakeTransport([])
            self.assertEqual(self.run_main("--table", T, transport=t), 2)
            self.assertEqual(t.calls, [])

    def test_newline_token_exit_1_without_echo(self):
        err_before = self.err.getvalue()
        rc = g.main(["--table", T], today=TODAY, env={"GCP_COSTTREE_TOKEN": "ya29.SECRET\nx"}, transport=FakeTransport([]),
                    stdout=self.out, stderr=self.err, open_browser=self.opened.append, cwd=self.dir)
        self.assertEqual(rc, 1)
        self.assertNotIn("SECRET", self.err.getvalue()[len(err_before):])

    def test_bad_end_fails_before_auth_or_queries(self):
        for extra in ([], ["--dry-run"]):
            t = FakeTransport([])
            rc = g.main(["--table", T, "--end", "2026-09-28"] + extra, today=TODAY, now=NOW, env={}, transport=t,
                        stdout=self.out, stderr=self.err, open_browser=self.opened.append, cwd=self.dir)
            self.assertEqual(rc, 2)
            self.assertEqual(t.calls, [])
            self.assertIn("--end must be before today", self.err.getvalue())

    def test_usage_error_exit_2(self):
        self.assertEqual(self.run_main("--demo", "--dry-run"), 2)
        self.assertIn("cannot be combined", self.err.getvalue())

    def test_runtime_error_exit_1_without_token(self):
        t = FakeTransport([err(403, "accessDenied", "denied")])
        self.assertEqual(self.run_main("--table", T, transport=t), 1)
        self.assertIn("access denied", self.err.getvalue())
        self.assertNotIn(TOKEN, self.err.getvalue())
