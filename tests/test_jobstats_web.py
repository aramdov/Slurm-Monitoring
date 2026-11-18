"""Tests for the read-only /jobstats Blueprint (JS1 summaries from sacct AdminComment)."""
import base64
import gzip
import json
import os
import tempfile
import threading
import time
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

os.environ["SLURM_DASHBOARD_DISABLE_RECORDER"] = "1"

import app as dashboard_app
import config
import history
import jobstats_web as jw

FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "jobstats_js1.json").read_text())
SEP = "\x1f"


def encode(payload):
    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return "JS1:" + base64.b64encode(gzip.compress(raw)).decode()


def sacct_line(jobid="100", jobid_raw=None, user="alice", name="train", state="COMPLETED", nodes="b200-8c",
               tres="billing=8,cpu=8,gres/gpu:b200=1,gres/gpu=1,mem=128G,node=1", elapsed="3600",
               submit="1793000000", start="1793000100", end="1793003700", comment=""):
    jobid_raw = jobid_raw or jobid.split("_")[0]
    return SEP.join([jobid_raw, jobid, user, name, state, nodes, tres, elapsed, submit, start, end, comment])


def fixture_line(name, **overrides):
    s = FIXTURES[name]["sacct"]
    fields = dict(jobid=s["JobID"], jobid_raw=s["JobIDRaw"], user=s.get("User", "alice"), name=name, state=s["State"],
                  nodes=s["NodeList"], tres=s["AllocTRES"], elapsed=s["ElapsedRaw"],
                  submit=s["Submit"], start=s["Start"], end=s["End"],
                  comment=encode(FIXTURES[name]["payload"]))
    fields.update(overrides)
    return sacct_line(**fields)


def row_from(line, now_s=1800000000):
    records, unparsed = jw.parse_sacct(line + "\n")
    assert unparsed == 0 and len(records) == 1, (records, unparsed)
    return jw.build_row(records[0], now_s)


# Synthetic payload with hand-computable values.
#   n1: 4 cpus, 200 CPU-s over 100 s elapsed -> 50 %; memory 50/100 -> 50 %
#   GPU 0: util 10, sm 0.2, mem 40/100 (40 %), power 100 W
#   GPU 1: util 30, no sm,  mem 60/200 (30 %), power 300 W
SYNTH = {
    "gpus": 2, "total_time": 100,
    "nodes": {"n1": {
        "cpus": 4, "total_time": 200, "used_memory": 50, "total_memory": 100,
        "gpu_utilization": {"0": 10, "1": 30},
        "sm_util_percent_avg": {"0": 0.2},
        "gpu_used_memory": {"0": 40, "1": 60},
        "gpu_total_memory": {"0": 100, "1": 200},
        "fp16_util_percent_max": {"0": 0, "1": 0},
        "power_usage_milliwatts_avg": {"0": 100000, "1": 300000},
    }},
}


def synth_line(payload=SYNTH, elapsed="100", tres="cpu=4,gres/gpu=2", **kw):
    return sacct_line(comment=encode(payload), elapsed=elapsed, tres=tres, **kw)


class DecodeTests(unittest.TestCase):
    def test_real_fixtures_decode_ok(self):
        for name, fx in FIXTURES.items():
            state, reason, payload = jw.decode_summary(encode(fx["payload"]))
            self.assertEqual((state, reason), ("ok", None), name)
            self.assertEqual(payload["gpus"], fx["payload"]["gpus"])
            self.assertEqual(set(payload["nodes"]), set(fx["payload"]["nodes"]))

    def test_simple_states(self):
        self.assertEqual(jw.decode_summary("JS1:Short")[:2], ("short", None))
        self.assertEqual(jw.decode_summary("JS1:None")[:2], ("none", None))
        self.assertEqual(jw.decode_summary("")[:2], ("missing", None))
        self.assertEqual(jw.decode_summary("set by admin | see ticket")[:2], ("other", None))

    def test_invalid_reasons(self):
        cases = {
            "JS1:!!not base64!!": "base64",
            "JS1:" + base64.b64encode(b"not gzip at all").decode(): "gzip",
            "JS1:" + base64.b64encode(gzip.compress(b"x" * 100)[:20]).decode(): "gzip",
            encode(b"{not json"): "json",
            encode(b"\xff\xfe"): "json",
            encode([1, 2]): "schema",
            encode({"gpus": 1, "total_time": 5, "nodes": None}): "schema",
            encode({"gpus": 1, "total_time": 5}): "schema",
            encode({"gpus": 1, "total_time": 5, "nodes": {}}): "schema",
            encode({"gpus": 1, "total_time": 5, "nodes": {"n": 1}}): "schema",
            encode({"gpus": -1, "total_time": 5, "nodes": {"n": {}}}): "schema",
            encode({"gpus": True, "total_time": 5, "nodes": {"n": {}}}): "schema",
            encode({"gpus": "8", "total_time": 5, "nodes": {"n": {}}}): "schema",
            encode(b'{"gpus": 1, "total_time": NaN, "nodes": {"n": {}}}'): "schema",
        }
        for comment, reason in cases.items():
            self.assertEqual(jw.decode_summary(comment)[:2], ("invalid", reason), comment[:40])

    def test_oversized_base64_is_too_large(self):
        comment = "JS1:" + "A" * (jw.B64_MAX + 4)
        self.assertEqual(jw.decode_summary(comment)[:2], ("invalid", "too_large"))

    def test_gzip_bomb_is_bounded(self):
        bomb = encode(b"[" + b" " * (50 * 1024 * 1024) + b"]")
        self.assertLess(len(bomb), jw.B64_MAX)
        lengths = []
        real = zlib.decompressobj

        class Spy:
            def __init__(self, *a, **k):
                self._d = real(*a, **k)

            def decompress(self, data, max_length=0):
                lengths.append(max_length)
                return self._d.decompress(data, max_length)

            def flush(self, *a):
                lengths.append(a[0] if a else 0)
                return self._d.flush(*a)

            def __getattr__(self, name):
                return getattr(self._d, name)

        with patch.object(jw.zlib, "decompressobj", Spy):
            self.assertEqual(jw.decode_summary(bomb)[:2], ("invalid", "too_large"))
        self.assertTrue(lengths and all(0 < n <= jw.JSON_MAX + 1 for n in lengths), lengths)

    def test_non_finite_metric_becomes_null_for_that_gpu_only(self):
        raw = (b'{"gpus": 2, "total_time": 10, "nodes": {"n": {"cpus": 1, "total_time": 5,'
               b' "gpu_utilization": {"0": NaN, "1": 40}, "sm_util_percent_avg": {"0": Infinity, "1": "x"}}}}')
        state, _, payload = jw.decode_summary(encode(raw))
        self.assertEqual(state, "ok")
        node = payload["nodes"]["n"]
        self.assertEqual(node["gpu_utilization"], {"0": None, "1": 40})
        self.assertEqual(node["sm_util_percent_avg"], {"0": None, "1": None})
        metrics = jw.derive_metrics(payload)
        self.assertEqual(metrics["gpu_pct"], 40)
        self.assertIsNone(metrics["sm"])
        json.dumps(metrics, allow_nan=False)


