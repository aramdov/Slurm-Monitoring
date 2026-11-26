"""Checks static/jobstats-core.js (Groups and Explorer logic) against jobstats_web.

Runs the JS module under Node. Skipped when Node is not installed (the production host has none);
set JOBSTATS_NODE to a node binary to run it elsewhere.
"""
import json
import math
import os
import random
import shutil
import subprocess
import unittest
from pathlib import Path

from test_jobstats_web import encode, fixture_line, row_from, sacct_line, FIXTURES  # noqa: F401

import jobstats_web as jw

NODE = os.environ.get("JOBSTATS_NODE") or shutil.which("node")
STATIC = Path(__file__).resolve().parent.parent / "static"
NOW_S = 1800000000


# Just enough DOM for jobstats-charts.js under Node: elements record attributes, children and listeners.
STUB_DOM = (
    "function mk(tag){const e={tag,attrs:{},children:[],style:{},listeners:{},textContent:'',"
    "classList:{set:new Set(),add(c){this.set.add(c)},toggle(c,on){on?this.set.add(c):this.set.delete(c)}},"
    "setAttribute(k,v){this.attrs[k]=String(v)},getAttribute(k){return this.attrs[k]},"
    "append(...k){this.children.push(...k)},replaceChildren(...k){this.children=k},"
    "addEventListener(t,f){(this.listeners[t]=this.listeners[t]||[]).push(f)}};"
    "Object.defineProperty(e,'className',{set(v){this.classList.set=new Set(v.split(' '))},"
    "get(){return [...this.classList.set].join(' ')}});return e}"
    "global.document={createElement:mk,createElementNS:(ns,t)=>mk(t)};global.self=global;"
)


def run_js(body, data):
    """Run `body` with `core` (jobstats-core.js), `tele` (gpu-telemetry.js) and `data` in scope."""
    script = (
        "const core = require(%s); const tele = require(%s);"
        "const data = JSON.parse(require('fs').readFileSync(0, 'utf8'));"
        "const out = (function () { %s })();"
        "process.stdout.write(JSON.stringify(out));"
    ) % (json.dumps(str(STATIC / "jobstats-core.js")), json.dumps(str(STATIC / "gpu-telemetry.js")), body)
    proc = subprocess.run([NODE, "-e", script], input=json.dumps(data), capture_output=True,
                          text=True, timeout=30)
    if proc.returncode:
        raise AssertionError(proc.stderr)
    return json.loads(proc.stdout)


def gpu_payload(rng, gpus):
    """Random but valid JS1 payload: some GPUs lack some fields, like real data."""
    node = {"cpus": rng.choice([4, 8, 16]), "total_time": rng.uniform(10, 5000),
            "used_memory": rng.uniform(1, 50), "total_memory": 100}
    fields = {
        "gpu_utilization": lambda: rng.uniform(0, 100),
        "sm_util_percent_avg": lambda: rng.uniform(0, 1),
        "any_tensor_util_percent_avg": lambda: rng.uniform(0, 0.5),
        "dram_bw_util_percent_avg": lambda: rng.uniform(0, 1),
        "sm_occupancy_percent_avg": lambda: rng.uniform(0, 1),
        "fp32_util_percent_avg": lambda: rng.uniform(0, 0.2),
        "integer_util_avg": lambda: rng.uniform(0, 0.4),
        "power_usage_milliwatts_avg": lambda: rng.uniform(1e5, 9e5),
        "gpu_used_memory": lambda: rng.uniform(1e9, 1.8e11),
        "fp16_util_percent_max": lambda: rng.choice([0, 0, 0.01]),
        "fp64_util_percent_max": lambda: rng.choice([0, 0.003]),
    }
    for name, gen in fields.items():
        vals = {str(g): gen() for g in range(gpus) if rng.random() > 0.15}
        if vals:
            node[name] = vals
    node["gpu_total_memory"] = {str(g): 192265846784 for g in range(gpus)}
    return {"gpus": gpus, "total_time": 100, "nodes": {"b200-8d": node}}


def random_rows(seed=7, n=120):
    rng = random.Random(seed)
    names = ["train-7b-s1", "train-7b-s2", "train-13b-s1", "eval", "genmol_v2", "genmol_v10",
             "train\u0661\u0662", "train\u0663"]   # Arabic-Indic digits: Python's \\d matches them
    rows = []
    for i in range(n):
        gpus = rng.choice([0, 1, 1, 2, 8])
        elapsed = rng.randint(1, 40000)
        kind = rng.random()
        if kind < 0.55:
            comment = encode(gpu_payload(rng, gpus)) if gpus else encode(FIXTURES["cpuonly"]["payload"])
        elif kind < 0.65:
            comment = "JS1:Short"
        elif kind < 0.7:
            comment = "JS1:None"
        else:
            comment = ""
        array = rng.random() < 0.4
        raw = str(5000 + i)
        jobid = "%d_%d" % (4000 + i % 5, i) if array else raw
        kw = dict(jobid=jobid, jobid_raw=raw, name=rng.choice(names), elapsed=str(elapsed),
                  tres="cpu=8" + (",gres/gpu=%d" % gpus if gpus else ""), comment=comment,
                  submit=str(1793000000 + i), start=str(1793000100 + i), end=str(1793000100 + i + elapsed))
        if rng.random() < 0.05:   # never started
            kw.update(state="CANCELLED by 1001", start="Unknown", elapsed="0", nodes="None assigned")
        rows.append(row_from(sacct_line(**kw), NOW_S)[0])
    for name in FIXTURES:
        rows.append(row_from(fixture_line(name), NOW_S)[0])
    return rows


def assert_close(test, a, b, path="$"):
    if isinstance(a, float) or isinstance(b, float):
        test.assertIsNotNone(a, path)
        test.assertIsNotNone(b, path)
        test.assertTrue(math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9), "%s: %r != %r" % (path, a, b))
    elif isinstance(a, dict) and isinstance(b, dict):
        test.assertEqual(sorted(a), sorted(b), path)
        for k in a:
            assert_close(test, a[k], b[k], "%s.%s" % (path, k))
    else:
        test.assertEqual(a, b, path)


@unittest.skipUnless(NODE, "node not installed")
class JsAggregateParityTests(unittest.TestCase):
    def test_group_aggregates_match_python(self):
        rows = random_rows()
        self.assertGreater(sum(r["summary_state"] == "ok" for r in rows), 40)
        for mode in ("name", "array", "family"):
            groups = {}
            for r in rows:
                if not r["not_started"]:
                    groups.setdefault(jw.group_key(r, mode), []).append(r)
            expected = {k: jw.aggregate(v) for k, v in groups.items()}
            got = run_js("return core.groupRows(data, %s).map(g => [g.key, g.agg]);" % json.dumps(mode), rows)
            got = dict((k, v) for k, v in got)
            self.assertEqual(sorted(got), sorted(expected), mode)
            for k in expected:
                assert_close(self, got[k], expected[k], "%s[%s]" % (mode, k))

    def test_whole_set_aggregate_matches_python(self):
        rows = random_rows(seed=11)
        assert_close(self, run_js("return core.aggregate(data);", rows), jw.aggregate(rows))

    def test_group_order_and_members(self):
        rows = random_rows()
        got = run_js("return core.groupRows(data, 'family').map(g => [g.key, g.agg.gpu_hours, g.names]);", rows)
        hours = [h for _, h, _ in got]
        self.assertEqual(hours, sorted(hours, reverse=True))
        fam = dict((k, n) for k, _, n in got)
        self.assertEqual(sorted(n for n, _ in fam["genmol_v#"]), ["genmol_v10", "genmol_v2"])

    def test_spread_needs_three_values(self):
        got = run_js("return [core.spread([1, 2]), core.spread([3, 1, 2]), core.percentile([0, 10], 0.25)];", None)
        self.assertEqual(got, [None, {"p10": 1.2, "median": 2, "p90": 2.8}, 2.5])


def explorer_rows():
    def r(**kw):
        return row_from(sacct_line(**kw), NOW_S)[0]
    return [
        r(jobid="3337", user="bob", name="Bind_Eval", nodes="b200-8c", tres="cpu=8"),
        r(jobid="2645_0", jobid_raw="2645", name="genmol-s1", nodes="b200-8"),
        r(jobid="2645_1", jobid_raw="2647", user="bobby", name="genmol-s1", nodes="b200-8b", state="FAILED"),
        r(jobid="26450", name="other", nodes="b200-8d", state="TIMEOUT",
          tres="cpu=64,gres/gpu=8", elapsed="36000", comment=encode(FIXTURES["gpu8"]["payload"])),
        r(jobid="4001", name="gone", state="CANCELLED by 1001", start="Unknown", elapsed="0",
          nodes="None assigned"),
        # ended after the hook went live, ran, no summary -> "not written"
        r(jobid="4002", name="late", submit="1793400000", start="1793400100", end="1793403700"),
        # ended before the hook went live
        r(jobid="4003", name="early", submit="1793200000", start="1793200100", end="1793203700"),
        r(jobid="4004", name="ok1", comment=encode(FIXTURES["gpu1"]["payload"])),
    ]


