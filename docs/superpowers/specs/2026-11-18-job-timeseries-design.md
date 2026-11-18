# Job page time series (jobstats phase B)

Date: 2026-11-18. Target branch: `feature/jobstats-page` of `github.com/aramdov/Slurm-Monitoring`
(then merged into BCM11's `bcm11-jobstats`). Visual reference: Aram's mockup
`/home/adovlatyan/images/jobstats-job-page-visuals_20261118_115947.png` on bcm11-headnode.

## Why

The job page (`/jobstats?job=...&submit=...`) shows the stored jobstats summary (JS1 in sacct
`AdminComment`): one mean per GPU over the whole run. A mean hides the shape of a workload. Repeating
spikes, phases, a slow warm-up or one GPU drifting from the others all disappear in it. The same
metrics over time show them. The page already promises this: its footnote says "Time series come in
phase B" (`static/jobstats.js`, `renderJob`).

## Goals

- On the job page, on demand, draw time series for one job from Prometheus:
  - Per GPU, one line per GPU: GPU duty, SM activity, tensor activity, DRAM bandwidth, GPU memory, GPU power.
  - Whole job, one line: CPU cores in use, RAM, each with a dashed "allocated" line.
- The user picks the resolution and, above 30 s, whether a point is the window's average or peak.
- Works on BCM11 and on Aram's other cluster, which also runs Prometheus.

## Non-goals

- No time series on the job table, Groups view or anywhere other than one job's page.
- No new data collection, exporters or Prometheus changes. Jobs older than Prometheus retention (180 d on
  BCM11) get no charts; that is accepted.
- No charting library. The page has none today and draws SVG by hand (`strip()` in `static/jobstats.js`).

## Anti-goals (a failure even if it works)

- Querying Prometheus when a job page merely opens. Users flip through jobs; only the button queries.
- A request that can load Prometheus heavily or hang the dashboard worker (single gunicorn worker on
  BCM11).
- Showing a chart that silently misrepresents: a resolution or aggregation other than the one stated,
  or empty axes that look like zero usage.

## The page (decided with Aram)

Below the existing GPU and CPU tables, a "Time series" section:

1. A control bar: **Resolution** select (30 s, 1 min, 2 min, 5 min, auto), **Aggregation** select
   (Average, Peak; disabled at 30 s), and a **Generate time-series plots** button. Nothing is fetched
   until the button is pressed. Changing a control does not refetch; pressing the button again does.
2. After loading: a line stating what is shown, e.g. "2 min steps, peak per window, 361 points".
   If the server coarsened the step (see limits), this line says so.
3. A row of GPU chips (`GPU 0` ... with each line's color swatch, as in the mockup). Each chip toggles
   that GPU on or off in all six GPU charts; any combination can be on and lines overlay. All on by
   default. Multi-node jobs label chips `gpu08/0`, `gpu07/3`. *Builder's call:* hide the chip row for
   a single-GPU job.
4. Eight charts in a two-column grid in the mockup's order: GPU duty, SM activity, Tensor activity,
   DRAM bandwidth, GPU memory, GPU power, CPU cores in use, RAM. Each has a title, a unit note
   ("%, 8 GPUs"; "cores, whole job"), y gridlines, and an x-axis in hours since job start (switch to
   minutes for jobs under ~2 h).
5. Hover on a chart: a vertical crosshair at the nearest time in every chart, and a readout of the
   value(s) at that time. Hovering near a line highlights that GPU in every chart (others dimmed).
6. Y-axes: percent charts 0-100; power 0-1000 W (Aram has seen ~800 W); GPU memory 0 to the GPU's
   total memory (80 GB on BCM11 H100s); CPU and RAM 0 to a bit above the allocation. No power-limit line.
7. A chart with no data for this job says "No data for this job" instead of drawing axes. Expected
   cases: SM/tensor/DRAM for jobs that ran before GPM was enabled (BCM11: 2026-10-28), jobs older than
   retention, CPU-only jobs (GPU charts hidden or "no GPUs").
8. The button is hidden when no Prometheus URL is configured, and the footnote no longer says
   "Time series come in phase B".

Which jobs get the section (*my call*): every job that started, whatever its summary state. Today
`renderJob` returns early unless `summary_state` is `ok`, so the section must be added on that early path
too. A running job has no JS1 yet (state `missing`); its message should say the summary is written when
the job ends, not that the job predates the summary hook. Running jobs: the range ends at "now".
Pending/never-started jobs: no section.

## Metrics and queries

Prometheus labels on BCM11 (verified 2026-11-18, `query` on `127.0.0.1:9090`):
`nvidia_gpu_*{cluster="slurm",instance="gpu08:9445",jobid="378083",minor_number="6",...}` and
`cgroup_*{cluster="slurm",instance="gpu03:9306",jobid="377566"}`. `jobid` is the **raw** Slurm id
(`JobIDRaw`), so array task `2645_1` is queried by its raw id. Scrape interval is 30 s for all three
scrape jobs.

| Chart | Series | Unit conversion |
|---|---|---|
| GPU duty | `nvidia_gpu_duty_cycle` | already 0-100 |
| SM activity | `nvidia_gpu_sm_util_percent` | fraction 0-1, x100 |
| Tensor activity | `nvidia_gpu_any_tensor_util_percent` | fraction 0-1, x100 |
| DRAM bandwidth | `nvidia_gpu_dram_bw_util_percent` | fraction 0-1, x100 |
| GPU memory | `nvidia_gpu_memory_used_bytes` (axis max from `nvidia_gpu_memory_total_bytes`) | bytes to GB |
| GPU power | `nvidia_gpu_power_usage_milliwatts` | mW to W |
| CPU cores in use | `rate(cgroup_cpu_total_seconds[w])` summed over nodes; allocated = sum `cgroup_cpus` | cores |
| RAM | `cgroup_memory_rss_bytes + cgroup_memory_shmem_bytes` (fallback rss alone) summed over nodes; allocated = sum `cgroup_memory_total_bytes` | bytes to GB |

GPM metrics are fractions despite the `_percent` names (CONTEXT §6 on BCM11). The RAM definition and the
`step="",task=""` cgroup label filter match upstream jobstats 1.1.0 (`jobstats.py:435-441`), so for a
single-node job the RAM chart's peak agrees with the job page's stored memory peak (the stored value is
per node).

Multi-node CPU and RAM (*my call*): the line is the job total. Peak is the peak of that total,
`max_over_time(sum(...)[<step>:30s])`, never a sum of per-node peaks, which can show a value the job
never reached.

Per GPU series are keyed by `(instance host, minor_number)`.

Resolution and aggregation:

- `query_range` with `start` = job start, `end` = job end (or now), `step` = the resolution.
- 30 s: the raw series (one sample per point).
- Coarser, Average: `avg_over_time(<series>[<step>])`. Peak: `max_over_time(<series>[<step>])`.
- CPU cores: Average = `rate(cgroup_cpu_total_seconds[<step>])`; Peak =
  `max_over_time(rate(cgroup_cpu_total_seconds[1m])[<step>:30s])`. At 30 s use `rate(...[1m])`.
- Auto: the smallest of 30 s, 1 min, 2 min, 5 min, 10 min, 30 min, 1 h that keeps a line under ~1,500
  points, and Average.

Configuration (so the other cluster works):

- `SLURM_DASHBOARD_PROMETHEUS_URL` (unset = feature off, button hidden). BCM11: `http://127.0.0.1:9090`.
- `SLURM_DASHBOARD_PROMETHEUS_SELECTOR`: extra label matchers added to every query, e.g.
  `cluster="slurm"`. Empty by default.
- Metric names, labels and the 30 s scrape interval are constants in one table in the module. Aram
  confirmed (2026-11-18) that the other cluster runs the same jobstats exporters (`nvml_exporter`,
  `cgroup_exporter`) at 30 s, so no label map or metric-name overrides.
- The page learns whether the feature is on from the existing job detail response (add a field such as
  `timeseries: true`), not a second request.

## Server

New route `GET /api/jobstats/job/<jobid>/timeseries?submit=<s>&step=<s|auto>&agg=<avg|max>`, in the
same Blueprint and under `@_contained`.

- The server looks the job up the same way the detail route does (sacct with `submit`, using the
  detail cache) and takes raw id, start, end and allocations from that. It never trusts client-sent
  times or ids beyond `jobid`/`submit` validation.
- `step` must be one of the allowed values; `agg` one of `avg`, `max`.
- Prometheus refuses more than 11,000 points per series. If `(end - start) / step` exceeds a cap
  (*builder's call*, around 5,000), use the next coarser allowed step and return the step used.
- Limits like the sacct path: HTTP timeout per query (~10 s), an overall deadline per request well
  under gunicorn's 120 s timeout (BCM11 runs 1 worker, 4 threads), one Prometheus job at a time with a
  semaphore (busy = 503 + `Retry-After`), cap on response size. Queries for one request may run
  in parallel or sequentially; *builder's call*.
- Cache finished jobs' results (key: raw id, submit, step, agg) in a small LRU like `_detail_cache`.
  Running jobs: no cache or a short TTL.
- Response: `{step_s, agg, start, end, coarsened, gpus: [{key, host, minor}], charts: {<name>:
  {unit, ymax, series: {<gpu key>|"job": [[t, v], ...]}, allocated}}}`. Missing data is an empty
  series, never zeros. Values rounded to keep the payload small.
- Prometheus errors (down, timeout, 4xx) surface as a message in the section, not a broken page.

## Tests

- Python (`tests/test_jobstats_web.py` style, `unittest`): query construction per step/agg, step
  coarsening, unit conversion, multi-node keying, raw id use for array jobs, empty/missing data,
  Prometheus error and timeout handling, feature off when the URL is unset. Prometheus is faked; no
  network.
- JS: pure helpers (axis ticks, scales, nearest-point lookup, auto step) in `static/jobstats-core.js`,
  checked under Node by `tests/test_jobstats_js.py` (Node 18 exists on bcm11-headnode).
- Manual on BCM11: job 378083 (8 GPUs, gpu08, post-GPM) at each resolution and both aggregations;
  a multi-node job; a pre-2026-10-28 job (GPM charts say no data); an array task; a running job.
  Jobs on nodes Prometheus does not scrape (BCM11: gpu09, ap-rtx) show "No data".
- Bump the `?v=` asset tags in `static/jobstats.html`.

## Left to the builder

Chart component structure, colors (reuse a palette consistent with the existing page), exact layout
breakpoints, tooltip styling, and anything marked *builder's call* above.

## Open on the other cluster

SLUID job ids arrive with Slurm 26.05 and change cgroup paths (CONTEXT §3 on BCM11); upstream jobstats
then queries `jobid=<sluid>` as a fallback. Not handled here.
