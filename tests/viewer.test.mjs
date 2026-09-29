import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const html = readFileSync(new URL("../viewer.html", import.meta.url), "utf8");
const lib = html.match(/<script id="lib">([\s\S]*?)<\/script>/)[1];
const ctx = {};
vm.createContext(ctx);
vm.runInContext(lib + "\n;globalThis.CT = CT;", ctx);
const CT = ctx.CT;

function data(extra = {}) {
  return {
    version: 1,
    meta: { period: ["2026-09-01", "2026-09-02"], prior: ["2026-08-30", "2026-08-31"], labels: ["env"],
      export: "detailed", currency: "USD", table_hash: "abc123", provisional_day: "2026-09-02",
      prior_covered: true, list_complete: { period: true, prior: true } },
    dims: { service: [["S1", "Compute Engine"], ["S2", "Cloud SQL"]], sku: [["K1", "Core"], ["K2", "Ram"], ["K3", "Storage"]],
      unit: ["hour"], project: ["p1", "p2"], region: ["r1"], cost_type: ["regular"], "label:env": ["prod", "(other)"] },
    daily_columns: ["service", "sku", "unit", "project", "region", "cost_type", "label:env", "day", "cost", "credits", "list", "usage"],
    daily: [
      [0, 0, 0, 0, 0, 0, 0, "2026-09-01", 100, -10, 110, 1],
      [0, 1, 0, 0, 0, 0, 0, "2026-09-02", 50, 0, 55, 2],
      [1, 2, 0, 1, 0, 0, 1, "2026-09-01", 30, 0, 33, 3],
      [0, 0, 0, 0, 0, 0, 0, "2026-08-31", 40, 0, 44, 1],
    ],
    resource_columns: ["service", "sku", "project", "region", "resource", "cur_cost", "cur_credits", "cur_list", "prior_cost", "prior_credits", "prior_list"],
    resources: [
      [0, 0, 0, 0, "//vm/a", 60, -10, 66, 40, 0, 44],
      [0, 0, 0, 0, "(other)", 40, 0, 44, 0, 0, 0],
      [0, 1, 0, 0, "//vm/a", 50, 0, 55, 0, 0, 0],
      [1, 2, 1, 0, "//db/x", 30, 0, 33, 0, 0, 0],
    ],
    flags: [], categories: {}, view: { metric: "net" }, ...extra,
  };
}
const find = (node, label) => node.children.find((c) => c.label === label);

test("model decodes days and periods", () => {
  const m = CT.buildModel(data());
  assert.equal(m.n, 4);
  assert.equal(Array.from(m.days).join(), "2026-08-30,2026-08-31,2026-09-01,2026-09-02");
  assert.equal(Array.from(m.cur).join(), "1,1,1,0");
  assert.equal(m.day[3], 1);
  assert.equal(m.hasResources, true);
});

test("service tree with signed totals and resource level", () => {
  const t = CT.buildTree(CT.buildModel(data()), "service", "net", "");
  assert.equal(t.cur, 170);
  assert.equal(t.prior, 40);
  assert.equal(t.children.map((c) => c.label).join(), "Compute Engine,Cloud SQL");
  const k1 = find(find(t, "Compute Engine"), "Core");
  assert.equal(k1.cur, 90);
  assert.equal(k1.children.map((c) => `${c.label}:${c.cur}`).join(), "a · p1 · r1:50,(other) · p1 · r1:40");
  assert.equal(find(k1, "(other) · p1 · r1").synthetic, true);
  assert.equal(t.weight, 170);
});

test("project grouping nests project > service > sku > resource", () => {
  const t = CT.buildTree(CT.buildModel(data()), "project", "net", "");
  const db = find(find(find(t, "p2"), "Cloud SQL"), "Storage");
  assert.equal(db.children[0].label, "x · r1");
});

test("same resource name in two projects stays two boxes", () => {
  const d = data();
  d.daily.push([0, 0, 0, 1, 0, 0, 0, "2026-09-01", 5, 0, 5, 1]);
  d.resources.push([0, 0, 1, 0, "//vm/a", 5, 0, 5, 0, 0, 0]);
  const k1 = find(find(CT.buildTree(CT.buildModel(d), "service", "net", ""), "Compute Engine"), "Core");
  assert.equal(k1.children.map((c) => c.label).join(), "a · p1 · r1,(other) · p1 · r1,a · p2 · r1");
  assert.notEqual(CT.markFromNode(CT.buildModel(d), find(k1, "a · p2 · r1")).error, "x");
});

