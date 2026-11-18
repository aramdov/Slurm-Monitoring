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
        for name in ("jobstats-charts.js", "jobstats.js", "jobstats-core.js"):
            proc = subprocess.run([NODE, "--check", str(STATIC / name)], capture_output=True, text=True, timeout=30)
            self.assertEqual(proc.returncode, 0, name + ": " + proc.stderr)

    def test_page_loads_charts_script_with_new_version(self):
        html = (STATIC / "jobstats.html").read_text()
        self.assertIn('src="jobstats-charts.js?v=m5-ts"', html)
        self.assertNotIn("m4-3-user", html)
        self.assertLess(html.index("jobstats-core.js"), html.index("jobstats-charts.js"))
        self.assertLess(html.index("jobstats-charts.js"), html.index('src="jobstats.js'))

