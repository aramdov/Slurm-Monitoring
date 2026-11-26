# Explorer scatter: X and Y axis pickers (jobstats phase C, part 2b)

Date: 2026-11-26. Branch: `feature/jobstats-scatter` (worktree `/home/adovlatyan/Slurm-Monitoring-ts`).
Builds on `docs/superpowers/specs/2026-11-18-explorer-scatter-design.md`, which stays in force where
this document is silent. Reviewed proposal: scratchpad `axis-proposal.md` (Claude Opus, 2026-11-26),
review by Fable 5.1 on 2026-11-26.

## Intent

The Scatter view plots every summarized GPU job at (GPU duty, SM activity). Duty and SM correlate at
0.87 over the last 20 days, so that pair mostly answers one question: did a kernel run. The stored
summary also carries occupancy, tensor-pipe, DRAM-bandwidth and FP32-pipe averages (`MEAN_FIELDS`,
`jobstats_web.py:56-65`). Half of the cluster's GPU-time runs tensor cores under 3 %, and the duty/SM
plot cannot show that. This change lets the reader choose which two of those metrics the dots are placed
by. Everything else about the view (filters, colors, size, split, ring, hollow, hover, click) stays.

## Goals

- Two pickers, X and Y, over a fixed list of per-job GPU metrics, plus four preset pairs.
- Default unchanged: duty on X, SM on Y. An existing URL draws exactly what it draws today.
- The count line, the tooltip, the hollow mark and the aria label follow the chosen axes.
- `x` and `y` in the URL, like the other controls.

## Non-goals

- Cause labels ("host-bound", "memory-bound"). The 2026-11-18 anti-goal stands: the plot shows where
  jobs sit, not why. Help copy may say what a region usually means and that it has several causes.
- Autoscaled or zoomed axes. All axes are 0-100 fixed (see "Axes").
- A color-by-metric mode. Color stays the group under "Group by".
- New metrics from the server. Every axis is a field the row already has, or a ratio of two.
- Changes to Groups, Jobs, the metric filter or the time-series charts.

## Why these axes and not others

On H100 the GPM fields are time fractions over the job's GPUs:

- `sm` (SM activity): fraction of SM-cycles with at least one warp resident.
- `occ` (SM occupancy): resident warps over the SM's maximum, averaged over all SM-cycles, idle cycles
  counting as zero. So `occ <= sm` holds per sample and after averaging (0 of 7,945 jobs break it).
  Raw occupancy therefore repeats SM to a large degree (correlation 0.80). `occ / sm` is the
  activity-weighted mean occupancy while active, which is the quantity a reader wants: how full the
  SMs were when they had work. It is noise when `sm` is near zero, so it is undefined below a floor.
- `tc`, `fp32`: pipe-active fractions. Both are bounded by `sm` for the same reason, but the bound sits
  far from the data (tensor p90 is 14 %, SM p90 87 %), so it is not drawn.
- `dram`: DRAM bandwidth utilisation. Not bounded by `sm`.
- `gpu_pct` (duty): NVML utilisation, a different sampler. SM can exceed it; the diagonal is a reference.

`int` is left out: the integer pipe runs address arithmetic in nearly every kernel, no threshold for it
exists in `~/docs/jobstats/CONTEXT.md`, and it would add a seventh percent axis with no reading attached.
`power_w` is left out because it is not a percentage and would break the fixed 0-100 frame.
`fp16_seen` / `fp64_seen` are booleans, not positions.

Low occupancy is often fine: cuBLAS and CUTLASS GEMMs run a few large CTAs per SM, and jobs with
tensor over 20 % sit at occupancy p50 44 %. The help copy says so; the axis still earns its place
because the `OCC` reading in CONTEXT.md ("under 15 % with SM over 50 %: kernels too small") is a pair
read, and `occ/sm` is that pair as one number.

## Axes

Defined once in `jobstats-core.js` as `SCATTER_AXES`, an ordered array of
`{ key, label, axis, field, needs }`. `key` is the URL and picker value, `label` the picker text,
`axis` the short text drawn on the canvas, `field` the row field it reads (null for the derived one),
`needs` the row fields whose coverage decides the hollow mark.