class ParsingTests(unittest.TestCase):
    def test_user_is_parsed_and_on_the_row(self):
        row, _ = row_from(sacct_line(user="hrant"))
        self.assertEqual(row["user"], "hrant")

    def test_pipe_in_name_and_non_js1_comment(self):
        line = sacct_line(name="a|b|c", comment="admin note | with pipe")
        records, unparsed = jw.parse_sacct(line + "\n")
        self.assertEqual(unparsed, 0)
        row, _ = jw.build_row(records[0], 1800000000)
        self.assertEqual(row["name"], "a|b|c")
        self.assertEqual(row["summary_state"], "other")

    def test_embedded_newline_drops_only_that_row(self):
        text = "\n".join([sacct_line(jobid="1"), sacct_line(jobid="2", name="bad\nname"), sacct_line(jobid="3")]) + "\n"
        records, unparsed = jw.parse_sacct(text)
        self.assertEqual([r["jobid"] for r in records], ["1", "3"])
        self.assertEqual(unparsed, 2)

    def test_array_task_ids(self):
        row, _ = row_from(sacct_line(jobid="1041_14", jobid_raw="1059"))
        self.assertEqual((row["jobid"], row["jobid_raw"], row["array_job"], row["array_task"]),
                         ("1041_14", "1059", "1041", "14"))
        self.assertEqual(row["key"], "1041_14@1793000000")
        plain, _ = row_from(sacct_line(jobid="3337"))
        self.assertIsNone(plain["array_job"])
        self.assertIsNone(plain["array_task"])

    def test_unexpanded_pending_range_dropped(self):
        text = sacct_line(jobid="1041_[20-30]", state="PENDING", start="Unknown", end="Unknown") + "\n"
        records, unparsed = jw.parse_sacct(text)
        self.assertEqual((records, unparsed), ([], 0))

    def test_unknown_times_and_running(self):
        row, _ = row_from(sacct_line(state="RUNNING", end="Unknown", elapsed="600"))
        self.assertIsNone(row["end"])
        self.assertFalse(row["not_started"])
        self.assertEqual(row["gpu_hours"], 600 / 3600)

    def test_never_started_rows(self):
        row, _ = row_from(sacct_line(state="CANCELLED by 1001", start="None", end="1793000500", elapsed="0"))
        self.assertTrue(row["not_started"])
        self.assertIsNone(row["start"])
        self.assertEqual(row["gpu_hours"], 0)

    def test_alloc_tres(self):
        self.assertEqual(jw.parse_alloc_tres("billing=128,cpu=128,gres/gpu:b200=8,gres/gpu=8,mem=900G,node=1"), (8, 128))
        self.assertEqual(jw.parse_alloc_tres("billing=1,cpu=1,mem=8G,node=1"), (0, 1))
        self.assertEqual(jw.parse_alloc_tres(""), (0, 0))

    def test_hostlist(self):
        self.assertEqual(jw.expand_hostlist("b200-8c"), ["b200-8c"])
        self.assertEqual(jw.expand_hostlist("b200-8,b200-8b"), ["b200-8", "b200-8b"])
        self.assertEqual(jw.expand_hostlist("node[01-03,7]"), ["node01", "node02", "node03", "node7"])
        self.assertEqual(jw.expand_hostlist("None assigned"), [])

    def test_outward_rounding_and_exact_filter(self):
        self.assertEqual(jw.query_bounds(1793000030500, 1793000090000), (1792999980, 1793000100))
        rows = [row_from(sacct_line(jobid=str(i), start=str(s), end=str(e)))[0]
                for i, (s, e) in enumerate([(1793000000, 1793000020),   # ends before from
                                             (1793000000, 1793000040),   # overlaps
                                             (1793000095, 1793000200)])]  # starts after to
        kept = jw.filter_window(rows, 1793000030.5, 1793000090.0, 1800000000)
        self.assertEqual([r["jobid"] for r in kept], ["1"])

    def test_running_job_overlaps_until_now(self):
        row, _ = row_from(sacct_line(start="1793000000", end="Unknown", state="RUNNING"))
        self.assertEqual(len(jw.filter_window([row], 1793500000, 1793600000, 1793550000)), 1)
        self.assertEqual(len(jw.filter_window([row], 1793500000, 1793600000, 1793400000)), 0)


