/* Explorer "Scatter" view: one dot per summarized GPU job at (GPU duty %, SM %), drawn on a canvas.
   Classification, colors, radius and hit lookup live in jobstats-core.js. Every string from the API
   reaches the page through textContent or setAttribute, never innerHTML. */
(function (root) {
  "use strict";
  const C = root.JobstatsCore;
  const ML = 40, MR = 12, MT = 10, MB = 34;   // plot frame inside the canvas, CSS px
  const ALPHA = 0.55;
  const RING = "#e4e4e7";
  const BUCKETS = [["1", "1 GPU"], ["2-7", "2–7 GPUs"], ["8+", "8+ GPUs"]];
  const REASON_LABELS = [
    ["not_started", "not started", false], ["cpu_only", "CPU-only", false], ["running", "running", true],
    ["no_summary", "no summary", true], ["no_value", "no duty or SM value", true],
  ];
  const NOTES = [
    "Duty and SM are measured separately (NVML and GPM) and averaged over the job's GPUs on their own, " +
      "so a job can sit above the line.",
    "GPU-h counts the whole job when it overlaps the range, even the part outside it.",
  ];

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  function clamp(v) { return Math.min(100, Math.max(0, v)); }

  function countText(sp) {
    let t = "Plotted " + C.fmtNum(sp.plotted.jobs) + " jobs, " + C.fmtGpuHours(sp.plotted.gpu_hours) + " GPU-h.";
    const parts = [];
    for (const [k, label, withHours] of REASON_LABELS) {
      const b = sp.excluded[k];
      if (!b.jobs) continue;
      parts.push(label + " " + C.fmtNum(b.jobs) + (withHours ? " (" + C.fmtGpuHours(b.gpu_hours) + " GPU-h)" : ""));
    }
    if (parts.length) t += " Not plotted: " + parts.join(", ") + ".";
    const out = sp.total_gpu_hours - sp.plotted.gpu_hours;
    if (sp.total_gpu_hours > 0 && out > 0) {
      t += " " + C.fmtNum(100 * out / sp.total_gpu_hours, 1) + " % of the selection's GPU-hours is not on the plot.";
    }
    return t;
  }

  function legendEl(colors) {
    const box = el("div", "js-sc-legend");
    for (const e of colors.legend) {
      const item = el("span", "js-sc-li");
      const sw = el("span", "js-sc-sw");
      sw.style.background = e.color;
      item.append(sw, el("span", "js-sc-key", String(e.key)), el("span", "js-dim", C.fmtGpuHours(e.gpu_hours) + " GPU-h"));
      box.append(item);
    }
    if (colors.other_groups) {
      const item = el("span", "js-sc-li");
      const sw = el("span", "js-sc-sw");
      sw.style.background = C.SCATTER_OTHER;
      item.append(sw, el("span", "js-sc-key", "other (" + C.fmtNum(colors.other_groups) + " groups)"));
      box.append(item);
    }
    const ring = el("span", "js-sc-li");
    ring.append(el("span", "js-sc-sw js-sc-sw-ring"), el("span", null, "ring: a GPU under 20 % duty"));
    const hollow = el("span", "js-sc-li");
    hollow.append(el("span", "js-sc-sw js-sc-sw-hollow"), el("span", null, "hollow: not every GPU reported"));
    box.append(ring, hollow);
    return box;
  }

  function tipLines(r) {
    const lines = [
      ["Job", r.jobid], ["Name", r.name], ["User", r.user || C.DASH], ["GPUs", C.fmtNum(r.alloc_gpus)],
      ["GPU-h", C.fmtGpuHours(r.gpu_hours)], ["Duty", C.fmtPct(r.gpu_pct)], ["SM", C.fmtPct(r.sm)],
    ];
    if (r.low_duty_gpus != null && r.summary_gpus) {
      lines.push(["Low-duty GPUs", r.low_duty_gpus + " of " + r.summary_gpus]);
    }
    const cov = r.coverage;
    if (cov && ((cov.gpu_pct != null && cov.gpu_pct < 1) || (cov.sm != null && cov.sm < 1))) {
      lines.push(["Coverage", "duty " + C.fmtPct(100 * (cov.gpu_pct ?? 0), 0) + ", SM " + C.fmtPct(100 * (cov.sm ?? 0), 0)]);
    }
    if (r.summary_gpus != null && r.summary_gpus < r.alloc_gpus) {
      lines.push(["Reported", r.summary_gpus + " of " + r.alloc_gpus + " GPUs"]);
    }
    return lines;
  }

  function render(container, rows, opts) {
    opts = opts || {};
    const sp = C.scatterSplit(rows);
    const colors = C.scatterColors(sp.points, opts.mode);
    let maxH = 0;
    for (const r of sp.points) if ((r.gpu_hours || 0) > maxH) maxH = r.gpu_hours;

    const tip = el("div", "js-sc-tip");
    tip.hidden = true;
    tip.setAttribute("role", "tooltip");
    const plots = el("div", "js-sc-plots");
    const notes = el("div", "js-sc-notes");
    for (const n of NOTES) notes.append(el("p", "js-hint", n));
    container.replaceChildren(el("div", "js-sc-count", countText(sp)), plots, legendEl(colors), notes, tip);

    const panels = [];
    if (!sp.points.length) {
      plots.append(el("div", "js-sc-empty", "No jobs to plot"));
    } else if (opts.split) {
      plots.classList.add("js-sc-split");
      for (const [b, label] of BUCKETS) {
        const pts = sp.points.filter((r) => C.scatterBucket(r.alloc_gpus) === b);
        let h = 0;
        for (const r of pts) h += r.gpu_hours || 0;
        panels.push(makePanel(label, pts, C.fmtNum(pts.length) + " jobs, " + C.fmtGpuHours(h) + " GPU-h"));
      }
    } else {
      panels.push(makePanel(null, sp.points, null));
    }
    for (const p of panels) plots.append(p.box);

    function makePanel(label, pts, sub) {
      const box = el("div", "js-sc-panel");
      if (label) {
        const head = el("div", "js-sc-head");
        head.append(el("span", "js-sc-title", label), el("span", "js-dim", sub));
        box.append(head);
      }
      const canvas = el("canvas", "js-sc-canvas");
      canvas.setAttribute("role", "img");
      canvas.setAttribute("aria-label", "Scatter of " + pts.length + " jobs: GPU duty against SM");
      box.append(canvas);
      const p = { box, canvas, pts, dots: [], index: null, hl: null, w: 0, h: 0 };
      bind(p);
      return p;
    }

    function layout(p) {
      const w = Math.max(160, Math.round(p.canvas.getBoundingClientRect().width || 0) || 600);
      const h = Math.max(200, Math.min(440, Math.round(w * 0.72)));
      const dpr = root.devicePixelRatio || 1;
      p.canvas.width = Math.round(w * dpr);
      p.canvas.height = Math.round(h * dpr);
      p.canvas.style.height = h + "px";
      p.w = w;
      p.h = h;
      const pw = w - ML - MR, ph = h - MT - MB;
      p.sx = (v) => ML + clamp(v) / 100 * pw;
      p.sy = (v) => MT + (1 - clamp(v) / 100) * ph;
      p.dots = p.pts.map((r) => ({ x: p.sx(r.gpu_pct), y: p.sy(r.sm), r: C.scatterRadius(r.gpu_hours, maxH, opts.size), row: r,
                                   color: colors.colorOf(r), marks: C.scatterMarks(r) }))
        .map((d, i) => [d, i]).sort((a, b) => b[0].r - a[0].r || a[1] - b[1]).map((x) => x[0]);
      p.index = C.scatterIndex(p.dots);
      p.hl = null;
      p.base = null;
      return dpr;
    }

    function draw(p, dpr) {
      const ctx = p.canvas.getContext("2d");
      if (!ctx) return;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, p.w, p.h);
      const x0 = p.sx(0), x1 = p.sx(100), y0 = p.sy(0), y1 = p.sy(100);
      ctx.lineWidth = 1;
      ctx.font = "10px ui-monospace, monospace";
      ctx.fillStyle = "#a1a1aa";
      for (let v = 0; v <= 100; v += 20) {
        ctx.strokeStyle = v === 0 ? "#52525b" : "#27272a";
        ctx.beginPath(); ctx.moveTo(p.sx(v), y0); ctx.lineTo(p.sx(v), y1); ctx.stroke();
        ctx.beginPath(); ctx.moveTo(x0, p.sy(v)); ctx.lineTo(x1, p.sy(v)); ctx.stroke();
        ctx.textAlign = "center"; ctx.textBaseline = "top";
        ctx.fillText(String(v), p.sx(v), y0 + 4);
        ctx.textAlign = "right"; ctx.textBaseline = "middle";
        ctx.fillText(String(v), x0 - 5, p.sy(v));
      }
      ctx.textAlign = "center"; ctx.textBaseline = "bottom";
      ctx.fillText("GPU duty %", (x0 + x1) / 2, p.h - 1);
      ctx.save();
      ctx.translate(10, (y0 + y1) / 2); ctx.rotate(-Math.PI / 2);
      ctx.textBaseline = "middle"; ctx.fillText("SM %", 0, 0);
      ctx.restore();
      ctx.setLineDash([5, 4]); ctx.strokeStyle = "#71717a";
      ctx.beginPath(); ctx.moveTo(x0, y0); ctx.lineTo(x1, y1); ctx.stroke();
      ctx.setLineDash([]);
      ctx.textAlign = "right"; ctx.textBaseline = "top";
      ctx.fillText("SM = duty (reference)", x1 - 4, y1 + 4);

      for (const d of p.dots) {
        const color = d.color, m = d.marks;
        ctx.beginPath();
        ctx.arc(d.x, d.y, d.r, 0, 2 * Math.PI);
        if (m.hollow) {
          ctx.globalAlpha = 1; ctx.lineWidth = 1.25; ctx.strokeStyle = color; ctx.stroke();
        } else {
          ctx.globalAlpha = ALPHA; ctx.fillStyle = color; ctx.fill();
        }
        ctx.globalAlpha = 1;
        if (m.ring) { ctx.lineWidth = 1.5; ctx.strokeStyle = RING; ctx.stroke(); }
      }
      try { p.base = ctx.getImageData(0, 0, p.canvas.width, p.canvas.height) || null; } catch (e) { p.base = null; }
      drawHighlight(p, ctx);
    }

    function drawHighlight(p, ctx) {
      if (!p.hl) return;
      ctx.beginPath();
      ctx.arc(p.hl.x, p.hl.y, p.hl.r + 2.5, 0, 2 * Math.PI);
      ctx.lineWidth = 2; ctx.strokeStyle = "#fafafa"; ctx.stroke();
    }

    // Hover: restore the saved base drawing and add the ring, instead of redrawing every dot.
    function rehighlight(p) {
      const ctx = p.canvas.getContext("2d");
      if (!ctx || !p.base) { draw(p, root.devicePixelRatio || 1); return; }
      const dpr = p.canvas.width / p.w;
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.putImageData(p.base, 0, 0);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      drawHighlight(p, ctx);
    }

    function showTip(p, d, cx, cy) {
      if (!d) { tip.hidden = true; return; }
      tip.replaceChildren(...tipLines(d.row).map(([k, v]) => {
        const line = el("div");
        line.append(el("span", "js-dim", k + " "), el("b", null, String(v)));
        return line;
      }));
      tip.hidden = false;
      const host = container.getBoundingClientRect ? container.getBoundingClientRect() : { left: 0, top: 0, width: 0 };
      const left = cx - host.left + 14;
      tip.style.left = Math.max(0, Math.min(left, (host.width || left + 240) - 240)) + "px";
      tip.style.top = (cy - host.top + 14) + "px";
    }

    function hitAt(p, e) {
      // Dots live in the layout's CSS-px space; the canvas may be shown at another size since then.
      const rect = p.canvas.getBoundingClientRect();
      const kx = rect.width > 0 ? p.w / rect.width : 1, ky = rect.height > 0 ? p.h / rect.height : 1;
      return C.scatterHit(p.index, (e.clientX - rect.left) * kx, (e.clientY - rect.top) * ky);
    }

    function bind(p) {
      let pending = null;
      p.canvas.addEventListener("pointermove", (e) => {
        const ev = { clientX: e.clientX, clientY: e.clientY };
        if (pending) { pending = ev; return; }
        pending = ev;
        const raf = root.requestAnimationFrame || ((f) => setTimeout(f, 16));
        raf(() => {
          const last = pending;
          pending = null;
          if (!p.index) return;
          const d = hitAt(p, last);
          if (d !== p.hl) {
            p.hl = d;
            rehighlight(p);
            p.canvas.style.cursor = d ? "pointer" : "";
          }
          showTip(p, d, last.clientX, last.clientY);
        });
      });
      p.canvas.addEventListener("pointerleave", () => {
        if (p.hl) { p.hl = null; rehighlight(p); }
        tip.hidden = true;
      });
      p.canvas.addEventListener("click", (e) => {
        if (!p.index) return;
        const d = hitAt(p, e);
        if (d && opts.onOpen) opts.onOpen(d.row, !!(e.ctrlKey || e.metaKey));
      });
    }

    function redraw() {
      tip.hidden = true;
      for (const p of panels) draw(p, layout(p));
    }

    redraw();
    return { redraw };
  }

  root.JobstatsScatter = { render };
})(typeof self !== "undefined" ? self : this);