@unittest.skipUnless(NODE, "node not installed")
class JsExplorerTests(unittest.TestCase):
    def ids(self, query, filters=None, now=NOW_S):
        body = "return core.filterRows(data, %s, %s, %d).map(r => r.jobid);" % (
            json.dumps(query), json.dumps(filters or {}), now)
        return run_js(body, explorer_rows())

    def test_free_text_and_tokens(self):
        started = ["3337", "2645_0", "2645_1", "26450", "4002", "4003", "4004"]
        cases = {
            "": started,
            "GENMOL": ["2645_0", "2645_1"],
            "gen s1": ["2645_0", "2645_1"],
            "id:3337": ["3337"],
            "id:2645_*": ["2645_0", "2645_1"],
            "id:2645": ["2645_0"],          # exact JobID or JobIDRaw, not a prefix
            "id:2647": ["2645_1"],
            "node:b200-8": ["2645_0"],      # exact node, so b200-8b and b200-8c do not match
            "node:b200-8c": ["3337", "4002", "4003", "4004"],
            "node:b200-8?": [],             # only * is a wildcard
            "node:b200-8* gen": ["2645_0", "2645_1"],
            "user:bob": ["3337"],           # exact user, so bobby does not match
            "user:BOB*": ["3337", "2645_1"],
            "user:bob user:alice gen": ["2645_0"],  # users OR, then AND with name text
            "state:failed": ["2645_1"],
            "state:failed state:timeout": ["2645_1", "26450"],
            "state:cancelled": ["4001"],    # a state: token shows never-started rows
            "summary:unwritten": ["4002"],
            "summary:ok": ["26450", "4004"],
            "foo:bar": [],                  # unknown token kinds are plain name text
            "id:": started,                 # empty token is ignored
        }
        for q, want in cases.items():
            self.assertEqual(sorted(self.ids(q)), sorted(want), q)

    def test_filters(self):
        self.assertEqual(self.ids("", {"gpus": "0"}), ["3337"])
        self.assertEqual(self.ids("", {"gpus": "8+"}), ["26450"])
        self.assertEqual(self.ids("", {"minGpuHours": 10}), ["26450"])
        # GPU % filters drop rows without a value instead of treating them as 0
        self.assertEqual(self.ids("", {"below": 100}), ["26450", "4004"])
        self.assertEqual(sorted(self.ids("", {"above": 0})), ["26450", "4004"])

    def test_metric_filter(self):
        rows = [{"jobid": j, "jobid_raw": j, "name": "x", "state": "COMPLETED", "nodes": ["n"], "not_started": False,
                 "alloc_gpus": 1, "gpu_hours": 1, "summary_state": "ok", "gpu_pct": g, "sm": sm, "power_w": pw,
                 "cpu_eff": ce}
                for j, g, sm, pw, ce in [("1", 90, 10, 650, 5), ("2", 10, 60, 200, 50), ("3", 50, None, None, None)]]
        def ids(f):
            return run_js("return core.filterRows(data, '', %s, %d).map(r => r.jobid);" % (json.dumps(f), NOW_S), rows)
        self.assertEqual(ids({"below": 50}), ["2"])                         # default metric is GPU %
        self.assertEqual(ids({"metric": "sm", "below": 50}), ["1"])         # null SM never matches
        self.assertEqual(ids({"metric": "sm", "above": 0}), ["1", "2"])
        self.assertEqual(ids({"metric": "sm", "above": 10, "below": 60}), ["1"])
        self.assertEqual(ids({"metric": "power_w", "above": 400}), ["1"])   # not capped at 100
        self.assertEqual(ids({"metric": "cpu_eff", "below": 20}), ["1"])
        self.assertEqual(ids({"metric": "jobid", "below": 50}), ["2"])      # unknown metric falls back to GPU %
        self.assertEqual(ids({"metric": "sm"}), ["1", "2", "3"])           # no bounds, no filter

    def test_filter_metrics_are_row_fields(self):
        metrics = run_js("return core.FILTER_METRICS;", None)
        self.assertEqual(metrics[0]["key"], "gpu_pct")
        row = row_from(fixture_line("2920"), NOW_S)[0] if "2920" in FIXTURES else \
            row_from(fixture_line(next(iter(FIXTURES))), NOW_S)[0]
        for m in metrics:
            self.assertIn(m["key"], row, m["key"])
            self.assertTrue(m["label"])
            self.assertIn(m["max"], (100, None), m["key"])

    def test_unwritten_label_needs_grace_period(self):
        end = 1793403700
        body = ("const r = data.find(x => x.jobid === '4002');"
                "return [core.summaryLabel(r, %d).text, core.summaryLabel(r, %d).text];" % (end + 599, end + 601))
        self.assertEqual(run_js(body, explorer_rows()), ["—", "not written"])

    def test_sort_puts_missing_last_and_running_first(self):
        body = ("const rows = data.concat([Object.assign({}, data[0], {jobid: 'run', jobid_raw: '9999', end: null})]);"
                "return [core.sortRows(rows, 'end', 'desc').map(r => r.jobid)[0],"
                "        core.sortRows(rows, 'gpu_pct', 'asc').map(r => r.gpu_pct).slice(-1)[0],"
                "        core.sortRows(rows, 'gpu_pct', 'desc').map(r => r.gpu_pct).slice(-1)[0],"
                "        core.sortRows(rows, 'jobid', 'asc').map(r => r.jobid).slice(0, 3)];")
        first, last_asc, last_desc, by_id = run_js(body, explorer_rows())
        self.assertEqual(first, "run")
        self.assertIsNone(last_asc)
        self.assertIsNone(last_desc)
        self.assertEqual(by_id, ["2645_0", "2645_1", "3337"])

    def test_sort_large_array_task_ids(self):
        rows = [row_from(sacct_line(jobid=j, jobid_raw=raw), NOW_S)[0]
                for j, raw in (("101_0", "201"), ("100_1000000", "200"), ("100_2", "202"), ("100", "100"))]
        got = run_js("return core.sortRows(data, 'jobid', 'asc').map(r => r.jobid);", rows)
        self.assertEqual(got, ["100", "100_2", "100_1000000", "101_0"])

    def test_gpu_colour_matches_dashboard(self):
        body = ("const v = [null, NaN, -1, 0, 19.99, 20, 39.9, 40, 59.9, 60, 79.9, 80, 100, 120];"
                "return v.map(x => [core.gpuColor(x), tele.color(x)]);")
        for mine, dash in run_js(body, None):
            self.assertEqual(mine, dash)

    def test_formatters(self):
        body = ("return [core.fmtElapsed(90061), core.fmtElapsed(59), core.fmtGB(192265846784),"
                "        core.fmtPct(null), core.fmtGpuHours(1234.56)];")
        self.assertEqual(run_js(body, None), ["1-01:01:01", "00:00:59", "179.1 GB", "—", "1,234.6"])


def multi_node_payload(rng):
    """Two nodes with 8 GPUs each; node names sort differently as strings and as numbers."""
    a, b = gpu_payload(rng, 8), gpu_payload(rng, 8)
    for n in (a, b):
        n["nodes"]["b200-8d"]["temperature_celsius_avg"] = {str(g): rng.uniform(30, 80) for g in range(8)}
    return {"gpus": 16, "total_time": 100,
            "nodes": {"b200-8d": a["nodes"]["b200-8d"], "b200-8b": b["nodes"]["b200-8d"]}}


