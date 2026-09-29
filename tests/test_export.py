import datetime as dt
import json
import unittest

import gcp_costtree as g
from tests.helpers import q1_row

TODAY = dt.date(2026, 9, 28)
STD = g.TableInfo("p.ds.t", "p", "ds", "t", "US", "standard", "_PARTITIONTIME", "TIMESTAMP")
W = g.compute_window(TODAY, 30, 2)


def export_of(c, cap=g.EXPORT_CAP):
    return g.build_export(c, g.evaluate_rules(c, g.load_rules({})), g.categorize(c, {}), cap)


def size(d):
    return len(json.dumps(d, separators=(",", ":"), ensure_ascii=False).encode())


class ExportTest(unittest.TestCase):
    def test_demo_shape_and_limits(self):
        e = export_of(g.make_demo(TODAY))
        self.assertEqual(e["schema"], "gcp-costtree/export/v1")
        for key, limit in (("by_service", 30), ("by_category", 30), ("by_region", 30), ("by_project", 50),
                           ("top_skus", 50), ("growers", 10), ("new_spend", 10), ("top_resources", 25), ("flags", 100)):
            self.assertLessEqual(len(e[key]["items"]), limit, key)
        self.assertEqual(set(e["by_label"]), {"env", "project:team"})
        svc_sum = sum(i["metrics"]["net"]["cur"] for i in e["by_service"]["items"])
        self.assertAlmostEqual(svc_sum, e["totals"]["net"]["cur"], places=1)
        self.assertIn("BigQuery Standard Edition for US (multi-region)", [i["sku"] for i in e["growers"]["items"]])
        self.assertNotIn("(other)", [i["resource"] for i in e["top_resources"]["items"]])
        self.assertNotIn("table", e["meta"])
        self.assertFalse(e["truncated"])
        self.assertLessEqual(size(e), g.EXPORT_CAP)

    def test_projection(self):
        e = export_of(g.make_demo(TODAY))
        self.assertAlmostEqual(e["totals"]["projection_30d"], e["totals"]["net"]["cur"], places=0)

    def test_standard_export_has_no_resources(self):
        c = g.build_cache(STD, W, [], 50, 25, "s", [q1_row(day="2026-09-01")], [], "f")
        self.assertNotIn("top_resources", export_of(c))

    def test_prior_not_covered_hides_comparisons(self):
        c = g.build_cache(STD, W, [], 50, 25, "s", [q1_row(day="2026-09-01")], [], "f")
        m = export_of(c)["totals"]["net"]
        self.assertEqual((m["prior"], m["delta"]), (None, None))

    def test_list_incomplete_hides_list_comparison_only(self):
        rows = [q1_row(day="2026-07-29"), q1_row(day="2026-09-01", list_nulls=1)]
        c = g.build_cache(STD, W, [], 50, 25, "s", rows, [], "f")
        t = export_of(c)["totals"]
        self.assertIsNone(t["list"]["delta"])
        self.assertIsNotNone(t["net"]["delta"])

    def test_growers_only_include_growth(self):
        rows = [q1_row(sku_id="A", day="2026-07-29", cost="100"), q1_row(sku_id="A", day="2026-09-01", cost="50"),
                q1_row(sku_id="B", day="2026-07-29", cost="10"), q1_row(sku_id="B", day="2026-09-01", cost="30")]
        e = export_of(g.build_cache(STD, W, [], 50, 25, "s", rows, [], "f"))
        self.assertEqual([i["sku_id"] for i in e["growers"]["items"]], ["B"])

    def test_cap_and_trim(self):
        rows = [q1_row(day="2026-09-01", project=f"project-{i:05d}-" + "x" * 150, sku_id=f"K{i}") for i in range(3000)]
        c = g.build_cache(STD, W, [], 50, 25, "s", rows, [], "f")
        e = export_of(c, cap=16 * 1024)
        self.assertLessEqual(size(e), 16 * 1024)
        self.assertTrue(e["truncated"])
        self.assertGreater(e["by_project"]["omitted"], 0)
        self.assertGreater(e["by_project"]["omitted_net"], 0)

    def test_trim_fails_loudly_when_metadata_alone_is_too_big(self):
        c = g.make_demo(TODAY)
        with self.assertRaisesRegex(g.CostTreeError, "cannot fit"):
            export_of(c, cap=300)

    def test_long_unicode_identifiers_truncated(self):
        long = "💸" * 500
        rows = [q1_row(day="2026-09-01", labels=(long,))]
        c = g.build_cache(STD, W, ["env"], 50, 25, "s", rows, [], "f")
        e = export_of(c)
        self.assertLessEqual(len(e["by_label"]["env"]["items"][0]["name"]), 200)
        self.assertLessEqual(size(e), g.EXPORT_CAP)