| key | label | axis | value | needs |
|---|---|---|---|---|
| `gpu_pct` | GPU duty % (NVML) | GPU duty % | `row.gpu_pct` | `gpu_pct` |
| `sm` | SM activity % | SM % | `row.sm` | `sm` |
| `occ` | Occupancy % | OCC % | `row.occ` | `occ` |
| `occ_sm` | Occupancy / SM % | OCC/SM % | `100 * row.occ / row.sm`, clamped to 100; null unless `row.sm >= 5` | `occ`, `sm` |
| `tc` | Tensor pipe % | TC % | `row.tc` | `tc` |
| `dram` | DRAM bandwidth % | DRAM % | `row.dram` | `dram` |
| `fp32` | FP32 pipe % | FP32 % | `row.fp32` | `fp32` |

`scatterValue(row, key)` returns the number or null. Null when the field is null or non-finite; for
`occ_sm`, also when `occ` or `sm` is null or `sm < SCATTER_OCC_SM_MIN_SM` (constant, 5). The floor is
a constant exported from core so the tests and the count line share it.

All axes run 0-100, fixed, gridlines every 20, values over 100 drawn at 100, as today. Fixed ranges keep
the three split panels and successive filters comparable, and the thresholds that matter on the pipe
axes (2, 10, 20, 45 %) are readable at that scale. A cloud glued to the bottom of a tensor axis is the
finding, not a display problem.

## Presets and defaults

Four preset buttons, each setting both axes:

| button | x | y |
|---|---|---|
| Duty vs SM | `gpu_pct` | `sm` |
| SM vs tensor | `sm` | `tc` |
| SM vs DRAM | `sm` | `dram` |
| SM vs occ/SM | `sm` | `occ_sm` |

Defaults: `x = gpu_pct`, `y = sm`. A preset button shows `aria-pressed="true"` when the current pair
equals it exactly (same orientation); any other pair presses none. Two `<select>` elements, X and Y,
list the seven axes in table order and always show the current pair, so a preset is a shortcut and the
selects are the state. `x === y` is allowed (a line of dots) and gets no special handling.

## URL state

`readUrl`: `st.x = pick(p.get("x"), AXIS_KEYS, "gpu_pct")`, `st.y = pick(p.get("y"), AXIS_KEYS, "sm")`.
Invalid or missing values fall back independently: `?x=tc&y=bogus` gives `x=tc, y=sm`. `writeUrl` writes
`x` and `y` only when they differ from the defaults, like `size` and `split`. Existing URLs carry no
`x`/`y`, so they draw the default pair. `x` and `y` are kept in the URL whatever the view, like `size`
and `split` already are, so switching Jobs to Scatter and back keeps the pair.

## Rows that become dots

`scatterSplit(rows, xKey, yKey)` with defaults `"gpu_pct", "sm"` so the old two-argument-free call
still works. Reason order, first match wins:

1. `not_started`
2. `cpu_only` (`alloc_gpus` not > 0)
3. `running` (`end == null`)
4. `no_summary` (`summary_state !== "ok"`)
5. `low_sm`: only when either axis is `occ_sm`; `row.sm` is non-null and below 5.
6. `no_value`: `scatterValue` is null for x or y.

`low_sm` comes before `no_value` so a job with `sm = 2` and `occ = null` is counted as low SM, which is
the reason the reader can act on. The result keeps today's shape (`points`, `excluded` keyed by all six
reasons, `plotted`, `total_gpu_hours`); `excluded.low_sm` is present with zeros when no axis is `occ_sm`.

Count line wording (renderer):

- `no_value`: "no &lt;X axis&gt; or &lt;Y axis&gt; value" using the `axis` text without the `%`, e.g.
  "no SM or TC value", "no GPU duty or SM value" (the default pair reads as today apart from the
  word "GPU", which the existing test must be updated for). When `x === y`: "no SM value".
- `low_sm`: "SM under 5 % (occ/SM undefined)".
- Both carry GPU-hours in parentheses, like `running` and `no_summary`.

## Diagonal