@unittest.skipUnless(NODE, "node not installed")
class JsJobPageTests(unittest.TestCase):
    def payloads(self):
        rng = random.Random(3)
        out = [gpu_payload(rng, g) for g in (1, 2, 4, 8, 8, 8)] + [multi_node_payload(rng)]
        out += [FIXTURES[n]["payload"] for n in FIXTURES if isinstance(FIXTURES[n]["payload"], dict)]
        return out

    def test_gpu_means_match_job_row(self):
        for p in self.payloads():
            nodes = jw.node_details(p)
            want = jw.derive_metrics(p)
            got = run_js("return core.gpuTable(data).summary;", nodes)
            for key in jw.MEAN_FIELDS:
                if want[key] is None:
                    self.assertIsNone(got[key], key)
                else:
                    assert_close(self, got[key], want[key], key)
            self.assertEqual(got["low_duty"], want["low_duty_gpus"])
            self.assertEqual(got["mem_peak_b"], want["gpu_mem_peak_b"])
            if want["gpu_mem_peak_pct"] is None:
                self.assertIsNone(got["mem_peak_pct"])
            else:
                assert_close(self, got["mem_peak_pct"], want["gpu_mem_peak_pct"], "mem_peak_pct")

    def test_gpu_rows_scale_and_order(self):
        nodes = {"b200-8d": {"cpus": 8, "cpu_eff": 50.0, "mem_peak_b": 1, "mem_total_b": 4, "gpus": {
            "10": {"gpu_utilization": 12.5},
            "2": {"gpu_utilization": 90, "sm_util_percent_avg": 0.4137, "power_usage_milliwatts_avg": 564672.8,
                  "gpu_used_memory": 29640556544, "gpu_total_memory": 192265846784,
                  "temperature_celsius_avg": 49.1, "fp16_util_percent_max": 0, "fp64_util_percent_max": 0.0003,
                  "nvlink_total_rx_per_sec_avg": 2 * 1024 ** 3, "pcie_tx_per_sec_avg": 1024 ** 3}}}}
        rows = run_js("return core.gpuTable(data).gpus;", nodes)
        self.assertEqual([(r["node"], r["minor"]) for r in rows], [("b200-8d", "2"), ("b200-8d", "10")])
        g = rows[0]
        assert_close(self, g["sm"], 41.37)
        assert_close(self, g["power_w"], 564.6728)
        assert_close(self, g["mem_pct"], 29640556544 / 192265846784 * 100)
        self.assertEqual(g["fp16_seen"], False)
        self.assertEqual(g["fp64_seen"], True)
        assert_close(self, g["nvlink_rx_gbs"], 2.0)
        assert_close(self, g["pcie_tx_gbs"], 1.0)
        self.assertIsNone(g["tc"])            # missing stays null, never 0
        self.assertIsNone(rows[1]["sm"])
        self.assertIsNone(rows[1]["mem_pct"])

    def test_node_rows(self):
        nodes = {"b200-8d": {"cpus": 8, "cpu_eff": 37.3, "mem_peak_b": 536080384, "mem_total_b": 17179869184,
                             "gpus": {}},
                 "b200-8b": {"cpus": None, "cpu_eff": None, "mem_peak_b": 5, "mem_total_b": 0, "gpus": {}}}
        rows = run_js("return core.nodeRows(data);", nodes)
        self.assertEqual([r["node"] for r in rows], ["b200-8d", "b200-8b"])
        assert_close(self, rows[0]["mem_pct"], 536080384 / 17179869184 * 100)
        self.assertIsNone(rows[1]["mem_pct"])   # total 0 -> no percentage
        self.assertEqual(rows[0]["gpus"], 0)

    def test_overflow_becomes_null(self):
        nodes = {"n": {"cpus": 1, "cpu_eff": 1.0, "mem_peak_b": 1e308, "mem_total_b": 1e-308, "gpus": {
            "0": {"sm_util_percent_avg": 1e308, "gpu_used_memory": 1e308, "gpu_total_memory": 1e-308,
                  "nvlink_total_rx_per_sec_avg": 1e308, "power_usage_milliwatts_avg": 1e308},
            "1": {"sm_util_percent_avg": 1e308, "power_usage_milliwatts_avg": 1e308}}}}
        # checked inside JS: JSON.stringify(Infinity) is null and would hide the bug
        body = ("const t = core.gpuTable(data), n = core.nodeRows(data);"
                "const vals = [];"
                "for (const g of t.gpus) vals.push(g.sm, g.mem_pct, g.nvlink_rx_gbs, g.power_w);"
                "vals.push(t.summary.sm, t.summary.mem_peak_pct, t.summary.power_w, n[0].mem_pct);"
                "return vals.map(v => v === null ? 'null' : Number.isFinite(v) ? 'finite' : String(v));")
        got = run_js(body, nodes)
        self.assertNotIn("Infinity", got)
        self.assertNotIn("NaN", got)

    def test_malformed_containers(self):
        nodes = {"a": None, "b": {"gpus": "bad"}, "c": {"gpus": {"0": None, "1": {"gpu_utilization": 50}}},
                 "d": {"gpus": ["x"]}}
        got = run_js("const t = core.gpuTable(data); const n = core.nodeRows(data);"
                     "return [t.gpus.map(g => g.node + ':' + g.minor), n.map(r => r.node + ':' + r.gpus)];", nodes)
        self.assertEqual(got[0], ["c:0", "c:1"])
        self.assertEqual(got[1], ["a:0", "b:0", "c:2", "d:0"])

    def test_lifecycle_text(self):
        def r(**kw):
            return row_from(sacct_line(**kw), NOW_S)[0]
        rows = [r(jobid="1", state="PENDING", start=str(NOW_S + 3600), end="Unknown", elapsed="0", nodes="None assigned"),
                r(jobid="2", state="CANCELLED by 1001", start="Unknown", elapsed="0", nodes="None assigned"),
                r(jobid="3", state="RUNNING", end="Unknown"),
                r(jobid="4")]
        got = run_js("return data.map(core.lifecycle);", rows)
        self.assertEqual(got[0], {"start": None, "end": None, "label": "pending"})
        self.assertEqual(got[1], {"start": None, "end": None, "label": "never started"})
        self.assertEqual(got[2]["end"], None)
        self.assertEqual(got[2]["label"], "running")
        self.assertIsNotNone(got[2]["start"])
        self.assertEqual(got[3]["label"], None)
        self.assertIsNotNone(got[3]["end"])


def note_row(**kw):
    """Job row with only the fields jobNotes reads; defaults trigger no note."""
    row = {"summary_gpus": 8, "low_duty_gpus": 0, "coverage": {"gpu_pct": 1.0, "gpu_mem": 1.0},
           "gpu_mem_peak_b": 100 * 1024 ** 3, "gpu_mem_peak_pct": 55.9, "cpu_eff": 40.0, "alloc_cpus": 64}
    row.update(kw)
    return row


@unittest.skipUnless(NODE, "node not installed")
class JsJobNotesTests(unittest.TestCase):
    def notes(self, rows):
        return run_js("return data.map(core.jobNotes);", rows)

    def test_limits_exported(self):
        got = run_js("return core.NOTE_LIMITS;", None)
        self.assertEqual(got, {"low_gpu_pct": 20, "gpu_mem_peak_pct": 25, "cpu_eff": 20})
        self.assertEqual(got["low_gpu_pct"], jw.LOW_DUTY_PCT)

    def test_no_notes_for_healthy_job(self):
        self.assertEqual(self.notes([note_row()]), [[]])

    def test_low_use_gpus(self):
        got = self.notes([note_row(low_duty_gpus=3), note_row(summary_gpus=1, low_duty_gpus=1),
                          note_row(low_duty_gpus=None)])
        self.assertEqual(got[0], ["3 of 8 GPUs averaged under 20 % busy (GPU %)."])
        self.assertEqual(got[1], ["1 of 1 GPU averaged under 20 % busy (GPU %)."])
        self.assertEqual(got[2], [])

    def test_partial_coverage(self):
        got = self.notes([note_row(coverage={"gpu_pct": 0.5, "power_w": 0.75, "sm": 1.0, "tc": None})])
        self.assertEqual(got[0], ["Some GPUs have no value for: GPU %, power."])

    def test_gpu_memory_threshold(self):
        got = self.notes([note_row(gpu_mem_peak_pct=25.0),
                          note_row(gpu_mem_peak_pct=24.9, gpu_mem_peak_b=int(44.8 * 1024 ** 3)),
                          note_row(gpu_mem_peak_pct=0.4, gpu_mem_peak_b=762970112),
                          note_row(gpu_mem_peak_pct=None, gpu_mem_peak_b=None)])
        self.assertEqual(got[0], [])
        self.assertEqual(got[1], ["GPU memory peaked at 44.8 GB, 24.9 % of the card (shown when under 25 %)."])
        self.assertEqual(got[2], ["GPU memory peaked at 0.7 GB, 0.4 % of the card (shown when under 25 %)."])
        self.assertEqual(got[3], [])

    def test_cpu_efficiency_threshold(self):
        got = self.notes([note_row(cpu_eff=20.0), note_row(cpu_eff=6.3125, alloc_cpus=128),
                          note_row(cpu_eff=0.018, alloc_cpus=1), note_row(cpu_eff=None)])
        self.assertEqual(got[0], [])
        self.assertEqual(got[1], ["CPU efficiency 6.3 % over 128 allocated cores (shown when under 20 %)."])
        self.assertEqual(got[2], ["CPU efficiency under 0.1 % over 1 allocated core (shown when under 20 %)."])
        self.assertEqual(got[3], [])

    def test_order_and_fixture_rows(self):
        got = self.notes([note_row(low_duty_gpus=8, coverage={"gpu_pct": 0.5}, gpu_mem_peak_pct=3.0,
                                   gpu_mem_peak_b=5703729152, cpu_eff=6.3, alloc_cpus=128)])[0]
        self.assertEqual([n.split(" ")[0] for n in got], ["8", "Some", "GPU", "CPU"])
        # real fixture rows: the function accepts whatever build_row produces
        rows = [row_from(fixture_line(n), NOW_S)[0] for n in FIXTURES]
        for r, notes in zip(rows, self.notes(rows)):
            self.assertIsInstance(notes, list, r["jobid"])
            for n in notes:
                self.assertNotIn("NaN", n)
                self.assertNotIn("undefined", n)


