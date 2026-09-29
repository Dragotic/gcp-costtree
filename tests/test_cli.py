import tempfile
import unittest
from pathlib import Path

import gcp_costtree as g

T = "p.ds.gcp_billing_export_resource_v1_X"


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def cfg(self, text):
        (self.cwd / "gcp-costtree.toml").write_text(text)

    def test_live_defaults(self):
        o = g.parse_args(["--table", T], self.cwd)
        self.assertEqual((o.mode, o.days, o.lag_days, o.labels, o.label_top, o.top_resources, o.max_gb),
                         ("live", 30, 2, [], 50, 25, 50.0))
        self.assertEqual((o.metric, o.export, o.out, o.no_open), ("net", None, "out/gcp-costtree.html", False))

    def test_live_requires_table(self):
        with self.assertRaises(g.UsageError):
            g.parse_args([], self.cwd)

    def test_modes_exclusive(self):
        for argv in (["--demo", "--dry-run"], ["--demo", "--from", "x.json"], ["--dry-run", "--from", "x"]):
            with self.assertRaises(g.UsageError, msg=argv):
                g.parse_args(argv, self.cwd)

    def test_fetch_flag_rejected_with_demo_and_from(self):
        for argv in (["--demo", "--days", "7"], ["--from", "c.json", "--label", "env"], ["--demo", "--table", T]):
            with self.assertRaises(g.UsageError, msg=argv):
                g.parse_args(argv, self.cwd)

    def test_fetch_flag_error_grammar(self):
        with self.assertRaisesRegex(g.UsageError, "^--days only applies when querying"):
            g.parse_args(["--demo", "--days", "7"], self.cwd)
        with self.assertRaisesRegex(g.UsageError, "^--days, --label only apply when querying"):
            g.parse_args(["--demo", "--days", "7", "--label", "env"], self.cwd)

    def test_config_fetch_values_ignored_in_demo(self):
        self.cfg(f'table = "{T}"\ndays = 7\nlabels = ["env"]\n')
        o = g.parse_args(["--demo"], self.cwd)
        self.assertEqual((o.mode, o.table, o.days, o.labels), ("demo", None, 30, []))

    def test_cli_labels_replace_config(self):
        self.cfg(f'table = "{T}"\nlabels = ["env", "team"]\n')
        self.assertEqual(g.parse_args(["--label", "app"], self.cwd).labels, ["app"])
        self.assertEqual(g.parse_args([], self.cwd).labels, ["env", "team"])

    def test_label_limits(self):
        many = []
        for i in range(7):
            many += ["--label", f"k{i}"]
        for argv in (many, ["--label", "a", "--label", "a"], ["--label", ""], ["--label", "a b"]):
            with self.assertRaises(g.UsageError, msg=argv):
                g.parse_args(["--table", T] + argv, self.cwd)
        self.assertEqual(g.parse_args(["--table", T, "--label", "project:team"], self.cwd).labels, ["project:team"])

    def test_end_flag_and_config(self):
        self.assertEqual(g.parse_args(["--table", T, "--end", "2026-08-25"], self.cwd).end, g.dt.date(2026, 8, 25))
        self.assertIsNone(g.parse_args(["--table", T], self.cwd).end)
        for bad in ("2026-8-25", "yesterday", "2026-02-30", "1999-12-31"):
            with self.assertRaises(g.UsageError, msg=bad):
                g.parse_args(["--table", T, "--end", bad], self.cwd)
        for mode in (["--demo"], ["--from", "c.json"]):
            with self.assertRaises(g.UsageError):
                g.parse_args(mode + ["--end", "2026-08-25"], self.cwd)
        self.cfg(f'table = "{T}"\nend = "2026-08-20"\n')
        self.assertEqual(g.parse_args([], self.cwd).end, g.dt.date(2026, 8, 20))
        self.cfg(f'table = "{T}"\nend = 2026-08-21\n')  # an unquoted TOML date works too
        self.assertEqual(g.parse_args([], self.cwd).end, g.dt.date(2026, 8, 21))
        self.cfg(f'table = "{T}"\nend = 2026-08-21T10:00:00\n')
        with self.assertRaisesRegex(g.UsageError, "without a time"):
            g.parse_args([], self.cwd)

    def test_export_optional_value(self):
        self.assertEqual(g.parse_args(["--demo", "--export"], self.cwd).export, "out/gcp-costtree-export.json")
        self.assertEqual(g.parse_args(["--demo", "--export", "e.json"], self.cwd).export, "e.json")

    def test_explicit_config_must_exist(self):
        with self.assertRaises(g.UsageError):
            g.parse_args(["--demo", "--config", "missing.toml"], self.cwd)

    def test_config_type_and_key_errors(self):
        for text in ('days = "30"\n', "no_open = 1\n", "bogus = 1\n", "days = true\n", "labels = \"env\"\n", "[rules\n"):
            self.cfg(f'table = "{T}"\n' + text)
            with self.assertRaises(g.UsageError, msg=text):
                g.parse_args([], self.cwd)

    def test_rules_and_categories_pass_through(self):
        self.cfg('[rules.growth]\nfactor = 2.0\n[categories]\n"Cloud Run" = "Compute"\n')
        o = g.parse_args(["--demo"], self.cwd)
        self.assertEqual(o.rules, {"growth": {"factor": 2.0}})
        self.assertEqual(o.categories, {"Cloud Run": "Compute"})

    def test_ranges(self):
        for argv in (["--days", "0"], ["--lag-days", "9"], ["--label-top", "0"], ["--top-resources", "0"], ["--max-gb", "0"],
                     ["--max-gb", "nan"], ["--max-gb", "inf"]):
            with self.assertRaises(g.UsageError, msg=argv):
                g.parse_args(["--table", T] + argv, self.cwd)

    def test_bad_table_rejected_before_any_call(self):
        with self.assertRaises(g.UsageError):
            g.parse_args(["--table", "p.d.t;x"], self.cwd)


if __name__ == "__main__":
    unittest.main()
