import datetime as dt
import json
import re
import unittest

import gcp_costtree as g

TODAY = dt.date(2026, 9, 28)
EVIL = '</script><script>alert(1)</script><img src=x onerror=alert(2)>&amp;'
BS = "\\"  # one backslash, kept out of string literals on purpose


def render(cache, rules=None):
    flags = g.evaluate_rules(cache, g.load_rules(rules or {}))
    return g.render_html(cache, flags, g.categorize(cache, {}), "net", g.TEMPLATE_PATH.read_text())


def payload(html):
    m = re.search(r'<script type="application/json" id="data">(.*?)</script>', html, re.S)
    return json.loads(m.group(1))


class RenderTest(unittest.TestCase):
    def test_embeds_data_without_table(self):
        c = g.make_demo(TODAY)
        html = render(c)
        self.assertNotIn("__COSTTREE_DATA__", html)
        d = payload(html)
        self.assertNotIn("table", d["meta"])
        self.assertEqual(d["meta"]["table_hash"], c["meta"]["table_hash"])
        self.assertEqual(d["view"], {"metric": "net"})
        self.assertEqual(d["daily"], c["daily"])
        self.assertIn("flags", d)
        self.assertIn("categories", d)
        self.assertLessEqual(len(json.dumps(d["export"], separators=(",", ":"), ensure_ascii=False).encode()),
                             g.EMBED_EXPORT_CAP)
        self.assertNotIn(c["meta"]["table"], html)

    def test_hostile_values_cannot_break_out(self):
        c = g.make_demo(TODAY)
        c["dims"]["label:env"][0] = EVIL
        c["resources"][0][4] = EVIL
        html = render(c, {"evil": {"service": ".", "sku": ".", "message": EVIL, "min_cost": 0}})
        self.assertEqual(html.count("</script>"), 3)  # data, lib, app
        self.assertNotIn("<img", html)
        self.assertIn(BS + "u003c/script" + BS + "u003e", html)
        d = payload(html)
        self.assertEqual(d["dims"]["label:env"][0], EVIL)
        self.assertIn(EVIL, [f["message"] for f in d["flags"]])

    def test_unicode_survives_embedding(self):
        c = g.make_demo(TODAY)
        c["dims"]["project"][0] = "prøject-💸- -עברית"
        self.assertEqual(payload(render(c))["dims"]["project"][0], "prøject-💸- -עברית")

    def test_invalid_cache_is_not_rendered(self):
        c = g.make_demo(TODAY)
        c["daily"][0][0] = 999
        with self.assertRaisesRegex(g.CostTreeError, "invalid cache"):
            g.render_html(c, [], {}, "net", g.TEMPLATE_PATH.read_text())

    def test_template_placeholder_must_exist_once(self):
        c = g.make_demo(TODAY)
        with self.assertRaises(g.CostTreeError):
            g.render_html(c, [], {}, "net", "<html></html>")

    def test_csp_present(self):
        html = render(g.make_demo(TODAY))
        self.assertIn("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:", html)