if __name__ == "__main__":
    unittest.main()

@unittest.skipUnless(NODE, "node not installed")
class JsTimeseriesTests(unittest.TestCase):
    def js(self, expr, data=None):
        return run_js("return " + expr + ";", data)

    def test_steps(self):
        self.assertEqual(self.js("core.TS_STEPS"),
                         [["30", "30 s"], ["60", "1 min"], ["120", "2 min"], ["300", "5 min"], ["auto", "auto"]])

    def test_ticks(self):
        self.assertEqual(self.js("core.tsTicks(100, 4)"), [0, 25, 50, 75, 100])
        self.assertEqual(self.js("core.tsTicks(73.6, 4)"), [0, 20, 40, 60, 80])
        self.assertEqual(self.js("core.tsTicks(1000, 4)"), [0, 250, 500, 750, 1000])
        self.assertEqual(self.js("core.tsTicks(85.9, 4)"), [0, 25, 50, 75, 100])
        self.assertEqual(self.js("core.tsTicks(0, 4)"), [0, 1])

    def test_time_axis(self):
        self.assertEqual(self.js("core.tsTimeAxis(12.2*3600)"), {"unit": "h", "div": 3600, "ticks": [0, 5, 10, 15]})
        self.assertEqual(self.js("core.tsTimeAxis(1800)")["unit"], "min")
        self.assertEqual(self.js("core.tsTimeAxis(1800)")["div"], 60)

    def test_nearest(self):
        pts = [[0, 1], [30, 2], [60, 3]]
        self.assertEqual([self.js(f"core.tsNearest(data, {t})", pts) for t in (-5, 14, 16, 999)], [0, 0, 1, 2])
        self.assertEqual(self.js("core.tsNearest([], 5)"), -1)

    def test_gpu_labels(self):
        one = [{"key": "gpu08/0", "host": "gpu08", "minor": "0"}, {"key": "gpu08/1", "host": "gpu08", "minor": "1"}]
        self.assertEqual(self.js("core.tsGpuLabels(data)", one), {"gpu08/0": "GPU 0", "gpu08/1": "GPU 1"})
        two = one[:1] + [{"key": "gpu07/0", "host": "gpu07", "minor": "0"}]
        self.assertEqual(self.js("core.tsGpuLabels(data)", two), {"gpu08/0": "gpu08/0", "gpu07/0": "gpu07/0"})

    def test_step_text(self):
        t = lambda b: self.js("core.tsStepText(data)", b)  # noqa: E731
        self.assertEqual(t({"step_s": 120, "agg": "max", "coarsened": False}), "2 min steps, peak per window")
        self.assertEqual(t({"step_s": 30, "agg": "avg", "coarsened": False}), "30 s steps (raw samples)")
        self.assertEqual(t({"step_s": 300, "agg": "avg", "coarsened": True, "requested_step": 30}),
                         "5 min steps (30 s would exceed 5000 points), average per window")
        self.assertEqual(t({"step_s": 600, "agg": "avg", "coarsened": False, "requested_step": None}),
                         "10 min steps (auto), average per window")
        self.assertEqual(t({"step_s": 120, "agg": "avg", "coarsened": True, "requested_step": 30, "max_points": 2500}),
                         "2 min steps (30 s would exceed 2500 points), average per window")

    def render(self, body, expr):
        return run_js(STUB_DOM + "self.JobstatsCore = core; require(%s);"
                      "const c = mk('div'); const h = self.JobstatsCharts.render(c, data, core.tsGpuLabels(data.gpus), {});"
                      "const cards = c.children[0].children; %s" % (json.dumps(str(STATIC / "jobstats-charts.js")), expr), body)

    def test_two_hosts_same_minor_look_different(self):
        gpus = [{"key": h + "/0", "host": h, "minor": "0"} for h in ("gpu07", "gpu08")]
        series = {g["key"]: [[0, 10], [60, 20]] for g in gpus}
        body = {"step_s": 60, "agg": "avg", "start": 0, "end": 60, "gpus": gpus,
                "charts": {"duty": {"title": "GPU duty", "unit": "%", "kind": "gpu", "ymax": 100, "allocated": None,
                                    "series": series}}}
        got = self.render(body, "const p = cards[0].children[1].children.find(x => x.attrs.class === 'js-ts-lines').children;"
                                "return {paths: p.map(x => [x.attrs.stroke, x.attrs['stroke-dasharray'] || ''] ), styles: h.styles};")
        (c1, d1), (c2, d2) = got["paths"]
        self.assertNotEqual((c1, d1), (c2, d2))
        self.assertEqual(got["styles"]["gpu08/0"]["dash"], d2)

    def test_gpu_memory_axis_ends_at_card_total(self):
        body = {"step_s": 60, "agg": "avg", "start": 0, "end": 60, "gpus": [{"key": "a/0", "host": "a", "minor": "0"}],
                "charts": {"gpu_mem": {"title": "GPU memory", "unit": "GB", "kind": "gpu", "ymax": 79.6, "allocated": None,
                                       "series": {"a/0": [[0, 41.0], [60, 41.5]]}}}}
        ticks = self.render(body, "const g = cards[0].children[1].children.find(x => x.attrs.class === 'js-ts-gl');"
                                  "return g.children.filter(x => x.tag === 'text' && x.attrs['text-anchor'] === 'end').map(x => x.textContent);")
        self.assertEqual(ticks[-1], "79.6")

    def test_chart_order_matches_server(self):
        import jobstats_prom
        self.assertEqual(self.js("core.TS_ORDER"), [c["name"] for c in jobstats_prom.CHARTS])

    def test_new_scripts_parse(self):
        for name in ("jobstats-charts.js", "jobstats.js", "jobstats-core.js", "jobstats-scatter.js"):
            proc = subprocess.run([NODE, "--check", str(STATIC / name)], capture_output=True, text=True, timeout=30)
            self.assertEqual(proc.returncode, 0, name + ": " + proc.stderr)

    def test_page_loads_charts_script_with_new_version(self):
        html = (STATIC / "jobstats.html").read_text()
        self.assertIn('src="jobstats-charts.js?v=m7-ax"', html)
        self.assertNotIn("m4-3-user", html)
        self.assertLess(html.index("jobstats-core.js"), html.index("jobstats-charts.js"))
        self.assertLess(html.index("jobstats-charts.js"), html.index('src="jobstats.js'))



def sc_row(jobid, **kw):
    """Synthetic Explorer row with the fields the scatter helpers read."""
    r = {"jobid": jobid, "name": "train", "array_job": None, "not_started": False, "alloc_gpus": 1,
         "end": 1793003700, "summary_state": "ok", "gpu_pct": 50.0, "sm": 30.0, "gpu_hours": 1.0,
         "low_duty_gpus": 0, "coverage": {"gpu_pct": 1.0, "sm": 1.0}, "summary_gpus": 1}
    r.update(kw)
    return r


