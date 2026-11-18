# Job Page Time Series Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** On the jobstats job page, a button draws per-GPU and whole-job time series for that job from Prometheus at a chosen resolution and aggregation.

**Architecture:** A new module `jobstats_prom.py` builds PromQL, calls Prometheus `query_range` and shapes the result. A new route in the existing `jobstats_web.py` Blueprint looks the job up through the existing sacct detail path and hands start, end and raw id to that module. In the browser, pure helpers go in `static/jobstats-core.js` (tested under Node), SVG chart drawing in a new `static/jobstats-charts.js`, and `static/jobstats.js` adds the control bar and wires it to the job page.

**Tech Stack:** Python 3 / Flask, `urllib.request` (no new dependency), vanilla JS with hand-built SVG, `unittest`, Node 18 for JS tests.

**Spec:** `docs/superpowers/specs/2026-11-18-job-timeseries-design.md` (approved 2026-11-18). Read it before any task.

## Global Constraints

- Branch: work in a worktree of `feature/jobstats-page`, path `/home/adovlatyan/Slurm-Monitoring-ts`, created from `origin/feature/jobstats-page` (at `abacb8c`). All git commands run as `runuser -u adovlatyan -- git ...`. Commit locally; **do not push** (Aram asks for pushes).
- Tests run from the worktree with the main checkout's venv: `runuser -u adovlatyan -- /home/adovlatyan/Slurm-Monitoring/.venv/bin/python -m unittest discover -s tests -q`. pytest is not installed. Node is `/usr/bin/node` v18.
- No new Python or JS dependency. No charting library.
- Config: `SLURM_DASHBOARD_PROMETHEUS_URL` (default `""` = feature off), `SLURM_DASHBOARD_PROMETHEUS_SELECTOR` (default `""`, extra label matchers such as `cluster="slurm"`).
- Prometheus labels are the jobstats exporters' (`nvml_exporter`, `cgroup_exporter`), 30 s scrape, on both clusters: GPU series `jobid` (raw Slurm id), `instance` = `host:port`, `minor_number`; cgroup series filtered `step="",task=""`.
- Resolutions offered: 30 s, 1 min, 2 min, 5 min, auto. Aggregation `avg` | `max`, ignored at 30 s. Auto picks the smallest of 30, 60, 120, 300, 600, 1800, 3600 s keeping a line ≤ 1,500 points, with `avg`.
- Server point cap: if `(end - start) / step + 1 > 5000`, move to the next coarser step in that ladder and report `coarsened: true`.
- Per-query HTTP timeout 10 s; per-request deadline 60 s; one time-series request at a time (busy = 503 with `Retry-After: 2`); response read cap 32 MB.
- Chart order and titles: GPU duty, SM activity, Tensor activity, DRAM bandwidth, GPU memory, GPU power, CPU cores in use, RAM. Y-axes: % charts 0-100, power 0-1000 W, GPU memory 0 to the GPU total (GB), CPU and RAM 0 to 1.15 x the allocation.
- No Prometheus query when a job page opens. Only the "Generate time-series plots" button queries.
- Missing data is an empty series, never zeros; the chart then reads "No data for this job".
- Bump every `?v=` tag in `static/jobstats.html` to `m5-ts`.

## Review Focus

1. **Array task** (`2645_1`): Prometheus must be queried with the raw id (`JobIDRaw`), never `2645` or `2645_1`. Test in Task 3.
2. **Running job**: range ends at now, result not cached, page message says the summary is written when the job ends. Tests in Tasks 3 and 5.
3. **Prometheus down, slow or returning `status: error`**: the route answers 503 with a readable message and the job page stays intact. Tests in Tasks 2 and 3.
4. **Very long job at a fine resolution** (7 d at 30 s = 20,161 points): the step is coarsened and the page states the step actually used. Tests in Tasks 1 and 4.
5. **Multi-node job with the same `minor_number` on two hosts**: lines stay separate (`gpu07/0` and `gpu08/0`), never merged. Test in Task 2.