Drawn only for these unordered pairs; label placed at the top-right end as today:

- `{gpu_pct, sm}`: dashed, label "SM = duty (reference)". The existing note about separate samplers stays.
- `{sm, occ}`: dashed, label "OCC = SM (ceiling)". Occupancy cannot exceed SM activity, so dots on the
  SM side of the line would be a data fault, not a job. The note under the plot for this pair says:
  "Occupancy counts idle SM-cycles as zero, so it cannot exceed SM activity. The distance below the line
  is how empty the SMs were while active."

With occupancy on X and SM on Y the same line is a floor: label "SM = OCC (floor)", and the note says
"above the line" instead of "below the line". (Amended after review, 2026-11-26.)

No other pair gets a line. `{sm, tc}` and `{sm, fp32}` do have a true ceiling, but it sits far above the
cloud and would read as a target. `{sm, occ_sm}` and anything with `dram` have no relation to draw.

## Marks, colors, size, split

- Ring (a GPU under 20 % duty) is unchanged and does not depend on the axes. When duty is not on either
  axis the ring is the only sign of a sleeping GPU, which is the point of keeping it.
- Hollow: `scatterMarks(r, xKey, yKey)`; hollow when coverage of any field in `needs` of either axis is
  below 1, or `summary_gpus < alloc_gpus`. Legend text unchanged.
- Color, dot size, draw order, the split panels and their count lines are unchanged. Split panels all use
  the chosen pair.

## Tooltip

Lines, in order: Job, Name, User, GPUs, GPU-h, Duty, SM, then one line for X and one for Y when the
axis is neither `gpu_pct` nor `sm`, labelled by `axis` text without `%`. The `occ_sm` line reads
"OCC/SM 31.3% (OCC 15.0% of SM 47.9%)". Then Low-duty GPUs, Coverage and Reported as today; the Coverage
line lists duty and SM plus every other `needs` field of the chosen axes that is below 1, as
"TC 50%". Values go through `fmtPct`, strings through `textContent`.

## Page text and controls

`#js-sc-bar` gains, before Dot size: a preset group `#js-preset` (four buttons, `data-x`, `data-y`),
then "X" `<select id="js-x">` and "Y" `<select id="js-y">`, options built from `SCATTER_AXES` the way
`#js-metric` is built from `FILTER_METRICS`. The bar already wraps (`flex-wrap`), so no layout change is
required; narrow-screen wrapping is the builder's call.

Under the plot, the notes block shows one pair-specific line when the current pair is a preset, and the
GPU-h note always. The duty/SM note ("a job can sit above the line") shows only when the duty/SM
line is drawn. (Amended after review, 2026-11-26: on other pairs it described a line that is not there.) The pair lines:

- Duty vs SM: "The gap between duty and SM is time when a kernel ran but most SMs sat empty."
- SM vs tensor: "Busy SMs with little tensor-core use. Expected for some codes, a lead for others."
- SM vs DRAM: "Separates memory-bandwidth-heavy work from the rest of the low-tensor group."
- SM vs occ/SM: "How full the SMs were while active. Low is normal for large matmul kernels, so read
  it only for a job that already looks slow."

The "How the numbers are computed" list's Scatter line becomes: "Scatter: one dot per job with a
summary; position is the job's mean of the two chosen metrics over its GPUs. OCC/SM is occupancy
divided by SM activity and is left out when SM is under 5 %. Ring and hollow mark uneven or incomplete
GPUs." The aria label becomes "Scatter of N jobs: &lt;X axis&gt; against &lt;Y axis&gt;".

