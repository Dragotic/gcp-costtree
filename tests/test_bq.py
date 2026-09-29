import subprocess
import unittest
from decimal import Decimal

import gcp_costtree as g
from tests.helpers import FakeTransport, err, rows, schema

TOKEN = "ya29.SECRET-TOKEN"


def bq(responses, **kw):
    t = FakeTransport(responses)
    return g.BigQuery(TOKEN, t, sleep=lambda s: None, **kw), t


class TokenTest(unittest.TestCase):
    def test_env_wins(self):
        self.assertEqual(g.get_token({"GCP_COSTTREE_TOKEN": " abc \n"}, run=None), "abc")

    def test_gcloud_argument_list_no_shell(self):
        seen = {}

        def run(args, **kw):
            seen["args"], seen["kw"] = args, kw
            return subprocess.CompletedProcess(args, 0, stdout="tok\n", stderr="")
        self.assertEqual(g.get_token({}, run=run), "tok")
        self.assertEqual(seen["args"], ["gcloud", "auth", "print-access-token"])
        self.assertNotIn("shell", seen["kw"])

    def test_malformed_token_rejected_without_echo(self):
        for bad in ("ya29.SECRET\nsecond-line", "ya29.SE CRET", "ya29.\x00X"):
            with self.assertRaises(g.CostTreeError) as cm:
                g.get_token({"GCP_COSTTREE_TOKEN": bad}, run=None)
            self.assertNotIn("SECRET", str(cm.exception))
            self.assertNotIn("SE CRET", str(cm.exception))

    def test_gcloud_failure(self):
        run = lambda args, **kw: subprocess.CompletedProcess(args, 1, stdout="", stderr="ERROR: reauth")
        with self.assertRaisesRegex(g.CostTreeError, "gcloud auth login"):
            g.get_token({}, run=run)

    def test_gcloud_missing(self):
        def run(args, **kw):
            raise FileNotFoundError
        with self.assertRaisesRegex(g.CostTreeError, "GCP_COSTTREE_TOKEN"):
            g.get_token({}, run=run)


