# Explorer Scatter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every job in the selected range reaches the Explorer, and a new Scatter view plots one dot per summarized GPU job at (GPU duty %, SM %).

**Architecture:** The jobs endpoint keeps its row format, raises its cap to 60,000 and gzips large bodies. Filtering stays in the browser. Pure scatter logic (classification, colors, radius, buckets, hit lookup) goes into `jobstats-core.js` next to the other pure helpers, where Node tests reach it. Drawing goes into a new `jobstats-scatter.js` (canvas dots, SVG-free) wired into `jobstats.js` as a third view.

**Tech Stack:** Python 3 / Flask (stdlib `gzip`), vanilla JS (no libraries), `unittest` with Node for JS.

**Spec:** `docs/superpowers/specs/2026-11-18-explorer-scatter-design.md`

## Global Constraints

- No new Python or JS dependency. No charting library.
- Branch `feature/jobstats-scatter` in worktree `/home/adovlatyan/Slurm-Monitoring-ts`; commits as `adovlatyan` (`runuser -u adovlatyan -- git ...` with `GIT_CONFIG_PARAMETERS="'safe.directory=*'"`). Never push.
- Test command (whole suite, from the worktree): `runuser -u adovlatyan -- .venv/bin/python -m unittest discover -s tests -q`. The worktree has no `.venv`: use `/home/adovlatyan/Slurm-Monitoring/.venv/bin/python`. Baseline: 193 pass, 1 skipped.
- Every string from the API reaches the page through `textContent` or `setAttribute`, never `innerHTML`.
- The diagonal is a reference line. No text anywhere calls dots above it impossible, or labels a cause ("host-bound", "memory-bound").
- `MAX_ROWS = 60000`. Gzip threshold 64 KiB (65536 bytes), level 6.
- Truncation message, exact: `Showing the newest 60,000 of N jobs in this range. Narrow the range to see the rest.` (60,000 and N formatted with `C.fmtNum`).
- Plotted when `summary_state === "ok"`, `alloc_gpus > 0`, `gpu_pct != null`, `sm != null`. Reason order: `not_started`, `cpu_only`, `running`, `no_summary`, `no_value`.
- Radius: √ scale, floor 2 px, ceiling 18 px, largest job in the set gets the ceiling; equal mode 3 px.
- 8 colors = the 8 values of `COLORS` in `jobstats-charts.js` (`#60a5fa #f59e0b #34d399 #f87171 #a78bfa #22d3ee #d4a373 #94a3b8`); rest is neutral gray, legend "other (N groups)".
- Ring when `low_duty_gpus > 0`. Hollow when `coverage.gpu_pct < 1` or `coverage.sm < 1` or `summary_gpus < alloc_gpus`.
- Hover hit: distance ≤ max(8, radius) from the centre; nearest centre wins.
- URL keys: `view=scatter`, `size=gpuh|equal` (default `gpuh`), `split=0|1` (default `0`); defaults are omitted from the URL like the others.

## Review Focus

1. A filter that leaves 0 plotted rows (or 0 selected rows): the plot shows the count line and "No jobs to plot", no exception, no NaN axes. Test in Task 2 (`test_scatter_empty`) and Task 3 (`test_render_empty`).
2. Group key is an empty string (job with no name) or contains `<`/`&`: it gets a color and its legend text renders literally. Test in Task 2 (`test_colors_empty_and_markup_names`) and Task 3 (`test_render_uses_text_nodes`).
3. Values outside 0-100 (`sm` 104 from rounding, negative) are clamped to the frame, not drawn outside it. Test in Task 3 (`test_render_clamps_values`).
4. Client without `Accept-Encoding: gzip`, and bodies at or under 64 KiB, get plain JSON with no `Content-Encoding`. Test in Task 1.
5. Switching Jobs → Scatter → Jobs keeps filters and does not refilter (same `st.cache` signature); the scatter redraws on window resize. Checked by hand in Task 4, step list.

---

### Task 1: Uncapped, compressed jobs endpoint