@unittest.skipUnless(NODE, "node not installed")
class JsScatterTests(unittest.TestCase):
    def test_split_reasons_and_order(self):
        rows = [
            sc_row("ns", not_started=True, alloc_gpus=0, end=None, summary_state="missing", gpu_hours=0),
            sc_row("cpu", alloc_gpus=0, end=None, summary_state="missing", gpu_hours=0),
            sc_row("run", end=None, summary_state="missing", gpu_hours=4.0),
            sc_row("p1", gpu_hours=2.0),
            sc_row("nosum", summary_state="short", gpu_hours=0.5),
            sc_row("nosm", sm=None, gpu_hours=3.0),
            sc_row("p2", gpu_hours=6.0),
        ]
        out = run_js("return core.scatterSplit(data);", rows)
        ex = out["excluded"]
        self.assertEqual(ex["not_started"]["jobs"], 1)
        self.assertEqual(ex["cpu_only"]["jobs"], 1)
        self.assertEqual(ex["running"], {"jobs": 1, "gpu_hours": 4.0})
        self.assertEqual(ex["no_summary"], {"jobs": 1, "gpu_hours": 0.5})
        self.assertEqual(ex["no_value"], {"jobs": 1, "gpu_hours": 3.0})
        self.assertEqual([r["jobid"] for r in out["points"]], ["p1", "p2"])
        self.assertEqual(out["plotted"], {"jobs": 2, "gpu_hours": 8.0})
        self.assertEqual(out["total_gpu_hours"], 15.5)

    def test_split_missing_gpu_pct_is_no_value(self):
        out = run_js("return core.scatterSplit(data);", [sc_row("a", gpu_pct=None)])
        self.assertEqual(out["excluded"]["no_value"]["jobs"], 1)
        self.assertEqual(out["points"], [])

    def test_scatter_empty(self):
        out = run_js("return core.scatterSplit([]);", None)
        self.assertEqual(out["plotted"], {"jobs": 0, "gpu_hours": 0})
        self.assertEqual(out["points"], [])
        self.assertEqual(out["total_gpu_hours"], 0)

    def colors(self, rows, mode="name"):
        body = ("const c = core.scatterColors(data, %s);"
                "return {legend: c.legend, other: c.other_groups, of: data.map(c.colorOf),"
                " palette: core.SCATTER_COLORS, gray: core.SCATTER_OTHER};") % json.dumps(mode)
        return run_js(body, rows)

    def test_colors_top8_and_ties(self):
        # groups g0..g9 with gpu_hours 10..1, except g6 and g7 both 3 -> key order decides
        hours = [10, 9, 8, 7, 6, 5, 3, 3, 2, 1]
        rows = [sc_row(str(i), name="g%d" % i, gpu_hours=h) for i, h in enumerate(hours)]
        rows[7]["name"] = "a-tie"      # "a-tie" < "g6": comes first among the tied pair
        out = self.colors(rows)
        self.assertEqual([e["key"] for e in out["legend"]], ["g0", "g1", "g2", "g3", "g4", "g5", "a-tie", "g6"])
        self.assertEqual([e["color"] for e in out["legend"]], out["palette"])
        self.assertEqual(out["other"], 2)
        self.assertEqual(out["of"][8], out["gray"])
        self.assertEqual(out["of"][9], out["gray"])
        self.assertEqual(out["of"][0], out["palette"][0])
        self.assertEqual(out["legend"][0]["gpu_hours"], 10)

    def test_palette_values(self):
        out = self.colors([])
        self.assertEqual(out["palette"], ["#60a5fa", "#f59e0b", "#34d399", "#f87171",
                                          "#a78bfa", "#22d3ee", "#d4a373", "#94a3b8"])
        self.assertEqual(out["gray"], "#6b7280")
        self.assertEqual(out["legend"], [])
        self.assertEqual(out["other"], 0)

    def test_colors_follow_mode(self):
        rows = [sc_row("1", name="run-1"), sc_row("2", name="run-2")]
        self.assertEqual(len(self.colors(rows, "name")["legend"]), 2)
        fam = self.colors(rows, "family")["legend"]
        self.assertEqual([e["key"] for e in fam], ["run-#"])
        self.assertEqual(fam[0]["gpu_hours"], 2)

    def test_colors_empty_and_markup_names(self):
        out = self.colors([sc_row("1", name=""), sc_row("2", name="<b>&x", gpu_hours=2)])
        self.assertEqual([e["key"] for e in out["legend"]], ["<b>&x", ""])
        self.assertEqual(out["of"], [out["palette"][1], out["palette"][0]])

    def test_radius(self):
        out = run_js("const r = core.scatterRadius;"
                     "return [r(100, 100, 'gpuh'), r(25, 100, 'gpuh'), r(0.0001, 100, 'gpuh'),"
                     " r(5, 0, 'gpuh'), r(5, 5, 'equal'), r(0, 0, 'gpuh'), r(7, 7, 'gpuh')];", None)
        self.assertEqual(out[0], 18)
        self.assertAlmostEqual(out[1], 9)
        self.assertEqual(out[2:], [2, 2, 3, 2, 18])

    def test_bucket(self):
        out = run_js("return [1, 2, 7, 8, 16].map(core.scatterBucket);", None)
        self.assertEqual(out, ["1", "2-7", "2-7", "8+", "8+"])

    def test_marks(self):
        rows = [
            sc_row("ring", low_duty_gpus=1, alloc_gpus=8, summary_gpus=8),
            sc_row("cov", coverage={"gpu_pct": 1, "sm": 0.5}),
            sc_row("cov2", coverage={"gpu_pct": 0.75, "sm": 1}),
            sc_row("fewer", alloc_gpus=8, summary_gpus=6),
            sc_row("ok"),
            sc_row("nullcov", coverage=None),
            sc_row("nullcov_fewer", coverage=None, alloc_gpus=4, summary_gpus=2),
        ]
        out = run_js("return data.map(core.scatterMarks);", rows)
        self.assertEqual(out, [
            {"ring": True, "hollow": False}, {"ring": False, "hollow": True}, {"ring": False, "hollow": True},
            {"ring": False, "hollow": True}, {"ring": False, "hollow": False}, {"ring": False, "hollow": False},
            {"ring": False, "hollow": True},
        ])

    def hit(self, dots, points):
        body = ("const idx = core.scatterIndex(data.dots);"
                "return data.points.map(([x, y]) => { const d = core.scatterHit(idx, x, y);"
                " return d ? d.row.jobid : null; });")
        return run_js(body, {"dots": dots, "points": points})

    def test_hit(self):
        dots = [{"x": 100, "y": 100, "r": 18, "row": {"jobid": "big"}},
                {"x": 130, "y": 100, "r": 2, "row": {"jobid": "small"}}]
        self.assertEqual(self.hit(dots, [[115, 100], [124, 100], [100, 140]]), ["big", "small", None])
        # both hit at (113, 100): big 13 <= 18, mid 7 <= 10 -> nearest centre wins
        mid = [dots[0], {"x": 120, "y": 100, "r": 10, "row": {"jobid": "mid"}}]
        self.assertEqual(self.hit(mid, [[113, 100], [109, 100]]), ["mid", "big"])
        twin = dots + [{"x": 100, "y": 100, "r": 5, "row": {"jobid": "twin"}}]
        self.assertEqual(self.hit(twin, [[100, 100]]), ["big"])

    def test_hit_across_cells_and_empty(self):
        # a dot of radius 40 is hittable 39 px away, past the 24 px grid cell
        dots = [{"x": 10, "y": 10, "r": 40, "row": {"jobid": "huge"}}]
        self.assertEqual(self.hit(dots, [[49, 10], [10, 51]]), ["huge", None])
        self.assertEqual(self.hit([], [[0, 0]]), [None])