class QueryTest(unittest.TestCase):
    SCHEMA = schema(("n", "INT64"), ("amount", "NUMERIC"), ("d", "DATE"), ("ts", "TIMESTAMP"),
                    ("f", "FLOAT64"), ("s", "STRING"))

    def test_single_page(self):
        client, t = bq([{"jobComplete": True, "jobReference": {"projectId": "p", "jobId": "j", "location": "US"},
                         "schema": self.SCHEMA,
                         "rows": rows(["3", "1.100000001", "2026-09-01", "1790585945.283", "2.5", None])}])
        out = client.query("p", "SELECT 1", [], "US", 10**9)
        self.assertEqual(out, [{"n": 3, "amount": Decimal("1.100000001"), "d": "2026-09-01",
                                "ts": "2026-09-28T08:59:05.283000Z", "f": 2.5, "s": None}])
        method, url, body, headers = t.calls[0]
        self.assertEqual((method, url), ("POST", "https://bigquery.googleapis.com/bigquery/v2/projects/p/queries"))
        self.assertEqual(body["useLegacySql"], False)
        self.assertEqual(body["parameterMode"], "NAMED")
        self.assertEqual(body["maximumBytesBilled"], "1000000000")
        self.assertEqual(body["location"], "US")
        self.assertEqual(headers["Authorization"], "Bearer " + TOKEN)

    def test_poll_then_pages_keep_location(self):
        job = {"projectId": "p", "jobId": "j1", "location": "EU"}
        client, t = bq([
            {"jobComplete": False, "jobReference": job},
            {"jobComplete": False, "jobReference": job},
            {"jobComplete": True, "jobReference": job, "schema": schema(("n", "INT64")), "rows": rows(["1"]),
             "pageToken": "tok/2"},
            {"jobComplete": True, "jobReference": job, "schema": schema(("n", "INT64")), "rows": rows(["2"])},
        ])
        self.assertEqual(client.query("p", "q", [], "EU", 1), [{"n": 1}, {"n": 2}])
        gets = [c[1] for c in t.calls[1:]]
        self.assertTrue(all("/projects/p/queries/j1?" in u and "location=EU" in u for u in gets))
        self.assertIn("pageToken=tok%2F2", gets[-1])

    def test_query_post_is_never_retried(self):
        client, t = bq([err(503, "backendError")])
        with self.assertRaises(g.CostTreeError):
            client.query("p", "q", [], "US", 1)
        self.assertEqual(len(t.calls), 1)

    def test_results_poll_is_retried(self):
        job = {"projectId": "p", "jobId": "j", "location": "US"}
        client, t = bq([{"jobComplete": False, "jobReference": job}, err(503, "backendError"),
                        {"jobComplete": True, "jobReference": job, "schema": schema(("n", "INT64")), "rows": rows(["1"])}])
        self.assertEqual(client.query("p", "q", [], "US", 1), [{"n": 1}])
        self.assertEqual(len(t.calls), 3)

    def test_retries_exhausted(self):
        client, t = bq([err(503, "backendError")] * 4)
        with self.assertRaises(g.CostTreeError):
            client.get_table("p", "d", "t")
        self.assertEqual(len(t.calls), 4)

    def test_retry_on_status_only_and_reason_only(self):
        for first in ((500, {"error": {"message": "x"}}), err(403, "rateLimitExceeded")):
            client, t = bq([first, {"schema": {"fields": []}}])
            client.get_table("p", "d", "t")
            self.assertEqual(len(t.calls), 2)

    def test_reflected_token_is_redacted_and_one_line(self):
        client, _ = bq([err(403, "accessDenied", f"bad bearer {TOKEN}\nsecond line")])
        with self.assertRaises(g.CostTreeError) as cm:
            client.get_table("p", "d", "t")
        self.assertNotIn(TOKEN, str(cm.exception))
        self.assertNotIn("\n", str(cm.exception))

    def test_token_across_truncation_boundary_is_redacted(self):
        long_token = "ya29." + "A" * 200
        t = FakeTransport([err(403, "accessDenied", "x" * 250 + " token=" + long_token)])
        client = g.BigQuery(long_token, t, sleep=lambda s: None)
        with self.assertRaises(g.CostTreeError) as cm:
            client.get_table("p", "d", "t")
        self.assertNotIn("ya29.AAAA", str(cm.exception))

    def test_row_cap_enforced_while_paging(self):
        job = {"projectId": "p", "jobId": "j", "location": "US"}
        page = {"jobComplete": True, "jobReference": job, "schema": schema(("n", "INT64")), "rows": rows(["1"], ["2"]),
                "pageToken": "t"}
        client, t = bq([page, page])
        with self.assertRaisesRegex(g.CostTreeError, "limit"):
            client.query("p", "q", [], "US", 1, max_rows=3)
        self.assertEqual(len(t.calls), 2)

    def test_network_errors_are_one_line(self):
        from unittest import mock
        for exc in (TimeoutError("timed out"), ConnectionResetError("reset")):
            with mock.patch.object(g.urllib.request, "urlopen", side_effect=exc):
                with self.assertRaisesRegex(g.CostTreeError, "cannot reach BigQuery"):
                    g.UrllibTransport().request("GET", "https://example.invalid", None, {})

    def test_access_denied_not_retried_and_hides_token(self):
        client, t = bq([err(403, "accessDenied", "User does not have bigquery.jobs.create")])
        with self.assertRaises(g.CostTreeError) as cm:
            client.query("p", "q", [], "US", 1)
        self.assertEqual(len(t.calls), 1)
        self.assertIn("bigquery.jobs.create", str(cm.exception))
        self.assertNotIn(TOKEN, str(cm.exception))

    def test_not_found(self):
        client, _ = bq([err(404, "notFound", "Not found: Table p:d.t")])
        with self.assertRaisesRegex(g.CostTreeError, "bq ls"):
            client.get_table("p", "d", "t")

    def test_bytes_billed_limit(self):
        client, _ = bq([err(400, "bytesBilledLimitExceeded")])
        with self.assertRaisesRegex(g.CostTreeError, "--max-gb"):
            client.query("p", "q", [], "US", 1)

    def test_401_during_poll_maps_to_login_hint(self):
        job = {"projectId": "p", "jobId": "j", "location": "US"}
        client, _ = bq([{"jobComplete": False, "jobReference": job}, (401, {"error": {"message": "expired"}})])
        with self.assertRaises(g.CostTreeError) as cm:
            client.query("p", "q", [], "US", 1)
        self.assertIn("gcloud auth login", str(cm.exception))
        self.assertNotIn(TOKEN, str(cm.exception))

    def test_dry_run_returns_bytes(self):
        client, t = bq([{"totalBytesProcessed": "2031120", "jobComplete": True}])
        self.assertEqual(client.query("p", "q", [], "US", 5, dry_run=True), 2031120)
        self.assertIs(t.calls[0][2]["dryRun"], True)

    def test_poll_timeout(self):
        now = [0.0]
        job = {"projectId": "p", "jobId": "j", "location": "US"}
        t = FakeTransport([{"jobComplete": False, "jobReference": job}] * 50)

        def sleep(s):
            now[0] += s
        client = g.BigQuery(TOKEN, t, sleep=sleep, clock=lambda: now[0], poll_timeout=5)
        with self.assertRaisesRegex(g.CostTreeError, "did not finish"):
            client.query("p", "q", [], "US", 1)

    def test_get_table_quotes_domain_project(self):
        client, t = bq([{"schema": {"fields": []}}])
        client.get_table("example.com:proj", "d", "t")
        self.assertIn("/projects/example.com%3Aproj/datasets/d/tables/t", t.calls[0][1])


if __name__ == "__main__":
    unittest.main()
