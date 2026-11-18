/* Jobstats page logic without DOM access: search tokens, filters, sorting, grouping and the
   group aggregates of spec 5.2. aggregate() mirrors jobstats_web.aggregate(), and
   tests/test_jobstats_js.py runs both on the same rows and compares the results. */
(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.JobstatsCore = api;
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  const MEAN_KEYS = ["gpu_pct", "sm", "tc", "dram", "occ", "fp32", "int", "power_w"];
  const SEEN_KEYS = ["fp16_seen", "fp64_seen"];
  const TOKEN_KINDS = ["id", "user", "node", "state", "summary"];
  // Job-level values the "below / at least" filter can use. max 100 = a percentage.
  const FILTER_METRICS = [
    { key: "gpu_pct", label: "GPU %", max: 100 }, { key: "sm", label: "SM %", max: 100 },
    { key: "tc", label: "TC %", max: 100 }, { key: "dram", label: "DRAM %", max: 100 },
    { key: "occ", label: "OCC %", max: 100 }, { key: "fp32", label: "FP32 %", max: 100 },
    { key: "int", label: "INT %", max: 100 }, { key: "power_w", label: "Power W", max: null },
    { key: "cpu_eff", label: "CPU eff %", max: 100 },
    { key: "gpu_mem_peak_pct", label: "GPU mem peak %", max: 100 },
    { key: "cpu_mem_peak_pct", label: "CPU mem peak %", max: 100 },
  ];
  const filterMetric = (k) => (FILTER_METRICS.some((m) => m.key === k) ? k : "gpu_pct");
  // EpilogSlurmctld started writing summaries at 2026-10-29 19:10:37 UTC (jobstats runbook, step 06).
  const HOOK_LIVE_S = 1793301037;
  // The epilog writes up to about 3 min after job end; wait 10 min before calling a summary missing.
  const UNWRITTEN_GRACE_S = 600;

  // ------------------------------------------------------------------ search and filters

  function parseQuery(q) {
    const out = { terms: [], id: [], user: [], node: [], state: [], summary: [] };
    for (const tok of String(q || "").trim().split(/\s+/)) {
      if (!tok) continue;
      const m = /^([a-z]+):(.*)$/i.exec(tok);
      const kind = m ? m[1].toLowerCase() : null;
      if (kind && TOKEN_KINDS.includes(kind)) {
        if (m[2]) out[kind].push(m[2].toLowerCase());
      } else {
        out.terms.push(tok.toLowerCase());
      }
    }
    return out;
  }

  function globRe(v) {
    return new RegExp("^" + v.replace(/[.+?^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*") + "$", "i");
  }

  function idMatch(row, v) {
    if (v.includes("*")) {
      const re = globRe(v);
      return re.test(row.jobid) || re.test(row.jobid_raw);
    }
    return row.jobid === v || row.jobid_raw === v;
  }

  function userMatch(row, v) {
    const u = (row.user || "").toLowerCase();
    return v.includes("*") ? globRe(v).test(u) : u === v;
  }

  function nodeMatch(row, v) {
    const re = v.includes("*") ? globRe(v) : null;
    return row.nodes.some((n) => (re ? re.test(n) : n.toLowerCase() === v));
  }

  function isUnwritten(row, nowS) {
    return row.summary_state === "missing" && !row.not_started && row.elapsed_s > 0 &&
      row.nodes.length > 0 && row.end != null && row.end > HOOK_LIVE_S && row.end < nowS - UNWRITTEN_GRACE_S;
  }

  function summaryMatch(row, v, nowS) {
    if (v === "unwritten") return isUnwritten(row, nowS);
    return row.summary_state === v;
  }

  function gpuCountMatch(n, sel) {
    switch (sel) {
      case "0": return n === 0;
      case "1": return n === 1;
      case "2-7": return n >= 2 && n <= 7;
      case "8+": return n >= 8;
      default: return true;
    }
  }

  /* Tokens of one kind are OR'd, different kinds and free-text terms are AND'd.
     Never-started rows are hidden unless the query has a state: or summary: token. */
  function filterRows(rows, query, filters, nowS) {
    const p = typeof query === "string" ? parseQuery(query) : query;
    const f = filters || {};
    const showUnstarted = p.state.length > 0 || p.summary.length > 0;
    const mk = filterMetric(f.metric);
    return rows.filter((r) => {
      if (r.not_started && !showUnstarted) return false;
      if (p.terms.length) {
        const name = r.name.toLowerCase();
        for (const t of p.terms) if (!name.includes(t)) return false;
      }
      if (p.id.length && !p.id.some((v) => idMatch(r, v))) return false;
      if (p.user.length && !p.user.some((v) => userMatch(r, v))) return false;
      if (p.node.length && !p.node.some((v) => nodeMatch(r, v))) return false;
      if (p.state.length && !p.state.some((v) => r.state.toLowerCase().startsWith(v))) return false;
      if (p.summary.length && !p.summary.some((v) => summaryMatch(r, v, nowS))) return false;
      if (!gpuCountMatch(r.alloc_gpus, f.gpus)) return false;
      if (f.minGpuHours && !(r.gpu_hours >= f.minGpuHours)) return false;
      // rows without a value for the metric never match a bound
      if (f.below != null && !(r[mk] != null && r[mk] < f.below)) return false;
      if (f.above != null && !(r[mk] != null && r[mk] >= f.above)) return false;
      return true;
    });
  }

  // ------------------------------------------------------------------ sorting

  // Array parent first, then task; a plain job sorts before task 0 of an array with the same id.
  function compareJobIds(x, y) {
    const px = Number(x.array_job || x.jobid_raw) || 0, py = Number(y.array_job || y.jobid_raw) || 0;
    if (px !== py) return px < py ? -1 : 1;
    const tx = x.array_task != null ? (Number(x.array_task) || 0) + 1 : 0;
    const ty = y.array_task != null ? (Number(y.array_task) || 0) + 1 : 0;
    return tx === ty ? 0 : (tx < ty ? -1 : 1);
  }

  const SORT_VALUES = {
    name: (r) => r.name.toLowerCase(),
    user: (r) => (r.user || "").toLowerCase(),
    state: (r) => r.state,
    nodes: (r) => r.nodes.join(","),
    summary_state: (r) => r.summary_state,
    // Running jobs have no end yet: they sort as the latest.
    end: (r) => (r.end == null ? (r.not_started ? null : Infinity) : r.end),
  };

  function sortValue(r, key) {
    const f = SORT_VALUES[key];
    return f ? f(r) : r[key];
  }

  function compareValues(a, b, dir) {
    if (a == null && b == null) return 0;
    if (a == null) return 1;            // missing values always last
    if (b == null) return -1;
    if (typeof a === "string") return a.localeCompare(b) * dir;
    if (a === b) return 0;
    return (a < b ? -1 : 1) * dir;
  }

  function sortRows(rows, key, dir) {
    const d = dir === "asc" ? 1 : -1;
    return rows.slice().sort((x, y) =>
      (key === "jobid" ? compareJobIds(x, y) * d : compareValues(sortValue(x, key), sortValue(y, key), d)) ||
      compareValues(sortValue(x, "end"), sortValue(y, "end"), -1) ||
      (Number(y.jobid_raw) - Number(x.jobid_raw)));
  }

  // ------------------------------------------------------------------ aggregation (spec 5.2)

  function percentile(sorted, q) {
    const pos = (sorted.length - 1) * q;
    const lo = Math.floor(pos), hi = Math.ceil(pos);
    return sorted[lo] + (sorted[hi] - sorted[lo]) * (pos - lo);
  }

  function spread(vals) {
    if (vals.length < 3) return null;
    const s = vals.slice().sort((a, b) => a - b);
    return { p10: percentile(s, 0.1), median: percentile(s, 0.5), p90: percentile(s, 0.9) };
  }

  const sum = (a, f) => a.reduce((t, r) => t + f(r), 0);
  const fieldGpus = (r, key) => (r.field_gpus || {})[key] || 0;

  function aggregate(rowsIn) {
    const rows = rowsIn.filter((r) => !r.not_started);
    const ok = rows.filter((r) => r.summary_state === "ok");
    const allocH = sum(rows, (r) => r.gpu_hours);
    const summH = sum(ok, (r) => r.gpu_hours);
    const states = {};
    for (const r of rows) states[r.summary_state] = (states[r.summary_state] || 0) + 1;
    const out = {
      jobs: rows.length,
      states,
      gpu_hours: allocH,
      summarized_gpu_hours: summH,
      summarized_share: allocH ? summH / allocH : null,
    };
    for (const key of MEAN_KEYS) {
      let num = 0, den = 0;
      for (const r of ok) {
        const n = fieldGpus(r, key);
        if (r[key] != null && n) {
          num += r.elapsed_s * r[key] * n;
          den += r.elapsed_s * n;
        }
      }
      out[key] = { mean: den ? num / den : null, spread: spread(ok.filter((r) => r[key] != null).map((r) => r[key])) };
    }
    const cpu = ok.filter((r) => r.cpu_eff != null);
    const avail = sum(cpu, (r) => r.cpu_avail_s);
    out.cpu_eff = { mean: avail ? sum(cpu, (r) => r.cpu_used_s) / avail * 100 : null,
                    spread: spread(cpu.map((r) => r.cpu_eff)) };
    for (const key of ["cpu_mem_peak_pct", "gpu_mem_peak_pct"]) {
      const vals = ok.filter((r) => r[key] != null).map((r) => r[key]).sort((a, b) => a - b);
      out[key] = vals.length ? { median: percentile(vals, 0.5), max: vals[vals.length - 1] } : null;
    }
    for (const key of SEEN_KEYS) {
      const vals = ok.filter((r) => r[key] != null);
      out[key] = vals.length ? vals.filter((r) => r[key] === true).length / vals.length : null;
    }
    const gpuOk = ok.filter((r) => r.alloc_gpus);
    const allocS = sum(gpuOk, (r) => r.elapsed_s * r.alloc_gpus);
    out.coverage = allocS ? sum(gpuOk, (r) => r.elapsed_s * fieldGpus(r, "gpu_pct")) / allocS : null;
    return out;
  }

  function groupKey(row, mode) {
    if (mode === "array") return row.array_job || row.jobid;
    // \p{Nd} matches what Python's \d matches in jobstats_web.group_key (any Unicode decimal digit).
    if (mode === "family") return row.name.replace(/\p{Nd}+/gu, "#");
    return row.name;
  }

  /* Groups of the given rows, never-started rows excluded, largest GPU-hours first. */
  function groupRows(rows, mode) {
    const map = new Map();
    for (const r of rows) {
      if (r.not_started) continue;
      const k = groupKey(r, mode);
      let g = map.get(k);
      if (!g) { g = { key: k, rows: [], names: new Map(), jobStates: {} }; map.set(k, g); }
      g.rows.push(r);
      g.names.set(r.name, (g.names.get(r.name) || 0) + 1);
      const st = r.state.split(" ")[0];
      g.jobStates[st] = (g.jobStates[st] || 0) + 1;
    }
    const out = [];
    for (const g of map.values()) {
      g.agg = aggregate(g.rows);
      g.gpuPctValues = g.rows.filter((r) => r.summary_state === "ok" && r.gpu_pct != null)
        .map((r) => r.gpu_pct).sort((a, b) => a - b);
      g.names = [...g.names.entries()].sort((a, b) => b[1] - a[1] || (a[0] < b[0] ? -1 : 1));
      out.push(g);
    }
    out.sort((a, b) => (b.agg.gpu_hours - a.agg.gpu_hours) || (b.agg.jobs - a.agg.jobs) ||
      (String(a.key) < String(b.key) ? -1 : 1));
    return out;
  }

  // ------------------------------------------------------------------ display helpers

  // Same thresholds and colours as color() in gpu-telemetry.js, so the dashboard and this page agree.
  function gpuColor(v) {
    if (typeof v !== "number" || !Number.isFinite(v)) return "#64748b";
    return v < 20 ? "#b91c1c" : v < 40 ? "#c2410c" : v < 60 ? "#a16207" : v < 80 ? "#4d7c0f" : "#15803d";
  }

  function summaryLabel(row, nowS) {
    if (row.not_started) return { cls: "na", text: "not started", title: "The job never started" };
    switch (row.summary_state) {
      case "ok": return { cls: "ok", text: "ok", title: "Summary stored by jobstats" };
      case "short": return { cls: "warn", text: "under 60 s", title: "Ran under 60 s, too short for a summary" };
      case "none": return { cls: "warn", text: "no data", title: "jobstats found no Prometheus data" };
      case "invalid": return { cls: "bad", text: "invalid: " + (row.invalid_reason || "?"), title: "The stored summary could not be decoded" };
      case "other": return { cls: "warn", text: "other", title: "AdminComment holds something other than a jobstats summary" };
      default: break;
    }
    if (isUnwritten(row, nowS)) {
      return { cls: "bad", text: "not written", title: "Ended after summaries started (29 Oct 19:10 UTC) but none was stored" };
    }
    if (row.end == null) return { cls: "na", text: "—", title: "Running: the summary is written after the job ends" };
    if (row.end <= HOOK_LIVE_S) return { cls: "na", text: "—", title: "Ended before summaries started (29 Oct 19:10 UTC)" };
    return { cls: "na", text: "—", title: "No summary" };
  }

  const DASH = "—";

  function fmtPct(v, digits) {
    if (v == null || !Number.isFinite(v)) return DASH;
    return v.toFixed(digits == null ? 1 : digits) + "%";
  }

  function fmtNum(v, digits) {
    if (v == null || !Number.isFinite(v)) return DASH;
    return v.toLocaleString("en-US", { minimumFractionDigits: digits || 0, maximumFractionDigits: digits || 0 });
  }

  // Memory uses 1024^3 and the label "GB", like the jobstats CLI (spec 4.5).
  function fmtGB(bytes) {
    if (bytes == null || !Number.isFinite(bytes)) return DASH;
    return (bytes / 1024 ** 3).toFixed(1) + " GB";
  }

  // sacct style: [D-]HH:MM:SS
  function fmtElapsed(s) {
    if (s == null || !Number.isFinite(s)) return DASH;
    s = Math.round(s);
    const d = Math.floor(s / 86400);
    s -= d * 86400;
    const p = (n) => String(n).padStart(2, "0");
    return (d ? d + "-" : "") + p(Math.floor(s / 3600)) + ":" + p(Math.floor((s % 3600) / 60)) + ":" + p(s % 60);
  }

  function fmtGpuHours(h) {
    if (h == null || !Number.isFinite(h)) return DASH;
    return fmtNum(h, h < 10 ? 2 : 1);
  }

  // ------------------------------------------------------------------ job page (per GPU, per node)

  // JS1 per-GPU field -> [column key, scale]. Mirrors MEAN_FIELDS in jobstats_web.py.
  const GPU_FIELDS = {
    gpu_pct: ["gpu_utilization", 1], sm: ["sm_util_percent_avg", 100],
    tc: ["any_tensor_util_percent_avg", 100], dram: ["dram_bw_util_percent_avg", 100],
    occ: ["sm_occupancy_percent_avg", 100], fp32: ["fp32_util_percent_avg", 100],
    int: ["integer_util_avg", 100], power_w: ["power_usage_milliwatts_avg", 0.001],
    temp_c: ["temperature_celsius_avg", 1],
  };
  const GIB = 1024 ** 3;
  const LINK_FIELDS = {
    nvlink_rx_gbs: "nvlink_total_rx_per_sec_avg", nvlink_tx_gbs: "nvlink_total_tx_per_sec_avg",
    pcie_rx_gbs: "pcie_rx_per_sec_avg", pcie_tx_gbs: "pcie_tx_per_sec_avg",
  };
  const num = (v) => (typeof v === "number" && Number.isFinite(v) ? v : null);
  const isObj = (v) => v != null && typeof v === "object" && !Array.isArray(v);

  function minorOrder(a, b) {
    return a.length - b.length || (a < b ? -1 : a > b ? 1 : 0);
  }

  // nodes: the "nodes" object of /api/jobstats/job. Returns one row per GPU (node order as given,
  // GPUs by minor number) and the job summary over GPUs (unweighted means, as spec 5.1).
  function gpuTable(nodes) {
    const gpus = [];
    for (const [node, info] of Object.entries(isObj(nodes) ? nodes : {})) {
      if (!isObj(info)) continue;
      const per = isObj(info.gpus) ? info.gpus : {};
      for (const minor of Object.keys(per).sort(minorOrder)) {
        const f = isObj(per[minor]) ? per[minor] : {};
        const g = { node, minor, raw: f };
        for (const [key, [field, scale]] of Object.entries(GPU_FIELDS)) {
          const v = num(f[field]);
          g[key] = v == null ? null : num(v * scale);
        }
        for (const [key, field] of Object.entries(LINK_FIELDS)) {
          const v = num(f[field]);
          g[key] = v == null ? null : num(v / GIB);
        }
        const used = num(f.gpu_used_memory), total = num(f.gpu_total_memory);
        g.mem_used_b = used;
        g.mem_total_b = total;
        g.mem_pct = used != null && total != null && total > 0 ? num((used / total) * 100) : null;
        const seen = (x) => { const v = num(x); return v == null ? null : v > 0; };
        g.fp16_seen = seen(f.fp16_util_percent_max);
        g.fp64_seen = seen(f.fp64_util_percent_max);
        gpus.push(g);
      }
    }
    const summary = {};
    for (const [key, [field, scale]] of Object.entries(GPU_FIELDS)) {
      const vals = gpus.map((g) => num(g.raw[field])).filter((v) => v != null);
      // divide before summing, like derive_metrics
      summary[key] = vals.length ? num(vals.reduce((a, v) => a + v / vals.length, 0) * scale) : null;
    }
    const used = gpus.map((g) => g.mem_used_b).filter((v) => v != null);
    const pcts = gpus.map((g) => g.mem_pct).filter((v) => v != null);
    summary.mem_peak_b = used.length ? Math.max(...used) : null;
    summary.mem_peak_pct = pcts.length ? Math.max(...pcts) : null;
    const util = gpus.filter((g) => g.gpu_pct != null);
    summary.low_duty = util.length ? util.filter((g) => g.gpu_pct < 20).length : null;
    for (const key of Object.keys(LINK_FIELDS)) {
      const vals = gpus.map((g) => g[key]).filter((v) => v != null);
      summary[key] = vals.length ? num(vals.reduce((a, v) => a + v / vals.length, 0)) : null;
    }
    for (const key of ["fp16_seen", "fp64_seen"]) {
      const vals = gpus.map((g) => g[key]).filter((v) => v != null);
      summary[key] = vals.length ? vals.some(Boolean) : null;
    }
    for (const g of gpus) delete g.raw;
    return { gpus, summary };
  }

  // Header start/end for the job page. A pending job may carry Slurm's estimated start; hide it.
  function lifecycle(row) {
    if (row.not_started) {
      return { start: null, end: null, label: String(row.state || "").startsWith("PENDING") ? "pending" : "never started" };
    }
    if (row.end == null) return { start: row.start, end: null, label: "running" };
    return { start: row.start, end: row.end, label: null };
  }

  function nodeRows(nodes) {
    return Object.entries(isObj(nodes) ? nodes : {}).map(([node, n]) => {
      if (!isObj(n)) n = {};
      const peak = num(n.mem_peak_b), total = num(n.mem_total_b);
      return {
        node, cpus: num(n.cpus), cpu_eff: num(n.cpu_eff), mem_peak_b: peak, mem_total_b: total,
        mem_pct: peak != null && total != null && total > 0 ? num((peak / total) * 100) : null,
        gpus: isObj(n.gpus) ? Object.keys(n.gpus).length : 0,
      };
    });
  }

  // Notes above the job page tables (decision 3, option B). The 20 % GPU line is jobstats'
  // (LOW_DUTY_PCT in jobstats_web.py); the GPU memory and CPU efficiency limits are our choice and
  // are open for review.
  const NOTE_LIMITS = { low_gpu_pct: 20, gpu_mem_peak_pct: 25, cpu_eff: 20 };
  const COVER_LABEL = { gpu_pct: "GPU %", sm: "SM", tc: "TC", dram: "DRAM", occ: "OCC", fp32: "FP32",
                        int: "INT", power_w: "power", gpu_mem: "GPU memory" };

  function notePct(v) {
    return v < 0.1 ? "under 0.1 %" : v.toFixed(1) + " %";
  }

  // row: a job row from the API. Returns plain-text notes, most specific to the GPUs first.
  function jobNotes(row) {
    const out = [];
    const L = NOTE_LIMITS;
    const low = num(row.low_duty_gpus), n = num(row.summary_gpus);
    if (low) {
      out.push(low + " of " + n + " GPU" + (n === 1 ? "" : "s") + " averaged under " + L.low_gpu_pct + " % busy (GPU %).");
    }
    const partial = Object.entries(isObj(row.coverage) ? row.coverage : {})
      .filter(([, v]) => num(v) != null && v < 1);
    if (partial.length) out.push("Some GPUs have no value for: " + partial.map(([k]) => COVER_LABEL[k] || k).join(", ") + ".");
    const memPct = num(row.gpu_mem_peak_pct), memB = num(row.gpu_mem_peak_b);
    if (memPct != null && memB != null && memPct < L.gpu_mem_peak_pct) {
      out.push("GPU memory peaked at " + fmtGB(memB) + ", " + notePct(memPct) + " of the card (shown when under " + L.gpu_mem_peak_pct + " %).");
    }
    const eff = num(row.cpu_eff), cpus = num(row.alloc_cpus);
    if (eff != null && eff < L.cpu_eff) {
      out.push("CPU efficiency " + notePct(eff) + " over " + cpus + " allocated core" + (cpus === 1 ? "" : "s") +
               " (shown when under " + L.cpu_eff + " %).");
    }
    return out;
  }

  // ---- Job page time series (charts drawn by jobstats-charts.js) ----
  const TS_STEPS = [["30", "30 s"], ["60", "1 min"], ["120", "2 min"], ["300", "5 min"], ["auto", "auto"]];
  const TS_MAX_POINTS = 5000;   // jobstats_prom.MAX_POINTS
  // Chart order on the page (jobstats_prom.CHARTS). JSON object keys arrive sorted, so order lives here.
  const TS_ORDER = ["duty", "sm", "tensor", "dram", "gpu_mem", "power", "cpu", "ram"];

  // "Nice" ticks from 0 to at least max: step 1, 2, 2.5 or 5 x 10^k, at most count + 1 values.
  function tsTicks(max, count) {
    if (!(typeof max === "number" && max > 0 && Number.isFinite(max))) return [0, 1];
    const raw = max / count;
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    let step = 10 * mag;
    for (const m of [1, 2, 2.5, 5, 10]) {
      if (m * mag >= raw - 1e-12) { step = m * mag; break; }
    }
    const n = Math.ceil(max / step - 1e-9);
    const out = [];
    for (let i = 0; i <= n; i++) out.push(Math.round(i * step * 1e6) / 1e6);
    return out;
  }

  // x-axis for a job of spanS seconds: hours, or minutes under 2 h. Ticks are in that unit.
  function tsTimeAxis(spanS) {
    const unit = spanS < 7200 ? "min" : "h";
    const div = unit === "h" ? 3600 : 60;
    return { unit, div, ticks: tsTicks(spanS / div, 4) };
  }

  // Index of the point whose timestamp is closest to t (points sorted by time); -1 when empty.
  function tsNearest(points, t) {
    if (!Array.isArray(points) || !points.length) return -1;
    let lo = 0, hi = points.length - 1;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (points[mid][0] < t) lo = mid + 1; else hi = mid;
    }
    if (lo > 0 && t - points[lo - 1][0] <= points[lo][0] - t) return lo - 1;
    return lo;
  }

  // Chip and readout labels: "GPU 3" when the job ran on one host, else "gpu07/3".
  function tsGpuLabels(gpus) {
    const list = Array.isArray(gpus) ? gpus : [];
    const hosts = new Set(list.map((g) => g.host));
    const out = {};
    for (const g of list) out[g.key] = hosts.size <= 1 ? "GPU " + g.minor : g.host + "/" + g.minor;
    return out;
  }

  function tsStepLabel(s) {
    if (s < 60) return s + " s";
    if (s < 3600) return s / 60 + " min";
    return s / 3600 + " h";
  }

  // The line above the charts that states exactly what each point is.
  function tsStepText(body) {
    let text = tsStepLabel(body.step_s) + " steps";
    if (body.step_s === 30) return text + " (raw samples)";
    if (body.coarsened && body.requested_step) {
      text += " (" + tsStepLabel(body.requested_step) + " would exceed " + TS_MAX_POINTS + " points)";
    } else if (body.requested_step === null) {
      text += " (auto)";
    }
    return text + (body.agg === "max" ? ", peak per window" : ", average per window");
  }

  return {
    MEAN_KEYS, SEEN_KEYS, FILTER_METRICS, HOOK_LIVE_S, UNWRITTEN_GRACE_S, DASH,
    parseQuery, filterRows, isUnwritten, sortRows, sortValue,
    percentile, spread, aggregate, groupKey, groupRows,
    gpuTable, nodeRows, lifecycle, NOTE_LIMITS, jobNotes, gpuColor, summaryLabel, fmtPct, fmtNum, fmtGB, fmtElapsed, fmtGpuHours,
    TS_STEPS, TS_ORDER, tsTicks, tsTimeAxis, tsNearest, tsGpuLabels, tsStepText,
  };
});