---

### Task 1: Query building and step choice

**Files:**
- Create: `jobstats_prom.py`
- Modify: `config.py` (two settings after `DISABLE_RECORDER`)
- Create: `tests/test_jobstats_prom.py`
- Add (copy from `/home/adovlatyan/Slurm-Monitoring/docs/superpowers/`): `docs/superpowers/specs/2026-11-18-job-timeseries-design.md`, `docs/superpowers/plans/2026-11-18-job-timeseries.md`

**Interfaces:**
- Produces:
  - `config.PROMETHEUS_URL: str`, `config.PROMETHEUS_SELECTOR: str`
  - `jobstats_prom.STEPS = (30, 60, 120, 300)`, `LADDER = (30, 60, 120, 300, 600, 1800, 3600)`, `AUTO_POINTS = 1500`, `MAX_POINTS = 5000`
  - `choose_step(span_s: int, requested: int | None) -> tuple[int, bool]`: `requested=None` means auto; returns `(step_s, coarsened)`. `coarsened` is True only when an explicit request was raised.
  - `CHARTS`: ordered tuple of dicts `{name, title, unit, kind: "gpu"|"job", scale}` for `duty, sm, tensor, dram, gpu_mem, power, cpu, ram`. Scales: duty 1, sm/tensor/dram 100, gpu_mem 1e-9, power 1e-3, cpu 1, ram 1e-9.
  - `build_queries(raw_id: str, step_s: int, agg: str, selector: str = "") -> dict[str, str]`: one PromQL per chart name plus `cpu_alloc`, `ram_alloc`, `gpu_mem_total`.

- [ ] **Step 1: Create the worktree and add the spec and plan**

```bash
cd /home/adovlatyan/Slurm-Monitoring
runuser -u adovlatyan -- git fetch https://github.com/aramdov/Slurm-Monitoring.git "+refs/heads/*:refs/remotes/origin/*"
runuser -u adovlatyan -- git worktree add -b feature/jobstats-timeseries /home/adovlatyan/Slurm-Monitoring-ts origin/feature/jobstats-page
mkdir -p /home/adovlatyan/Slurm-Monitoring-ts/docs/superpowers/{specs,plans}
cp docs/superpowers/specs/2026-11-18-job-timeseries-design.md /home/adovlatyan/Slurm-Monitoring-ts/docs/superpowers/specs/
cp docs/superpowers/plans/2026-11-18-job-timeseries.md /home/adovlatyan/Slurm-Monitoring-ts/docs/superpowers/plans/
chown -R adovlatyan:adovlatyan /home/adovlatyan/Slurm-Monitoring-ts
```

Expected: `git -C /home/adovlatyan/Slurm-Monitoring-ts log --oneline -1` shows the tip of `origin/feature/jobstats-page`. If that tip is newer than `abacb8c`, read its commits before going on.

- [ ] **Step 2: Write the failing tests** in `tests/test_jobstats_prom.py` (class `QueryTests`):

