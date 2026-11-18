"""jobstats_prom: PromQL building, step choice, fetching and shaping. Prometheus is faked; no network."""
import io
import json
import os
import socket
import sys
import unittest
import urllib.error
import urllib.parse
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import jobstats_prom as jp  # noqa: E402


class QueryTests(unittest.TestCase):
    def test_choose_step_explicit_fits(self):
        self.assertEqual(jp.choose_step(12 * 3600, 30), (30, False))      # 1441 points

    def test_choose_step_coarsens_long_job(self):
        self.assertEqual(jp.choose_step(7 * 86400, 30), (300, True))      # 20161 -> 10081 -> 5041 -> 2017

    def test_choose_step_explicit_coarse_not_flagged(self):
        self.assertEqual(jp.choose_step(7 * 86400, 300), (300, False))

    def test_choose_step_auto(self):
        self.assertEqual(jp.choose_step(12 * 3600, None), (30, False))    # 1441 points <= 1500
        self.assertEqual(jp.choose_step(13 * 3600, None), (60, False))    # 30 s would be 1561

    def test_choose_step_auto_retention_limit(self):
        self.assertEqual(jp.choose_step(180 * 86400, None), (3600, False))  # 4321 points

    def test_gpu_queries(self):
        q = jp.build_queries("385855", 30, "avg")
        self.assertEqual(q["duty"], 'nvidia_gpu_duty_cycle{jobid="385855"}')
        q = jp.build_queries("385855", 120, "max", 'cluster="slurm"')
        self.assertEqual(q["sm"], 'max_over_time(nvidia_gpu_sm_util_percent{jobid="385855",cluster="slurm"}[120s])')
        self.assertEqual(jp.build_queries("1", 300, "avg")["power"],
                         'avg_over_time(nvidia_gpu_power_usage_milliwatts{jobid="1"}[300s])')

    def test_cpu_queries(self):
        c = 'cgroup_cpu_total_seconds{jobid="1",step="",task=""}'
        self.assertEqual(jp.build_queries("1", 30, "max")["cpu"], f"sum(rate({c}[1m]))")
        self.assertEqual(jp.build_queries("1", 120, "avg")["cpu"], f"sum(rate({c}[120s]))")
        self.assertEqual(jp.build_queries("1", 120, "max")["cpu"], f"max_over_time(sum(rate({c}[1m]))[120s:30s])")

    def test_ram_queries(self):
        r = 'cgroup_memory_rss_bytes{jobid="1",step="",task=""}'
        s = 'cgroup_memory_shmem_bytes{jobid="1",step="",task=""}'
        m = f"sum(({r} + {s}) or {r})"
        self.assertEqual(jp.build_queries("1", 30, "avg")["ram"], m)
        self.assertEqual(jp.build_queries("1", 60, "avg")["ram"], f"avg_over_time({m}[60s:30s])")
        self.assertEqual(jp.build_queries("1", 60, "max")["ram"], f"max_over_time({m}[60s:30s])")

    def test_alloc_queries(self):
        q = jp.build_queries("1", 60, "avg")
        self.assertEqual(q["cpu_alloc"], 'sum(cgroup_cpus{jobid="1",step="",task=""})')
        self.assertEqual(q["ram_alloc"], 'sum(cgroup_memory_total_bytes{jobid="1",step="",task=""})')
        self.assertEqual(q["gpu_mem_total"], 'max(nvidia_gpu_memory_total_bytes{jobid="1"})')

    def test_rejects_bad_input(self):
        for args in (("1;x", 30, "avg"), ("1", 45, "avg"), ("1", 60, "min"), ("2645_1", 60, "avg")):
            with self.assertRaises(ValueError):
                jp.build_queries(*args)

    def test_chart_order(self):
        self.assertEqual([c["name"] for c in jp.CHARTS],
                         ["duty", "sm", "tensor", "dram", "gpu_mem", "power", "cpu", "ram"])


T0 = 1800000000


def _gpu(host, minor, values, t0=T0):
    return {"metric": {"instance": f"{host}:9445", "minor_number": minor, "jobid": "1"},
            "values": [[t0 + 30 * i, v] for i, v in enumerate(values)]}