test("label grouping stops at sku and marks (other) synthetic", () => {
  const t = CT.buildTree(CT.buildModel(data()), "label:env", "net", "");
  const other = find(t, "(other)");
  assert.equal(other.synthetic, true);
  assert.equal(find(find(other, "Cloud SQL"), "Storage").children.length, 0);
});

test("text filter and metric switch", () => {
  const m = CT.buildModel(data());
  assert.equal(CT.buildTree(m, "service", "net", "RAM").cur, 50);
  assert.equal(CT.buildTree(m, "service", "list", "").cur, 198);
  assert.equal(CT.buildTree(m, "service", "cost", "").cur, 180);
});

test("mixed-sign weights keep the positive child", () => {
  const d = data();
  d.daily[2][9] = -60; d.resources[3][6] = -60;
  const t = CT.buildTree(CT.buildModel(d), "service", "net", "");
  assert.equal(t.cur, 110);
  assert.equal(find(t, "Cloud SQL").weight, 0);
  assert.equal(t.weight, 140);
});

test("all-negative tree has zero weight", () => {
  const d = data({ resources: [] });
  d.meta.export = "standard";
  d.daily.forEach((r) => { r[9] = -1000; });
  const t = CT.buildTree(CT.buildModel(d), "service", "net", "");
  assert.equal(t.weight, 0);
  assert.equal(CT.squarify(t.children, 0, 0, 100, 100).length, 0);
});

test("standard model has no resource level", () => {
  const d = data({ resources: [] });
  d.meta.export = "standard";
  const t = CT.buildTree(CT.buildModel(d), "service", "net", "");
  assert.equal(find(find(t, "Compute Engine"), "Core").children.length, 0);
});

test("series and usage", () => {
  const m = CT.buildModel(data());
  assert.equal(CT.series(m, { service: 0 }, "net").join(), "0,40,90,50");
  assert.equal(JSON.stringify(CT.usageByUnit(m, { service: 0 })), '{"hour":3}');
});

test("squarify fills the box without overlap", () => {
  const items = [6, 6, 4, 3, 2, 2, 1, 0].map((w) => ({ weight: w }));
  const r = CT.squarify(items, 0, 0, 600, 400);
  assert.equal(r.length, 7);
  const area = r.reduce((s, x) => s + x.w * x.h, 0);
  assert.ok(Math.abs(area - 240000) < 1e-6);
  for (const x of r) assert.ok(x.x >= -1e-9 && x.y >= -1e-9 && x.x + x.w <= 600 + 1e-9 && x.y + x.h <= 400 + 1e-9);
  for (let i = 0; i < r.length; i++) for (let j = i + 1; j < r.length; j++) {
    const a = r[i], b = r[j];
    const overlap = Math.max(0, Math.min(a.x + a.w, b.x + b.w) - Math.max(a.x, b.x)) * Math.max(0, Math.min(a.y + a.h, b.y + b.h) - Math.max(a.y, b.y));
    assert.ok(overlap < 1e-6);
  }
});

test("neighbor selection on a 2x2 grid", () => {
  const g = [{ x: 0, y: 0, w: 10, h: 10 }, { x: 10, y: 0, w: 10, h: 10 }, { x: 0, y: 10, w: 10, h: 10 }, { x: 10, y: 10, w: 10, h: 10 }];
  assert.equal(CT.neighbor(g, 0, "right"), 1);
  assert.equal(CT.neighbor(g, 0, "down"), 2);
  assert.equal(CT.neighbor(g, 3, "up"), 1);
  assert.equal(CT.neighbor(g, 3, "left"), 2);
  assert.equal(CT.neighbor(g, 0, "left"), 0);
});

