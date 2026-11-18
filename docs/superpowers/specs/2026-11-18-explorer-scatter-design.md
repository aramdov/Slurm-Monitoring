# Explorer scatter: duty against SM over a filtered set of jobs (jobstats phase C, part 1)

Date: 2026-11-18. Target branch: `feature/jobstats-page` of `github.com/aramdov/Slurm-Monitoring`
(then merged into BCM11's `bcm11-jobstats`). Visual reference: Aram's mockup
`/home/adovlatyan/images/jobstats-scatter-and-compare_20261118_144745.png` on bcm11-headnode, left half
("Scatter"). The right half ("Compare") is part 3 of the order below and is not in this design.

## Why

The Explorer (`/jobstats`) lists jobs and groups with their stored jobstats summaries (JS1 in sacct
`AdminComment`). A table answers "what did this job do". It does not show the shape of the whole
cluster: how many GPU-hours sit at low duty, whether busy GPUs do real SM work, which job names own the
big idle allocations. One dot per job, placed by GPU duty and SM activity, shows that at a glance and
lets Aram click through to the job behind any dot.

Two reviews shaped this design (Fable 5.1, then GPT-6 Astra on top of it; files in the session
scratchpad, `astra-eda/`). Their agreed order for the EDA work, accepted by Aram on 2026-11-18:

1. Make every job in the selected range reach the browser (today only 5,000 do).
2. This scatter.
3. Compare: SM over time for 2-4 checked jobs, on an explicit button.
4. Groups additions: estimated inactive GPU-hours, over-requested GPUs, short and failed jobs per group.
5. Later: CSV export, then a daily rollup once history outgrows one sacct call.

This document covers 1 and 2.

## Goals

- Every job of the selected range reaches the browser, so filters, Groups and the scatter see the
  same complete set. No silent cut at 5,000.
- A "Scatter" view beside Jobs and Groups: one dot per job, x = GPU duty %, y = SM %, built from the
  rows the current filters select.
- The view says what it leaves out: running jobs, jobs without a summary, jobs missing either value.
- Hover shows the job; click opens its job page.

## Non-goals

- Compare (time series of several jobs). Clicking a dot does not add it anywhere.
- New metrics or a server-side copy of the filter language. Filtering stays in `jobstats-core.js`.
- Clipping GPU-hours to the selected window. A job that overlaps the window counts in full, as it
  does in the table today. Noted in the page text; fixing it belongs to the Groups additions.
- Selecting dots with a box or lasso.
- A charting library. The page draws by hand today (`jobstats-charts.js`, `strip()`).

## Anti-goals (a failure even if it works)

- A plot that implies a rule the data does not follow. SM can exceed duty: duty is NVML utilization,
  SM is a GPM fraction, and each is a mean over the job's GPUs computed on its own. Aram's mockup has
  dots above the diagonal. The diagonal is a reference line, never a ceiling or a "waste" boundary.
- Labels that claim a cause ("host-bound", "memory-bound"). The plot shows where jobs sit, not why.
- A plot that drops jobs without saying so, or a truncated set that looks complete.
- A view that freezes the tab on a 20-day range.

## Part 1: all rows reach the browser

### What exists

`api_jobstats_jobs` (`jobstats_web.py` ~550-575) runs one sacct for the window, keeps overlapping rows,
sorts them newest end first (running jobs first, `_sort_key`) and returns `selected[:MAX_ROWS]` with
`MAX_ROWS = 5000` and a `truncated` flag. All filtering and group aggregation then happen in the
browser (`filterRows`, `aggregate` in `static/jobstats-core.js`). The cut therefore happens before any
filter, so a 7-day range shows the newest ~5,000 jobs of every kind and the scatter would plot
whichever of those have summaries.

Measured on bcm11-headnode 2026-11-18 with the app's own code (`explorer_rows` + `filter_window`):

| Range | Rows | With summary (`ok`) | Running | JSON | gzipped | Server time |
|---|---|---|---|---|---|---|
| 1 d | 1,295 | 766 | 270 | 1.0 MB | 0.08 MB | 0.2 s |
| 7 d | 10,784 | 6,835 | 270 | 8.7 MB | 0.67 MB | 1.0 s |
| 20 d | 21,838 | 14,617 | 270 | 17.6 MB | 1.45 MB | 2.6 s |

Summaries exist only for jobs that ended after 29 Oct 19:10 UTC, so ~20 days is all the summarized
history today. The sacct limits (`SACCT_TIMEOUT_S = 10`, `SACCT_MAX_BYTES = 32 MiB`) hold for 20 days
with room to spare; at today's rate a 90-day sacct would be ~38 MB and fail with the existing
"too much output" error. That failure is accepted until step 5 (rollup).

### Decision

Keep filtering in the browser. Raise the row cap and compress the response, instead of moving the
filter language to the server. One filter implementation is the reason: the token grammar, the
GPU-count buckets and the metric thresholds already exist and are tested in `jobstats-core.js`, and a
Python copy would have to match it forever.

- `MAX_ROWS` goes from 5,000 to **60,000** (about 2.7 times today's 20-day count). The `truncated`
  flag and its message stay for anything above it. The message changes to name the number:
  "Showing the newest 60,000 of N jobs in this range. Narrow the range to see the rest." The response
  gains `total` (rows before the cut) so the message can say N.
- The jobs endpoint gzips its JSON body when the request's `Accept-Encoding` includes `gzip` and the
  body is over 64 KiB, with `Content-Encoding: gzip` and `Vary: Accept-Encoding`. Standard library
  `gzip`, level 6. No new dependency. Other routes are unchanged.
- Nothing else in the row format changes. A slimmer row format would save little after gzip (the
  measured "plotted fields only" payload is the same 1.45 MB as gzipped full rows) and would split the
  page into two data paths.

Builder checks: the browser keeps one copy of the rows and the filter cache (`st.cache`) already avoids
refiltering on every render. Confirm a 20-day range stays responsive in the Jobs view (the table
renders 300 rows per page) and that the gunicorn worker's memory stays reasonable with the explorer
cache full (`CACHE_BYTES = 64 MiB` estimate, unchanged).

## Part 2: the scatter

### Where it lives

A third button in the view bar: **Jobs | Groups | Scatter** (`#js-view`). The scatter uses exactly the
rows the filters above it select (`filtered()` in `jobstats.js`), so every filter, token and threshold
applies to it. The "Group by" control (`#js-by`) is shown in Scatter as well, because it sets the
colors. The view and its two toggles are kept in the URL like the other controls (`readUrl` /
`writeUrl`): `view=scatter`, `size=gpuh|equal`, `split=0|1`.

### Which rows become dots

A row is plotted when all hold:

- `summary_state === "ok"`
- `alloc_gpus > 0`
- `gpu_pct` and `sm` are both non-null

Every other selected row is counted, by reason, in a line above the plot, with jobs and GPU-hours:

> Plotted 6,512 jobs, 48,210 GPU-h. Not plotted: running 270 (1,904 GPU-h), no summary 3,100
> (2,050 GPU-h), no SM value 120 (310 GPU-h), CPU-only 760.

Reasons, checked in this order so each row lands in one bucket: not started (only present when a
`state:` or `summary:` token lets them through `filterRows`; counted as "not started N" so a
`state:PENDING` query explains its empty plot; my call), CPU-only (`alloc_gpus === 0`), running (`end == null`), summary not `ok`,
duty or SM missing. The GPU-hours of the unplotted rows are shown as a share as well:
"x % of the selection's GPU-hours is not on the plot".

### Axes and reference line

- x: GPU duty % (row `gpu_pct`, NVML utilization). y: SM % (row `sm`, GPM). Both 0-100, fixed, with
  gridlines every 20. Values over 100 (should not happen) are drawn at 100.
- A dashed diagonal from (0,0) to (100,100), labeled "SM = duty (reference)".
- A one-line note under the plot: "Duty and SM are measured separately (NVML and GPM) and averaged
  over the job's GPUs on their own, so a job can sit above the line."

### Dots

- **Size**, toggle "Dot size: GPU-hours | equal". GPU-hours (default): radius proportional to
  √(gpu_hours), so area is linear in GPU-hours, with a floor of 2 px and a ceiling of 18 px, scaled so
  the largest job in the current set gets the ceiling. Equal: every dot 3 px. Equal size is the view
  for "how many jobs", GPU-hours for "how much allocation".
- **Draw order**: largest first, so small dots stay visible on top. Fill opacity about 0.55.
- **Color**: by group under the active Group-by mode (`groupKey` in `jobstats-core.js`: exact name,
  array parent, name family). The 8 groups with the most plotted GPU-hours get the 8 colors of
  `JobstatsCharts.COLORS`; all others are one neutral gray, legend entry "other (N groups)". The legend
  lists the 8 with their GPU-hours.
- **Ring** (dark outline, about 1.5 px) when `low_duty_gpus > 0`: at least one of the job's GPUs ran
  under 20 % duty (`LOW_DUTY_PCT`). Legend entry: "ring: a GPU under 20 % duty".
- **Hollow** (outline only, no fill) when not every GPU reported duty and SM: `coverage.gpu_pct < 1`
  or `coverage.sm < 1` (as `derive_metrics` computes it, over the summary's own GPU list), or
  `summary_gpus < alloc_gpus`. Legend entry: "hollow: not every GPU reported". Checked 2026-11-18 over
  20 days: 0 of 7,833 GPU jobs with a summary have `summary_gpus != alloc_gpus`, and 1 has coverage
  below 1, so hollow dots should be rare. (My call: both checks, because they are cheap and the second
  covers a case the first cannot see.)

### Split by GPU count

Toggle "Split by GPUs" (default off). On: three small plots side by side, same axes, for 1 GPU, 2-7
GPUs and 8+ GPUs (the page's existing buckets), each with its own count line. Dot size keeps one scale
across the three so they compare. Reason: an 8-GPU job with one busy GPU averages to the same spot as a
lightly used 1-GPU job.

### Hover and click

- Hover: a dot is hit when the pointer is within max(8 px, its radius) of its centre; among hits, the
  nearest centre wins (my call, so a large dot is hoverable anywhere on it). The hit dot is outlined and
  a tooltip shows job id, name, user, GPUs, GPU-h, duty %, SM %, low-duty GPUs as "k of n" with
  n = `summary_gpus` (as `jobNotes` already does), and coverage when below 1. All strings go through
  `textContent`, never `innerHTML` (as in `jobstats-charts.js`).
- Click on a dot: opens that job's page, the same route a job link in the table uses (`jobHref`).
  Ctrl/Cmd-click opens it in a new tab.
- Keyboard: the plot is not keyboard-navigable in this version; the Jobs view remains the accessible
  list of the same rows. The plot has an `aria-label` stating the plotted count.

### Performance

A 20-day range plots ~15,000 dots today. Requirements: the first draw finishes within about 300 ms on
a laptop, and hover stays smooth. My call, left open to the builder: draw dots on a `<canvas>` (SVG with
15,000 circles is slow to hover) with axes, labels and the diagonal in SVG or on the same canvas, and
find the hovered dot through a coarse grid index built once per draw. Redraw on resize and on filter
changes; keep the device-pixel ratio so dots are sharp.

### Text on the page

The "How the numbers are computed" box gains two lines:

- Scatter: one dot per job with a summary; position is the job's mean duty and mean SM over its GPUs.
  Ring and hollow mark uneven or incomplete GPUs.
- GPU-h counts the whole job when it overlaps the range, even the part outside it.

## Tests

- Python (`tests/test_jobstats_web.py`): a window with more than 5,000 rows returns them all and
  `truncated` false; above `MAX_ROWS` it returns `MAX_ROWS` rows, `truncated` true and the right
  `total`; gzip is applied only when requested and the body is over 64 KiB, and decompresses to the same
  JSON.
- JS (`tests/test_jobstats_js.py`, node on `jobstats-core.js`): the pure scatter helpers, kept in
  `jobstats-core.js` like the time-series helpers:
  - plot/exclude classification, with one row per reason and the reason order above;
  - top-8 color assignment by GPU-hours under each group mode, ties broken by group key;
  - radius scale: √ scaling, floor, ceiling, single-job set, all-zero GPU-hours;
  - split buckets 1 / 2-7 / 8+;
  - nearest-dot lookup within the hover radius, and none outside it.
- By hand on bcm11: 1 d and 20 d ranges, each toggle, a filter that leaves 0 plotted dots (the plot
  shows the count line and "No jobs to plot"), click-through to a job page.

## Left to the builder

Canvas details, tooltip placement, exact colors of ring and gray, legend layout on narrow screens,
and how the three split plots wrap below ~900 px.