**Files:**
- Modify: `jobstats_web.py` (`MAX_ROWS` line 40; `api_jobstats_jobs` ~550-575)
- Modify: `static/jobstats.js` (`load()`, the `setMsg(body.truncated ...)` line)
- Test: `tests/test_jobstats_web.py` (class `LimitTests`)

**Interfaces:**
- Produces: response field `total` (int, selected rows before the cut) beside `jobs`, `counts`, `window`, `truncated`. Helper `_json_response(body: dict) -> flask.Response` applying gzip rules.

- [ ] **Step 1: Write the failing tests** in `LimitTests`:

```python
def test_max_rows_is_60000(self):
    self.assertEqual(jw.MAX_ROWS, 60000)

def test_truncated_reports_total(self):          # extend the existing test_truncated_over_max_rows
    ...  # same 11 rows, MAX_ROWS patched to 10
    self.assertEqual(body["total"], 11)

def test_not_truncated_has_total(self):
    # 3 rows, default MAX_ROWS
    self.assertFalse(body["truncated"]); self.assertEqual(body["total"], 3); self.assertEqual(len(body["jobs"]), 3)

def test_gzip_when_accepted_and_large(self):
    # 400 rows (> 64 KiB of JSON), header Accept-Encoding: gzip, deflate
    self.assertEqual(r.headers["Content-Encoding"], "gzip")
    self.assertIn("Accept-Encoding", r.headers["Vary"])
    self.assertEqual(json.loads(gzip.decompress(r.data)), plain_body_without_generated_at)  # compare jobs/total/counts

def test_no_gzip_without_header_or_small(self):
    # same 400 rows without the header -> no Content-Encoding, r.get_json() works
    # 1 row with the header -> no Content-Encoding
```

Use `sacct_line(jobid=str(i), name="train-" + "x" * 200, ...)` to make 400 rows exceed 64 KiB, and `self.client.get(url, headers={"Accept-Encoding": "gzip, deflate"})`.

- [ ] **Step 2: Run them to verify they fail**

Run: `runuser -u adovlatyan -- /home/adovlatyan/Slurm-Monitoring/.venv/bin/python -m unittest tests.test_jobstats_web.LimitTests -q` (from the worktree)
Expected: FAIL on `MAX_ROWS` 5000, missing `total`, missing `Content-Encoding`.

- [ ] **Step 3: Implement.** `MAX_ROWS = 60000`. Add `"total": len(selected)` to the body. Add `_json_response(body)` that serializes with `json.dumps(body, separators=(",", ":"))`, and gzips (`gzip.compress(data, 6)`) only when `"gzip"` is in `request.headers.get("Accept-Encoding", "")` and `len(data) > 65536`; it always sets `Vary: Accept-Encoding` and `Content-Type: application/json`. Use it only in `api_jobstats_jobs`. In `jobstats.js` `load()`, replace the message with the exact text from Global Constraints using `body.total`.

- [ ] **Step 4: Run the class, then the suite**

Expected: `LimitTests` pass; suite 193 + new tests pass, 1 skipped.

- [ ] **Step 5: Commit** `jobstats_web.py static/jobstats.js tests/test_jobstats_web.py` — "Explorer: return up to 60,000 jobs, gzip large responses".

---

### Task 2: Pure scatter helpers in `jobstats-core.js`

**Files:**
- Modify: `static/jobstats-core.js` (new section "scatter", add names to the exported object at ~line 508)
- Test: `tests/test_jobstats_js.py` (new class `JsScatterTests`, uses `run_js`)