```python
def test_choose_step_explicit_fits(self):
    self.assertEqual(jp.choose_step(12 * 3600, 30), (30, False))      # 1441 points
def test_choose_step_coarsens_long_job(self):
    self.assertEqual(jp.choose_step(7 * 86400, 30), (300, True))      # 20161 -> 10081 -> 5041 -> 2017
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
    for args in (("1;x", 30, "avg"), ("1", 45, "avg"), ("1", 60, "min")):
        with self.assertRaises(ValueError):
            jp.build_queries(*args)
def test_chart_order(self):
    self.assertEqual([c["name"] for c in jp.CHARTS],
                     ["duty", "sm", "tensor", "dram", "gpu_mem", "power", "cpu", "ram"])
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `cd /home/adovlatyan/Slurm-Monitoring-ts && runuser -u adovlatyan -- /home/adovlatyan/Slurm-Monitoring/.venv/bin/python -m unittest tests.test_jobstats_prom -q`
Expected: ImportError, `No module named 'jobstats_prom'`.

- [ ] **Step 4: Implement** `config.PROMETHEUS_URL`/`PROMETHEUS_SELECTOR` (from env, default `""`, URL `rstrip("/")`), plus `STEPS`, `LADDER`, `CHARTS`, `choose_step` and `build_queries` in `jobstats_prom.py`. Module docstring: the module has no import-time I/O. `build_queries` validates `raw_id` against `^[0-9]{1,10}$`, `step_s` against `LADDER` and `agg` against `{"avg", "max"}`. It raises `ValueError` on any mismatch.

- [ ] **Step 5: Run the tests to verify they pass**

Run: same as Step 3, then the full suite (`-m unittest discover -s tests -q`).
Expected: all pass. The existing suite is unchanged.

- [ ] **Step 6: Commit**

```bash
runuser -u adovlatyan -- git add config.py jobstats_prom.py tests/test_jobstats_prom.py docs/superpowers
runuser -u adovlatyan -- git commit -m "Add Prometheus query building for job time series"
```

---

### Task 2: Fetching and shaping

**Files:**
- Modify: `jobstats_prom.py`
- Test: `tests/test_jobstats_prom.py` (class `FetchTests`)

**Interfaces:**
- Consumes: `build_queries`, `CHARTS` (Task 1)
- Produces:
  - `class PromError(Exception)` with `.message: str` and `.busy: bool`
  - `query_range(url: str, promql: str, start: int, end: int, step: int, timeout: float) -> list[dict]`: POSTs form data to `{url}/api/v1/query_range` and returns `data.result`. Raises `PromError` on a network error, timeout, non-200, `status != "success"` or a body over 32 MB.
  - `fetch_job_series(url: str, raw_id: str, start: int, end: int, step_s: int, agg: str, selector: str = "", deadline_s: float = 60) -> dict` returns:
    `{"step_s", "agg", "start", "end", "gpus": [{"key": "gpu08/6", "host": "gpu08", "minor": "6"}], "charts": {name: {"unit", "ymax", "allocated", "series": {gpu_key or "job": [[t, v], ...]}}}}`
    - `gpus` is sorted by host, then by minor as an integer.
    - Values are scaled per `CHARTS` and rounded: 1 decimal for %, 2 for GB and cores, 0 for W.
    - `ymax`: 100 for %, 1000 for power, GB of `gpu_mem_total` for gpu_mem (`null` when absent), `1.15 * allocated` for cpu and ram (`null` when no allocation).
    - `allocated`: the scalar from `cpu_alloc` or `ram_alloc` (RAM in GB), else `null`.
    - Non-finite values are dropped. A metric with no result gives `{}` series.
  - `_slot = threading.Semaphore(1)`: `fetch_job_series` acquires it without blocking and raises `PromError("busy", busy=True)` when it is taken.

- [ ] **Step 1: Write the failing tests.** Patch `jobstats_prom.urllib.request.urlopen` with a fake that maps a PromQL substring to a canned `{"status":"success","data":{"result":[...]}}` and records each request body. Tests:

```python
def test_multi_node_same_minor_stays_separate(self):
    # duty: two series, instance gpu07:9445 minor 0 and gpu08:9445 minor 0, values "50" and "100"
    out = self.fetch()
    self.assertEqual([g["key"] for g in out["gpus"]], ["gpu07/0", "gpu08/0"])
    self.assertEqual(out["charts"]["duty"]["series"]["gpu07/0"], [[1800000000, 50.0]])
def test_units(self):
    # sm "0.9223" -> 92.2 ; gpu_mem_used 41e9 -> 41.0 ; power "512345" mW -> 512 ; ram 2.5e11 -> 250.0
def test_minor_sorted_numerically(self):
    # minors "10", "2" on one host -> keys gpu01/2 then gpu01/10
def test_ymax_and_allocated(self):
    # gpu_mem_total 85899345920 -> ymax 85.9 ; cpu_alloc 64 -> allocated 64, ymax 73.6 ; percent charts ymax 100 ; power 1000