test("marks: union, coverage, conflicts, stale", () => {
  const m = CT.buildModel(data());
  const t = CT.buildTree(m, "service", "net", "");
  const s1 = find(t, "Compute Engine"), k1 = find(s1, "Core");
  const mS1 = CT.markFromNode(m, s1), mK1 = CT.markFromNode(m, k1);
  assert.equal(mS1.kind, "fact");
  assert.equal(JSON.stringify(mS1.filter), '{"service":"S1"}');
  assert.equal(CT.unionTotal(m, [mS1, mK1], "net").total, 140);
  const mA = CT.markFromNode(m, find(k1, "a · p1 · r1"));
  assert.equal(mA.kind, "resource");
  assert.equal(CT.unionTotal(m, [mS1, mA], "net").total, 140);
  assert.equal(CT.unionTotal(m, [mA], "net").total, 50);
  assert.ok(CT.markFromNode(m, find(k1, "(other) · p1 · r1")).error);
  const lt = CT.buildTree(m, "label:env", "net", "");
  assert.ok(CT.markFromNode(m, find(lt, "(other)")).error);
  const mProd = CT.markFromNode(m, find(lt, "prod"));
  assert.ok(CT.markConflict(m, [mProd], mA));
  assert.ok(CT.markConflict(m, [mA], mProd));
  assert.equal(CT.markConflict(m, [mS1], mA), null);
  const disjoint = { kind: "fact", id: "fd", filter: { "label:env": "prod", service: "S2" } };
  assert.equal(CT.markConflict(m, [disjoint], mA), null);
  const stale = { kind: "fact", id: "f-stale", filter: { service: "GONE" } };
  assert.deepEqual(Array.from(CT.unionTotal(m, [stale], "net").stale), ["f-stale"]);
  const empty = { kind: "fact", id: "f-empty", filter: { service: "S2", sku: "K1" } };
  assert.deepEqual(Array.from(CT.unionTotal(m, [empty], "net").stale), ["f-empty"]);
  const kept = CT.normalizeMarks(m, [
    { ...mS1, label: "s1" }, { ...mS1, id: "different", label: "dup" }, null,
    { kind: "fact", id: "x", label: "o", filter: { "label:env": "(other)" } },
    { ...mA, label: "a" }, { ...mProd, label: "prod conflicts with a" }]);
  assert.equal(kept.map((x) => x.label).join(), "s1,a");
  assert.equal(CT.markAmount(m, mS1, "net", false), 140);
  assert.equal(Array.from(CT.skusMatching(m, mS1)).sort().join(), "0|0,0|1");
  assert.equal(Array.from(CT.skusMatching(m, mA)).join(), "0|0");
  assert.equal(CT.markAmount(m, mS1, "net", true), 40);
});

test("marking is refused while a name filter is active", () => {
  const m = CT.buildModel(data());
  const t = CT.buildTree(m, "service", "net", "ram");
  const s1 = find(t, "Compute Engine");
  assert.ok(CT.markFromNode(m, s1, "ram").error);
  assert.equal(CT.markFromNode(m, s1, "").kind, "fact");
  assert.equal(CT.markFromNode(m, s1).kind, "fact");
});

test("validMark rejects malformed stored marks", () => {
  assert.equal(CT.validMark({ kind: "fact", id: "f", label: "l", filter: { service: "S1" } }), true);
  assert.equal(CT.validMark({ kind: "resource", id: "r", label: "l",
    resource: { service: "S", sku: "K", project: "p", region: "r", name: "n" } }), true);
  for (const bad of [null, {}, { kind: "fact", id: "f", label: "l" }, { kind: "fact", id: "f", label: "l", filter: {} },
    { kind: "fact", id: "f", label: "l", filter: { service: 1 } }, { kind: "resource", id: "r", label: "l", resource: { service: "S" } },
    { kind: "other", id: "x", label: "l" }, { kind: "fact", id: 5, label: "l", filter: { service: "S" } }]) {
    assert.equal(CT.validMark(bad), false, JSON.stringify(bad));
  }
});

test("helpers", () => {
  assert.equal(CT.storageKey(data().meta), "gcp-costtree:v1:abc123:detailed:env");
  assert.equal(CT.mdEscape("a|b`c\\d"), "a\\|b\\`c\\\\d");
  assert.equal(CT.formatMoney(1234.5, "USD"), "$1,235");
  assert.equal(CT.formatMoney(12.345, "USD"), "$12.35");
});

