import csv
import datetime as dt
import unittest
from pathlib import Path

import gcp_costtree as g
from tests.helpers import q1_row

STD = g.TableInfo("p.ds.t", "p", "ds", "t", "US", "standard", "_PARTITIONTIME", "TIMESTAMP")
W = g.compute_window(dt.date(2026, 9, 28), 30, 2)
FIXTURE = Path(__file__).parent / "fixtures" / "export_skus.csv"


def cache(rows):
    return g.build_cache(STD, W, [], 50, 25, "s", rows, [], "f")


def line(service, sku, cur, prior=None, sid=None, kid=None):
    sid, kid = sid or service, kid or sku
    out = [q1_row(service_id=sid, sku_id=kid, service=service, sku=sku, day="2026-09-01", cost=str(cur))]
    if prior is not None:
        out.append(q1_row(service_id=sid, sku_id=kid, service=service, sku=sku, day="2026-07-29", cost=str(prior)))
    return out


def rule_ids(flags):
    return sorted({f["rule"] for f in flags})


class FixtureTest(unittest.TestCase):
    def test_real_sku_descriptions(self):
        rules = [r for r in g.load_rules({}) if r.type == "sku"]
        with FIXTURE.open(newline="") as f:
            fixture = list(csv.DictReader(f))
        for row in fixture:
            hits = sorted(r.id for r in rules if r.service.search(row["service"]) and r.sku.search(row["sku"]))
            want = [] if row["expect"] == "-" else [row["expect"]]
            self.assertEqual(hits, want, row["sku"])