def test_missing_metric_is_empty_not_zero(self):
    # tensor query returns []  -> charts["tensor"]["series"] == {} ; ram_alloc [] -> allocated None, ymax None
def test_nan_values_dropped(self):
    # a "NaN" sample is absent from the output list
def test_prometheus_error_status(self):
    # body {"status":"error","error":"bad"} -> PromError, message contains "bad"
def test_timeout_and_connection_error(self):
    # urlopen raises socket.timeout / URLError -> PromError
def test_busy_slot(self):
    jp._slot.acquire()
    try:
        with self.assertRaises(jp.PromError) as cm: self.fetch()
        self.assertTrue(cm.exception.busy)
    finally:
        jp._slot.release()
def test_deadline(self):
    # patch time.monotonic so the deadline has passed after the first query -> PromError("timed out")
def test_post_body(self):
    # first request: method POST, form fields query/start/end/step == build_queries(...)["duty"], start, end, "30"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `... -m unittest tests.test_jobstats_prom -q`
Expected: `AttributeError: module 'jobstats_prom' has no attribute 'fetch_job_series'`.

- [ ] **Step 3: Implement** `PromError`, `query_range` and `fetch_job_series`:
  - Run queries sequentially.
  - Before each query, check the remaining deadline and pass `min(10, remaining)` as the timeout.
  - Release `_slot` in `finally`.
  - Get the host from `instance` with `rsplit(":", 1)[0]`.
  - `cpu_alloc`, `ram_alloc` and `gpu_mem_total` are scalars. Take the last value of their range result.

- [ ] **Step 4: Run the tests to verify they pass**

Expected: all of `tests.test_jobstats_prom` and the full suite pass.

- [ ] **Step 5: Commit** with message `Fetch and shape job time series from Prometheus`.

---

### Task 3: API route

**Files:**
- Modify: `jobstats_web.py`
  - Extract `job_detail(jobid, submit)` from `api_jobstats_job`.
  - Add a `timeseries` flag to the detail body.
  - Add the new route.
- Test: `tests/test_jobstats_web.py` (new class `TimeseriesRouteTests(RouteTestBase)`)

**Interfaces:**
- Consumes: `choose_step`, `fetch_job_series`, `PromError` (Tasks 1-2). Also the existing `FakeSacct`, `sacct_line`, `fixture_line` and `RouteTestBase` in the tests.
- Produces:
  - Detail body (`/api/jobstats/job/<jobid>`) gains `"timeseries": bool(config.PROMETHEUS_URL)`.
  - `GET /api/jobstats/job/<jobid>/timeseries?submit=<s>&step=<30|60|120|300|auto>&agg=<avg|max>` answers with `fetch_job_series`'s dict plus `"coarsened": bool` and `"requested_step": int | null`.
    - 400: bad id, bad submit, bad step or bad agg.
    - 404: no Prometheus URL configured, or job not found.
    - 409: job not started.
    - 503: `PromError`, with `Retry-After: 2` when busy.
  - `_ts_cache` is an `OrderedDict` with 16 entries, key `(jobid, submit, step_s, agg)`. It caches only jobs with an `end`, and `clear_caches()` clears it.

- [ ] **Step 1: Write the failing tests** (patch `jw.run_capped` with `FakeSacct`, patch `jw.jobstats_prom.fetch_job_series` with a recorder, set `config.PROMETHEUS_URL = "http://prom"` in `setUp` and restore it in `tearDown`):

