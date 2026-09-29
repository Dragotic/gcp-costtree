import datetime as dt
import unittest

import gcp_costtree as g

STD = g.TableInfo("p.ds.t", "p", "ds", "t", "US", "standard", "_PARTITIONTIME", "TIMESTAMP")
DET = g.TableInfo("p.ds.r", "p", "ds", "r", "US", "detailed", "_PARTITIONTIME", "TIMESTAMP")
W = g.compute_window(dt.date(2026, 9, 28), 30, 2)
SNAP = "2026-09-28 10:00:00.000000+00"

GOLDEN_Q1_STD = """WITH base AS (
  SELECT
    IF(cost_type = 'regular', IFNULL(service.id, '(unknown)'), '_tax') AS service_id,
    IF(cost_type = 'regular', IFNULL(service.description, '(unknown)'), 'Tax & adjustments') AS service,
    IF(cost_type = 'regular', IFNULL(sku.id, '(unknown)'), cost_type) AS sku_id,
    IF(cost_type = 'regular', IFNULL(sku.description, '(unknown)'), cost_type) AS sku,
    IFNULL(usage.pricing_unit, '') AS unit,
    IFNULL(project.id, '(no project)') AS project,
    COALESCE(location.region, location.location, '(unspecified)') AS region,
    cost_type,
    DATE(usage_start_time) AS usage_day,
    currency,
    export_time,
    CAST(cost AS NUMERIC) AS cost,
    IFNULL((SELECT SUM(CAST(c.amount AS NUMERIC)) FROM UNNEST(credits) c), 0) AS credits,
    CAST(cost_at_list AS NUMERIC) AS list_cost,
    IFNULL(usage.amount_in_pricing_units, 0) AS usage_amount
  FROM `p.ds.t` FOR SYSTEM_TIME AS OF @snapshot_ts
  WHERE _PARTITIONTIME >= @part_start
    AND _PARTITIONTIME < @part_end
    AND usage_start_time >= TIMESTAMP(@prior_start)
    AND usage_start_time < TIMESTAMP(@period_end)
)
SELECT service_id, sku_id, unit, project, region, cost_type, usage_day, currency,
  ANY_VALUE(service) AS service, ANY_VALUE(sku) AS sku, SUM(cost) AS cost, SUM(credits) AS credits, IFNULL(SUM(list_cost), 0) AS list_cost, SUM(usage_amount) AS usage_amount, COUNTIF(list_cost IS NULL AND cost_type = 'regular') AS list_nulls, MAX(export_time) AS latest_export
FROM base
GROUP BY service_id, sku_id, unit, project, region, cost_type, usage_day, currency"""


def names(params):
    return {p["name"]: (p["parameterType"]["type"], p["parameterValue"]["value"]) for p in params}


class SqlTest(unittest.TestCase):
    def test_q1_standard_golden(self):
        self.assertEqual(g.build_q1(STD, []), GOLDEN_Q1_STD)

    def test_q1_labels_use_parameters_and_fold(self):
        sql = g.build_q1(STD, ["env", "project:team"])
        self.assertIn("FROM UNNEST(labels) l WHERE l.key = @label_key_0", sql)
        self.assertIn("FROM UNNEST(project.labels) l WHERE l.key = @label_key_1", sql)
        self.assertNotIn("'env'", sql)
        self.assertNotIn("team", sql)
        self.assertIn("top_label_0 AS (", sql)
        self.assertIn("LIMIT @label_top", sql)
        self.assertIn("IF(label_1 IN (SELECT v FROM top_label_1), label_1, '(other)') AS label_1", sql)
        self.assertIn("FROM folded", sql)
        self.assertIn("GROUP BY service_id, sku_id, unit, project, region, cost_type, label_0, label_1, usage_day, currency", sql)

    def test_custom_partition_field_is_quoted(self):
        info = g.TableInfo("p.ds.t", "p", "ds", "t", "EU", "standard", "export_date", "DATE")
        sql = g.build_q1(info, [])
        self.assertIn("WHERE `export_date` >= @part_start", sql)
        p = names(g.query_params(info, W, SNAP))
        self.assertEqual(p["part_start"], ("DATE", "2026-07-28"))
        self.assertEqual(p["part_end"], ("DATE", "2026-09-29"))

    def test_unpartitioned_table_has_no_partition_filter(self):
        info = g.TableInfo("p.ds.t", "p", "ds", "t", "US", "standard", None, "TIMESTAMP")
        self.assertNotIn("@part_start", g.build_q1(info, []))
        self.assertNotIn("part_start", names(g.query_params(info, W, SNAP)))

    def test_bad_partition_column_rejected(self):
        info = g.TableInfo("p.ds.t", "p", "ds", "t", "US", "standard", "x`; y", "DATE")
        with self.assertRaises(g.CostTreeError):
            g.build_q1(info, [])

    def test_q2_shape(self):
        sql = g.build_q2(DET)
        self.assertIn("COALESCE(resource.global_name, resource.name, '(no resource)') AS resource", sql)
        self.assertIn("AND cost_type = 'regular'", sql)
        self.assertIn("ORDER BY GREATEST(cur_cost + cur_credits, prior_cost + prior_credits) DESC, project, region, resource", sql)
        self.assertIn(") <= @top", sql)
        self.assertIn("UNION ALL", sql)
        self.assertIn("SELECT 'total' AS row_kind", sql)
        self.assertIn("IFNULL(SUM(IF(usage_day >= @period_start, list_cost, NULL)), 0) AS cur_list", sql)

    def test_params_q1_without_labels(self):
        p = names(g.query_params(STD, W, SNAP))
        self.assertEqual(set(p), {"prior_start", "period_end", "snapshot_ts", "part_start", "part_end"})
        self.assertEqual(p["prior_start"], ("DATE", "2026-07-29"))
        self.assertEqual(p["period_end"], ("DATE", "2026-09-27"))
        self.assertEqual(p["snapshot_ts"], ("TIMESTAMP", SNAP))
        self.assertEqual(p["part_start"], ("TIMESTAMP", "2026-07-28 00:00:00+00"))
        self.assertEqual(p["part_end"], ("TIMESTAMP", "2026-09-29 00:00:00+00"))

    def test_params_q1_with_labels(self):
        p = names(g.query_params(STD, W, SNAP, labels=["env", "project:team"], label_top=50))
        self.assertEqual(p["label_key_0"], ("STRING", "env"))
        self.assertEqual(p["label_key_1"], ("STRING", "team"))
        self.assertEqual(p["label_top"], ("INT64", "50"))
        self.assertEqual(p["period_start"], ("DATE", "2026-08-28"))

    def test_params_q2(self):
        p = names(g.query_params(DET, W, SNAP, top=25))
        self.assertEqual(p["top"], ("INT64", "25"))
        self.assertIn("period_start", p)
        self.assertNotIn("label_top", p)


if __name__ == "__main__":
    unittest.main()
