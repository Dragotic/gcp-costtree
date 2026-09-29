import datetime as dt
import unittest

import gcp_costtree as g

TODAY = dt.date(2026, 9, 28)


class DemoTest(unittest.TestCase):
    def test_deterministic_and_valid(self):
        a, b = g.make_demo(TODAY), g.make_demo(TODAY)
        self.assertEqual(a, b)
        g.validate_cache(a)
        self.assertEqual(a["meta"]["labels"], ["env", "project:team"])
        self.assertEqual(a["meta"]["export"], "detailed")
        self.assertTrue(a["meta"]["prior_covered"])
        self.assertIs(a["meta"]["demo"], True)

    def test_every_builtin_rule_fires(self):
        flags = g.evaluate_rules(g.make_demo(TODAY), g.load_rules({}))
        self.assertEqual({f["rule"] for f in flags}, {r["id"] for r in g.BUILTIN_RULES})

    def test_has_other_bucket_and_tax(self):
        c = g.make_demo(TODAY)
        self.assertIn("(other)", [r[4] for r in c["resources"]])
        self.assertIn(["_tax", "Tax & adjustments"], c["dims"]["service"])

    def test_categories_known(self):
        cats = g.categorize(g.make_demo(TODAY), {})
        self.assertNotIn("Other", cats.values())


class DemoStackTest(unittest.TestCase):
    def test_gke_not_cloud_run(self):
        c = g.make_demo(TODAY)
        services = [desc for _, desc in c["dims"]["service"]]
        self.assertNotIn("Cloud Run", services)
        net = {}
        for (si,), a in g.aggregate(c, ("service",)).items():
            net[c["dims"]["service"][si][1]] = a[0] + a[1]
        self.assertEqual(max(net, key=net.get), "Kubernetes Engine")
        gke = c["dims"]["service"].index(["CCD8-9BF1-090E", "Kubernetes Engine"])
        names = [r[4] for r in c["resources"] if r[0] == gke]
        self.assertTrue(names and all(n.startswith("//container.googleapis.com/") for n in names), names)


    def test_demo_resource_hosts_are_real_api_hosts(self):
        real = {"container", "compute", "sqladmin", "logging", "storage", "bigqueryreservation", "redis",
                "datastream", "artifactregistry"}
        for r in g.make_demo(TODAY)["resources"]:
            if r[4].startswith("//"):
                self.assertIn(r[4][2:].split(".googleapis.com")[0], real, r[4])