class DerivedValueTests(unittest.TestCase):
    def test_synthetic_hand_computed(self):
        row, _ = row_from(synth_line())
        self.assertEqual(row["summary_state"], "ok")
        self.assertAlmostEqual(row["gpu_pct"], 20)
        self.assertAlmostEqual(row["sm"], 20)
        self.assertIsNone(row["tc"])
        self.assertIs(row["fp16_seen"], False)
        self.assertIsNone(row["fp64_seen"])
        self.assertEqual(row["gpu_mem_peak_b"], 60)
        self.assertAlmostEqual(row["gpu_mem_peak_pct"], 40)
        self.assertAlmostEqual(row["power_w"], 200)
        self.assertEqual(row["low_duty_gpus"], 1)
        self.assertAlmostEqual(row["cpu_eff"], 50)
        self.assertAlmostEqual(row["cpu_mem_peak_pct"], 50)
        self.assertEqual(row["coverage"]["gpu_pct"], 1.0)
        self.assertEqual(row["coverage"]["sm"], 0.5)
        self.assertEqual(row["coverage"]["tc"], 0.0)
        self.assertEqual(row["field_gpus"]["sm"], 1)
        self.assertEqual((row["alloc_gpus"], row["alloc_cpus"]), (2, 4))
        self.assertAlmostEqual(row["gpu_hours"], 2 * 100 / 3600)

    def test_multi_node(self):
        payload = json.loads(json.dumps(SYNTH))
        payload["nodes"]["n2"] = {"cpus": 4, "total_time": 100, "used_memory": 90, "total_memory": 100,
                                  "gpu_utilization": {"0": 80}}
        payload["gpus"] = 3
        row, _ = row_from(synth_line(payload))
        self.assertAlmostEqual(row["cpu_eff"], 300 / 800 * 100)
        self.assertAlmostEqual(row["cpu_mem_peak_pct"], 90)
        self.assertAlmostEqual(row["gpu_pct"], 40)
        self.assertEqual(row["low_duty_gpus"], 1)

    def test_missing_cpu_field_or_zero_denominator(self):
        payload = json.loads(json.dumps(SYNTH))
        del payload["nodes"]["n1"]["cpus"]
        self.assertIsNone(jw.derive_metrics(payload)["cpu_eff"])
        payload = json.loads(json.dumps(SYNTH))
        payload["total_time"] = 0
        self.assertIsNone(jw.derive_metrics(payload)["cpu_eff"])
        payload = json.loads(json.dumps(SYNTH))
        payload["nodes"]["n1"]["total_memory"] = 0
        self.assertIsNone(jw.derive_metrics(payload)["cpu_mem_peak_pct"])

    def test_real_one_gpu(self):
        row, _ = row_from(fixture_line("gpu1"))
        self.assertAlmostEqual(row["gpu_pct"], 52.6)
        self.assertAlmostEqual(row["sm"], 18.97)
        self.assertIs(row["fp16_seen"], True)
        self.assertIs(row["fp64_seen"], False)
        self.assertAlmostEqual(row["cpu_eff"], 4557.9 / (8 * 2703) * 100)
        self.assertEqual((row["array_job"], row["array_task"]), ("2921", "0"))

    def test_real_eight_gpu_matches_cli(self):
        row, _ = row_from(fixture_line("gpu8"))
        utils = [63.4, 66.9, 62.8, 65.9, 63.6, 62.7, 63.6, 63.5]
        self.assertAlmostEqual(row["gpu_pct"], sum(utils) / 8)
        self.assertEqual(round(row["gpu_pct"]), 64)        # CLI: GPU utilization 64%
        self.assertEqual(round(row["cpu_eff"], 1), 25.7)    # CLI: efficiency=25.7%
        self.assertEqual(round(row["sm"]), 41)              # CLI: SM util. 41%
        self.assertEqual(row["gpu_hours"], 8 * 29477 / 3600)
        self.assertEqual(row["coverage"]["gpu_pct"], 1.0)

    def test_cpu_only_job(self):
        row, _ = row_from(fixture_line("cpuonly"))
        self.assertEqual(row["alloc_gpus"], 0)
        self.assertIsNone(row["gpu_pct"])
        self.assertIsNone(row["coverage"])
        self.assertIsNone(row["low_duty_gpus"])
        self.assertAlmostEqual(row["cpu_eff"], 412.4 / 663 * 100)

    def test_gpu_job_without_gpu_data(self):
        row, _ = row_from(fixture_line("nogpudata"))
        self.assertEqual(row["summary_state"], "ok")
        self.assertIsNone(row["gpu_pct"])
        self.assertIsNone(row["gpu_mem_peak_b"])
        self.assertEqual(row["coverage"]["gpu_pct"], 0.0)
        self.assertEqual(row["alloc_gpus"], 8)
        self.assertAlmostEqual(row["gpu_hours"], 8 * 48401 / 3600)

    def test_non_ok_rows_have_null_metrics(self):
        for comment in ("JS1:Short", "JS1:None", "", "JS1:@@@"):
            row, payload = row_from(sacct_line(comment=comment))
            self.assertIsNone(payload)
            for key in ("gpu_pct", "sm", "cpu_eff", "power_w", "fp16_seen", "coverage"):
                self.assertIsNone(row[key], (comment, key))
            self.assertGreater(row["gpu_hours"], 0)
        row, _ = row_from(sacct_line(comment="JS1:@@@"))
        self.assertEqual((row["summary_state"], row["invalid_reason"]), ("invalid", "base64"))

    def test_row_has_all_spec_fields(self):
        row, _ = row_from(synth_line())
        spec_fields = ("key jobid jobid_raw array_job array_task name state nodes alloc_gpus alloc_cpus elapsed_s "
                       "submit start end not_started summary_state invalid_reason gpu_hours gpu_pct sm tc dram occ "
                       "fp32 int fp16_seen fp64_seen gpu_mem_peak_b gpu_mem_peak_pct cpu_eff cpu_mem_peak_pct "
                       "low_duty_gpus power_w coverage").split()
        self.assertTrue(set(spec_fields) <= set(row), set(spec_fields) - set(row))

    def test_node_details(self):
        _, payload = row_from(fixture_line("gpu8"))
        nodes = jw.node_details(payload)
        node = nodes["b200-8d"]
        self.assertEqual(node["cpus"], 64)
        self.assertAlmostEqual(node["cpu_eff"], 484071.4 / (64 * 29477) * 100)
        self.assertEqual(node["mem_total_b"], 644245094400)
        self.assertEqual(sorted(node["gpus"]), [str(i) for i in range(8)])
        self.assertEqual(node["gpus"]["1"]["gpu_utilization"], 66.9)
        self.assertEqual(node["gpus"]["0"]["gpu_total_memory"], 192265846784)