```python
def test_array_task_uses_raw_id(self):
    line = sacct_line(jobid="2645_1", jobid_raw="385855", submit="1793000000")
    # GET /api/jobstats/job/2645_1/timeseries?submit=1793000000&step=60&agg=avg
    # recorder called with raw_id "385855", start 1793000100, end 1793003700, step_s 60, agg "avg"
def test_running_job_ends_now_and_is_not_cached(self):
    # state RUNNING, end "Unknown": end passed == int(time.time()) (patch jw.time.time); two GETs -> two fetch calls
def test_finished_job_is_cached(self):
    # two identical GETs -> one fetch call; different agg -> second call
def test_auto_and_coarsening_reported(self):
    # 7-day job (start 1793000100, end +7*86400), step=30 -> fetch step_s 300, body coarsened True, requested_step 30
    # step=auto -> coarsened False, requested_step None, agg forced "avg"
def test_bad_params(self):
    # step=45, step=, agg=min, missing submit, jobid "12;ls" -> 400, fetch not called
def test_not_started_409_and_not_found_404(self):
def test_feature_off(self):
    # config.PROMETHEUS_URL = "" -> timeseries route 404 ; detail body "timeseries" False
def test_prom_error_503(self):
    # recorder raises PromError("Prometheus unreachable") -> 503, body error contains "unreachable"
    # busy=True -> 503 with Retry-After "2"
def test_detail_has_timeseries_flag(self):
    # config.PROMETHEUS_URL set -> detail body "timeseries" True
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `... -m unittest tests.test_jobstats_web -q`
Expected: the new tests fail (404 on the new route, `KeyError: 'timeseries'`). The existing tests pass.

- [ ] **Step 3: Implement.**
  - `job_detail(jobid, submit) -> dict | None` is the cached sacct lookup moved verbatim out of `api_jobstats_job`. The detail route keeps its behaviour and its tests stay green.
  - Add `import config, jobstats_prom` at the top.
  - The new route under `@_contained` maps `PromError` to `_error(e.message, 503, retry_after=2 if e.busy else None)`.
  - Running-job `end` is `int(time.time())`. `start` is `row["start"]`.

- [ ] **Step 4: Run the tests to verify they pass**

Expected: the full suite passes, including `ExistingRoutesTests` and `IdentityTests`.

- [ ] **Step 5: Commit** with message `Add /api/jobstats/job/<id>/timeseries route`.

---

### Task 4: Chart helpers in jobstats-core.js

**Files:**
- Modify: `static/jobstats-core.js` (add helpers and export them)
- Test: `tests/test_jobstats_js.py` (new class `JsTimeseriesTests`, uses the existing `run_js`)

**Interfaces:**
- Produces (exported from `JobstatsCore`):
  - `TS_STEPS = [["30", "30 s"], ["60", "1 min"], ["120", "2 min"], ["300", "5 min"], ["auto", "auto"]]`
  - `tsTicks(max: number, count: number) -> number[]`: "nice" ticks from 0 to ≥ max (steps 1, 2, 2.5 or 5 × 10^k), at most `count + 1` values.
  - `tsTimeAxis(spanS: number) -> {unit: "h" | "min", div: 3600 | 60, ticks: number[]}`: minutes when `spanS < 7200`. Ticks are in that unit, from `tsTicks`.
  - `tsNearest(points: [number, number][], t: number) -> number`: index of the closest timestamp, by binary search. Returns -1 for an empty array.
  - `tsGpuLabels(gpus: {key, host, minor}[]) -> {[key]: string}`: `"GPU " + minor` when every GPU is on one host, else `host + "/" + minor`.
  - `tsStepText(body) -> string`, e.g. `"2 min steps, peak per window"` or `"30 s steps (raw samples)"`. When `coarsened`: `"5 min steps (30 s would exceed 5000 points), average per window"`.

- [ ] **Step 1: Write the failing tests** (each calls `run_js` with `core.<fn>(...)`):

```python
def test_ticks(self):
    self.assertEqual(self.js("core.tsTicks(100, 4)"), [0, 25, 50, 75, 100])
    self.assertEqual(self.js("core.tsTicks(73.6, 4)"), [0, 20, 40, 60, 80])
    self.assertEqual(self.js("core.tsTicks(1000, 4)"), [0, 250, 500, 750, 1000])