**Interfaces:**
- Produces (all exported on `JobstatsCore`):
  - `SCATTER_COLORS`: the 8 colors from Global Constraints, in that order. `SCATTER_OTHER = "#6b7280"`.
  - `scatterSplit(rows) -> { points: Row[], excluded: { not_started, cpu_only, running, no_summary, no_value: {jobs, gpu_hours} }, plotted: {jobs, gpu_hours}, total_gpu_hours }`. Each row lands in the first matching reason; `points` keep input order.
  - `scatterColors(points, mode) -> { colorOf: (row) => string, legend: [{key, gpu_hours, color}], other_groups: int }`. Top 8 groups by summed `gpu_hours` under `groupKey(row, mode)`, ties by key ascending (`<` string compare); `legend` in that order.
  - `scatterRadius(gpuHours, maxGpuHours, size) -> number`. `size === "equal"` → 3. Else `maxGpuHours <= 0` → 2; otherwise `max(2, 18 * sqrt(gpuHours / maxGpuHours))`.
  - `scatterBucket(allocGpus) -> "1" | "2-7" | "8+"`.
  - `scatterMarks(row) -> { ring: bool, hollow: bool }` per Global Constraints.
  - `scatterIndex(dots, cell) -> index` and `scatterHit(index, x, y) -> dot | null`. `dots` are `{x, y, r, row}` in pixels; `cell` default 24. Hit rule from Global Constraints; the lookup checks the cells that `max(8, rMax)` around (x, y) can reach, where `rMax` is the largest radius in the index.

- [ ] **Step 1: Write the failing tests** (`JsScatterTests`). Build rows with the existing helpers (`row_from(fixture_line(...))` / synthetic dicts carrying only the fields used).

```python
def test_split_reasons_and_order(self):
    # one row per reason + 2 plotted; a running row with no summary counts as "running", not "no_summary";
    # a CPU-only running row counts as "cpu_only"
    self.assertEqual(out["excluded"]["running"], {"jobs": 1, "gpu_hours": 4.0})
    self.assertEqual([r["jobid"] for r in out["points"]], ["p1", "p2"])
    self.assertEqual(out["plotted"]["jobs"], 2)

def test_scatter_empty(self):
    out = run_js("return core.scatterSplit([]);", None)
    self.assertEqual(out["plotted"], {"jobs": 0, "gpu_hours": 0}); self.assertEqual(out["points"], [])

def test_colors_top8_and_ties(self):
    # 10 groups, gpu_hours 10..1; two tied at 3 -> key order decides; groups 9-10 -> SCATTER_OTHER, other_groups == 2
def test_colors_follow_mode(self):
    # names "run-1","run-2": mode name -> 2 legend entries, family -> 1 ("run-#")
def test_colors_empty_and_markup_names(self):
    # name "" and name "<b>&x" each get a legend entry with key exactly "" / "<b>&x"

def test_radius(self):
    self.assertEqual(r(100, 100, "gpuh"), 18); self.assertAlmostEqual(r(25, 100, "gpuh"), 9)
    self.assertEqual(r(0.0001, 100, "gpuh"), 2); self.assertEqual(r(5, 0, "gpuh"), 2); self.assertEqual(r(5, 5, "equal"), 3)

def test_bucket(self):
    self.assertEqual([b(1), b(2), b(7), b(8), b(16)], ["1", "2-7", "2-7", "8+", "8+"])

def test_marks(self):
    # low_duty_gpus 1 -> ring; coverage {"gpu_pct":1,"sm":0.5} -> hollow; summary_gpus 6 < alloc_gpus 8 -> hollow;
    # healthy row -> {ring: false, hollow: false}; coverage null -> hollow false unless summary_gpus < alloc_gpus

def test_hit(self):
    dots = [{"x":100,"y":100,"r":18,"row":{"jobid":"big"}}, {"x":130,"y":100,"r":2,"row":{"jobid":"small"}}]
    # (115,100): big 15 <= 18 is a hit; small 15 > max(8,2) is not -> "big"
    # (124,100): big 24 > 18, small 6 <= 8 -> "small"
    # (100,100) with a second dot {"x":100,"y":100,"r":5,"row":{"jobid":"twin"}} appended -> tie at 0 -> "big" (earlier in input)
    # (100,140): nothing -> None
```

- [ ] **Step 2: Run** `... -m unittest tests.test_jobstats_js.JsScatterTests -q`. Expected: FAIL, `core.scatterSplit is not a function`.

- [ ] **Step 3: Implement** the helpers with the signatures above. Ties in `scatterHit` go to the earlier dot in `dots` order.

- [ ] **Step 4: Run the class, then the suite.** Expected: pass.

