"""Test fakes shared by several test modules."""
from decimal import Decimal


class FakeTransport:
    """Returns queued responses in order; records every call."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, body, headers):
        self.calls.append((method, url, body, headers))
        if not self.responses:
            raise AssertionError(f"unexpected call {method} {url}")
        r = self.responses.pop(0)
        return r if isinstance(r, tuple) else (200, r)


def err(status, reason, message="boom"):
    return (status, {"error": {"code": status, "message": message, "errors": [{"reason": reason}]}})


def schema(*names_types):
    return {"fields": [{"name": n, "type": t} for n, t in names_types]}


def rows(*values):
    return [{"f": [{"v": v} for v in vs]} for vs in values]


def table_meta(detailed=True, field=None, ftype="DATE", location="US"):
    names = ["billing_account_id", "service", "sku", "usage_start_time", "project", "labels", "location",
             "export_time", "cost", "currency", "usage", "credits", "cost_type", "cost_at_list"]
    if detailed:
        names.append("resource")
    fields = [{"name": n, "type": "STRING"} for n in names]
    if field:
        fields.append({"name": field, "type": ftype})
    meta = {"location": location, "schema": {"fields": fields}}
    meta["timePartitioning"] = {"type": "DAY", "field": field} if field else {"type": "DAY"}
    return meta


def q1_row(service_id="S1", sku_id="K1", day="2026-09-01", cost="10", credits="0", list_cost="11",
           project="p1", region="us-central1", cost_type="regular", labels=(), service=None, sku=None,
           unit="hour", usage=1.0, list_nulls=0, currency="USD", latest="2026-09-27T01:00:00.000000Z"):
    r = {"service_id": service_id, "sku_id": sku_id, "unit": unit, "project": project, "region": region,
         "cost_type": cost_type, "usage_day": day, "currency": currency,
         "service": service or f"svc {service_id}", "sku": sku or f"sku {sku_id}",
         "cost": Decimal(cost), "credits": Decimal(credits), "list_cost": Decimal(list_cost),
         "usage_amount": usage, "list_nulls": list_nulls, "latest_export": latest}
    for i, v in enumerate(labels):
        r[f"label_{i}"] = v
    return r


def q2_row(kind, service_id="S1", sku_id="K1", project="p1", region="us-central1", resource="r1",
           cur=("10", "0", "11"), prior=("0", "0", "0")):
    keys = ["cur_cost", "cur_credits", "cur_list", "prior_cost", "prior_credits", "prior_list"]
    r = {"row_kind": kind, "service_id": service_id, "sku_id": sku_id, "project": project, "region": region,
         "resource": None if kind == "total" else resource}
    r.update({k: Decimal(v) for k, v in zip(keys, list(cur) + list(prior))})
    return r