class AggregateTests(unittest.TestCase):
    def _row(self, util_by_gpu, elapsed, alloc_gpus=None, comment=None, **kw):
        alloc_gpus = len(util_by_gpu) if alloc_gpus is None else alloc_gpus
        payload = {"gpus": alloc_gpus, "total_time": elapsed, "nodes": {"n": {
            "cpus": 2, "total_time": elapsed, "gpu_utilization": util_by_gpu}}}
        line = sacct_line(comment=comment if comment is not None else encode(payload), elapsed=str(elapsed),
                          tres=f"cpu=2,gres/gpu={alloc_gpus}", **kw)
        return row_from(line)[0]

    def test_weighted_mean_and_totals(self):
        a = self._row({"0": 40, "1": 60}, 100)               # mean 50 over 2 GPUs for 100 s
        b = self._row({"0": 10}, 300, alloc_gpus=2)          # 1 of 2 GPUs reported, 300 s
        m = self._row({}, 3600, comment="", alloc_gpus=4)    # no summary
        agg = jw.aggregate([a, b, m])
        self.assertAlmostEqual(agg["gpu_pct"]["mean"], (100 * 100 + 300 * 10) / (100 * 2 + 300 * 1))
        self.assertIsNone(agg["gpu_pct"]["spread"])          # fewer than 3 values
        self.assertEqual(agg["jobs"], 3)
        self.assertEqual(agg["states"], {"ok": 2, "missing": 1})
        self.assertAlmostEqual(agg["gpu_hours"], (2 * 100 + 2 * 300 + 4 * 3600) / 3600)
        self.assertAlmostEqual(agg["summarized_gpu_hours"], (2 * 100 + 2 * 300) / 3600)
        self.assertAlmostEqual(agg["summarized_share"], 800 / 15200)
        self.assertAlmostEqual(agg["coverage"], (100 * 2 + 300 * 1) / (100 * 2 + 300 * 2))
        self.assertAlmostEqual(agg["cpu_eff"]["mean"], (100 + 300) / (2 * 100 + 2 * 300) * 100)

    def test_spread_with_three_values(self):
        rows = [self._row({"0": v}, 100) for v in (10, 20, 30, 40)]
        spread = jw.aggregate(rows)["gpu_pct"]["spread"]
        self.assertAlmostEqual(spread["median"], 25)
        self.assertAlmostEqual(spread["p10"], 13)
        self.assertAlmostEqual(spread["p90"], 37)

    def test_never_started_excluded_and_empty(self):
        ns = self._row({"0": 50}, 0, start="None")
        agg = jw.aggregate([ns])
        self.assertEqual(agg["jobs"], 0)
        self.assertIsNone(agg["gpu_pct"]["mean"])
        self.assertIsNone(agg["summarized_share"])

    def test_fp_seen_fraction_and_memory(self):
        p1 = json.loads(json.dumps(SYNTH))
        p2 = json.loads(json.dumps(SYNTH))
        p2["nodes"]["n1"]["fp16_util_percent_max"] = {"0": 0.01}
        p2["nodes"]["n1"]["used_memory"] = 80
        rows = [row_from(synth_line(p))[0] for p in (p1, p2)]
        agg = jw.aggregate(rows)
        self.assertEqual(agg["fp16_seen"], 0.5)
        self.assertIsNone(agg["fp64_seen"])
        self.assertEqual(agg["cpu_mem_peak_pct"], {"median": 65, "max": 80})

    def test_group_keys(self):
        r, _ = row_from(sacct_line(jobid="2645_3", jobid_raw="2645", name="sweep_lr3e4_s12"))
        self.assertEqual(jw.group_key(r, "exact"), "sweep_lr3e4_s12")
        self.assertEqual(jw.group_key(r, "array"), "2645")
        self.assertEqual(jw.group_key(r, "family"), "sweep_lr#e#_s#")
        plain, _ = row_from(sacct_line(jobid="3337"))
        self.assertEqual(jw.group_key(plain, "array"), "3337")


class FakeSacct:
    """Replaces jw.run_capped; records argv and returns canned stdout."""

    def __init__(self, text="", exc=None):
        self.text, self.exc, self.calls = text, exc, []

    def __call__(self, argv, env, timeout, max_bytes):
        self.calls.append((argv, env))
        if self.exc:
            raise self.exc
        return self.text