class RulesTest(unittest.TestCase):
    def test_threshold_default_floor(self):
        nat = ("Networking", "Networking Cloud Nat Data Processing")
        self.assertEqual(rule_ids(g.evaluate_rules(cache(line(*nat, 60)), g.load_rules({}))), ["nat-processing"])
        self.assertEqual(g.evaluate_rules(cache(line(*nat, 40)), g.load_rules({})), [])

    def test_many_fractional_rows_hit_exact_threshold(self):
        rows = [q1_row(service_id="Networking", sku_id="nat", service="Networking",
                       sku="Networking Cloud Nat Data Processing", day="2026-09-01", cost="0.1", project=f"p{i}")
                for i in range(1000)]
        flags = g.evaluate_rules(cache(rows), g.load_rules({"nat-processing": {"min_cost": 100}}))
        self.assertEqual(rule_ids(flags), ["nat-processing"])

    def test_large_row_counts_sum_exactly(self):
        rows = [q1_row(service_id="Networking", sku_id="nat", service="Networking",
                       sku="Networking Cloud Nat Data Processing", day="2026-09-01", cost="100.01", project=f"p{i}")
                for i in range(100_000)]
        flags = g.evaluate_rules(cache(rows), g.load_rules({"nat-processing": {"min_cost": 10_001_000}}))
        self.assertEqual(rule_ids(flags), ["nat-processing"])

    def test_threshold_exact_with_credits(self):
        rows = [q1_row(service_id="Networking", sku_id="nat", service="Networking",
                       sku="Networking Cloud Nat Data Processing", day="2026-09-01", cost="199.998", credits="-99.998")]
        flags = g.evaluate_rules(cache(rows), g.load_rules({"nat-processing": {"min_cost": 100}}))
        self.assertEqual(rule_ids(flags), ["nat-processing"])

    def test_growth_exact_boundary(self):
        rows = [q1_row(sku_id="y", day="2026-07-29", cost="200.3", credits="-0.3"),
                q1_row(sku_id="y", day="2026-09-01", cost="300.7", credits="-0.7")]
        self.assertEqual(rule_ids(g.evaluate_rules(cache(rows), g.load_rules({}))), ["growth"])

    def test_threshold_scales_with_total(self):
        rows = line("Networking", "Networking Cloud Nat Data Processing", 60) + line("Cloud Run", "cpu", 100000)
        self.assertEqual(g.evaluate_rules(cache(rows), g.load_rules({})), [])  # 0.5% of 100060 > 60

    def test_negative_total_uses_floor(self):
        rows = line("Networking", "Networking Cloud Nat Data Processing", 60) + \
            [q1_row(service_id="C", sku_id="c", day="2026-09-01", cost="0", credits="-5000")]
        self.assertEqual(rule_ids(g.evaluate_rules(cache(rows), g.load_rules({}))), ["nat-processing"])

    def test_growth(self):
        rules = g.load_rules({})
        self.assertEqual(rule_ids(g.evaluate_rules(cache(line("X", "y", 250, 100)), rules)), ["growth"])
        self.assertEqual(g.evaluate_rules(cache(line("X", "y", 190, 100)), rules), [])  # delta < 100
        self.assertEqual(g.evaluate_rules(cache(line("X", "y", 140, 100)), rules), [])  # < 1.5x
        self.assertEqual(g.evaluate_rules(cache(line("X", "y", 500, 0)), rules), [])    # prior 0
        self.assertEqual(g.evaluate_rules(cache(line("X", "y", 500, -10)), rules), [])  # prior negative

    def test_growth_off_when_prior_not_covered(self):
        c = cache(line("X", "y", 250, 100))
        c["meta"]["prior_covered"] = False
        self.assertEqual(g.evaluate_rules(c, g.load_rules({})), [])

    def test_flag_shape(self):
        f = g.evaluate_rules(cache(line("Networking", "Networking Cloud Nat Data Processing", 60, 50)), g.load_rules({}))[0]
        self.assertEqual({k: f[k] for k in ("rule", "service_id", "sku", "net", "prior_net")},
                         {"rule": "nat-processing", "service_id": "Networking",
                          "sku": "Networking Cloud Nat Data Processing", "net": 60.0, "prior_net": 50.0})
        self.assertIn("NAT", f["message"])

    def test_config_overrides(self):
        nat = line("Networking", "Networking Cloud Nat Data Processing", 60)
        self.assertEqual(g.evaluate_rules(cache(nat), g.load_rules({"nat-processing": {"enabled": False}})), [])
        self.assertEqual(g.evaluate_rules(cache(nat), g.load_rules({"nat-processing": {"min_cost": 100}})), [])
        custom = {"idle": {"service": "(?i)cloud run", "sku": "(?i)min instance", "message": "Idle mins", "min_cost": 1}}
        rows = line("Cloud Run", "Services Min Instance CPU (Request-based billing)", 5)
        flags = g.evaluate_rules(cache(rows), g.load_rules(custom))
        self.assertEqual([(f["rule"], f["message"]) for f in flags], [("idle", "Idle mins")])
        growth = g.load_rules({"growth": {"factor": 3.0}})
        self.assertEqual(g.evaluate_rules(cache(line("X", "y", 250, 100)), growth), [])

    def test_config_errors(self):
        for bad in ({"nat-processing": {"bogus": 1}}, {"new": {"service": "x"}}, {"new": {"service": "(", "sku": "x", "message": "m"}},
                    {"growth": {"factor": "2"}}, {"nat-processing": {"min_cost": True}}, {"nat-processing": 5},
                    {"nat-processing": {"enabled": "false"}}):
            with self.assertRaises(g.UsageError, msg=bad):
                g.load_rules(bad)


class CategoriesTest(unittest.TestCase):
    def test_defaults_overrides_and_fallbacks(self):
        rows = line("Cloud Run", "cpu", 1, sid="152E") + line("Mystery", "m", 1, sid="ZZ") + \
            [q1_row(service_id="_tax", sku_id="tax", service="Tax & adjustments", cost_type="tax", day="2026-09-01")]
        c = cache(rows)
        self.assertEqual(g.categorize(c, {}), {"152E": "Compute", "ZZ": "Other", "_tax": "Tax & Support"})
        self.assertEqual(g.categorize(c, {"Mystery": "Databases"})["ZZ"], "Databases")
        with self.assertRaises(g.UsageError):
            g.categorize(c, {"Mystery": "Weird"})


if __name__ == "__main__":
    unittest.main()