def test_time_axis(self):
    self.assertEqual(self.js("core.tsTimeAxis(12.2*3600)"), {"unit": "h", "div": 3600, "ticks": [0, 5, 10, 15]})
    self.assertEqual(self.js("core.tsTimeAxis(1800)")["unit"], "min")
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
    # {"step_s":120,"agg":"max","coarsened":False} -> "2 min steps, peak per window"
    # {"step_s":30,...} -> "30 s steps (raw samples)"
    # {"step_s":300,"agg":"avg","coarsened":True,"requested_step":30} -> "5 min steps (30 s would exceed 5000 points), average per window"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `... -m unittest tests.test_jobstats_js -q`
Expected: the new tests fail with `core.tsTicks is not a function`.

- [ ] **Step 3: Implement** the five helpers and `TS_STEPS`, and add them to the returned API object.

- [ ] **Step 4: Run the tests to verify they pass.** Expected: the whole JS test class passes. The suite was not skipped (Node is present).

- [ ] **Step 5: Commit** with message `Add time-series axis and label helpers to jobstats-core`.

---

### Task 5: Charts and job-page integration

**Files:**
- Create: `static/jobstats-charts.js`
- Modify:
  - `static/jobstats.js` (`renderJob`, `NO_DETAIL`, footnote, new `tsSection`)
  - `static/jobstats.css` (`.js-ts*` rules)
  - `static/jobstats.html` (load `jobstats-charts.js` between core and page script; bump all `?v=` to `m5-ts`)
  - `README.md` (configuration lines for the two env vars)
- Test: `tests/test_jobstats_js.py` (`node --check` of the new file), plus manual checks in Task 6

**Interfaces:**
- Consumes: the route body (Task 3) and `JobstatsCore.ts*` (Task 4).
- Produces: `window.JobstatsCharts.render(container: Element, body: object, labels: {[key]: string}) -> {setVisible(key: string, on: boolean): void}`.

Behaviour, copied from the spec's "The page" section:
- The control bar has Resolution (`TS_STEPS`, default `auto`), Aggregation (Average/Peak, disabled when Resolution is 30 s or auto) and a **Generate time-series plots** button. It appears only when the detail body has `timeseries: true` and the job started. Nothing is fetched until the button is clicked. While loading, the button is disabled and reads "Loading…". On a non-OK response, show the server's `error` text in the section only.
- After a load, the section shows:
  - the `tsStepText` line
  - the GPU chips: `aria-pressed`, a colour swatch, and a toggle through `setVisible`; hidden for a single-GPU job
  - the charts: two columns, one column under 900 px
- GPU line colours: eight distinct colours, cycling for more GPUs. Reuse the page's existing palette variables where they exist.
- Each chart: title and unit note ("%, 8 GPUs" or "cores, whole job"), y gridlines from `tsTicks(ymax, 4)`, x-axis from `tsTimeAxis`, label "hours since job start" or "minutes since job start", a dashed "allocated N" line on CPU and RAM, and "No data for this job" when the chart's series is empty.
- If a job has no GPUs, the six GPU charts are hidden.
- Hover: one crosshair at the same time in every chart (`tsNearest`), plus a readout of the value at that time. The readout lists every visible GPU, sorted, with the closest line first. The GPU line nearest the pointer is highlighted in every chart and the others are dimmed. When the pointer leaves, the highlight clears.
- `renderJob` changes:
  - The section goes after the CPU table on the `ok` path.
  - On the early-return path, it goes after the message, for any job with `!r.not_started`.
  - When `r.state` starts with `RUNNING` and the summary is `missing`, the message reads "The job is still running. Its jobstats summary is written when it ends."
  - Footnote: replace "Time series come in phase B." with "Time series below come from Prometheus at the resolution you choose."
- Only build DOM through the existing `el`/`svgEl` helpers. Never insert `innerHTML` built from API strings.