class RouteTestBase(unittest.TestCase):
    def setUp(self):
        jw.clear_caches()
        self.client = dashboard_app.app.test_client()


class LimitTests(RouteTestBase):
    def test_busy_slot_returns_503_at_once(self):
        fake = FakeSacct("")
        self.assertTrue(jw._sacct_slot.acquire(blocking=False))
        try:
            with patch.object(jw, "run_capped", fake):
                r = self.client.get("/api/jobstats/jobs?from=1793000000000&to=1793086400000")
        finally:
            jw._sacct_slot.release()
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.headers.get("Retry-After"), "2")
        self.assertEqual(fake.calls, [])

    def test_cache_hit_does_not_call_sacct(self):
        fake = FakeSacct(fixture_line("gpu8") + "\n")
        with patch.object(jw, "run_capped", fake):
            for _ in range(2):
                r = self.client.get("/api/jobstats/jobs?from=1793200000000&to=1793400000000")
                self.assertEqual(r.status_code, 200)
            # Same outward-rounded bounds: still a hit.
            self.client.get("/api/jobstats/jobs?from=1793200010000&to=1793399990000")
        self.assertEqual(len(fake.calls), 1)

    def test_run_capped_kills_on_byte_cap(self):
        with self.assertRaises(jw.SourceError) as ctx:
            jw.run_capped(["yes"], dict(os.environ), timeout=5, max_bytes=100000)
        self.assertEqual(ctx.exception.message, "too_large")

    def test_run_capped_timeout(self):
        with self.assertRaises(jw.SourceError) as ctx:
            jw.run_capped(["sleep", "5"], dict(os.environ), timeout=0.3, max_bytes=1000)
        self.assertEqual(ctx.exception.message, "sacct timed out")

    def test_run_capped_nonzero_exit(self):
        with self.assertRaises(jw.SourceError):
            jw.run_capped(["false"], dict(os.environ), timeout=5, max_bytes=1000)

    def test_too_large_maps_to_503(self):
        with patch.object(jw, "run_capped", FakeSacct(exc=jw.SourceError("too_large"))):
            r = self.client.get("/api/jobstats/jobs?from=1793000000000&to=1793086400000")
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.get_json()["error"], "too_large")

    def test_truncated_over_max_rows(self):
        text = "".join(sacct_line(jobid=str(i), start=str(1793000000 + i), end=str(1793001000 + i)) + "\n"
                       for i in range(1, 12))
        with patch.object(jw, "run_capped", FakeSacct(text)), patch.object(jw, "MAX_ROWS", 10):
            body = self.client.get("/api/jobstats/jobs?from=1793000000000&to=1793086400000").get_json()
        self.assertTrue(body["truncated"])
        self.assertEqual(len(body["jobs"]), 10)
        self.assertEqual(body["counts"]["missing"], 11)
        self.assertEqual(body["jobs"][0]["jobid"], "11")    # sorted by end desc

    def test_slot_released_after_failure(self):
        with patch.object(jw, "run_capped", FakeSacct(exc=jw.SourceError("sacct failed"))):
            self.client.get("/api/jobstats/jobs?from=1793000000000&to=1793086400000")
        self.assertTrue(jw._sacct_slot.acquire(blocking=False))
        jw._sacct_slot.release()


class IdentityTests(RouteTestBase):
    def test_detail_exact_match_among_siblings(self):
        text = "".join(sacct_line(jobid=f"2921_{i}", jobid_raw=str(2922 + i), submit="1793273132") + "\n"
                       for i in range(3))
        fake = FakeSacct(text)
        with patch.object(jw, "run_capped", fake):
            r = self.client.get("/api/jobstats/job/2921_1?submit=1793273132")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual((body["job"]["jobid"], body["job"]["jobid_raw"]), ("2921_1", "2923"))
        argv, env = fake.calls[0]
        self.assertEqual(argv[argv.index("-j") + 1], "2921_1")
        self.assertEqual(env["SLURM_TIME_FORMAT"], "%s")
        self.assertEqual(env["TZ"], "UTC")
        self.assertEqual(argv[argv.index("-S") + 1], "2026-10-28T11:25:32")

    def test_submit_mismatch_is_404(self):
        with patch.object(jw, "run_capped", FakeSacct(sacct_line(jobid="3337", submit="1793000000") + "\n")):
            r = self.client.get("/api/jobstats/job/3337?submit=1793000001")
        self.assertEqual(r.status_code, 404)

    def test_raw_id_of_task_does_not_match_task_row(self):
        with patch.object(jw, "run_capped", FakeSacct(sacct_line(jobid="2921_0", jobid_raw="2922") + "\n")):
            r = self.client.get("/api/jobstats/job/2922?submit=1793000000")
        self.assertEqual(r.status_code, 404)

    def test_detail_nodes_and_cache(self):
        fake = FakeSacct(fixture_line("gpu8") + "\n")
        with patch.object(jw, "run_capped", fake):
            for _ in range(2):
                body = self.client.get("/api/jobstats/job/2920?submit=1793262977").get_json()
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(body["source"], "sacct")
        self.assertIn("generated_at", body)
        self.assertEqual(len(body["nodes"]["b200-8d"]["gpus"]), 8)

    def test_detail_for_non_ok_job_has_empty_nodes(self):
        with patch.object(jw, "run_capped", FakeSacct(sacct_line(jobid="5", comment="JS1:Short") + "\n")):
            body = self.client.get("/api/jobstats/job/5?submit=1793000000").get_json()
        self.assertEqual(body["job"]["summary_state"], "short")
        self.assertEqual(body["nodes"], {})


