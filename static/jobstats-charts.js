/* Job page time series: hand-drawn SVG line charts for /api/jobstats/job/<id>/timeseries.
   Pure helpers (ticks, axes, nearest point, labels) live in jobstats-core.js. Every string from the
   API reaches the page through textContent or setAttribute, never innerHTML. */
(function (root) {
  "use strict";
  const C = root.JobstatsCore;
  const NS = "http://www.w3.org/2000/svg";
  const W = 560, H = 230, ML = 44, MR = 14, MT = 14, MB = 38;
  const PW = W - ML - MR, PH = H - MT - MB;
  const COLORS = ["#60a5fa", "#f59e0b", "#34d399", "#f87171", "#a78bfa", "#22d3ee", "#d4a373", "#94a3b8"];
  const JOB_COLOR = "#2dd4bf";
  // Multi-node jobs: colour follows the GPU's place on its node, the dash pattern follows the node,
  // so gpu07/0 and gpu08/0 never draw as one line.
  const DASHES = ["", "7 3", "2 2.5", "10 3 2 3"];
  const HL_PX = 14;   // pointer within this many px (viewBox units) of a GPU line highlights it

  function svg(tag, attrs) {
    const e = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs || {})) e.setAttribute(k, v);
    return e;
  }

  function div(cls, text) {
    const e = document.createElement("div");
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  function fmtVal(v, unit) {
    if (v == null) return "—";
    if (unit === "W") return Math.round(v) + " W";
    if (unit === "%") return v.toFixed(1) + " %";
    return (v >= 100 ? v.toFixed(0) : v.toFixed(v >= 10 ? 1 : 2)) + " " + unit;
  }

  function fmtTick(v) {
    if (v >= 1000 && v % 1000 === 0) return v / 1000 + "k";
    return String(Math.round(v * 100) / 100);
  }

  function seriesMax(series) {
    let m = 0;
    for (const pts of Object.values(series)) for (const p of pts) if (p[1] > m) m = p[1];
    return m;
  }

  // One chart: frame, gridlines, axes, lines, allocated line, crosshair. Returns its handle.
  function drawChart(def, body, keys, colorOf, dashOf, axis) {
    const card = div("js-ts-card");
    const head = div("js-ts-head");
    head.append(div("js-ts-title", def.title), div("js-ts-unit", def.note));
    card.append(head);
    const pts = def.series;
    const hasData = Object.values(pts).some((p) => p.length);
    if (!hasData) {
      card.append(div("js-ts-empty", "No data for this job"));
      card.classList.add("js-ts-nodata");
      return { card, def, empty: true };
    }
    const span = Math.max(1, body.end - body.start);
    const dataMax = seriesMax(pts);
    const top = Math.max(def.ymax || 0, def.allocated || 0, dataMax);
    let yt = C.tsTicks(top, 4);
    // GPU memory: the axis ends at the card's total (spec), with a labelled top tick
    if (def.name === "gpu_mem" && def.ymax && dataMax <= def.ymax) {
      yt = yt.filter((t) => t < def.ymax - 1e-9).concat([def.ymax]);
    }
    const ymax = yt[yt.length - 1];
    const x = (t) => ML + ((t - body.start) / span) * PW;
    const y = (v) => MT + PH - (Math.max(0, Math.min(v, ymax)) / ymax) * PH;

    const s = svg("svg", { viewBox: "0 0 " + W + " " + H, class: "js-ts-svg", role: "img",
                           "aria-label": def.title + " over time" });
    const grid = svg("g", { class: "js-ts-gl" });
    for (const v of yt) {
      grid.append(svg("line", { x1: ML, x2: ML + PW, y1: y(v), y2: y(v) }));
      const lab = svg("text", { x: ML - 6, y: y(v) + 3.5, "text-anchor": "end", class: "js-ts-tick" });
      lab.textContent = fmtTick(v);
      grid.append(lab);
    }
    for (const tv of axis.ticks) {
      if (tv * axis.div > span + 1e-6) continue;
      const xx = ML + (tv * axis.div / span) * PW;
      grid.append(svg("line", { x1: xx, x2: xx, y1: MT, y2: MT + PH, class: "js-ts-vgrid" }));
      const lab = svg("text", { x: xx, y: MT + PH + 14, "text-anchor": "middle", class: "js-ts-tick" });
      lab.textContent = fmtTick(tv);
      grid.append(lab);
    }
    const xl = svg("text", { x: ML + PW / 2, y: H - 4, "text-anchor": "middle", class: "js-ts-axl" });
    xl.textContent = (axis.unit === "h" ? "hours" : "minutes") + " since job start";
    grid.append(xl, svg("line", { x1: ML, x2: ML + PW, y1: MT + PH, y2: MT + PH, class: "js-ts-axis" }),
                svg("line", { x1: ML, x2: ML, y1: MT, y2: MT + PH, class: "js-ts-axis" }));
    s.append(grid);

    if (def.allocated != null) {
      const ay = y(def.allocated);
      s.append(svg("line", { x1: ML, x2: ML + PW, y1: ay, y2: ay, class: "js-ts-alloc" }));
      const lab = svg("text", { x: ML + PW - 2, y: ay - 4, "text-anchor": "end", class: "js-ts-alloclab" });
      lab.textContent = "allocated " + (def.unit === "GB" ? fmtVal(def.allocated, "GB") : def.allocated);
      s.append(lab);
    }

    const gap = body.step_s * 2.5;
    const paths = {};
    const lines = svg("g", { class: "js-ts-lines" });
    for (const key of keys) {
      const p = pts[key];
      if (!p || !p.length) continue;
      let d = "";
      for (let i = 0; i < p.length; i++) {
        const cmd = i === 0 || p[i][0] - p[i - 1][0] > gap ? "M" : "L";
        d += cmd + x(p[i][0]).toFixed(1) + " " + y(p[i][1]).toFixed(1);
      }
      if (p.length === 1) d += "h1";
      const path = svg("path", { d, stroke: colorOf(key), class: "js-ts-line" });
      if (dashOf(key)) path.setAttribute("stroke-dasharray", dashOf(key));
      paths[key] = path;
      lines.append(path);
    }
    s.append(lines);
    const cross = svg("line", { x1: 0, x2: 0, y1: MT, y2: MT + PH, class: "js-ts-cross", visibility: "hidden" });
    s.append(cross);
    const hit = svg("rect", { x: ML, y: MT, width: PW, height: PH, class: "js-ts-hit" });
    s.append(hit);
    card.append(s);
    const read = div("js-ts-read", " ");
    card.append(read);
    return { card, def, svg: s, paths, cross, read, x, y, ymax, span, empty: false };
  }

  // container: element to fill. body: the route's JSON. labels: {gpu key: label}.
  // opts.gpuCharts: false hides the six per-GPU charts (job without GPUs).
  function render(container, body, labels, opts) {
    opts = opts || {};
    const keys = (body.gpus || []).map((g) => g.key);
    const colors = {}, styles = {};
    const hosts = [], perHost = {};
    for (const g of body.gpus || []) {
      if (!(g.host in perHost)) { perHost[g.host] = 0; hosts.push(g.host); }
      const color = COLORS[perHost[g.host]++ % COLORS.length];
      const dash = DASHES[hosts.indexOf(g.host) % DASHES.length];
      colors[g.key] = color;
      styles[g.key] = { color, dash };
    }
    const colorOf = (k) => (k === "job" ? JOB_COLOR : colors[k] || COLORS[0]);
    const dashOf = (k) => (styles[k] && styles[k].dash) || "";
    const hidden = new Set();
    const axis = C.tsTimeAxis(Math.max(1, body.end - body.start));
    const nGpu = keys.length || opts.allocGpus || 0;
    const charts = [];
    const grid = div("js-ts-charts");
    const all = body.charts || {};
    const names = C.TS_ORDER.filter((k) => all[k]).concat(Object.keys(all).filter((k) => !C.TS_ORDER.includes(k)));
    for (const name of names) {
      const ch = all[name];
      const isGpu = ch.kind !== "job";
      if (isGpu && opts.gpuCharts === false) continue;
      const note = ch.unit + ", " + (isGpu ? nGpu + " GPU" + (nGpu === 1 ? "" : "s") : "whole job");
      const def = Object.assign({ name, note }, ch);
      const c = drawChart(def, body, isGpu ? keys : ["job"], colorOf, dashOf, axis);
      charts.push(c);
      grid.append(c.card);
    }
    container.replaceChildren(grid);

    let hl = null;
    function applyStyles() {
      for (const c of charts) {
        if (c.empty) continue;
        for (const [k, p] of Object.entries(c.paths)) {
          p.style.display = hidden.has(k) ? "none" : "";
          p.classList.toggle("dim", hl != null && k !== "job" && k !== hl);
          p.classList.toggle("hl", hl != null && k === hl);
        }
      }
    }

    function clear() {
      for (const c of charts) {
        if (c.empty) continue;
        c.cross.setAttribute("visibility", "hidden");
        c.read.textContent = " ";
      }
      if (hl != null) { hl = null; applyStyles(); }
    }

    function update(src, vx, vy) {
      const t = body.start + ((vx - ML) / PW) * src.span;
      // highlight: the visible GPU line in the hovered chart closest to the pointer
      let best = null, bestD = Infinity;
      for (const [k, p] of Object.entries(src.def.series)) {
        if (k === "job" || hidden.has(k) || !p.length) continue;
        const i = C.tsNearest(p, t);
        const d = Math.abs(src.y(p[i][1]) - vy);
        if (d < bestD) { bestD = d; best = k; }
      }
      const nextHl = bestD <= HL_PX ? best : null;
      if (nextHl !== hl) { hl = nextHl; applyStyles(); }
      for (const c of charts) {
        if (c.empty) continue;
        let tAt = null;
        const vals = [];
        for (const [k, p] of Object.entries(c.def.series)) {
          if (hidden.has(k) || !p.length) continue;
          const i = C.tsNearest(p, t);
          if (Math.abs(p[i][0] - t) > body.step_s * 1.5) continue;
          tAt = p[i][0];
          vals.push([k, p[i][1]]);
        }
        if (tAt == null) {
          c.cross.setAttribute("visibility", "hidden");
          c.read.textContent = " ";
          continue;
        }
        const cx = c.x(tAt).toFixed(1);
        c.cross.setAttribute("x1", cx);
        c.cross.setAttribute("x2", cx);
        c.cross.setAttribute("visibility", "visible");
        vals.sort((a, b) => (a[0] === hl ? -1 : b[0] === hl ? 1 : b[1] - a[1]));
        const when = ((tAt - body.start) / axis.div).toFixed(2) + " " + axis.unit;
        c.read.textContent = when + ": " + vals.map(([k, v]) =>
          (k === "job" ? "" : (labels[k] || k) + " ") + fmtVal(v, c.def.unit)).join(", ");
      }
    }

    for (const c of charts) {
      if (c.empty) continue;
      let pending = null;
      c.svg.addEventListener("pointermove", (e) => {
        const r = c.svg.getBoundingClientRect();
        if (!r.width) return;
        const vx = ((e.clientX - r.left) / r.width) * W;
        const vy = ((e.clientY - r.top) / r.height) * H;
        if (vx < ML || vx > ML + PW) { clear(); return; }
        if (pending) cancelAnimationFrame(pending);
        pending = requestAnimationFrame(() => { pending = null; update(c, vx, vy); });
      });
      c.svg.addEventListener("pointerleave", () => {
        if (pending) { cancelAnimationFrame(pending); pending = null; }
        clear();
      });
    }

    return {
      colors,
      styles,
      setVisible(key, on) {
        if (on) hidden.delete(key); else hidden.add(key);
        if (hl === key && !on) hl = null;
        applyStyles();
      },
    };
  }

  root.JobstatsCharts = { render, COLORS };
})(typeof self !== "undefined" ? self : this);
