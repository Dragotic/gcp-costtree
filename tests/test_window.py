import datetime as dt
import unittest

import gcp_costtree as g


class WindowTest(unittest.TestCase):
    def test_defaults_match_spec_example(self):
        w = g.compute_window(dt.date(2026, 9, 28), 30, 2)
        self.assertEqual(w.last_day, dt.date(2026, 9, 26))
        self.assertEqual(w.period_end, dt.date(2026, 9, 27))
        self.assertEqual(w.period_start, dt.date(2026, 8, 28))
        self.assertEqual(w.prior_start, dt.date(2026, 7, 29))
        self.assertEqual(w.partition_end, dt.date(2026, 9, 29))

    def test_lag_one_includes_yesterday(self):
        w = g.compute_window(dt.date(2026, 9, 28), 7, 1)
        self.assertEqual(w.last_day, dt.date(2026, 9, 27))
        self.assertEqual(w.period_start, dt.date(2026, 9, 21))

    def test_end_sets_last_day(self):
        w = g.compute_window(dt.date(2026, 9, 29), 14, 2, end=dt.date(2026, 8, 25))
        self.assertEqual((w.period_start, w.last_day), (dt.date(2026, 8, 12), dt.date(2026, 8, 25)))
        self.assertEqual(w.prior_start, dt.date(2026, 7, 29))
        self.assertEqual(w.partition_end, dt.date(2026, 9, 30))  # late rows for old days still land in newer partitions

    def test_end_cannot_be_today_or_later(self):
        for end in (dt.date(2026, 9, 29), dt.date(2026, 10, 1)):
            with self.assertRaises(g.UsageError):
                g.compute_window(dt.date(2026, 9, 29), 14, 2, end=end)

    def test_end_ignores_lag_and_rejects_unrepresentable_dates(self):
        a = g.compute_window(dt.date(2026, 9, 29), 14, 1, end=dt.date(2026, 8, 25))
        b = g.compute_window(dt.date(2026, 9, 29), 14, 7, end=dt.date(2026, 8, 25))
        self.assertEqual(a, b)
        for end in (dt.date(1, 1, 1), dt.date(1, 3, 1)):
            with self.assertRaises(g.UsageError):
                g.compute_window(dt.date(2026, 9, 29), 90, 2, end=end)

    def test_ranges(self):
        for days, lag in ((0, 2), (91, 2), (30, 0), (30, 8)):
            with self.assertRaises(g.UsageError):
                g.compute_window(dt.date(2026, 9, 28), days, lag)


class TableIdTest(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(g.parse_table_id("p-1.ds_1.t_1"), ("p-1", "ds_1", "t_1"))

    def test_domain_scoped_project(self):
        self.assertEqual(g.parse_table_id("example.com:proj.ds.t"), ("example.com:proj", "ds", "t"))

    def test_hostile_rejected(self):
        for bad in ("p.d", "p.d.t`; DROP", "p.d.t --", "p.d.t\n", "`p.d.t`", "p.d e.t", ""):
            with self.assertRaises(g.UsageError, msg=bad):
                g.parse_table_id(bad)


if __name__ == "__main__":
    unittest.main()