- [ ] **Step 5: Commit** `static/jobstats-core.js tests/test_jobstats_js.py` — "Explorer: scatter helpers (classification, colors, radius, hit test)".

---

### Task 3: Scatter renderer `jobstats-scatter.js`

**Files:**
- Create: `static/jobstats-scatter.js` (UMD-style like `jobstats-charts.js`: `root.JobstatsScatter = { render }`)
- Modify: `static/jobstats.css` (scatter classes, prefix `js-sc-`)
- Test: `tests/test_jobstats_js.py` (class `JsScatterRenderTests`, extend `STUB_DOM` with a recording canvas)

**Interfaces:**
- Consumes: Task 2 exports via `root.JobstatsCore`.
- Produces: `JobstatsScatter.render(container, rows, opts) -> { redraw() }` where `opts = { mode, size: "gpuh"|"equal", split: bool, onOpen(row, newTab) }`. It fills `container` with: the count line, one panel (or three: "1 GPU", "2–7 GPUs", "8+ GPUs"), the legend, the two notes from the spec, and one tooltip element.

- [ ] **Step 1: Write the failing tests.** Extend the stub so `createElement("canvas")` returns an element with `getContext()` returning a proxy that records `arc`, `fill`, `stroke` calls with arguments, and `getBoundingClientRect()` returning `{left:0, top:0, width:600, height:400}`. Load `jobstats-core.js` then `jobstats-scatter.js` with `self` set, as the time-series render tests do.

```python
def test_render_counts_and_panels(self):   # 3 plotted rows, 1 running -> count line text contains "Plotted 3 jobs" and "running 1"
def test_render_split_three_panels(self):  # split true -> 3 canvases; labels "1 GPU", "2–7 GPUs", "8+ GPUs"
def test_render_empty(self):               # 0 rows -> text "No jobs to plot", no arc calls, no exception
def test_render_clamps_values(self):       # sm 104, gpu_pct -3 -> arc centre inside the plot frame
def test_render_uses_text_nodes(self):     # name "<b>&x" appears in a textContent, never as markup; no innerHTML assigned
def test_draw_order_largest_first(self):   # arc radii in call order are non-increasing
```

The count line text follows the spec: `Plotted N jobs, X GPU-h. Not plotted: running a (b GPU-h), no summary …` listing only non-zero reasons, then `x % of the selection's GPU-hours is not on the plot` when x > 0. Reason labels: `not started`, `CPU-only`, `running`, `no summary`, `no duty or SM value`. CPU-only shows only the job count.

- [ ] **Step 2: Run** `... JsScatterRenderTests -q`. Expected: FAIL, file missing.

- [ ] **Step 3: Implement.** Canvas sized to the panel's CSS width × `devicePixelRatio`. Axes 0-100 both ways, gridlines every 20, axis titles "GPU duty %" and "SM %", dashed diagonal labeled "SM = duty (reference)". Dots sorted by radius descending, fill alpha 0.55, hollow = stroke only in the group color, ring = 1.5 px dark stroke over the fill. Radius scale uses the max `gpu_hours` over all plotted points, shared across split panels. One `scatterIndex` per panel; `pointermove` (throttled with `requestAnimationFrame`) highlights the hit and fills the tooltip with: job id, name, user, GPUs, GPU-h (`fmtGpuHours`), duty %, SM % (`fmtPct`), low-duty `k of n` (n = `summary_gpus`), coverage when < 1. `click` calls `opts.onOpen(row, e.ctrlKey || e.metaKey)`. `aria-label` on each canvas: `"Scatter of N jobs: GPU duty against SM"`. Legend: 8 swatches with key and GPU-h, then "other (N groups)", then "ring: a GPU under 20 % duty" and "hollow: not every GPU reported". Notes: the two sentences from the spec's "Axes and reference line" and "Text on the page".

- [ ] **Step 4: Run the class, then the suite.** Expected: pass.

- [ ] **Step 5: Commit** `static/jobstats-scatter.js static/jobstats.css tests/test_jobstats_js.py` — "Explorer: scatter renderer".

---

### Task 4: Wire the Scatter view into the page