@unittest.skipUnless(NODE, "node not installed")
class JsScatterAxesTests(unittest.TestCase):
    """X and Y axis pickers (spec 2026-11-26-explorer-scatter-axes-design.md): core helpers."""

    def test_axes_table(self):
        out = run_js("return {axes: core.SCATTER_AXES, mean: core.MEAN_KEYS, min: core.SCATTER_OCC_SM_MIN_SM};", None)
        axes = out["axes"]
        self.assertEqual([a["key"] for a in axes], ["gpu_pct", "sm", "occ", "occ_sm", "tc", "dram", "fp32"])
        for a in axes:
            if a["field"] is not None:
                self.assertIn(a["field"], out["mean"])
                self.assertEqual(a["needs"], [a["field"]])
        occ_sm = next(a for a in axes if a["key"] == "occ_sm")
        self.assertIsNone(occ_sm["field"])
        self.assertEqual(occ_sm["needs"], ["occ", "sm"])
        self.assertEqual([a["label"] for a in axes],
                         ["GPU duty % (NVML)", "SM activity %", "Occupancy %", "Occupancy / SM %",
                          "Tensor pipe %", "DRAM bandwidth %", "FP32 pipe %"])
        self.assertEqual([a["axis"] for a in axes],
                         ["GPU duty %", "SM %", "OCC %", "OCC/SM %", "TC %", "DRAM %", "FP32 %"])
        self.assertEqual(out["min"], 5)

    def test_scatter_value(self):
        cases = [
            ({"tc": 12.5}, "tc"), ({"tc": None}, "tc"), ({"dram": 40}, "dram"), ({"gpu_pct": 0}, "gpu_pct"),
            ({"occ": 15, "sm": 48}, "occ_sm"), ({"occ": 60, "sm": 50}, "occ_sm"), ({"occ": 1, "sm": 4.9}, "occ_sm"),
            ({"occ": 1, "sm": None}, "occ_sm"), ({"occ": None, "sm": 50}, "occ_sm"), ({"occ": 0, "sm": 50}, "occ_sm"),
            ({"tc": 5}, "bogus"),
        ]
        out = run_js("return data.map(([r, k]) => core.scatterValue(r, k));", cases)
        self.assertEqual(out, [12.5, None, 40, 0, 31.25, 100, None, None, None, 0, None])
        # NaN / Infinity cannot travel through JSON; check them inside Node
        nf = run_js("return [core.scatterValue({sm: NaN}, 'sm'), core.scatterValue({sm: Infinity}, 'sm'),"
                    " core.scatterValue({occ: NaN, sm: 50}, 'occ_sm')];", None)
        self.assertEqual(nf, [None, None, None])

    def split_rows(self):
        return [
            sc_row("ns", not_started=True, alloc_gpus=0, end=None, summary_state="missing", gpu_hours=0),
            sc_row("cpu", alloc_gpus=0, end=None, summary_state="missing", gpu_hours=0),
            sc_row("run", end=None, summary_state="missing", gpu_hours=4.0),
            sc_row("p1", gpu_hours=2.0),
            sc_row("nosum", summary_state="short", gpu_hours=0.5),
            sc_row("nosm", sm=None, gpu_hours=3.0),
            sc_row("p2", gpu_hours=6.0),
        ]

    def test_split_default_axes_unchanged(self):
        out = run_js("return [core.scatterSplit(data), core.scatterSplit(data, 'gpu_pct', 'sm')];", self.split_rows())
        for sp in out:
            ex = sp["excluded"]
            self.assertEqual(set(ex), {"not_started", "cpu_only", "running", "no_summary", "low_sm", "no_value"})
            self.assertEqual(ex["not_started"]["jobs"], 1)
            self.assertEqual(ex["cpu_only"]["jobs"], 1)
            self.assertEqual(ex["running"], {"jobs": 1, "gpu_hours": 4.0})
            self.assertEqual(ex["no_summary"], {"jobs": 1, "gpu_hours": 0.5})
            self.assertEqual(ex["no_value"], {"jobs": 1, "gpu_hours": 3.0})
            self.assertEqual(ex["low_sm"], {"jobs": 0, "gpu_hours": 0})
            self.assertEqual([r["jobid"] for r in sp["points"]], ["p1", "p2"])
            self.assertEqual(sp["plotted"], {"jobs": 2, "gpu_hours": 8.0})
            self.assertEqual(sp["total_gpu_hours"], 15.5)

    def test_split_follows_axes(self):
        rows = [sc_row("a", sm=None, tc=20, dram=30), sc_row("b", sm=40, tc=None, dram=30)]
        out = run_js("return [core.scatterSplit(data, 'sm', 'tc'), core.scatterSplit(data, 'gpu_pct', 'dram')];", rows)
        self.assertEqual(out[0]["excluded"]["no_value"]["jobs"], 2)
        self.assertEqual(out[0]["points"], [])
        self.assertEqual(out[1]["excluded"]["no_value"]["jobs"], 0)
        self.assertEqual([r["jobid"] for r in out[1]["points"]], ["a", "b"])

    def test_split_low_sm_before_no_value(self):
        rows = [sc_row("a", sm=2, occ=None, gpu_hours=1.5), sc_row("b", sm=2, occ=1, gpu_hours=2.5),
                sc_row("c", sm=5, occ=None), sc_row("d", sm=5, occ=2)]
        body = ("const a = core.scatterSplit(data, 'sm', 'occ_sm'), b = core.scatterSplit(data, 'sm', 'occ');"
                "return [a, b, a.points.map(r => core.scatterValue(r, 'occ_sm'))];")
        a, b, vals = run_js(body, rows)
        self.assertEqual(a["excluded"]["low_sm"], {"jobs": 2, "gpu_hours": 4.0})
        self.assertEqual(a["excluded"]["no_value"], {"jobs": 1, "gpu_hours": 1.0})
        self.assertEqual([r["jobid"] for r in a["points"]], ["d"])
        self.assertEqual(vals, [40])
        self.assertEqual(b["excluded"]["low_sm"], {"jobs": 0, "gpu_hours": 0})
        self.assertEqual([r["jobid"] for r in b["points"]], ["b", "d"])
        self.assertEqual(b["excluded"]["no_value"]["jobs"], 2)

    def test_marks_follow_axes(self):
        rows = [sc_row("tc", tc=10, coverage={"gpu_pct": 1, "sm": 1, "tc": 0.5}),
                sc_row("occ", occ=10, coverage={"gpu_pct": 1, "sm": 1, "occ": 0.5}),
                sc_row("fewer", tc=10, occ=10, alloc_gpus=8, summary_gpus=6)]
        body = ("const pairs = [['sm', 'tc'], ['gpu_pct', 'sm'], ['sm', 'occ_sm'], ['dram', 'fp32']];"
                "return data.map(r => pairs.map(([x, y]) => core.scatterMarks(r, x, y).hollow));")
        out = run_js(body, rows)
        self.assertEqual(out[0], [True, False, False, False])
        self.assertEqual(out[1], [False, False, True, False])
        self.assertEqual(out[2], [True, True, True, True])


# STUB_DOM plus a canvas whose 2D context records every method call, and an innerHTML trap.
SC_STUB = STUB_DOM + (
    "global.__calls=[];global.__inner=[];"
    "const mk0=document.createElement;"
    "document.createElement=function(tag){const e=mk0(tag);"
    "Object.defineProperty(e,'innerHTML',{set(v){__inner.push(v)},get(){return ''}});"
    "e.getBoundingClientRect=()=>({left:0,top:0,width:600,height:400});"
    "if(tag==='canvas'){e.__id=Math.random();const rec={getImageData:()=>({image:true})};"
    "e.getContext=()=>new Proxy(rec,{get(t,p){if(p in t)return t[p];"
    "return (...a)=>{__calls.push({c:e.__id,m:String(p),a})}},set(t,p,v){t[p]=v;return true}});}"
    "return e};"
    "function walk(e,f){f(e);(e.children||[]).forEach(c=>{if(c&&typeof c==='object')walk(c,f)})}"
    "function texts(e){const t=[];walk(e,x=>{if(x.textContent)t.push(x.textContent)});return t}"
    "function byTag(e,tag){const t=[];walk(e,x=>{if(x.tag===tag)t.push(x)});return t}"
)