Asset version query strings move from `m6-sc` to a new value (builder's choice, e.g. `m7-ax`) on all
five references; `test_asset_versions_bumped` pins that string and must change with it.

## Backwards compatibility

- URLs without `x`/`y`: identical plot and count line to today, apart from the "no GPU duty or SM
  value" wording.
- `scatterSplit(rows)` and `scatterMarks(r)` with no axis arguments behave as before.
- Rows from before each node's GPM switch (D29) have null `sm`, `tc`, `dram`, `occ`, `fp32`. On any
  non-duty axis they land in `no_value`, which the count line shows with GPU-hours. That is expected.

## Tests

JS, `tests/test_jobstats_js.py`, through `run_js` on `jobstats-core.js` and the render stub:

- `test_axes_table`: `SCATTER_AXES` has exactly the seven keys in table order; every non-derived `field`
  is in `MEAN_KEYS`; `occ_sm.needs == ["occ", "sm"]`.
- `test_scatter_value`: `scatterValue` returns the field for plain keys, null for a null field;
  `occ_sm` gives 31.25 for `occ=15, sm=48`, 100 for `occ=60, sm=50` (clamp), null for `sm=4.9`,
  null for `sm=null` or `occ=null`, 0 for `occ=0, sm=50`.
- `test_split_default_axes_unchanged`: the existing `test_split_reasons_and_order` fixture through
  `scatterSplit(rows)` gives the same buckets as today, plus `excluded.low_sm == {jobs: 0, gpu_hours: 0}`.
- `test_split_follows_axes`: rows with `sm=null, tc=20` and `sm=40, tc=null`; on `("sm","tc")` both are
  `no_value`; on `("gpu_pct","dram")` with `dram` set, both plot.
- `test_split_low_sm_before_no_value`: on `("sm","occ_sm")`, `sm=2, occ=null` is `low_sm`,
  `sm=2, occ=1` is `low_sm`, `sm=5, occ=null` is `no_value`, `sm=5, occ=2` plots at `occ_sm=40`; on
  `("sm","occ")` the `sm=2` rows plot and `low_sm` stays zero.
- `test_marks_follow_axes`: `coverage.tc = 0.5` is hollow on `("sm","tc")` and not on `("gpu_pct","sm")`;
  `coverage.occ = 0.5` is hollow on `("sm","occ_sm")`; `summary_gpus < alloc_gpus` is hollow on any pair.
- `test_render_axis_labels_and_diagonal`: the render stub records `fillText` and `setLineDash`; with
  `x="sm", y="tc"` the axis texts are "SM %" and "TC %", no dashed segment is drawn and no "(reference)"
  or "(ceiling)" text appears; with `x="sm", y="occ"` the "OCC = SM (ceiling)" label appears; the default
  pair still draws "SM = duty (reference)".
- `test_render_dots_use_axis_values`: one row with `gpu_pct=80, sm=50, tc=10`; the first arc's centre on
  `("sm","tc")` is at `sx(50), sy(10)` (recompute from the stub's canvas size as the clamp test does).
- `test_render_count_line_wording`: on `("sm","tc")` with one `tc=null` row the count line contains
  "no SM or TC value"; on `("sm","occ_sm")` with one `sm=2` row it contains "SM under 5 % (occ/SM undefined)".
- `test_render_tooltip_lines`: on `("sm","occ_sm")` hovering the dot yields lines Duty, SM, then an
  "OCC/SM" line containing "OCC 15.0% of SM 48.0%"; on the default pair no extra metric line appears.
- `test_render_pair_note`: a preset pair shows its note text under the plot; a non-preset pair
  `("tc","dram")` shows only the two standing notes.
- `test_aria_label_follows_axes`: contains "SM % against TC %" on that pair.
- Page wiring (string checks on `jobstats.html` / `jobstats.js`, like `JsPageWiringTests`):
  `id="js-x"`, `id="js-y"`, `id="js-preset"`, the four preset buttons with their `data-x`/`data-y`,
  `x: "gpu_pct"` and `y: "sm"` in `DEF`, and the new asset version on all five references.

Python: none needed; no server change.

By hand on bcm11, 20-day range: each preset, a non-preset pair, split on, a filter leaving 0 dots, a
URL with `?x=tc&y=bogus`, hover on an occ/SM dot, click-through.

## Left to the builder

The exact layout of presets and selects inside the bar on narrow screens; whether `scatterValue`
clamps before or after formatting in the tooltip (the plotted position clamps, the tooltip may show the
raw ratio); the asset version string; the stub additions needed to record `fillText` and
`setLineDash` in the render tests; and the note placement (same `js-sc-notes` block is fine).