test("regroup benchmark at the 500,000-row cap", () => {
  const n = 500000, services = 50, skus = 20, projects = 10, regions = 4;
  const d = data({ resources: [] });
  d.meta.export = "standard";
  d.dims.service = Array.from({ length: services }, (_, i) => [`S${i}`, `Service ${i}`]);
  d.dims.sku = Array.from({ length: services * skus }, (_, i) => [`K${i}`, `Sku ${i}`]);
  d.dims.project = Array.from({ length: projects }, (_, i) => `p${i}`);
  d.dims.region = Array.from({ length: regions }, (_, i) => `r${i}`);
  d.dims["label:env"] = ["prod", "dev", "(none)"];
  d.daily = new Array(n);
  for (let i = 0; i < n; i++) {
    const s = i % services;
    d.daily[i] = [s, s * skus + ((i >> 5) % skus), 0, (i >> 3) % projects, (i >> 7) % regions, 0, (i >> 2) % 3,
      i % 2 ? "2026-09-01" : "2026-08-31", 1, 0, 1, 1];
  }
  const m = CT.buildModel(d);
  for (const grouping of ["service", "project", "region", "label:env"]) {
    const t0 = performance.now();
    CT.buildTree(m, grouping, "net", "");
    const ms = performance.now() - t0;
    assert.ok(ms < 500, `${grouping} took ${ms.toFixed(0)} ms`);
  }
});


test("app script parses", () => {
  const app = html.match(/<script id="app">([\s\S]*?)<\/script>/)[1];
  assert.ok(app.trim().length > 1000, "app script is empty");
  new vm.Script(app);
});

test("formatCompact", () => {
  assert.equal(CT.formatCompact(999.4, "USD"), "$999");
  assert.equal(CT.formatCompact(999.6, "USD"), "$1k");
  assert.equal(CT.formatCompact(1000, "USD"), "$1k");
  assert.equal(CT.formatCompact(35059.31, "USD"), "$35.1k");
  assert.equal(CT.formatCompact(-1500, "USD"), "-$1.5k");
  assert.equal(CT.formatCompact(1.25e6, "USD"), "$1.3M");
  assert.equal(CT.formatCompact(2500, "EUR"), "€2.5k");
});

test("worthALook", () => {
  const flag = (rule, sid, kid, net, message = rule + " msg") => ({ rule, service_id: sid, sku_id: kid, service: "S" + sid, sku: "K" + kid, net, message });
  const grower = (sid, kid, cur, prior) => ({ service_id: sid, sku_id: kid, metrics: { net: { cur, prior, delta: cur - prior } } });
  const flags = [flag("nat", 1, 1, 100), flag("growth", 1, 1, 100, "Grew"), flag("egress", 2, 2, 300), flag("snap", 3, 3, 300)];
  const a = CT.worthALook(flags, [grower(1, 1, 100, 40), grower(2, 2, 300, 200), grower(9, 9, 999, 1)], 6);
  assert.equal(a.count, 3);
  assert.equal(JSON.stringify(a.rows.map((r) => r.title)), JSON.stringify(["S2 · K2", "S3 · K3", "S1 · K1"]));  // ties by title
  const r1 = a.rows[2];
  assert.equal(r1.kind, "growth");
  assert.equal(r1.reason, "nat msg");
  assert.equal(JSON.stringify(r1.rules), JSON.stringify(["nat", "growth"]));
  assert.equal(r1.delta, 60);
  assert.equal(a.rows[0].delta, null);  // grower data only enriches growth rows
  const b = CT.worthALook([flags[1], flags[0]], [], 6);  // order does not matter
  assert.equal(b.rows[0].kind, "growth");
  assert.equal(b.rows[0].reason, "nat msg");
  assert.equal(CT.worthALook(flags, [], 1).rows.length, 1);
  const g = CT.worthALook([flag("growth", 5, 5, 50, "Grew 50%")], [], 6).rows[0];  // growth-only, grower trimmed
  assert.equal(g.kind, "growth");
  assert.equal(g.reason, "Grew 50%");
  assert.equal(g.delta, null);
  assert.equal(CT.worthALook([], [], 6).count, 0);
});

test("filter matches resource names and off-path names", () => {
  const m = CT.buildModel(data());
  const byRes = CT.buildTree(m, "service", "net", "//db/x");  // resource name only
  assert.equal(byRes.cur, 30);
  assert.equal(find(byRes, "Cloud SQL").children[0].children[0].tuple.name, "//db/x");
  const byProject = CT.buildTree(m, "service", "net", "p2");  // project is not on the service path
  assert.equal(byProject.cur, 30);
  assert.equal(CT.buildTree(m, "service", "net", "nothing-matches").cur, 0);
});

test("cutChars never splits a character", () => {
  const s = "a" + "💸".repeat(300);
  const cut = CT.cutChars(s, 200);
  assert.equal(Array.from(cut).length, 200);
  assert.ok(!/[\ud800-\udbff]$/.test(cut));
  assert.equal(CT.cutChars("short", 200), "short");
});

