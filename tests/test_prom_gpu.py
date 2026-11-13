import os
import unittest
import urllib.parse
from unittest import mock

os.environ.setdefault("SLURM_DASHBOARD_DISABLE_RECORDER", "1")
import prom_gpu  # noqa: E402

GIB = 1024 ** 3
LABELS = {"jobid": "385855", "instance": "gpu03:9445", "uuid": "GPU-aaa", "minor_number": "2"}
NOW_MS = 1_800_000_000_000


def vec(value, labels=LABELS):
    return [{"metric": dict(labels), "value": [NOW_MS / 1000, str(value)]}]


def fake_prometheus(answers, calls):
    """answers: {(function, metric_name): value or None}; None = empty vector."""
    def get(path):
        calls.append(path)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)["query"][0]
        fn = query.split("(", 1)[0]
        name = next((n for (_, n) in answers if n in query), None)
        value = answers.get((fn, name))
        return [] if value is None else vec(value)
    return get


def full_answers():
    a = {}
    for fn, duty, used in (("count_over_time", 120, 120), ("avg_over_time", 50.0, 40 * GIB),
                           ("stddev_over_time", 10.0, 2 * GIB), ("max_over_time", 100.0, 60 * GIB),
                           ("last_over_time", 75.0, 41 * GIB)):
        a[(fn, "nvidia_gpu_duty_cycle")] = duty
        a[(fn, "nvidia_gpu_memory_used_bytes")] = used
    a[("max_over_time", "nvidia_gpu_memory_total_bytes")] = 80 * GIB
    a[("min_over_time", "nvidia_gpu_duty_cycle")] = (NOW_MS - 3_600_000) / 1000
    a[("max_over_time", "nvidia_gpu_duty_cycle")] = 100.0   # overridden below for timestamp()
    return a


def running_job(**kw):
    job = {"job_id": "382796_9", "raw_job_id": "385855", "state": "RUNNING", "req_gpus": 1,
           "start_time": NOW_MS - 3_600_000, "end_time": None,
           "allocations": [{"node": "gpu03", "gpus": 1}]}
    job.update(kw)
    return job


class PromGpuTests(unittest.TestCase):
    def setUp(self):
        prom_gpu._final_cache.clear()
        self.calls = []

    def run_metrics(self, answers, jobs):
        def get(path):
            self.calls.append(path)
            query = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)["query"][0]
            if query.startswith("max_over_time(timestamp("):
                return vec((NOW_MS - 10_000) / 1000)
            return fake_prometheus(answers, [])(path)
        with mock.patch.object(prom_gpu, "_get", side_effect=get):
            return prom_gpu.get_gpu_metrics(jobs, NOW_MS)

    def test_summary_shape_and_vram_derivation(self):
        job = self.run_metrics(full_answers(), [running_job()])[0]
        t = job["gpu_telemetry"]
        self.assertAlmostEqual(t["lifetime"]["gpu"]["mean"], 50.0)
        self.assertAlmostEqual(t["lifetime"]["gpu"]["std"], 10.0)      # survives the m2 round trip
        self.assertAlmostEqual(t["lifetime"]["vram"]["mean"], 50.0)     # 40 GiB of 80 GiB
        self.assertAlmostEqual(t["lifetime"]["vram"]["std"], 2.5)       # 2 GiB of 80 GiB
        self.assertAlmostEqual(t["lifetime"]["vram"]["peak"], 75.0)
        self.assertIsNone(t["lifetime"]["memory"]["mean"])              # unknown, not 0
        self.assertEqual(t["fresh_gpus"], 1)
        self.assertAlmostEqual(t["current"]["gpu"], 75.0)
        self.assertEqual(t["devices"][0]["minor"], 2)
        self.assertEqual(t["devices"][0]["node"], "gpu03")
        self.assertTrue(all('jobid=~"385855"' in urllib.parse.unquote_plus(c) for c in self.calls
                            if "jobid" in urllib.parse.unquote_plus(c)))

    def test_partial_answer_is_unobserved_not_a_crash(self):
        answers = full_answers()
        del answers[("avg_over_time", "nvidia_gpu_duty_cycle")]
        job = self.run_metrics(answers, [running_job()])[0]
        self.assertEqual(job["gpu_telemetry"]["lifetime"]["gpu"]["n"], 0)
        self.assertIsNone(job["gpu_telemetry"]["lifetime"]["gpu"]["mean"])

    def test_array_task_without_raw_id_is_never_queried(self):
        job = running_job(raw_job_id=None)
        self.run_metrics(full_answers(), [job])
        self.assertNotIn("gpu_telemetry", job)
        self.assertEqual(self.calls, [])

    def test_finished_job_is_served_from_cache(self):
        done = dict(state="COMPLETED", end_time=NOW_MS - 600_000)
        self.run_metrics(full_answers(), [running_job(**done)])
        first = len(self.calls)
        self.assertGreater(first, 0)
        job = self.run_metrics(full_answers(), [running_job(**done)])[0]
        self.assertEqual(len(self.calls), first)
        self.assertIn("gpu_telemetry", job)
        self.assertIsNone(job["gpu_telemetry"]["current"]["gpu"])       # not running: no "now"

    def test_cpu_only_and_pending_jobs_are_skipped(self):
        jobs = [running_job(req_gpus=0), running_job(state="PENDING", start_time=None)]
        self.run_metrics(full_answers(), jobs)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
