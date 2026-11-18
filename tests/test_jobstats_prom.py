"""jobstats_prom: PromQL building, step choice, fetching and shaping. Prometheus is faked; no network."""
import os
import sys
import unittest

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


if __name__ == "__main__":
    unittest.main()