@unittest.skipUnless(NODE, "node not installed")
class JsScatterRenderTests(unittest.TestCase):
    def render(self, rows, expr, opts=None):
        o = {"mode": "name", "size": "gpuh", "split": False}
        o.update(opts or {})
        return run_js(SC_STUB + "self.JobstatsCore = core; require(%s);"
                      "const o = %s; o.onOpen = (r, n) => (self.__open || (() => {}))(r, n);"
                      "const c = mk('div'); const h = self.JobstatsScatter.render(c, data, o);"
                      "const arcs = __calls.filter(x => x.m === 'arc'); %s"
                      % (json.dumps(str(STATIC / "jobstats-scatter.js")), json.dumps(o), expr), rows)

    def test_render_counts_and_panels(self):
        rows = [sc_row("1"), sc_row("2", gpu_hours=4), sc_row("3", alloc_gpus=8, summary_gpus=8),
                sc_row("4", end=None, summary_state="missing", gpu_hours=2)]
        out = self.render(rows, "return {t: texts(c), canv: byTag(c, 'canvas').length, arcs: arcs.length,"
                                " fn: typeof h.redraw};")
        count = [t for t in out["t"] if t.startswith("Plotted")]
        self.assertEqual(len(count), 1)
        self.assertIn("Plotted 3 jobs", count[0])
        self.assertIn("running 1 (2.00 GPU-h)", count[0])
        self.assertIn("% of the selection's GPU-hours is not on the plot", count[0])
        self.assertEqual(out["canv"], 1)
        self.assertEqual(out["arcs"], 3)
        self.assertEqual(out["fn"], "function")

    def test_render_split_three_panels(self):
        rows = [sc_row("1"), sc_row("2", alloc_gpus=4, summary_gpus=4), sc_row("3", alloc_gpus=8, summary_gpus=8)]
        out = self.render(rows, "return {t: texts(c), canv: byTag(c, 'canvas').length};", {"split": True})
        self.assertEqual(out["canv"], 3)
        for label in ("1 GPU", "2–7 GPUs", "8+ GPUs"):
            self.assertIn(label, out["t"])

    def test_render_empty(self):
        out = self.render([], "return {t: texts(c), arcs: arcs.length, canv: byTag(c, 'canvas').length};")
        self.assertIn("No jobs to plot", out["t"])
        self.assertEqual(out["arcs"], 0)
        self.assertEqual(out["canv"], 0)
        out = self.render([sc_row("r", end=None, summary_state="missing")], "return texts(c);")
        self.assertIn("No jobs to plot", out)

    def test_render_redraw_after_empty(self):
        out = self.render([], "h.redraw(); return arcs.length;")
        self.assertEqual(out, 0)

    def test_render_clamps_values(self):
        rows = [sc_row("a", gpu_pct=-3, sm=104), sc_row("b", gpu_pct=0, sm=100)]
        out = self.render(rows, "return arcs.map(x => [x.a[0], x.a[1]]);", {"size": "equal"})
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0], out[1])
        for x, y in out:
            self.assertTrue(0 < x < 600 and 0 < y < 600, (x, y))

    def test_render_uses_text_nodes(self):
        rows = [sc_row("1", name="<b>&x"), sc_row("2", name="")]
        out = self.render(rows, "return {t: texts(c), inner: __inner};")
        self.assertIn("<b>&x", out["t"])
        self.assertEqual(out["inner"], [])
        src = (STATIC / "jobstats-scatter.js").read_text()
        self.assertNotIn("innerHTML =", src)

    def test_draw_order_largest_first(self):
        rows = [sc_row(str(i), gpu_hours=h) for i, h in enumerate([1, 9, 4, 16, 0.5])]
        out = self.render(rows, "return arcs.map(x => x.a[2]);")
        self.assertEqual(len(out), 5)
        self.assertEqual(out, sorted(out, reverse=True))
        self.assertEqual(out[0], 18)

    def test_aria_label(self):
        out = self.render([sc_row("1"), sc_row("2")], "return byTag(c, 'canvas')[0].attrs['aria-label'];")
        self.assertEqual(out, "Scatter of 2 jobs: GPU duty % against SM %")

    def test_click_opens_job(self):
        rows = [sc_row("only", gpu_pct=50, sm=50)]
        body = ("const cv = byTag(c, 'canvas')[0]; const a = arcs[0].a; const got = [];"
                "self.__open = (r, n) => got.push([r.jobid, n]);"
                "cv.listeners.click.forEach(f => f({clientX: a[0], clientY: a[1], ctrlKey: true, metaKey: false}));"
                "cv.listeners.click.forEach(f => f({clientX: 1, clientY: 1, ctrlKey: false, metaKey: false}));"
                "return got;")
        out = self.render(rows, body)
        self.assertEqual(out, [["only", True]])


@unittest.skipUnless(NODE, "node not installed")
class JsScatterAxesRenderTests(unittest.TestCase):
    """X and Y axis pickers: the renderer follows opts.x / opts.y."""
    render = JsScatterRenderTests.render
    STANDING = ["Duty and SM are measured separately", "GPU-h counts the whole job"]

    def test_render_axis_labels_and_diagonal(self):
        body = ("return {t: __calls.filter(x => x.m === 'fillText').map(x => x.a[0]),"
                " dash: __calls.filter(x => x.m === 'setLineDash' && x.a[0].length > 0).length};")
        rows = [sc_row("1", tc=10, occ=10)]
        out = self.render(rows, body, {"x": "sm", "y": "tc"})
        self.assertIn("SM %", out["t"])
        self.assertIn("TC %", out["t"])
        self.assertNotIn("GPU duty %", out["t"])
        self.assertEqual(out["dash"], 0)
        self.assertFalse([t for t in out["t"] if "(reference)" in t or "(ceiling)" in t], out["t"])
        out = self.render(rows, body, {"x": "sm", "y": "occ"})
        self.assertIn("OCC = SM (ceiling)", out["t"])
        self.assertEqual(out["dash"], 1)
        out = self.render(rows, body, {"x": "occ", "y": "sm"})
        self.assertIn("SM = OCC (floor)", out["t"])
        self.assertNotIn("OCC = SM (ceiling)", out["t"])
        self.assertEqual(out["dash"], 1)
        out = self.render(rows, body)
        self.assertIn("SM = duty (reference)", out["t"])
        self.assertIn("GPU duty %", out["t"])
        self.assertEqual(out["dash"], 1)
        out = self.render(rows, body, {"x": "sm", "y": "gpu_pct"})
        self.assertIn("SM = duty (reference)", out["t"])

    def test_render_dots_use_axis_values(self):
        rows = [sc_row("1", gpu_pct=80, sm=50, tc=10)]
        # stub canvas: 600 px wide, so h = 432; frame ML 40, MR 12, MT 10, MB 34
        sx = lambda v: 40 + v / 100 * (600 - 40 - 12)
        sy = lambda v: 10 + (1 - v / 100) * (432 - 10 - 34)
        out = self.render(rows, "return [arcs[0].a[0], arcs[0].a[1]];", {"x": "sm", "y": "tc"})
        self.assertAlmostEqual(out[0], sx(50))
        self.assertAlmostEqual(out[1], sy(10))
        out = self.render(rows, "return [arcs[0].a[0], arcs[0].a[1]];")
        self.assertAlmostEqual(out[0], sx(80))
        self.assertAlmostEqual(out[1], sy(50))

    def count(self, rows, opts):
        out = self.render(rows, "return texts(c).filter(t => t.startsWith('Plotted'));", opts)
        self.assertEqual(len(out), 1)
        return out[0]

    def test_render_count_line_wording(self):
        rows = [sc_row("1", tc=10), sc_row("2", tc=None, gpu_hours=2)]
        self.assertIn("no SM or TC value 1 (2.00 GPU-h)", self.count(rows, {"x": "sm", "y": "tc"}))
        self.assertIn("no SM value 1 (2.00 GPU-h)", self.count([sc_row("1"), sc_row("2", sm=None, gpu_hours=2)],
                                                               {"x": "sm", "y": "sm"}))
        line = self.count([sc_row("1", gpu_pct=None, gpu_hours=2), sc_row("2")], {})
        self.assertIn("no GPU duty or SM value 1 (2.00 GPU-h)", line)
        rows = [sc_row("1", sm=50, occ=10), sc_row("2", sm=2, occ=1, gpu_hours=3)]
        line = self.count(rows, {"x": "sm", "y": "occ_sm"})
        self.assertIn("SM under 5 % (occ/SM undefined) 1 (3.00 GPU-h)", line)
        self.assertNotIn("no SM", line)

    def tip(self, rows, opts):
        body = ("global.requestAnimationFrame = (f) => f();"
                "const cv = byTag(c, 'canvas')[0]; const a = arcs[0].a;"
                "const hh = Number(cv.style.height.replace('px', ''));"
                "cv.getBoundingClientRect = () => ({left: 0, top: 0, width: 600, height: hh});"
                "cv.listeners.pointermove.forEach(f => f({clientX: a[0], clientY: a[1]}));"
                "const tip = c.children[c.children.length - 1];"
                "return {hidden: tip.hidden, lines: tip.children.map(l => l.children.map(x => x.textContent))};")
        out = self.render(rows, body, opts)
        self.assertFalse(out["hidden"])
        return [(k.strip(), v) for k, v in out["lines"]]

    def test_render_tooltip_lines(self):
        rows = [sc_row("1", sm=48, occ=15)]
        lines = self.tip(rows, {"x": "sm", "y": "occ_sm"})
        keys = [k for k, _ in lines]
        self.assertEqual(keys[:8], ["Job", "Name", "User", "GPUs", "GPU-h", "Duty", "SM", "OCC/SM"])
        self.assertEqual(lines[7][1], "31.3% (OCC 15.0% of SM 48.0%)")
        self.assertEqual(keys.count("SM"), 1)
        keys = [k for k, _ in self.tip(rows, {})]
        self.assertEqual(keys, ["Job", "Name", "User", "GPUs", "GPU-h", "Duty", "SM", "Low-duty GPUs"])
        # a non-duty, non-SM pair gets one line per axis, labelled by the axis text without "%"
        rows = [sc_row("1", tc=12, dram=40, coverage={"gpu_pct": 1, "sm": 1, "tc": 0.5, "dram": 1})]
        lines = dict(self.tip(rows, {"x": "tc", "y": "dram"}))
        self.assertEqual(lines["TC"], "12.0%")
        self.assertEqual(lines["DRAM"], "40.0%")
        self.assertEqual(lines["Coverage"], "duty 100%, SM 100%, TC 50%")

    def notes(self, opts):
        return self.render([sc_row("1", tc=10, dram=20, occ=10)],
                           "return byTag(c, 'p').map(p => p.textContent);", opts)

    def test_render_pair_note(self):
        presets = {
            ("gpu_pct", "sm"): "The gap between duty and SM is time when a kernel ran but most SMs sat empty.",
            ("sm", "tc"): "Busy SMs with little tensor-core use. Expected for some codes, a lead for others.",
            ("sm", "dram"): "Separates memory-bandwidth-heavy work from the rest of the low-tensor group.",
            ("sm", "occ_sm"): "How full the SMs were while active. Low is normal for large matmul kernels, so read "
                              "it only for a job that already looks slow.",
        }
        for (x, y), text in presets.items():
            out = self.notes({"x": x, "y": y})
            self.assertEqual(out[0], text)
            # The duty/SM note talks about "the line", so it shows only where that line is drawn.
            standing = self.STANDING if (x, y) == ("gpu_pct", "sm") else self.STANDING[1:]
            self.assertEqual(len(out), 1 + len(standing), out)
            for i, s in enumerate(standing):
                self.assertTrue(out[i + 1].startswith(s), out)
        out = self.notes({"x": "tc", "y": "dram"})
        self.assertEqual(len(out), 1, out)
        self.assertTrue(out[0].startswith(self.STANDING[1]), out)
        self.assertEqual(self.notes({})[0], presets[("gpu_pct", "sm")])
        self.assertTrue(self.notes({"x": "sm", "y": "gpu_pct"})[0].startswith(self.STANDING[0]))
        occ = self.notes({"x": "sm", "y": "occ"})
        self.assertEqual(occ[0], "Occupancy counts idle SM-cycles as zero, so it cannot exceed SM activity. "
                                 "The distance below the line is how empty the SMs were while active.")
        self.assertEqual(len(occ), 2, occ)
        flip = self.notes({"x": "occ", "y": "sm"})
        self.assertEqual(flip[0], "Occupancy counts idle SM-cycles as zero, so it cannot exceed SM activity. "
                                  "The distance above the line is how empty the SMs were while active.")
        self.assertEqual(len(flip), 2, flip)

    def test_aria_label_follows_axes(self):
        out = self.render([sc_row("1", tc=10)], "return byTag(c, 'canvas')[0].attrs['aria-label'];",
                          {"x": "sm", "y": "tc"})
        self.assertIn("SM % against TC %", out)
        self.assertEqual(out, "Scatter of 1 jobs: SM % against TC %")

    def test_render_bogus_axis_falls_back(self):
        out = self.render([sc_row("1")], "return byTag(c, 'canvas')[0].attrs['aria-label'];",
                          {"x": "bogus", "y": None})
        self.assertEqual(out, "Scatter of 1 jobs: GPU duty % against SM %")


