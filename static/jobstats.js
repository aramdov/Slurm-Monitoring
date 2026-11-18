/* Jobstats page: Explorer (Jobs) and Groups views over /api/jobstats/jobs, plus the job view.
   Logic without DOM access lives in jobstats-core.js. Job text (names, states, nodes) only
   reaches the page through textContent or attribute setters, never through innerHTML. */
(function () {
  "use strict";
  const C = window.JobstatsCore;
  const DASH = C.DASH;
  const $ = (s) => document.querySelector(s);
  const DAY_MS = 86400000;
  const RANGES = { "1d": 1, "7d": 7, "30d": 30, "90d": 90 };
  const PAGE = 300;
  const GPAGE = 100;
  const MEMBER_LIMIT = 200;

  const DEF = { range: "7d", from: null, to: null, q: "", gpus: "all", mingh: 0, metric: "gpu_pct", below: null, above: null,
                sort: "end", dir: "desc", view: "jobs", by: "name", gsort: "gpu_hours", gdir: "desc" };
  const st = Object.assign({}, DEF, { limit: PAGE, glimit: GPAGE, data: null, open: new Set(), cache: null });

  // ------------------------------------------------------------------ DOM helpers

  function el(tag, attrs, ...kids) {
    const e = document.createElement(tag);
    if (attrs) {
      for (const [k, v] of Object.entries(attrs)) {
        if (v == null || v === false) continue;
        if (k === "class") e.className = v;
        else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
        else e.setAttribute(k, v === true ? "" : v);
      }
    }
    for (const k of kids) if (k != null) e.append(k instanceof Node ? k : String(k));
    return e;
  }

  function svgEl(tag, attrs) {
    const e = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [k, v] of Object.entries(attrs || {})) e.setAttribute(k, v);
    return e;
  }

  function badge(v) {
    if (v == null) return el("span", { class: "js-dim" }, DASH);
    return el("span", { class: "js-badge", style: "background:" + C.gpuColor(v) }, C.fmtPct(v));
  }

  function pill(row) {
    const s = C.summaryLabel(row, st.nowS);
    return el("span", { class: "js-pill " + s.cls, title: s.title }, s.text);
  }

  function stateCell(state) {
    const base = state.split(" ")[0];
    return el("span", { class: "st-" + base.replace(/[^A-Z_]/g, ""), title: state }, base);
  }

  function fmtTime(s) {
    if (s == null) return DASH;
    return new Date(s * 1000).toLocaleString("en-GB", { day: "2-digit", month: "short", hour: "2-digit",
                                                         minute: "2-digit", hour12: false });
  }

  function fmtDateTime(s) {
    if (s == null) return DASH;
    return new Date(s * 1000).toLocaleString("en-GB", { year: "numeric", day: "2-digit", month: "short",
                                                         hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
  }

  function endText(r) {
    if (r.not_started) return "never started";
    if (r.end == null) return "running";
    return fmtTime(r.end);
  }

  function jobHref(r) {
    return "jobstats?job=" + encodeURIComponent(r.jobid) + "&submit=" + encodeURIComponent(r.submit);
  }

  function jobLink(r) {
    return el("a", { href: jobHref(r), "data-job": r.jobid, "data-submit": r.submit }, r.jobid);
  }

  const memCell = (r) => (r.gpu_mem_peak_b == null ? DASH : C.fmtGB(r.gpu_mem_peak_b) + " · " + C.fmtPct(r.gpu_mem_peak_pct, 0));
  const peak = (p) => (p ? "max " + C.fmtPct(p.max, 0) : DASH);
  const peak2 = (p) => (p ? "med " + C.fmtPct(p.median, 0) + " · max " + C.fmtPct(p.max, 0) : DASH);
  const share = (v) => (v == null ? DASH : C.fmtPct(v * 100, 0));
  const MEAN_TITLE = "GPU-time weighted mean over jobs with a summary";

  // ------------------------------------------------------------------ columns

  const COLS = [
    { k: "jobid", h: "Job ID", cell: jobLink, tot: () => "Visible" },
    { k: "user", h: "User", cls: "user", cell: (r) => r.user || DASH },
    { k: "name", h: "Name", cls: "name", cell: (r) => r.name, title: (r) => r.name,
      tot: (a, rows) => C.fmtNum(a.jobs) + " jobs" +
        (rows.length > a.jobs ? " (+" + C.fmtNum(rows.length - a.jobs) + " never started, not counted)" : "") },
    { k: "state", h: "State", cell: (r) => stateCell(r.state) },
    { k: "end", h: "End", cell: endText,
      title: (r) => "submit " + fmtDateTime(r.submit) + "\nstart " + fmtDateTime(r.start) + "\nend " + fmtDateTime(r.end) },
    { k: "elapsed_s", h: "Elapsed", n: 1, cell: (r) => C.fmtElapsed(r.elapsed_s) },
    { k: "alloc_gpus", h: "GPUs", n: 1, cell: (r) => String(r.alloc_gpus) },
    { k: "gpu_hours", h: "GPU-h", n: 1, cell: (r) => (r.alloc_gpus ? C.fmtGpuHours(r.gpu_hours) : DASH),
      tot: (a) => C.fmtGpuHours(a.gpu_hours) },
    { k: "nodes", h: "Node", cls: "nodes", cell: (r) => r.nodes.join(",") || DASH, title: (r) => r.nodes.join(",") },
    { k: "gpu_pct", h: "GPU %", n: 1, cell: (r) => badge(r.gpu_pct), tot: (a) => badge(a.gpu_pct.mean), ttl: MEAN_TITLE },
    { k: "sm", h: "SM", n: 1, cell: (r) => C.fmtPct(r.sm), tot: (a) => C.fmtPct(a.sm.mean), ttl: MEAN_TITLE },
    { k: "tc", h: "TC", n: 1, cell: (r) => C.fmtPct(r.tc), tot: (a) => C.fmtPct(a.tc.mean), ttl: MEAN_TITLE },
    { k: "dram", h: "DRAM", n: 1, cell: (r) => C.fmtPct(r.dram), tot: (a) => C.fmtPct(a.dram.mean), ttl: MEAN_TITLE },
    { k: "gpu_mem_peak_pct", h: "GPU mem peak", n: 1, cell: memCell, tot: (a) => peak(a.gpu_mem_peak_pct),
      ttl: "Largest per-job peak", htitle: "Largest peak used on any GPU (GB = 1024³ bytes) and the largest peak as % of a GPU's memory. " +
        "On mixed GPU models the two can come from different GPUs." },
    { k: "cpu_eff", h: "CPU eff", n: 1, cell: (r) => C.fmtPct(r.cpu_eff), tot: (a) => C.fmtPct(a.cpu_eff.mean),
      ttl: "CPU-seconds used / allocated, jobs with a summary" },
    { k: "cpu_mem_peak_pct", h: "CPU mem peak", n: 1, cell: (r) => C.fmtPct(r.cpu_mem_peak_pct, 0),
      tot: (a) => peak(a.cpu_mem_peak_pct), ttl: "Largest per-job peak", htitle: "Peak on the fullest node, % of allocated memory" },
    { k: "power_w", h: "Power W", n: 1, cell: (r) => C.fmtNum(r.power_w, 0), tot: (a) => C.fmtNum(a.power_w.mean, 0),
      ttl: MEAN_TITLE, htitle: "Mean power per GPU" },
    { k: "summary_state", h: "Summary", cell: pill,
      tot: (a) => "ok " + C.fmtNum(a.states.ok || 0) + " of " + C.fmtNum(a.jobs) },
  ];
  const COLKEYS = COLS.map((c) => c.k);
  const MEMBER_COLS = COLS.filter((c) => !["gpu_mem_peak_pct", "cpu_mem_peak_pct", "power_w"].includes(c.k));

  function rowEl(r, cols) {
    const tr = el("tr");
    for (const c of cols) {
      tr.append(el("td", { class: [c.n ? "n" : "", c.cls || ""].join(" ").trim() || null,
                           title: c.title ? c.title(r) : null }, c.cell(r)));
    }
    return tr;
  }

  function sortHead(cols, key, dir, attr) {
    const tr = el("tr", { class: "js-head" });
    for (const c of cols) {
      const on = c.k === key;
      const th = el("th", { class: c.n ? "n" : null, title: c.htitle || null,
                            "aria-sort": on ? (dir === "asc" ? "ascending" : "descending") : null });
      th.append(el("button", { type: "button", class: "js-sort", [attr]: c.k },
                   c.h, el("span", { class: "ar" }, on ? (dir === "asc" ? "▲" : "▼") : "")));
      tr.append(th);
    }
    return tr;
  }

  // ------------------------------------------------------------------ URL state

  const num = (v) => (v === null || v === "" || !Number.isFinite(Number(v)) ? null : Number(v));
  const pick = (v, ok, d) => (ok.includes(v) ? v : d);

  function readUrl() {
    const p = new URLSearchParams(location.search);
    st.range = pick(p.get("range"), [...Object.keys(RANGES), "custom"], DEF.range);
    const minute = (ms) => (ms ? Math.floor(ms / 60000) * 60000 : null);
    st.from = minute(Date.parse(p.get("from") || ""));
    st.to = minute(Date.parse(p.get("to") || ""));
    if (st.range === "custom" && !(st.from && st.to)) st.range = DEF.range;
    if (p.has("from") && p.has("to") && st.from && st.to) st.range = "custom";
    st.q = p.get("q") || "";
    st.gpus = pick(p.get("gpus"), ["all", "0", "1", "2-7", "8+"], DEF.gpus);
    st.mingh = Number(pick(p.get("mingh"), ["0", "1", "10", "100"], "0"));
    st.metric = pick(p.get("metric"), C.FILTER_METRICS.map((m) => m.key), DEF.metric);
    st.below = num(p.get("below"));
    st.above = num(p.get("above"));
    st.sort = pick(p.get("sort"), COLKEYS, DEF.sort);
    st.dir = pick(p.get("dir"), ["asc", "desc"], DEF.dir);
    st.view = pick(p.get("view"), ["jobs", "groups"], DEF.view);
    st.by = pick(p.get("by"), ["name", "array", "family"], DEF.by);
    st.gsort = pick(p.get("gsort"), GCOLKEYS, DEF.gsort);
    st.gdir = pick(p.get("gdir"), ["asc", "desc"], DEF.gdir);
  }

  function isoMinute(ms) { return new Date(ms).toISOString().slice(0, 16) + "Z"; }

  function writeUrl() {
    const p = new URLSearchParams();
    if (st.range === "custom") { p.set("from", isoMinute(st.from)); p.set("to", isoMinute(st.to)); }
    else if (st.range !== DEF.range) p.set("range", st.range);
    if (st.q) p.set("q", st.q);
    for (const k of ["gpus", "sort", "dir", "view", "by", "gsort", "gdir"]) if (st[k] !== DEF[k]) p.set(k, st[k]);
    if (st.mingh) p.set("mingh", st.mingh);
    if (st.metric !== DEF.metric) p.set("metric", st.metric);
    if (st.below != null) p.set("below", st.below);
    if (st.above != null) p.set("above", st.above);
    const qs = p.toString();
    history.replaceState(null, "", location.pathname + (qs ? "?" + qs : ""));
  }

  // ------------------------------------------------------------------ data

  function bounds() {
    if (st.range === "custom") return [st.from, st.to];
    const to = Date.now();
    return [to - RANGES[st.range] * DAY_MS, to];
  }

  function setMsg(text, bad) {
    const m = $("#js-msg");
    m.hidden = !text;
    m.className = "js-msg" + (bad ? " bad" : "");
    m.textContent = text || "";
  }

  function clearTables() {
    for (const id of ["#js-jobs", "#js-groups"]) { $(id).tHead.replaceChildren(); $(id).tBodies[0].replaceChildren(); }
    $("#js-stats").replaceChildren();
    $("#js-more").hidden = true;
  }

  let loadSeq = 0;
  async function load(attempt) {
    attempt = attempt || 0;
    const seq = ++loadSeq;
    const sig = rangeSig();
    if (st.dataFor !== sig) clearTables();
    const [from, to] = bounds();
    if (!(from < to) || to - from > 90 * DAY_MS) {
      setMsg("The range must be at most 90 days, with From before To.", true);
      return;
    }
    setMsg("Loading jobs from sacct…");
    let res, body = null;
    try {
      res = await fetch("api/jobstats/jobs?from=" + Math.floor(from) + "&to=" + Math.floor(to), { cache: "no-store" });
      body = await res.json().catch(() => null);
    } catch (e) {
      if (seq === loadSeq) setMsg("Could not reach the server: " + e.message, true);
      return;
    }
    if (seq !== loadSeq) return;
    if (res.status === 503 && attempt < 3) {
      const wait = Number(res.headers.get("Retry-After")) || 2;
      setMsg("sacct is busy, retrying in " + wait + " s…");
      setTimeout(() => { if (seq === loadSeq) load(attempt + 1); }, wait * 1000);
      return;
    }
    if (!res.ok || !body || !Array.isArray(body.jobs)) {
      setMsg("Request failed (" + res.status + "): " + ((body && body.error) || res.statusText), true);
      return;
    }
    st.data = body;
    st.dataFor = sig;
    st.nowS = body.generated_at / 1000;
    st.cache = null;
    setMsg(body.truncated ? "Showing the first 5,000 jobs of this range. Narrow the range to see the rest." : null, body.truncated);
    $("#js-updated").textContent = "sacct at " + new Date(body.generated_at).toLocaleTimeString("en-GB", { hour12: false });
    if (!$("#js-main").hidden) render();
  }

  function filtered() {
    const f = { gpus: st.gpus, minGpuHours: st.mingh, metric: st.metric, below: st.below, above: st.above };
    const sig = JSON.stringify([st.q, f]);
    if (!st.cache || st.cache.sig !== sig) {
      st.cache = { sig, rows: C.filterRows(st.data.jobs, st.q, f, st.nowS) };
    }
    return st.cache.rows;
  }

  // ------------------------------------------------------------------ render: shared

  function syncControls() {
    const press = (sel, v) => document.querySelectorAll(sel + " button").forEach((b) =>
      b.setAttribute("aria-pressed", String(b.dataset.v === String(v))));
    press("#js-range", st.showCustom ? "custom" : st.range);
    press("#js-gpus", st.gpus);
    press("#js-mingh", st.mingh);
    press("#js-view", st.view);
    press("#js-by", st.by);
    $("#js-custom").hidden = !(st.range === "custom" || st.showCustom);
    if (st.range === "custom" && !st.showCustom && st.from && st.to) {
      $("#js-from").value = localInput(st.from);
      $("#js-to").value = localInput(st.to);
    }
    if ($("#js-q").value !== st.q) $("#js-q").value = st.q;
    $("#js-metric").value = st.metric;
    const cap = (C.FILTER_METRICS.find((m) => m.key === st.metric) || {}).max;
    for (const id of ["#js-below", "#js-above"]) {
      if (cap) $(id).setAttribute("max", cap); else $(id).removeAttribute("max");
    }
    $("#js-below").value = st.below == null ? "" : st.below;
    $("#js-above").value = st.above == null ? "" : st.above;
    $("#js-by").hidden = st.view !== "groups";
    $("#js-famwarn").hidden = !(st.view === "groups" && st.by === "family");
    $("#js-jobs-wrap").hidden = st.view !== "jobs";
    $("#js-groups-wrap").hidden = st.view !== "groups";
  }

  function localInput(ms) {
    const d = new Date(ms);
    const p = (n) => String(n).padStart(2, "0");
    return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) + "T" + p(d.getHours()) + ":" + p(d.getMinutes());
  }

  function render() {
    if ($("#js-main").hidden) return;       // the job view owns the URL while it is shown
    writeUrl();
    syncControls();
    if (!st.data || st.dataFor !== rangeSig()) return;
    const rows = filtered();
    if (st.view === "groups") renderGroups(rows); else renderJobs(rows);
  }

  function stat(label, value) {
    return el("span", null, el("b", null, value), " " + label);
  }

  // ------------------------------------------------------------------ render: jobs

  function renderJobs(rows) {
    const sorted = C.sortRows(rows, st.sort, st.dir);
    const agg = C.aggregate(rows);
    const table = $("#js-jobs");
    const thead = table.tHead;
    thead.replaceChildren(sortHead(COLS, st.sort, st.dir, "data-k"));
    const tot = el("tr", { class: "js-total" });
    for (const c of COLS) {
      tot.append(el("th", { class: c.n ? "n" : null, title: c.tot && c.ttl ? c.ttl : null }, c.tot ? c.tot(agg, rows) : ""));
    }
    thead.append(tot);
    // The totals row sticks just under the header row, whatever height the header has.
    table.style.setProperty("--headh", thead.rows[0].getBoundingClientRect().height + "px");
    const body = table.tBodies[0];
    const frag = document.createDocumentFragment();
    for (const r of sorted.slice(0, st.limit)) frag.append(rowEl(r, COLS));
    if (!sorted.length) {
      frag.append(el("tr", null, el("td", { colspan: COLS.length, class: "js-dim" },
        "No jobs match. Clear the search or a filter, or widen the range.")));
    }
    body.replaceChildren(frag);
    const more = $("#js-more");
    more.hidden = sorted.length <= st.limit;
    more.textContent = "Show " + PAGE + " more (" + C.fmtNum(sorted.length - st.limit) + " left)";
    renderStats(rows, agg);
  }

  function renderStats(rows, agg) {
    const c = st.data.counts;
    const hidden = st.data.jobs.length - rows.length;
    const s = $("#js-stats");
    s.replaceChildren(
      stat("jobs shown", C.fmtNum(rows.length)),
      stat("GPU-h", C.fmtGpuHours(agg.gpu_hours)),
      stat("of GPU-h has a summary", share(agg.summarized_share)),
      el("span", { title: "Every job sacct returned for the range, before search and filters" },
        "range: " + C.fmtNum(st.data.jobs.length) + " jobs · ok " + c.ok + " · under 60 s " + c.short +
        " · no data " + c.none + " · missing " + c.missing + (c.invalid ? " · invalid " + c.invalid : "") +
        (c.other ? " · other " + c.other : "") + " · never started " + c.not_started +
        (hidden ? " (" + C.fmtNum(hidden) + " filtered out)" : "")));
  }

  // ------------------------------------------------------------------ render: groups

  const GCOLS = [
    { k: "key", h: "Group", v: (g) => String(g.key).toLowerCase() },
    { k: "jobs", h: "Jobs", n: 1, v: (g) => g.agg.jobs },
    { k: "states", h: "Job states" },
    { k: "gpu_hours", h: "GPU-h", n: 1, v: (g) => g.agg.gpu_hours },
    { k: "summarized_share", h: "Summarized", n: 1, v: (g) => g.agg.summarized_share,
      htitle: "Jobs with a summary, their GPU-hours, and the share of the group's GPU-hours" },
    { k: "gpu_pct", h: "GPU %", n: 1, v: (g) => g.agg.gpu_pct.mean, htitle: MEAN_TITLE },
    { k: "spread", h: "GPU % per job", htitle: "One dot per job; ticks at p10 and p90, heavy tick at the median (3+ jobs)" },
    { k: "sm", h: "SM", n: 1, v: (g) => g.agg.sm.mean, htitle: MEAN_TITLE },
    { k: "tc", h: "TC", n: 1, v: (g) => g.agg.tc.mean, htitle: MEAN_TITLE },
    { k: "dram", h: "DRAM", n: 1, v: (g) => g.agg.dram.mean, htitle: MEAN_TITLE },
    { k: "occ", h: "OCC", n: 1, v: (g) => g.agg.occ.mean, htitle: "SM occupancy. " + MEAN_TITLE },
    { k: "fp32", h: "FP32", n: 1, v: (g) => g.agg.fp32.mean, htitle: MEAN_TITLE },
    { k: "int", h: "INT", n: 1, v: (g) => g.agg.int.mean, htitle: MEAN_TITLE },
    { k: "power_w", h: "Power W", n: 1, v: (g) => g.agg.power_w.mean, htitle: "Mean power per GPU. " + MEAN_TITLE },
    { k: "cpu_eff", h: "CPU eff", n: 1, v: (g) => g.agg.cpu_eff.mean, htitle: "CPU-seconds used / allocated" },
    { k: "gpu_mem", h: "GPU mem peak", n: 1, v: (g) => g.agg.gpu_mem_peak_pct && g.agg.gpu_mem_peak_pct.max,
      htitle: "Median and max of per-job peak %" },
    { k: "cpu_mem", h: "CPU mem peak", n: 1, v: (g) => g.agg.cpu_mem_peak_pct && g.agg.cpu_mem_peak_pct.max,
      htitle: "Median and max of per-job peak %" },
    { k: "fp16", h: "FP16", n: 1, v: (g) => g.agg.fp16_seen, htitle: "Share of jobs with nonzero FP16 activity seen" },
    { k: "fp64", h: "FP64", n: 1, v: (g) => g.agg.fp64_seen, htitle: "Share of jobs with nonzero FP64 activity seen" },
    { k: "coverage", h: "Coverage", n: 1, v: (g) => g.agg.coverage,
      htitle: "GPU-seconds with a GPU % value / allocated GPU-seconds, jobs with a summary" },
  ];
  const GCOLKEYS = GCOLS.filter((c) => c.v).map((c) => c.k);

  function sortGroups(groups) {
    const c = GCOLS.find((x) => x.k === st.gsort);
    if (!c || !c.v) return groups;
    const d = st.gdir === "asc" ? 1 : -1;
    return groups.slice().sort((a, b) => {
      const x = c.v(a), y = c.v(b);
      if (x == null && y == null) return b.agg.gpu_hours - a.agg.gpu_hours;
      if (x == null) return 1;
      if (y == null) return -1;
      if (x === y) return b.agg.gpu_hours - a.agg.gpu_hours;
      return (x < y ? -1 : 1) * d;
    });
  }

  function strip(values, sp) {
    const W = 130, H = 20, L = 3, pw = W - 6;
    const X = (v) => (L + Math.max(0, Math.min(100, v)) / 100 * pw).toFixed(1);
    const svg = svgEl("svg", { class: "js-strip", viewBox: "0 0 " + W + " " + H, width: W, height: H, role: "img",
                               "aria-label": "GPU % per job: p10 " + C.fmtPct(sp.p10) + ", median " + C.fmtPct(sp.median) + ", p90 " + C.fmtPct(sp.p90) });
    svg.append(svgEl("line", { class: "ax", x1: L, x2: L + pw, y1: 10, y2: 10 }));
    for (const t of [0, 50, 100]) svg.append(svgEl("line", { class: "ax", x1: X(t), x2: X(t), y1: 6, y2: 14 }));
    values.forEach((v, i) => svg.append(svgEl("circle", { class: "dot", cx: X(v), cy: 5 + ((i * 7919) % 11), r: 2 })));
    for (const q of [sp.p10, sp.p90]) svg.append(svgEl("line", { class: "q", x1: X(q), x2: X(q), y1: 2, y2: 18 }));
    svg.append(svgEl("line", { class: "med", x1: X(sp.median), x2: X(sp.median), y1: 0, y2: 20 }));
    return svg;
  }

  // A group mean, with the per-job spread (p10, median, p90; 3+ jobs) in its tooltip.
  function meanCell(m, fmt) {
    const sp = m.spread;
    return el("td", { class: "n", title: sp ? "per job: p10 " + fmt(sp.p10) + " · median " + fmt(sp.median) +
                                             " · p90 " + fmt(sp.p90) : null }, fmt(m.mean));
  }
  const watts = (v) => C.fmtNum(v, 0);

  function namesText(g, limit) {
    const shown = limit ? g.names.slice(0, limit) : g.names;
    return (g.names.length > 1 ? g.names.length + " names: " : "") + shown.map(([n, k]) => n + " (" + k + ")").join(", ") +
      (limit && g.names.length > limit ? ", +" + (g.names.length - limit) + " more (expand to list all)" : "");
  }

  function groupCells(g) {
    const a = g.agg;
    const open = st.open.has(g.key);
    const key = el("td", null, el("span", { class: "js-tog" }, open ? "▾" : "▸"), el("span", { class: "js-key" }, String(g.key)));
    if (st.by !== "name") key.append(el("span", { class: "js-members" }, namesText(g, 5)));
    const states = Object.entries(g.jobStates).sort((x, y) => y[1] - x[1]).map(([s, n]) => n + " " + s).join(", ");
    const ok = a.states.ok || 0;
    const sp = a.gpu_pct.spread;
    const cov = a.coverage;
    return [
      key,
      el("td", { class: "n" }, C.fmtNum(a.jobs)),
      el("td", { class: "js-dim" }, states),
      el("td", { class: "n" }, C.fmtGpuHours(a.gpu_hours)),
      el("td", { class: "n" }, "ok " + ok + "/" + a.jobs + " · " + C.fmtGpuHours(a.summarized_gpu_hours) +
        " h (" + share(a.summarized_share) + ")"),
      el("td", { class: "n" }, badge(a.gpu_pct.mean)),
      el("td", { title: sp ? "p10 " + C.fmtPct(sp.p10) + " · median " + C.fmtPct(sp.median) + " · p90 " + C.fmtPct(sp.p90) : null },
        sp ? el("span", { class: "js-spread" }, strip(g.gpuPctValues, sp), C.fmtPct(sp.median, 0))
           : el("span", { class: "js-dim" }, g.gpuPctValues.length ? "fewer than 3 jobs" : DASH)),
      meanCell(a.sm, C.fmtPct),
      meanCell(a.tc, C.fmtPct),
      meanCell(a.dram, C.fmtPct),
      meanCell(a.occ, C.fmtPct),
      meanCell(a.fp32, C.fmtPct),
      meanCell(a.int, C.fmtPct),
      meanCell(a.power_w, watts),
      meanCell(a.cpu_eff, C.fmtPct),
      el("td", { class: "n" }, peak2(a.gpu_mem_peak_pct)),
      el("td", { class: "n" }, peak2(a.cpu_mem_peak_pct)),
      el("td", { class: "n" }, share(a.fp16_seen)),
      el("td", { class: "n" }, share(a.fp64_seen)),
      el("td", { class: "n" + (cov != null && cov >= 0.9995 ? " js-dim" : "") }, share(cov)),
    ];
  }

  function memberTable(g) {
    const cols = st.by === "name" ? MEMBER_COLS.filter((c) => c.k !== "name") : MEMBER_COLS;
    const rows = C.sortRows(g.rows, "end", "desc");
    const t = el("table", { class: "js-table" });
    const head = el("tr", { class: "js-head" });
    for (const c of cols) head.append(el("th", { class: c.n ? "n" : null }, c.h));
    t.append(el("thead", null, head));
    const body = el("tbody");
    for (const r of rows.slice(0, MEMBER_LIMIT)) body.append(rowEl(r, cols));
    if (rows.length > MEMBER_LIMIT) {
      body.append(el("tr", null, el("td", { colspan: cols.length, class: "js-dim" },
        "+" + C.fmtNum(rows.length - MEMBER_LIMIT) + " more jobs; narrow the search to list them")));
    }
    t.append(body);
    return t;
  }

  function renderGroups(rows) {
    const groups = sortGroups(C.groupRows(rows, st.by));
    const cols = GCOLS.map((c) => (c.v ? c : Object.assign({}, c, { nosort: true })));
    const thead = $("#js-groups").tHead;
    const head = el("tr", { class: "js-head" });
    for (const c of cols) {
      const label = c.k === "key" ? { name: "Name", array: "Array parent", family: "Name family" }[st.by] : c.h;
      if (c.nosort) { head.append(el("th", { class: c.n ? "n" : null, title: c.htitle || null }, label)); continue; }
      const on = c.k === st.gsort;
      head.append(el("th", { class: c.n ? "n" : null, title: c.htitle || null,
                             "aria-sort": on ? (st.gdir === "asc" ? "ascending" : "descending") : null },
        el("button", { type: "button", class: "js-sort", "data-g": c.k }, label,
           el("span", { class: "ar" }, on ? (st.gdir === "asc" ? "▲" : "▼") : ""))));
    }
    thead.replaceChildren(head);
    const frag = document.createDocumentFragment();
    for (const g of groups.slice(0, st.glimit)) {
      const open = st.open.has(g.key);
      const tr = el("tr", { class: "js-grow-row" + (open ? " js-open" : ""), tabindex: "0", "aria-expanded": String(open) });
      tr.dataset.key = String(g.key);
      tr.append(...groupCells(g));
      frag.append(tr);
      if (open) {
        const cell = el("td", { colspan: GCOLS.length });
        if (st.by !== "name") cell.append(el("p", { class: "js-members js-allnames" }, namesText(g, 0)));
        cell.append(memberTable(g));
        frag.append(el("tr", { class: "js-sub" }, cell));
      }
    }
    if (!groups.length) {
      frag.append(el("tr", null, el("td", { colspan: GCOLS.length, class: "js-dim" }, "No jobs match the search and filters.")));
    }
    $("#js-groups").tBodies[0].replaceChildren(frag);
    const more = $("#js-more");
    more.hidden = groups.length <= st.glimit;
    more.textContent = "Show " + GPAGE + " more groups (" + C.fmtNum(groups.length - st.glimit) + " left)";
    const started = rows.filter((r) => !r.not_started).length;
    $("#js-stats").replaceChildren(stat("groups", C.fmtNum(groups.length)), stat("jobs", C.fmtNum(started)),
      el("span", null, "click a group to list its jobs"));
  }

  // ------------------------------------------------------------------ job view

  const NO_DETAIL = {
    short: "The job ran under 60 s, too short for jobstats to write a summary.",
    none: "jobstats found no Prometheus data for this job, so the summary has no numbers.",
    missing: "No summary is stored for this job. Jobs that ended before the summary hook went live " +
             "(29 Oct 19:10 UTC) have none.",
    invalid: "The stored summary could not be decoded",
  };

  function bar(v, color) {
    const w = v == null ? 0 : Math.max(0, Math.min(100, v));
    return el("span", { class: "js-bar", "aria-hidden": "true" },
      el("span", { class: "js-bar-fill", style: "width:" + w.toFixed(1) + "%;background:" + color }));
  }

  function barCell(v, color, asBadge) {
    return el("span", { class: "js-barcell" }, asBadge ? badge(v) : el("span", { class: "js-bv" }, C.fmtPct(v)),
      bar(v, color));
  }

  function memText(used, total, pct) {
    if (used == null) return DASH;
    return C.fmtGB(used).replace(" GB", "") + " / " + C.fmtGB(total) + (pct == null ? "" : " (" + C.fmtPct(pct, 0) + ")");
  }

  const seenText = (v) => (v == null ? DASH : v ? "yes" : "no");
  const rate = (v) => (v == null ? DASH : v.toFixed(v < 10 ? 2 : 1));

  // Per-GPU columns. `more` columns appear after "More columns".
  const GPU_COLS = [
    { k: "node", label: "Node", multi: true, cell: (g) => g.node },
    { k: "minor", label: "GPU", n: true, cell: (g) => g.minor },
    { k: "gpu_pct", label: "GPU %", cell: (g) => barCell(g.gpu_pct, C.gpuColor(g.gpu_pct), true),
      mean: (m) => barCell(m.gpu_pct, C.gpuColor(m.gpu_pct), true) },
    { k: "sm", label: "SM %", cell: (g) => barCell(g.sm, "var(--accent)"), mean: (m) => barCell(m.sm, "var(--accent)") },
    { k: "tc", label: "TC %", n: true, cell: (g) => C.fmtPct(g.tc), mean: (m) => C.fmtPct(m.tc) },
    { k: "dram", label: "DRAM %", n: true, cell: (g) => C.fmtPct(g.dram), mean: (m) => C.fmtPct(m.dram) },
    { k: "occ", label: "OCC %", n: true, cell: (g) => C.fmtPct(g.occ), mean: (m) => C.fmtPct(m.occ) },
    { k: "fp32", label: "FP32 %", n: true, cell: (g) => C.fmtPct(g.fp32), mean: (m) => C.fmtPct(m.fp32) },
    { k: "int", label: "INT %", n: true, cell: (g) => C.fmtPct(g.int), mean: (m) => C.fmtPct(m.int) },
    { k: "mem", label: "GPU mem peak", n: true, title: "Peak used / total, 1024^3 bytes per GB",
      cell: (g) => memText(g.mem_used_b, g.mem_total_b, g.mem_pct),
      mean: (m) => (m.mem_peak_b == null ? DASH : "max " + C.fmtGB(m.mem_peak_b) + " (" + C.fmtPct(m.mem_peak_pct, 0) + ")") },
    { k: "power_w", label: "Power W", n: true, cell: (g) => C.fmtNum(g.power_w), mean: (m) => C.fmtNum(m.power_w) },
    { k: "temp_c", label: "Temp °C", n: true, cell: (g) => C.fmtNum(g.temp_c), mean: (m) => C.fmtNum(m.temp_c) },
    { k: "fp16", label: "FP16 seen", more: true, title: "Any FP16 activity (max > 0) during the run",
      cell: (g) => seenText(g.fp16_seen), mean: (m) => seenText(m.fp16_seen) },
    { k: "fp64", label: "FP64 seen", more: true, title: "Any FP64 activity (max > 0) during the run",
      cell: (g) => seenText(g.fp64_seen), mean: (m) => seenText(m.fp64_seen) },
    { k: "nvrx", label: "NVLink rx", n: true, more: true, title: "Mean GB/s (1024^3 bytes)",
      cell: (g) => rate(g.nvlink_rx_gbs), mean: (m) => rate(m.nvlink_rx_gbs) },
    { k: "nvtx", label: "NVLink tx", n: true, more: true, title: "Mean GB/s (1024^3 bytes)",
      cell: (g) => rate(g.nvlink_tx_gbs), mean: (m) => rate(m.nvlink_tx_gbs) },
    { k: "pcrx", label: "PCIe rx", n: true, more: true, title: "Mean GB/s (1024^3 bytes)",
      cell: (g) => rate(g.pcie_rx_gbs), mean: (m) => rate(m.pcie_rx_gbs) },
    { k: "pctx", label: "PCIe tx", n: true, more: true, title: "Mean GB/s (1024^3 bytes)",
      cell: (g) => rate(g.pcie_tx_gbs), mean: (m) => rate(m.pcie_tx_gbs) },
  ];

  function gpuSection(nodes, showMore, row) {
    const t = C.gpuTable(nodes);
    if (!t.gpus.length) return null;
    // the mean row shows the API's job values, so it matches the header and the Explorer exactly
    for (const k of C.MEAN_KEYS) if (row && row[k] !== undefined) t.summary[k] = row[k];
    const multi = Object.keys(nodes).length > 1;
    const cols = GPU_COLS.filter((c) => (!c.multi || multi) && (!c.more || showMore));
    const th = (c) => el("th", { class: c.n ? "n" : null, title: c.title || null, scope: "col" }, c.label);
    const meanRow = el("tr", { class: "js-total" }, ...cols.map((c, i) =>
      el("th", { class: c.n && i ? "n" : null }, i === 0 ? "job mean" : c.mean ? c.mean(t.summary) : "")));
    const body = t.gpus.map((g) => el("tr", { class: g.gpu_pct != null && g.gpu_pct < 20 ? "js-low" : null },
      ...cols.map((c) => el("td", { class: c.n ? "n" : null }, c.cell(g)))));
    const table = el("table", { class: "js-table js-gputable" },
      el("thead", null, el("tr", { class: "js-head" }, ...cols.map(th)), meanRow), el("tbody", null, ...body));
    return { table, summary: t.summary, count: t.gpus.length };
  }

  function cpuSection(nodes) {
    const rows = C.nodeRows(nodes);
    if (!rows.length) return null;
    return el("table", { class: "js-table js-cputable" },
      el("thead", null, el("tr", { class: "js-head" },
        el("th", { scope: "col" }, "Node"), el("th", { class: "n", scope: "col" }, "CPUs"),
        el("th", { scope: "col" }, "CPU eff"),
        el("th", { class: "n", scope: "col", title: "Peak used / allocated, 1024^3 bytes per GB" }, "CPU mem peak"),
        el("th", { class: "n", scope: "col" }, "GPUs"))),
      el("tbody", null, ...rows.map((r) => el("tr", null,
        el("td", null, r.node), el("td", { class: "n" }, C.fmtNum(r.cpus)),
        el("td", null, barCell(r.cpu_eff, "var(--accent)")),
        el("td", { class: "n" }, memText(r.mem_peak_b, r.mem_total_b, r.mem_pct)),
        el("td", { class: "n" }, String(r.gpus))))));
  }

  // ------------------------------------------------------------------ job time series

  // Built once per job and reused when renderJob redraws (e.g. "More columns"), so loaded charts stay.
  // Nothing is fetched until the button is pressed: people flip through jobs.
  let tsSeq = 0;
  function tsSection(r) {
    if (!st.job || !st.job.timeseries || r.not_started) return null;
    if (st.ts && st.ts.key === r.key) return st.ts.el;
    const res = el("select", { "aria-label": "Resolution" },
      ...C.TS_STEPS.map(([val, label]) => el("option", { value: val }, label)));
    res.value = "auto";
    const agg = el("select", { "aria-label": "Aggregation" },
      el("option", { value: "avg" }, "Average"), el("option", { value: "max" }, "Peak"));
    const syncAgg = () => {
      agg.disabled = res.value === "30" || res.value === "auto";
      agg.title = agg.disabled ? (res.value === "30" ? "30 s is one raw sample per point" : "auto always averages") : "";
    };
    res.addEventListener("change", syncAgg);
    syncAgg();
    const btn = el("button", { type: "button", class: "js-ts-go" }, "Generate time-series plots");
    const status = el("p", { class: "js-ts-status", "aria-live": "polite" },
      "Choose a resolution and press the button. Nothing is fetched before that.");
    const chips = el("div", { class: "js-ts-chips", role: "group", "aria-label": "GPUs shown", hidden: true });
    const charts = el("div", { class: "js-ts-body" });
    const ctl = (label, input) => el("label", { class: "js-ctl" }, el("span", { class: "js-lbl" }, label), input);

    btn.addEventListener("click", async () => {
      const seq = ++tsSeq;
      const aggVal = agg.disabled ? "avg" : agg.value;
      btn.disabled = true;
      btn.textContent = "Loading…";
      status.className = "js-ts-status";
      status.textContent = "Querying Prometheus…";
      let res2, data = null, failure = null;
      try {
        res2 = await fetch("api/jobstats/job/" + encodeURIComponent(r.jobid) + "/timeseries?submit=" +
          encodeURIComponent(r.submit) + "&step=" + encodeURIComponent(res.value) + "&agg=" + aggVal, { cache: "no-store" });
        data = await res2.json().catch(() => null);
        if (!res2.ok || !data || !data.charts) {
          failure = ((data && data.error) || res2.statusText || "request failed") + " (" + res2.status + ")";
        }
      } catch (e) {
        failure = "Could not reach the server: " + e.message;
      }
      if (seq !== tsSeq) return;
      btn.disabled = false;
      btn.textContent = "Generate time-series plots";
      if (failure) {
        status.className = "js-ts-status bad";
        status.textContent = "Time series: " + failure;
        chips.hidden = true;
        charts.replaceChildren();
        return;
      }
      const points = Math.floor((data.end - data.start) / data.step_s) + 1;
      status.textContent = C.tsStepText(data) + ", " + points + " points" +
        (r.end == null ? ". The job is still running; the charts end now." : ".");
      const labels = C.tsGpuLabels(data.gpus);
      const handle = window.JobstatsCharts.render(charts, data, labels,
        { gpuCharts: r.alloc_gpus > 0 || data.gpus.length > 0, allocGpus: r.alloc_gpus });
      chips.replaceChildren();
      chips.hidden = data.gpus.length < 2;
      for (const g of data.gpus) {
        const chip = el("button", { type: "button", class: "js-chip js-ts-chip", "aria-pressed": "true" },
          el("span", { class: "js-ts-sw", style: "background:" + handle.colors[g.key] }), labels[g.key]);
        chip.addEventListener("click", () => {
          const on = chip.getAttribute("aria-pressed") !== "true";
          chip.setAttribute("aria-pressed", on ? "true" : "false");
          handle.setVisible(g.key, on);
        });
        chips.append(chip);
      }
    });

    const section = el("section", { class: "js-ts", "aria-label": "Time series" },
      el("div", { class: "js-sechead" }, el("h3", null, "Time series"),
        el("span", { class: "js-hint" }, "One line per GPU. Hover a chart to read the values at that time and highlight " +
          "the nearest GPU in every chart; click a GPU chip to hide or show it.")),
      el("div", { class: "js-ts-bar" }, ctl("Resolution", res), ctl("Aggregation", agg), btn),
      status, chips, charts);
    st.ts = { key: r.key, el: section };
    return section;
  }

  let jobSeq = 0;
  async function showJob(jobid, submit) {
    const seq = ++jobSeq;
    const current = () => seq === jobSeq;
    $("#js-main").hidden = true;
    const v = $("#js-jobview");
    v.hidden = false;
    const back = el("a", { href: "jobstats", class: "js-back" }, "← all jobs");
    back.addEventListener("click", (e) => {
      if (st.fromList) { e.preventDefault(); history.back(); }
    });
    v.replaceChildren(el("p", null, back), el("p", { class: "js-dim" }, "Loading job " + jobid + "…"));
    let res, body = null;
    try {
      res = await fetch("api/jobstats/job/" + encodeURIComponent(jobid) + "?submit=" + encodeURIComponent(submit), { cache: "no-store" });
      body = await res.json().catch(() => null);
    } catch (e) {
      if (current()) v.replaceChildren(el("p", null, back), el("p", { class: "js-msg bad" }, "Could not reach the server: " + e.message));
      return;
    }
    if (!current()) return;
    if (!res.ok || !body || !body.job) {
      v.replaceChildren(el("p", null, back), el("p", { class: "js-msg bad" },
        "Job " + jobid + ": " + ((body && body.error) || res.statusText) + " (" + res.status + ")"));
      return;
    }
    st.job = body;
    st.ts = null;
    tsSeq++;                                 // drop a time-series response for the previous job
    renderJob(back);
  }

  function renderJob(back) {
    const body = st.job;
    const r = body.job;
    const nodes = body.nodes || {};
    st.nowS = st.nowS || Date.now() / 1000;
    const v = $("#js-jobview");
    const lc = C.lifecycle(r);
    const kv = (k, val) => el("div", null, el("span", { class: "k" }, k), el("span", { class: "v" }, val));
    const cmd = el("input", { type: "text", readonly: true, value: "jobstats " + r.jobid, "aria-label": "jobstats command" });
    const copy = el("button", { type: "button" }, "Copy");
    copy.addEventListener("click", () => {
      cmd.select();
      (navigator.clipboard ? navigator.clipboard.writeText(cmd.value) : Promise.reject())
        .then(() => { copy.textContent = "Copied"; }, () => { document.execCommand("copy"); copy.textContent = "Copied"; });
    });
    const parts = [
      el("p", null, back),
      el("h2", { class: "js-jobtitle" }, r.name),
      el("div", { class: "js-jobhead" },
        kv("Job ID", r.jobid + (r.jobid !== r.jobid_raw ? " (raw " + r.jobid_raw + ")" : "")),
        kv("User", r.user || DASH), kv("State", stateCell(r.state)), kv("Nodes", r.nodes.join(", ") || DASH),
        kv("Elapsed", C.fmtElapsed(r.elapsed_s)), kv("GPUs / CPUs", r.alloc_gpus + " / " + r.alloc_cpus),
        kv("GPU-hours", C.fmtGpuHours(r.gpu_hours)), kv("Summary", pill(r)),
        kv("GPU %", badge(r.gpu_pct)), kv("CPU eff", C.fmtPct(r.cpu_eff)),
        kv("Submit", fmtDateTime(r.submit)), kv("Start", lc.start == null ? lc.label : fmtDateTime(lc.start)),
        kv("End", lc.end == null ? lc.label : fmtDateTime(lc.end))),
      el("div", { class: "js-cmd" }, cmd, copy),
    ];
    if (r.summary_state !== "ok" || r.not_started) {
      const running = /^RUNNING/.test(r.state || "") && r.summary_state === "missing";
      const why = r.not_started ? "The job never started, so there is nothing to show."
        : running ? "The job is still running. Its jobstats summary is written when it ends."
        : (NO_DETAIL[r.summary_state] || "No summary.") + (r.summary_state === "invalid" ? " (" + (r.invalid_reason || "?") + ")." : "");
      parts.push(el("p", { class: "js-msg" }, why));
      const ts = tsSection(r);
      if (ts) parts.push(ts);
      v.replaceChildren(...parts);
      return;
    }
    const gs = gpuSection(nodes, st.jobMore, r);
    for (const n of C.jobNotes(r)) parts.push(el("p", { class: "js-warn" }, n));
    if (gs) {
      const more = el("button", { type: "button", class: "js-chip", "aria-pressed": st.jobMore ? "true" : "false" },
        st.jobMore ? "Fewer columns" : "More columns");
      more.addEventListener("click", () => { st.jobMore = !st.jobMore; renderJob(back); });
      parts.push(el("div", { class: "js-sechead" }, el("h3", null, "GPUs (" + gs.count + ")"),
                    el("span", { class: "js-hint" }, "means over the run; a red edge marks GPUs under 20 % busy"), more),
                 el("div", { class: "js-tw js-tw-auto" }, gs.table));
    } else {
      parts.push(el("p", { class: "js-msg" }, r.alloc_gpus ? "The summary has no per-GPU values." : "No GPUs allocated."));
    }
    const cpu = cpuSection(nodes);
    if (cpu) parts.push(el("div", { class: "js-sechead" }, el("h3", null, "CPU and memory per node")),
                        el("div", { class: "js-tw js-tw-auto" }, cpu));
    const ts = tsSection(r);
    if (ts) parts.push(ts);
    parts.push(el("p", { class: "js-note" },
      "Values in the tables come from the stored jobstats summary (JS1): one mean per GPU over the whole run. It has " +
      "no sample counts, so a mean assumes the sampled period represents the run." +
      (st.job.timeseries ? " Time series below come from Prometheus at the resolution you choose." : "")));
    v.replaceChildren(...parts);
  }

  // ------------------------------------------------------------------ routing and events

  function route() {
    const p = new URLSearchParams(location.search);
    if (p.has("job")) { showJob(p.get("job"), p.get("submit") || ""); return; }
    st.fromList = false;
    jobSeq++;                                // drop any detail response still in flight
    $("#js-jobview").hidden = true;
    $("#js-main").hidden = false;
    readUrl();
    syncControls();
    if (st.data && st.dataFor === rangeSig()) render(); else load();
  }

  function rangeSig() { return st.range === "custom" ? st.from + "-" + st.to : st.range; }

  function reload() { st.limit = PAGE; st.glimit = GPAGE; writeUrl(); syncControls(); load(); }

  function bindSeg(sel, fn) {
    $(sel).addEventListener("click", (e) => {
      const b = e.target.closest("button[data-v]");
      if (b) fn(b.dataset.v);
    });
  }

  function update(changes) {
    Object.assign(st, changes, { limit: PAGE, glimit: GPAGE });
    render();
  }

  bindSeg("#js-range", (v) => {
    if (v === "custom") {
      const [f, t] = bounds();
      st.showCustom = true;
      $("#js-from").value = localInput(f);
      $("#js-to").value = localInput(t);
      syncControls();
      return;
    }
    st.showCustom = false;
    st.range = v;
    reload();
  });
  $("#js-apply").addEventListener("click", () => {
    const f = new Date($("#js-from").value).getTime(), t = new Date($("#js-to").value).getTime();
    if (!f || !t) { setMsg("Pick both From and To.", true); return; }
    st.from = Math.floor(f / 60000) * 60000; st.to = Math.floor(t / 60000) * 60000;
    st.range = "custom"; st.showCustom = false;
    reload();
  });
  $("#js-reload").addEventListener("click", reload);
  $("#js-reset").addEventListener("click", () => {
    const reRange = st.range !== DEF.range;
    Object.assign(st, DEF, { open: new Set(), showCustom: false });
    if (reRange) reload(); else update({});
  });
  bindSeg("#js-gpus", (v) => update({ gpus: v }));
  bindSeg("#js-mingh", (v) => update({ mingh: Number(v) }));
  bindSeg("#js-view", (v) => update({ view: v }));
  bindSeg("#js-by", (v) => update({ by: v, open: new Set() }));

  let qTimer = null;
  $("#js-q").addEventListener("input", (e) => {
    clearTimeout(qTimer);
    qTimer = setTimeout(() => update({ q: e.target.value }), 150);
  });
  $("#js-metric").replaceChildren(...C.FILTER_METRICS.map((m) => el("option", { value: m.key }, m.label)));
  $("#js-metric").addEventListener("change", (e) => update({ metric: e.target.value }));
  for (const id of ["below", "above"]) {
    $("#js-" + id).addEventListener("input", (e) => update({ [id]: num(e.target.value.trim()) }));
  }
  $("#js-tokens").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-tok]");
    if (!b) return;
    const tok = b.dataset.tok;
    const parts = st.q.split(/\s+/).filter(Boolean);
    const q = parts.includes(tok) ? parts.filter((x) => x !== tok).join(" ") : parts.concat(tok).join(" ");
    update({ q });
  });
  $("#js-more").addEventListener("click", () => {
    if (st.view === "groups") st.glimit += GPAGE; else st.limit += PAGE;
    render();
  });
  $("#js-jobs").tHead.addEventListener("click", (e) => {
    const b = e.target.closest("button[data-k]");
    if (!b) return;
    const k = b.dataset.k;
    const textual = ["user", "name", "state", "nodes", "summary_state"].includes(k);
    update(st.sort === k ? { dir: st.dir === "asc" ? "desc" : "asc" } : { sort: k, dir: textual ? "asc" : "desc" });
    const nb = document.querySelector('#js-jobs button[data-k="' + k + '"]');
    if (nb) nb.focus();
  });
  $("#js-groups").tHead.addEventListener("click", (e) => {
    const b = e.target.closest("button[data-g]");
    if (!b) return;
    const k = b.dataset.g;
    update(st.gsort === k ? { gdir: st.gdir === "asc" ? "desc" : "asc" } : { gsort: k, gdir: k === "key" ? "asc" : "desc" });
    const nb = document.querySelector('#js-groups button[data-g="' + k + '"]');
    if (nb) nb.focus();
  });

  function toggleGroup(tr) {
    const k = tr.dataset.key;
    const groups = C.groupRows(filtered(), st.by);
    const g = groups.find((x) => String(x.key) === k);
    if (!g) return;
    if (st.open.has(g.key)) st.open.delete(g.key); else st.open.add(g.key);
    render();
    const again = [...document.querySelectorAll("#js-groups tr.js-grow-row")].find((x) => x.dataset.key === k);
    if (again) again.focus();
  }
  $("#js-groups").tBodies[0].addEventListener("click", (e) => {
    if (e.target.closest("a")) return;
    const tr = e.target.closest("tr.js-grow-row");
    if (tr) toggleGroup(tr);
  });
  $("#js-groups").tBodies[0].addEventListener("keydown", (e) => {
    const tr = e.target.closest("tr.js-grow-row");
    if (tr && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); toggleGroup(tr); }
  });

  // Job links open the job view in place; the explorer state stays in the history entry behind it.
  document.addEventListener("click", (e) => {
    const a = e.target.closest("a[data-job]");
    if (!a || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    e.preventDefault();
    history.pushState(null, "", a.getAttribute("href"));
    st.fromList = true;
    window.scrollTo(0, 0);
    route();
  });
  window.addEventListener("popstate", route);

  route();
})();