def _job(values, t0=T0):
    return {"metric": {}, "values": [[t0 + 30 * i, v] for i, v in enumerate(values)]}


class FakeProm:
    """urlopen stand-in: the first substring found in the PromQL picks the canned result list."""

    def __init__(self, results=None, raw=None, exc=None):
        self.results = results or {}
        self.raw = raw
        self.exc = exc
        self.requests = []

    def __call__(self, req, timeout=None):
        form = dict(urllib.parse.parse_qsl(req.data.decode()))
        self.requests.append({"url": req.full_url, "method": req.get_method(), "form": form,
                              "timeout": timeout})
        if self.exc:
            raise self.exc
        if self.raw is not None:
            body = self.raw
        else:
            result = []
            for sub, res in self.results.items():
                if sub in form["query"]:
                    result = res
                    break
            body = json.dumps({"status": "success", "data": {"resultType": "matrix", "result": result}})
        resp = io.BytesIO(body.encode())
        resp.status = 200
        return resp


class FetchTests(unittest.TestCase):
    def fetch(self, fake=None, **kw):
        self.fake = fake or FakeProm({})
        args = dict(url="http://prom", raw_id="1", start=T0, end=T0 + 3600, step_s=30, agg="avg")
        args.update(kw)
        with mock.patch.object(jp.urllib.request, "urlopen", self.fake):
            return jp.fetch_job_series(**args)

    def test_multi_node_same_minor_stays_separate(self):
        out = self.fetch(FakeProm({"nvidia_gpu_duty_cycle": [_gpu("gpu08", "0", ["100"]),
                                                             _gpu("gpu07", "0", ["50"])]}))
        self.assertEqual([g["key"] for g in out["gpus"]], ["gpu07/0", "gpu08/0"])
        self.assertEqual(out["gpus"][0], {"key": "gpu07/0", "host": "gpu07", "minor": "0"})
        self.assertEqual(out["charts"]["duty"]["series"]["gpu07/0"], [[T0, 50.0]])
        self.assertEqual(out["charts"]["duty"]["series"]["gpu08/0"], [[T0, 100.0]])

    def test_units(self):
        out = self.fetch(FakeProm({
            "nvidia_gpu_sm_util_percent": [_gpu("gpu08", "6", ["0.9223"])],
            "nvidia_gpu_memory_used_bytes": [_gpu("gpu08", "6", ["41000000000"])],
            "nvidia_gpu_power_usage_milliwatts": [_gpu("gpu08", "6", ["512345"])],
            "cgroup_memory_rss_bytes": [_job(["250000000000"])],
            "cgroup_cpu_total_seconds": [_job(["12.3456"])],
        }))
        c = out["charts"]
        self.assertEqual(c["sm"]["series"]["gpu08/6"], [[T0, 92.2]])
        self.assertEqual(c["gpu_mem"]["series"]["gpu08/6"], [[T0, 41.0]])
        self.assertEqual(c["power"]["series"]["gpu08/6"], [[T0, 512]])
        self.assertEqual(c["ram"]["series"]["job"], [[T0, 250.0]])
        self.assertEqual(c["cpu"]["series"]["job"], [[T0, 12.35]])
        self.assertEqual(c["power"]["unit"], "W")

    def test_minor_sorted_numerically(self):
        out = self.fetch(FakeProm({"nvidia_gpu_duty_cycle": [_gpu("gpu01", "10", ["1"]),
                                                             _gpu("gpu01", "2", ["1"])]}))
        self.assertEqual([g["key"] for g in out["gpus"]], ["gpu01/2", "gpu01/10"])

    def test_ymax_and_allocated(self):
        out = self.fetch(FakeProm({
            "nvidia_gpu_memory_total_bytes": [_job(["85899345920"])],
            "cgroup_cpus": [_job(["64", "64"])],
            "cgroup_memory_total_bytes": [_job(["200000000000"])],
        }))
        c = out["charts"]
        self.assertEqual(c["gpu_mem"]["ymax"], 85.9)
        self.assertEqual(c["cpu"]["allocated"], 64)
        self.assertEqual(c["cpu"]["ymax"], 73.6)
        self.assertEqual(c["ram"]["allocated"], 200.0)
        self.assertEqual(c["ram"]["ymax"], 230.0)
        for name in ("duty", "sm", "tensor", "dram"):
            self.assertEqual(c[name]["ymax"], 100)
        self.assertEqual(c["power"]["ymax"], 1000)
        self.assertIsNone(c["duty"]["allocated"])

    def test_missing_metric_is_empty_not_zero(self):
        out = self.fetch(FakeProm({"nvidia_gpu_duty_cycle": [_gpu("gpu08", "0", ["5"])]}))
        c = out["charts"]
        self.assertEqual(c["tensor"]["series"], {})
        self.assertEqual(c["cpu"]["series"], {})
        self.assertIsNone(c["ram"]["allocated"])
        self.assertIsNone(c["ram"]["ymax"])
        self.assertIsNone(c["gpu_mem"]["ymax"])

    def test_nan_values_dropped(self):
        out = self.fetch(FakeProm({"nvidia_gpu_duty_cycle": [_gpu("gpu08", "0", ["5", "NaN", "+Inf", "7"])]}))
        self.assertEqual(out["charts"]["duty"]["series"]["gpu08/0"], [[T0, 5.0], [T0 + 90, 7.0]])

    def test_envelope(self):
        out = self.fetch(step_s=120, agg="max")
        self.assertEqual((out["step_s"], out["agg"], out["start"], out["end"]), (120, "max", T0, T0 + 3600))
        self.assertEqual(list(out["charts"]), [c["name"] for c in jp.CHARTS])

    def test_prometheus_error_status(self):
        with self.assertRaises(jp.PromError) as cm:
            self.fetch(FakeProm(raw=json.dumps({"status": "error", "error": "bad"})))
        self.assertIn("bad", cm.exception.message)
        self.assertFalse(cm.exception.busy)

    def test_non_json_body(self):
        with self.assertRaises(jp.PromError):
            self.fetch(FakeProm(raw="<html>proxy error</html>"))

    def test_oversized_body(self):
        with mock.patch.object(jp, "READ_MAX", 10):
            with self.assertRaises(jp.PromError) as cm:
                self.fetch(FakeProm({"nvidia_gpu_duty_cycle": [_gpu("gpu08", "0", ["5"])]}))
        self.assertIn("too large", cm.exception.message)

    def test_timeout_and_connection_error(self):
        for exc in (socket.timeout("timed out"), urllib.error.URLError("refused"),
                    urllib.error.HTTPError("http://prom", 400, "Bad Request", {}, io.BytesIO(b"{}"))):
            with self.assertRaises(jp.PromError):
                self.fetch(FakeProm(exc=exc))
        # the slot is released after a failure
        self.assertTrue(jp._slot.acquire(blocking=False))
        jp._slot.release()

    def test_busy_slot(self):
        jp._slot.acquire()
        try:
            with self.assertRaises(jp.PromError) as cm:
                self.fetch()
            self.assertTrue(cm.exception.busy)
            self.assertEqual(self.fake.requests, [])
        finally:
            jp._slot.release()

    def test_deadline(self):
        clock = iter([0, 0, 61] + [61] * 50)
        with mock.patch.object(jp.time, "monotonic", lambda: next(clock)):
            with self.assertRaises(jp.PromError) as cm:
                self.fetch()
        self.assertIn("timed out", cm.exception.message)
        self.assertEqual(len(self.fake.requests), 1)

    def test_post_body(self):
        self.fetch()
        first = self.fake.requests[0]
        self.assertEqual(first["url"], "http://prom/api/v1/query_range")
        self.assertEqual(first["method"], "POST")
        self.assertEqual(first["form"], {"query": jp.build_queries("1", 30, "avg")["duty"],
                                         "start": str(T0), "end": str(T0 + 3600), "step": "30"})
        self.assertLessEqual(first["timeout"], jp.QUERY_TIMEOUT_S)
        self.assertEqual(len(self.fake.requests), len(jp.build_queries("1", 30, "avg")))

    def test_selector_passed_through(self):
        self.fetch(selector='cluster="slurm"')
        self.assertIn('cluster="slurm"', self.fake.requests[0]["form"]["query"])


if __name__ == "__main__":
    unittest.main()