class JsPageWiringTests(unittest.TestCase):
    html = (STATIC / "jobstats.html").read_text()
    js = (STATIC / "jobstats.js").read_text()

    def test_scatter_view_button(self):
        bar = self.html[self.html.index('id="js-view"'):]
        bar = bar[:bar.index("</div>")]
        self.assertIn('<button type="button" data-v="scatter">Scatter</button>', bar)

    def test_scatter_controls(self):
        for frag in ('id="js-sc-bar"', 'id="js-size"', 'data-v="gpuh"', 'data-v="equal"', 'id="js-split"',
                     'id="js-scatter-wrap"'):
            self.assertIn(frag, self.html)

    def test_scatter_script_order(self):
        h = self.html
        self.assertLess(h.index('src="jobstats-core.js'), h.index('src="jobstats-scatter.js'))
        self.assertLess(h.index('src="jobstats-scatter.js'), h.index('src="jobstats.js'))

    def test_scatter_notes(self):
        note = self.html[self.html.index("How the numbers are computed"):]
        self.assertIn("one dot per job with a summary", note)
        self.assertIn("even the part outside it", note)

    def test_asset_versions_bumped(self):
        import re
        refs = re.findall(r'(?:src|href)="(jobstats[\w-]*\.(?:js|css)[^"]*)"', self.html)
        self.assertEqual(len(refs), 5, refs)
        for ref in refs:
            self.assertTrue(ref.endswith("?v=m7-ax"), ref)

    def test_url_keys(self):
        self.assertIn('["jobs", "groups", "scatter"]', self.js)
        self.assertIn('size: "gpuh"', self.js)
        self.assertIn('split: "0"', self.js)
        self.assertIn("JobstatsScatter.render(", self.js)

    def test_axis_controls(self):
        for frag in ('id="js-x"', 'id="js-y"', 'id="js-preset"'):
            self.assertIn(frag, self.html)
        bar = self.html[self.html.index('id="js-sc-bar"'):]
        self.assertLess(bar.index('id="js-preset"'), bar.index('id="js-x"'))
        self.assertLess(bar.index('id="js-y"'), bar.index('id="js-size"'))
        group = self.html[self.html.index('id="js-preset"'):]
        group = group[:group.index("</div>")]
        for x, y, label in (("gpu_pct", "sm", "Duty vs SM"), ("sm", "tc", "SM vs tensor"),
                            ("sm", "dram", "SM vs DRAM"), ("sm", "occ_sm", "SM vs occ/SM")):
            self.assertIn('<button type="button" data-x="%s" data-y="%s">%s</button>' % (x, y, label), group)
        self.assertEqual(group.count("<button"), 4)

    def test_axis_url_state(self):
        self.assertIn('x: "gpu_pct"', self.js)
        self.assertIn('y: "sm"', self.js)
        self.assertIn('st.x = pick(p.get("x"), AXIS_KEYS, DEF.x)', self.js)
        self.assertIn('st.y = pick(p.get("y"), AXIS_KEYS, DEF.y)', self.js)
        self.assertIn('"size", "split", "x", "y"]', self.js)
        self.assertIn("x: st.x, y: st.y", self.js)
        self.assertIn("C.SCATTER_AXES.map(", self.js)

    def test_axis_scatter_note(self):
        note = self.html[self.html.index("How the numbers are computed"):]
        self.assertIn("Scatter: one dot per job with a summary; position is the job's mean of the two chosen metrics "
                      "over its GPUs. OCC/SM is occupancy divided by SM activity and is left out when SM is under 5 %. "
                      "Ring and hollow mark uneven or incomplete GPUs.", " ".join(note.split()))


@unittest.skipUnless(NODE, "node not installed")
class JsScatterReviewFixTests(unittest.TestCase):
    """Final-review fixes: pointer scaling and hover cost."""
    render = JsScatterRenderTests.render

    def test_click_scales_to_drawn_size(self):
        # laid out at 600 px wide; the canvas is later shown at 300 px without a resize event
        rows = [sc_row("only", gpu_pct=80, sm=50)]
        body = ("const cv = byTag(c, 'canvas')[0]; const a = arcs[0].a; const got = [];"
                "self.__open = (r) => got.push(r.jobid);"
                "cv.getBoundingClientRect = () => ({left: 0, top: 0, width: 300, height: cv.__h / 2});"
                "cv.__h = Number(cv.style.height.replace('px', ''));"
                "cv.listeners.click.forEach(f => f({clientX: a[0] / 2, clientY: a[1] / 2}));"
                "return got;")
        self.assertEqual(self.render(rows, body, {"size": "equal"}), ["only"])

    def test_hover_does_not_redraw_every_dot(self):
        rows = [sc_row(str(i), gpu_pct=i % 100, sm=(i * 7) % 100) for i in range(300)]
        body = ("global.requestAnimationFrame = (f) => f();"
                "const cv = byTag(c, 'canvas')[0]; const a = arcs[0].a; const before = arcs.length;"
                "const hh = Number(cv.style.height.replace('px', ''));"
                "cv.getBoundingClientRect = () => ({left: 0, top: 0, width: 600, height: hh});"
                "cv.listeners.pointermove.forEach(f => f({clientX: a[0], clientY: a[1]}));"
                "const after = __calls.filter(x => x.m === 'arc').length;"
                "return {before, added: after - before, put: __calls.filter(x => x.m === 'putImageData').length};")
        out = self.render(rows, body, {"size": "equal"})
        self.assertEqual(out["before"], 300)
        self.assertEqual(out["added"], 1)      # only the highlight ring
        self.assertEqual(out["put"], 1)