class RouteTests(RouteTestBase):
    def test_bad_explorer_params(self):
        for qs in ("", "?from=1", "?from=a&to=b", "?from=10&to=10", "?from=20&to=10",
                   f"?from=0&to={jw.MAX_SPAN_MS + 1}", "?from=1.5&to=100",
                   "?from=99999999999990&to=99999999999999"):
            with patch.object(jw, "run_capped", FakeSacct()) as fake:
                r = self.client.get("/api/jobstats/jobs" + qs)
            self.assertEqual(r.status_code, 400, qs)
            self.assertIn("error", r.get_json())
            self.assertEqual(fake.calls, [])

    def test_bad_detail_params(self):
        for path in ("/api/jobstats/job/abc?submit=1", "/api/jobstats/job/12_?submit=1",
                     "/api/jobstats/job/1_2_3?submit=1", "/api/jobstats/job/12%3Bls?submit=1",
                     "/api/jobstats/job/12", "/api/jobstats/job/12?submit=x",
                     "/api/jobstats/job/12?submit=999999999999999", "/api/jobstats/job/12?submit=0",
                     "/api/jobstats/job/12345678901?submit=1793000000"):
            with patch.object(jw, "run_capped", FakeSacct()) as fake:
                r = self.client.get(path)
            self.assertEqual(r.status_code, 400, path)
            self.assertEqual(fake.calls, [])

    def test_explorer_response_shape(self):
        text = "\n".join([
            fixture_line("gpu8"), fixture_line("cpuonly"),
            sacct_line(jobid="9", start="None", end="1793300000", elapsed="0", state="CANCELLED by 1001"),
            sacct_line(jobid="10", comment="JS1:Short", start="1793290000", end="1793290030"),
            "garbage line",
        ]) + "\n"
        fake = FakeSacct(text)
        with patch.object(jw, "run_capped", fake):
            r = self.client.get("/api/jobstats/jobs?from=1793250000000&to=1793400000000")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body["source"], "sacct")
        self.assertEqual(body["window"], {"from": 1793250000000, "to": 1793400000000})
        self.assertFalse(body["truncated"])
        self.assertEqual(body["counts"], {"ok": 1, "short": 1, "none": 0, "invalid": 0, "other": 0,
                                          "missing": 0, "unparsed": 1, "not_started": 1})
        # cpuonly ended at 1793238725, before the window: filtered out.
        self.assertEqual([j["jobid"] for j in body["jobs"]], ["9", "2920", "10"])
        argv, env = fake.calls[0]
        self.assertEqual(argv[0], "sacct")
        self.assertIn("-X", argv)
        self.assertEqual(argv[argv.index("--delimiter") + 1], SEP)
        self.assertEqual(argv[argv.index("-S") + 1], "2026-10-29T05:00:00")
        self.assertEqual(env["TZ"], "UTC")

    def test_sacct_failure_is_503(self):
        with patch.object(jw, "run_capped", FakeSacct(exc=jw.SourceError("sacct failed (exit 1)"))):
            r = self.client.get("/api/jobstats/jobs?from=1793000000000&to=1793086400000")
        self.assertEqual(r.status_code, 503)

    def test_unexpected_exception_is_contained(self):
        with patch.object(jw, "run_capped", FakeSacct(exc=RuntimeError("boom"))), \
                self.assertLogs("jobstats_web", "ERROR"):
            r = self.client.get("/api/jobstats/jobs?from=1793000000000&to=1793086400000")
        self.assertEqual(r.status_code, 500)
        self.assertEqual(r.get_json(), {"error": "internal error"})
        self.assertEqual(self.client.get("/usage").status_code, 200)

    def test_page_route(self):
        r = self.client.get("/jobstats")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"<html", r.data.lower())


class ExistingRoutesTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.folder.name, "h.db")
        history.init_db(self.db)
        history.migrate_db(self.db)
        self.old_db = config.HISTORY_DB_PATH
        config.HISTORY_DB_PATH = self.db
        self.client = dashboard_app.app.test_client()

    def tearDown(self):
        config.HISTORY_DB_PATH = self.old_db
        self.folder.cleanup()

    def test_existing_routes_unchanged(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/usage").status_code, 200)
        self.assertEqual(self.client.get("/api/jobs").status_code, 400)
        with patch.object(dashboard_app, "get_partitions", return_value={}):
            self.assertEqual(self.client.get("/api/jobs?from=1793000000000&to=1793086400000").status_code, 200)


class SecurityTests(RouteTestBase):
    def test_html_in_name_is_returned_as_json_text(self):
        evil = "<img src=x onerror=alert(1)>"
        with patch.object(jw, "run_capped", FakeSacct(sacct_line(jobid="7", name=evil) + "\n")):
            r = self.client.get("/api/jobstats/job/7?submit=1793000000")
        self.assertEqual(r.mimetype, "application/json")
        self.assertEqual(r.get_json()["job"]["name"], evil)

    def test_subprocess_gets_argv_list_without_shell(self):
        with patch.object(jw.subprocess, "Popen", side_effect=OSError("no sacct")) as popen:
            with self.assertRaises(jw.SourceError):
                jw.run_capped(["sacct", "-j", "1;rm -rf /"], dict(os.environ), timeout=1, max_bytes=10)
        args, kwargs = popen.call_args
        self.assertIsInstance(args[0], list)
        self.assertFalse(kwargs.get("shell", False))

    def test_import_runs_no_subprocess(self):
        import importlib
        with patch("subprocess.Popen") as popen, patch("subprocess.run") as run:
            importlib.reload(jw)
        popen.assert_not_called()
        run.assert_not_called()


class HardeningTests(RouteTestBase):
    """Codex M2 review findings (reviews/2026-11-06-codex-m2-code-review.md)."""

    def payload(self, **node_overrides):
        p = json.loads(json.dumps(SYNTH))
        p["nodes"]["n1"].update(node_overrides)
        return p

    def test_scalar_field_sent_as_dict_is_null(self):
        row, _ = row_from(synth_line(self.payload(cpus={"0": 1}, total_memory={"0": 100})))
        self.assertEqual(row["summary_state"], "ok")
        self.assertIsNone(row["cpu_eff"])
        self.assertIsNone(row["cpu_mem_peak_pct"])
        self.assertAlmostEqual(row["gpu_pct"], 20)          # GPU fields unaffected

    def test_gpu_field_sent_as_scalar_is_ignored(self):
        row, _ = row_from(synth_line(self.payload(sm_util_percent_avg=0.5)))
        self.assertEqual(row["summary_state"], "ok")
        self.assertIsNone(row["sm"])

    def test_huge_integer_is_null_not_an_error(self):
        row, _ = row_from(synth_line(self.payload(gpu_utilization={"0": 10 ** 400, "1": 30})))
        self.assertEqual(row["summary_state"], "ok")
        self.assertAlmostEqual(row["gpu_pct"], 30)
        self.assertEqual(jw._num(10 ** 400), None)

    def test_overflowing_mean_is_finite_or_null(self):
        row, _ = row_from(synth_line(self.payload(gpu_utilization={"0": 1e308, "1": 1e308},
                                                  sm_util_percent_avg={"0": 1e308})))
        self.assertEqual(row["gpu_pct"], 1e308)              # mean of two 1e308 is 1e308, not inf
        self.assertIsNone(row["sm"])                         # 1e308 x 100 does not fit: null

    def test_response_never_contains_non_finite_numbers(self):
        line = synth_line(self.payload(gpu_utilization={"0": 1e308, "1": 1e308},
                                       sm_util_percent_avg={"0": 1e308}, total_time=1e308, cpus=1e-300),
                          start="1793290000", end="1793290100")
        with patch.object(jw, "run_capped", FakeSacct(line + "\n")):
            r = self.client.get("/api/jobstats/jobs?from=1793250000000&to=1793400000000")
        self.assertEqual(r.status_code, 200)
        text = r.get_data(as_text=True)
        self.assertNotIn("Infinity", text)
        self.assertNotIn("NaN", text)
        json.loads(text, parse_constant=lambda c: self.fail("non-finite " + c))

    def test_negative_memory_totals_are_null(self):
        row, _ = row_from(synth_line(self.payload(total_memory=-100,
                                                  gpu_total_memory={"0": -100, "1": 200})))
        self.assertIsNone(row["cpu_mem_peak_pct"])
        self.assertAlmostEqual(row["gpu_mem_peak_pct"], 30)  # GPU 0 skipped, GPU 1 60/200

    def test_pending_with_estimated_start_is_not_started(self):
        row, _ = row_from(sacct_line(state="PENDING", submit="1793000000", start="1793500000",
                                     end="Unknown", elapsed="0"))
        self.assertTrue(row["not_started"])
        self.assertEqual(row["gpu_hours"], 0)
        # still listed in a window that ends before the estimated start
        kept = jw.filter_window([row], 1793000000, 1793100000, 1793050000)
        self.assertEqual(len(kept), 1)

    def test_timeout_kills_children_holding_stdout(self):
        t0 = time.monotonic()
        with self.assertRaises(jw.SourceError) as ctx:
            jw.run_capped(["sh", "-c", "sleep 20 & sleep 20"], dict(os.environ), timeout=0.3, max_bytes=1000)
        self.assertEqual(ctx.exception.message, "sacct timed out")
        self.assertLess(time.monotonic() - t0, 5)

    def test_result_larger_than_cache_budget_is_not_cached(self):
        fake = FakeSacct(fixture_line("gpu8") + "\n")
        with patch.object(jw, "run_capped", fake), patch.object(jw, "CACHE_BYTES", 100):
            for _ in range(2):
                self.assertEqual(self.client.get("/api/jobstats/jobs?from=1793200000000&to=1793400000000").status_code, 200)
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(len(jw._explorer_cache), 0)


class TimeseriesRouteTests(RouteTestBase):
    """GET /api/jobstats/job/<jobid>/timeseries. sacct and Prometheus are both faked."""

    def setUp(self):
        super().setUp()
        self._url = config.PROMETHEUS_URL
        config.PROMETHEUS_URL = "http://prom"
        self.calls = []
        self.raise_exc = None

    def tearDown(self):
        config.PROMETHEUS_URL = self._url

    def fake_fetch(self, url, raw_id, start, end, step_s, agg, selector="", deadline_s=60):
        self.calls.append(dict(url=url, raw_id=raw_id, start=start, end=end, step_s=step_s, agg=agg))
        if self.raise_exc:
            raise self.raise_exc
        return {"step_s": step_s, "agg": agg, "start": start, "end": end, "gpus": [], "charts": {}}

    def get(self, line, path):
        with patch.object(jw, "run_capped", FakeSacct(line + "\n")), \
                patch.object(jw.jobstats_prom, "fetch_job_series", self.fake_fetch):
            return self.client.get(path)

    def test_array_task_uses_raw_id(self):
        line = sacct_line(jobid="2645_1", jobid_raw="385855", submit="1793000000")
        r = self.get(line, "/api/jobstats/job/2645_1/timeseries?submit=1793000000&step=60&agg=avg")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.calls, [dict(url="http://prom", raw_id="385855", start=1793000100,
                                           end=1793003700, step_s=60, agg="avg")])

    def test_running_job_ends_now_and_is_not_cached(self):
        line = sacct_line(jobid="77", state="RUNNING", end="Unknown")
        with patch.object(jw.time, "time", lambda: 1793010000.5):
            for _ in range(2):
                r = self.get(line, "/api/jobstats/job/77/timeseries?submit=1793000000&step=60&agg=max")
                self.assertEqual(r.status_code, 200)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[0]["end"], 1793010000)
        self.assertEqual(self.calls[0]["start"], 1793000100)

    def test_finished_job_is_cached(self):
        line = sacct_line(jobid="78")
        for agg in ("avg", "avg", "max"):
            r = self.get(line, f"/api/jobstats/job/78/timeseries?submit=1793000000&step=120&agg={agg}")
            self.assertEqual(r.status_code, 200)
        self.assertEqual([c["agg"] for c in self.calls], ["avg", "max"])

    def test_auto_and_coarsening_reported(self):
        line = sacct_line(jobid="79", start="1793000100", end=str(1793000100 + 7 * 86400))
        body = self.get(line, "/api/jobstats/job/79/timeseries?submit=1793000000&step=30&agg=max").get_json()
        self.assertEqual(self.calls[-1]["step_s"], 300)
        self.assertEqual((body["step_s"], body["coarsened"], body["requested_step"]), (300, True, 30))
        body = self.get(line, "/api/jobstats/job/79/timeseries?submit=1793000000&step=auto&agg=max").get_json()
        self.assertEqual(self.calls[-1]["agg"], "avg")
        self.assertEqual((body["coarsened"], body["requested_step"], body["agg"]), (False, None, "avg"))
        self.assertEqual(self.calls[-1]["step_s"], 600)       # 7 d at <= 1500 points

    def test_many_gpus_coarsen_and_report_budget(self):
        tres = "billing=448,cpu=128,gres/gpu:h100=16,gres/gpu=16,mem=400G,node=2"
        line = sacct_line(jobid="90", tres=tres, start="1793000100", end=str(1793000100 + 2 * 86400))
        body = self.get(line, "/api/jobstats/job/90/timeseries?submit=1793000000&step=30&agg=avg").get_json()
        self.assertEqual(self.calls[-1]["step_s"], 120)
        self.assertEqual((body["coarsened"], body["max_points"]), (True, 2500))

    def test_large_body_not_cached_and_cache_bytes_bounded(self):
        big = {"pad": "x" * 5000}
        line = sacct_line(jobid="91")
        orig = self.fake_fetch
        self.fake_fetch = lambda *a, **k: dict(orig(*a, **k), **big)
        with patch.object(jw, "TS_CACHE_BYTES", 8000):
            for _ in range(2):
                self.get(line, "/api/jobstats/job/91/timeseries?submit=1793000000&step=60&agg=avg")
        self.assertEqual(len(self.calls), 2)          # > a quarter of the budget: never cached
        self.fake_fetch = orig
        with patch.object(jw, "TS_CACHE_BYTES", 8000):
            for agg in ("avg", "max", "avg"):
                self.get(line, f"/api/jobstats/job/91/timeseries?submit=1793000000&step=120&agg={agg}")
            self.assertLessEqual(sum(len(v) for v in jw._ts_cache.values()), 8000)
        self.assertEqual(len(self.calls), 4)          # small bodies are cached

    def test_bad_params(self):
        line = sacct_line(jobid="81")
        for q in ("81/timeseries?submit=1793000000&step=45&agg=avg",
                  "81/timeseries?submit=1793000000&step=&agg=avg",
                  "81/timeseries?submit=1793000000&step=60&agg=min",
                  "81/timeseries?step=60&agg=avg",
                  "12;ls/timeseries?submit=1793000000&step=60&agg=avg"):
            r = self.get(line, "/api/jobstats/job/" + q)
            self.assertEqual(r.status_code, 400, q)
        self.assertEqual(self.calls, [])

    def test_not_started_409_and_not_found_404(self):
        line = sacct_line(jobid="82", state="PENDING", start="Unknown", end="Unknown", elapsed="0")
        r = self.get(line, "/api/jobstats/job/82/timeseries?submit=1793000000&step=60&agg=avg")
        self.assertEqual(r.status_code, 409)
        r = self.get(line, "/api/jobstats/job/83/timeseries?submit=1793000000&step=60&agg=avg")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self.calls, [])

    def test_feature_off(self):
        config.PROMETHEUS_URL = ""
        line = sacct_line(jobid="84")
        r = self.get(line, "/api/jobstats/job/84/timeseries?submit=1793000000&step=60&agg=avg")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self.calls, [])
        body = self.get(line, "/api/jobstats/job/84?submit=1793000000").get_json()
        self.assertIs(body["timeseries"], False)

    def test_prom_error_503(self):
        line = sacct_line(jobid="85")
        self.raise_exc = jw.jobstats_prom.PromError("Prometheus unreachable: refused")
        r = self.get(line, "/api/jobstats/job/85/timeseries?submit=1793000000&step=60&agg=avg")
        self.assertEqual(r.status_code, 503)
        self.assertIn("unreachable", r.get_json()["error"])
        self.assertNotIn("Retry-After", r.headers)
        self.raise_exc = jw.jobstats_prom.PromError("busy", busy=True)
        r = self.get(line, "/api/jobstats/job/85/timeseries?submit=1793000000&step=60&agg=avg")
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.headers["Retry-After"], "2")

    def test_detail_has_timeseries_flag(self):
        body = self.get(sacct_line(jobid="86"), "/api/jobstats/job/86?submit=1793000000").get_json()
        self.assertIs(body["timeseries"], True)
        self.assertEqual(self.calls, [])



if __name__ == "__main__":
    unittest.main()