test("neighbor and nextVisible skip boxes too small to draw", () => {
  const g = [{ x: 0, y: 0, w: 10, h: 10 }, { x: 10, y: 0, w: 1, h: 10 }, { x: 11, y: 0, w: 10, h: 10 }];
  assert.equal(CT.neighbor(g, 0, "right", 5), 2);
  assert.equal(CT.nextVisible(g, 0, 1, 5), 2);
  assert.equal(CT.nextVisible(g, 0, -1, 5), 2);
  assert.equal(CT.nextVisible([{ x: 0, y: 0, w: 1, h: 1 }], 0, 1, 5), 0);
});

test("filtered trees attach only matching resources and stay consistent", () => {
  const d = data();
  d.daily.push([0, 0, 0, 1, 0, 0, 0, "2026-09-01", 5, 0, 5, 1]);
  d.resources.push([0, 0, 1, 0, "//vm/a", 5, 0, 5, 0, 0, 0]);
  const m = CT.buildModel(d);
  const byProject = CT.buildTree(m, "service", "net", "p2");
  const core = find(find(byProject, "Compute Engine"), "Core");
  assert.equal(core.children.map((c) => c.label).join(), "a · p2 · r1");
  assert.equal(core.weight, core.cur);
  assert.equal(byProject.weight, byProject.cur);
  const byLabel = CT.buildTree(m, "service", "net", "prod");  // resources carry no labels: SKU becomes a leaf
  const k1 = find(find(byLabel, "Compute Engine"), "Core");
  assert.equal(k1.children.length, 0);
  assert.equal(k1.weight, k1.cur);
  const byRes = CT.buildTree(m, "service", "net", "//db/x");
  assert.equal(byRes.weight, byRes.cur);
});

test("resource level survives float drift at scale", () => {
  const d = data({ resources: [] });
  d.daily = []; d.resources = [];
  let total = 0;
  for (let i = 0; i < 3000; i++) {
    const v = Math.round((1234.567891 + i * 0.000731) * 1e6) / 1e6;
    d.daily.push([0, 0, 0, 0, 0, 0, 0, "2026-09-01", v, 0, v, 1]);
    total += v;
  }
  // Same money, summed in a different order and split across two resources.
  const half = Math.round(total / 2 * 1e6) / 1e6;
  d.resources.push([0, 0, 0, 0, "//vm/a", half, 0, half, 0, 0, 0], [0, 0, 0, 0, "//vm/b", total - half, 0, total - half, 0, 0, 0]);
  const core = find(find(CT.buildTree(CT.buildModel(d), "service", "net", ""), "Compute Engine"), "Core");
  assert.equal(core.children.length, 2);
  // Very large totals: drift above 0.005 absolute but negligible relative to the SKU.
  const big = data({ resources: [] });
  big.daily = [[0, 0, 0, 0, 0, 0, 0, "2026-09-01", 2e10, 0, 2e10, 1]];
  big.resources = [[0, 0, 0, 0, "//vm/a", 2e10 + 0.02, 0, 2e10, 0, 0, 0]];
  const bigCore = find(find(CT.buildTree(CT.buildModel(big), "service", "net", ""), "Compute Engine"), "Core");
  assert.equal(bigCore.children.length, 1);
});

test("resource level must match both periods", () => {
  const d = data({ resources: [] });
  d.dims.project = ["p1", "p2", "p3"];
  d.dims["label:env"] = ["prod", "(other)", "p2-team"];
  d.daily = [
    [0, 0, 0, 1, 0, 0, 0, "2026-09-01", 100, 0, 100, 1],   // p2, current
    [0, 0, 0, 1, 0, 0, 0, "2026-08-31", 40, 0, 40, 1],     // p2, prior
    [0, 0, 0, 2, 0, 0, 2, "2026-08-31", 60, 0, 60, 1],     // p3, prior only, label "p2-team"
  ];
  d.resources = [[0, 0, 1, 0, "//vm/c", 100, 0, 100, 40, 0, 40], [0, 0, 2, 0, "//vm/d", 0, 0, 0, 60, 0, 60]];
  const core = find(find(CT.buildTree(CT.buildModel(d), "service", "net", "p2"), "Compute Engine"), "Core");
  assert.equal(core.cur, 100);
  assert.equal(core.prior, 100);
  assert.equal(core.children.length, 0);  // resources would show only $40 of the $100 prior
});