- [ ] **Step 1: Write the failing test** `test_new_scripts_parse` in `JsTimeseriesTests`. It runs `node --check static/jobstats-charts.js` and `node --check static/jobstats.js` and asserts that both exit with 0.
- [ ] **Step 2: Run it to verify it fails.** Expected: the file is missing.
- [ ] **Step 3: Implement** `jobstats-charts.js`, the `jobstats.js` changes, the CSS, the HTML version bump and the README lines.
- [ ] **Step 4: Run the full suite.** Expected: all pass.
- [ ] **Step 5: Commit** with message `Draw job time series on the jobstats job page`.

---

### Task 6: Verify on BCM11 and merge into the deploy branch

**Files:** none new. This task changes the `bcm11-jobstats` checkout at `/home/adovlatyan/Slurm-Monitoring` and `/home/adovlatyan/jobstats-deploy/v2-dashboard/README.md`.

- [ ] **Step 1: Run the worktree on a spare port and drive the API**

```bash
cd /home/adovlatyan/Slurm-Monitoring-ts
runuser -u adovlatyan -- env PATH=/cm/local/apps/slurm/25.05/bin:$PATH SLURM_CONF=/cm/shared/apps/slurm/etc/slurm/slurm.conf \
  SLURM_DASHBOARD_PROMETHEUS_URL=http://127.0.0.1:9090 SLURM_DASHBOARD_DISABLE_RECORDER=1 \
  SLURM_DASHBOARD_DB=/tmp/claude-0/-home-adovlatyan/76e97d65-3c75-478b-b291-532039c097c0/scratchpad/ts.db \
  /home/adovlatyan/Slurm-Monitoring/.venv/bin/gunicorn --workers 1 --threads 4 --timeout 120 -b 127.0.0.1:19528 app:app
```

Run it in the background. Confirm that this branch's `config.py` reads `SLURM_DASHBOARD_DB`; if it doesn't, drop that variable. Then, for each case below, curl `/api/jobstats/job/<id>/timeseries?submit=<s>&step=<x>&agg=<y>` and check:
- **job 378083** (8 GPUs, gpu08, after GPM was enabled), at 30, 60, 120 and 300 with `avg` and `max`, and at `auto`:
  - 8 GPU keys
  - SM values between 0 and 100
  - power in hundreds of W
  - the mean of the 30 s duty series is within ~2 pp of `gpu_pct` on the job's detail row
- **a multi-node job, a job that started before 2026-10-28, an array task and a running job:**
  - the expected keys
  - GPM charts empty for the pre-GPM job
  - `end` close to now for the running job

Find ids with `sacct -a -X -S now-7days -o JobID,JobIDRaw,NodeList,Start,End,State`.
- [ ] **Step 2: Check the browser view.** Tell Aram the URL: `ssh -N -L 19528:127.0.0.1:19528 ...`, then `http://127.0.0.1:19528/jobstats?job=378083&submit=<s>`. Wait for his look before merging.
- [ ] **Step 3: Stop the test server.** Then merge into `bcm11-jobstats`: `runuser -u adovlatyan -- git -C /home/adovlatyan/Slurm-Monitoring merge feature/jobstats-timeseries`. Expect a conflict in `config.py`, because `bcm11-jobstats` already defines `PROMETHEUS_URL` with the default `http://127.0.0.1:9090`. Keep that line and `PROMETHEUS_SCRAPE_SEC`, and add `PROMETHEUS_SELECTOR`. Run the full suite in the main checkout. Expected: all pass, including `tests/test_prom_gpu.py`.
- [ ] **Step 4: Restart the deployed instance.** Run `systemctl stop slurm-dashboard-aram`, then the `systemd-run` line from `~/jobstats-deploy/v2-dashboard/README.md`. Check that `curl -s 127.0.0.1:19527/jobstats | grep m5-ts` matches.
- [ ] **Step 5: Record it.** Add a dated line to the jobstats section of `~/jobstats-deploy/v2-dashboard/README.md` with the commit ids. Push `feature/jobstats-timeseries` to `feature/jobstats-page` only when Aram asks, using the HTTPS command in that README.