**Files:**
- Modify: `static/jobstats.html` (view button, scatter toolbar, container `#js-scatter-wrap`, script tag, two notes in "How the numbers are computed", bump `?v=` to `m6-sc` on all jobstats assets)
- Modify: `static/jobstats.js` (`DEF`, `readUrl`, `writeUrl`, `syncControls`, `render`, bindings, resize)
- Test: `tests/test_jobstats_js.py` (class `JsPageWiringTests`: static checks on the HTML)

**Interfaces:**
- Consumes: `JobstatsScatter.render` (Task 3), `jobHref(r)` in `jobstats.js` for click-through.

- [ ] **Step 1: Write the failing tests** (read the HTML file as text):

```python
def test_scatter_view_button(self):    # '#js-view' contains <button type="button" data-v="scatter">Scatter</button>
def test_scatter_script_order(self):   # jobstats-scatter.js tag after jobstats-core.js and before jobstats.js
def test_scatter_notes(self):          # note text contains "one dot per job with a summary" and "even the part outside it"
def test_asset_versions_bumped(self):  # every jobstats*.js / jobstats.css reference carries ?v=m6-sc
```

- [ ] **Step 2: Run** `... JsPageWiringTests -q`. Expected: FAIL.

- [ ] **Step 3: Implement.**
  - HTML: Scatter button in `#js-view`; a `#js-sc-bar` (hidden unless view is scatter) with two segmented controls, `#js-size` (`gpuh` "GPU-hours", `equal` "equal") and `#js-split` (`0` "one plot", `1` "split by GPUs"); `<div id="js-scatter-wrap" hidden>`; the two note lines from the spec.
  - JS: `DEF.size = "gpuh"`, `DEF.split = "0"`; `readUrl` accepts `view` in `["jobs","groups","scatter"]`, `size`, `split`; `writeUrl` adds `size`, `split` to the defaults-omitted keys. `syncControls` shows `#js-by` for groups and scatter, `#js-sc-bar` for scatter, hides the "more" button in scatter. `render()` calls `JobstatsScatter.render($("#js-scatter-wrap"), filtered(), {mode: st.by, size: st.size, split: st.split === "1", onOpen})` where `onOpen(row, newTab)` opens `jobHref(row)` in a new tab when `newTab`, else navigates the same way a job link click does. One `resize` listener (debounced 150 ms) calls `redraw()` on the current handle when the scatter is visible. The `js-stats` totals line stays as in other views.

- [ ] **Step 4: Run the suite.** Expected: all pass.

- [ ] **Step 5: Check by hand** on a scratch instance (not the shared unit): `cd /home/adovlatyan/Slurm-Monitoring-ts && runuser -u adovlatyan -- env PATH=/cm/local/apps/slurm/25.05/bin:$PATH SLURM_DASHBOARD_DISABLE_RECORDER=1 SLURM_DASHBOARD_HOST=127.0.0.1 SLURM_DASHBOARD_PORT=19528 SLURM_DASHBOARD_GPU_SOURCE=prometheus /home/adovlatyan/Slurm-Monitoring/.venv/bin/python app.py` in the background, then with curl: `/api/jobstats/jobs` for 1 d and 20 d with `Accept-Encoding: gzip` (expect `Content-Encoding: gzip`, `total` = row count, `truncated` false) and `/jobstats?view=scatter` (200, includes `jobstats-scatter.js?v=m6-sc`). Record sizes and timings in the ledger. Browser checks (each toggle, empty filter, click-through, Jobs → Scatter → Jobs keeps filters, resize) are for Aram via the SSH tunnel; list them in the final message. Stop the scratch instance afterwards.

- [ ] **Step 6: Commit** `static/jobstats.html static/jobstats.js tests/test_jobstats_js.py` — "Explorer: Scatter view".

---

After the final review: merge `feature/jobstats-scatter` into `bcm11-jobstats` and restart `slurm-dashboard-aram` only when Aram asks (shared deploy, stated reverse path: `git reset --hard 7601863` and restart). Then the onboarding HTML artifact for the EDA tools, built by a subagent.
