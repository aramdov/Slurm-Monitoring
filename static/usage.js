'use strict';

const ADMIN_TOKEN_STORAGE_KEY = 'slurm-monitor-admin-token';

function apiUrl(path) {
  return new URL(String(path).replace(/^\/+/, ''), document.baseURI).toString();
}

async function adminFetch(path, options = {}) {
  for (let attempt = 0; attempt < 2; attempt++) {
    let token = sessionStorage.getItem(ADMIN_TOKEN_STORAGE_KEY);
    if (!token) {
      token = window.prompt('Admin token required to change units and assignments:');
      if (!token) return null;
      sessionStorage.setItem(ADMIN_TOKEN_STORAGE_KEY, token);
    }

    const headers = new Headers(options.headers || {});
    headers.set('X-Slurm-Monitor-Admin-Token', token);
    const response = await fetch(apiUrl(path), { ...options, headers });
    if (response.status === 401) {
      sessionStorage.removeItem(ADMIN_TOKEN_STORAGE_KEY);
      if (attempt === 0) alert('That admin token was not accepted.');
      continue;
    }
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      alert(data.error || `Request failed (HTTP ${response.status})`);
      return null;
    }
    return response;
  }
  return null;
}

const PALETTE = [
  '#3b82f6','#10b981','#f59e0b','#ef4444','#8b5cf6',
  '#ec4899','#06b6d4','#84cc16','#f97316','#6366f1',
  '#14b8a6','#eab308','#f43f5e','#a855f7','#22c55e',
  '#0ea5e9','#d946ef','#fb923c','#4ade80','#60a5fa',
];

const state = {
  data: null,
  waitData: null,
  visibleUsers: new Set(),
  allUsers: [],
  userColors: {},
  gpuChart: null,
  cpuChart: null,
  queueChart: null,
  waitingData: null,   // GET /api/waiting response, null when that call failed
  waitingChart: null,
  waitingSort: { key: 'waiting_h', descending: true },
  waitingIncludeEstimates: false,
  waitingOpenUser: null,   // person whose job detail row is open
  waitingRangeEnd: null,   // to_ms of the last /api/waiting request
  viewMode: 'users',   // 'users' | 'projects' | 'units'
  _lastFetchedMode: null,
  unitsData: null,     // cached GET /api/units response
};

// ── Date helpers ─────────────────────────────────────────────

function getPresetRange(preset) {
  const now = Date.now();
  const d = new Date();
  switch (preset) {
    case 'last1':       return { from: now - 1  * 86400000, to: now };
    case 'last2':       return { from: now - 2  * 86400000, to: now };
    case 'last4':       return { from: now - 4  * 86400000, to: now };
    case 'last7':       return { from: now - 7  * 86400000, to: now };
    case 'last30':      return { from: now - 30 * 86400000, to: now };
    case 'last3months': return { from: now - 90 * 86400000, to: now };
    case 'last6months': return { from: now - 180 * 86400000, to: now };
    case 'lastyear':    return { from: now - 365 * 86400000, to: now };
    case 'thismonth':
      return { from: new Date(d.getFullYear(), d.getMonth(), 1).getTime(), to: now };
    case 'lastmonth': {
      const y = d.getMonth() === 0 ? d.getFullYear() - 1 : d.getFullYear();
      const m = d.getMonth() === 0 ? 11 : d.getMonth() - 1;
      return {
        from: new Date(y, m, 1).getTime(),
        to: new Date(y, m + 1, 1, 0, 0, 0, -1).getTime(),
      };
    }
    default: return { from: now - 7 * 86400000, to: now };
  }
}

function msToDateInput(ms) {
  const d = new Date(ms);
  const y = d.getFullYear();
  const mo = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  const h = String(d.getHours()).padStart(2, '0');
  const min = String(d.getMinutes()).padStart(2, '0');
  return `${y}-${mo}-${day}T${h}:${min}`;
}

function msToDateStr(ms) {
  if (ms == null) return '';
  const d = new Date(ms);
  return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
}

function dateStrToMs(str) {
  if (!str) return null;
  const [y, m, d] = str.split('-').map(Number);
  return new Date(y, m - 1, d, 0, 0, 0, 0).getTime();
}

function dateInputToMsStart(val) {
  if (!val) return NaN;
  const [date, time] = val.includes('T') ? val.split('T') : [val, '00:00'];
  const [y, mo, d] = date.split('-').map(Number);
  const [h, min] = time.split(':').map(Number);
  return new Date(y, mo - 1, d, h, min, 0, 0).getTime();
}

function dateInputToMsEnd(val) {
  if (!val) return NaN;
  const [date, time] = val.includes('T') ? val.split('T') : [val, '23:59'];
  const [y, mo, d] = date.split('-').map(Number);
  const [h, min] = time.split(':').map(Number);
  return new Date(y, mo - 1, d, h, min, 59, 999).getTime();
}

// ── Formatting ───────────────────────────────────────────────

function hexToRgba(hex, alpha) {
  const r = parseInt(hex.slice(1, 3), 16);
  const g = parseInt(hex.slice(3, 5), 16);
  const b = parseInt(hex.slice(5, 7), 16);
  return `rgba(${r},${g},${b},${alpha})`;
}

function fmtHours(h) {
  return Math.round(h).toLocaleString();
}

function fmtWait(h) {
  if (h < 1 / 60) return '< 1m';
  if (h < 1) return `${Math.round(h * 60)}m`;
  const hh = Math.floor(h);
  const mm = Math.round((h - hh) * 60);
  return mm > 0 ? `${hh}h ${mm}m` : `${hh}h`;
}

// ── GPU/CPU chart helpers ─────────────────────────────────────

function buildDatasets(userMap) {
  return state.allUsers
    .filter(u => state.visibleUsers.has(u))
    .map(u => {
      const color = state.userColors[u] || '#888';
      const { buckets, bucket_durations_ms, bucket_size_ms } = state.data;
      const data = (userMap[u] || []).map((y, i) => ({ x: buckets[i], y }));
      const last = data.length - 1;
      if (last >= 0) {
        const duration = bucket_durations_ms?.[last] ?? bucket_size_ms;
        if (duration > 0) data.push({ x: buckets[last] + duration, y: data[last].y });
      }
      return { label: u, data,
        fill: true,
        borderColor: color,
        backgroundColor: hexToRgba(color, 1.0),
        borderWidth: 1,
        pointRadius: 0,
        stepped: 'before',
        stack: 'stack',
      };
    });
}

function buildCapacityDatasets(resource) {
  const points = state.data.capacity_points || [];
  return [
    [resource, 'Installed capacity', '#e4e4e7', []],
    [`drained_${resource}`, 'Installed minus draining / drained', '#fbbf24', [6, 4]],
    [`down_${resource}`, 'Installed minus down / unavailable', '#f87171', [3, 4]],
  ].filter(([key], i) => i === 0 || points.some(p => p[key] > 0))
    .map(([key, label, color, dash]) => ({
      label,
      data: points.map(p => {
        const installed = p[resource];
        if (key === resource) return { x: p.timestamp_ms, y: installed };
        const reduction = p[key] ?? 0;
        return { x: p.timestamp_ms, y: installed == null ? null : installed - reduction, reduction };
      }),
      isCapacity: true, fill: false, borderColor: color, borderDash: dash,
      // Chart.js "before" holds the previous value until the next timestamp.
      borderWidth: 2, pointRadius: 0, stepped: 'before', spanGaps: false,
      stack: key, order: key === resource ? -11 : -10,
    }));
}

function resourceHours(values, data = state.data) {
  return (values || []).reduce((sum, value, i) =>
    sum + value * (data.bucket_durations_ms?.[i] ?? data.bucket_size_ms) / 3_600_000, 0);
}

function installedHours(resource) {
  if (!state.data.capacity_coverage_complete) return null;
  return state.data[resource === 'gpus' ? 'installed_gpu_hours' : 'installed_cpu_hours'];
}

function allocationPercent(hours, available) {
  return available == null ? null : (available > 0 ? hours / available * 100 : 0);
}

function chartMaximum(resource, userMap) {
  const points = (state.data.capacity_points || []).map(p => p[resource] || 0);
  const allocations = state.data.buckets.map((_, i) =>
    Object.values(userMap).reduce((sum, values) => sum + (values[i] || 0), 0));
  return Math.max(1, ...points, ...allocations);
}

const usageYAxisWidth = 48;
const allocationTickFormat = new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 });
let queueTimeAxis;

const queueTimeAxisSync = {
  id: 'queueTimeAxisSync',
  afterUpdate(chart) {
    const { min, max, ticks } = chart.scales.x;
    const next = { min, max, ticks: ticks.map(({ value, major, label }) => ({ value, major, label })) };
    if (JSON.stringify(queueTimeAxis) === JSON.stringify(next)) return;
    queueTimeAxis = next;
    requestAnimationFrame(() => {
      for (const chart of [state.gpuChart, state.cpuChart]) chart?.update('none');
    });
  },
};

function makeChartOptions(yMax, resource) {
  const gridColor = '#2a2a30';
  const tickColor = '#71717a';
  const fontOpts = { family: '"JetBrains Mono","Fira Code",ui-monospace,monospace', size: 11 };
  return {
    responsive: true,
    maintainAspectRatio: false,
    animation: false,
    interaction: { mode: 'nearest', axis: 'x', intersect: false },
    plugins: {
      legend: { display: false },
      tooltip: {
        backgroundColor: '#18181c',
        borderColor: '#2a2a30',
        borderWidth: 1,
        titleFont: fontOpts,
        bodyFont: fontOpts,
        footerFont: { ...fontOpts, weight: '600' },
        callbacks: {
          label(ctx) {
            if (ctx.raw?.reduction != null) {
              if (ctx.raw.reduction === 0 || ctx.parsed.y == null) return null;
              return ` ${ctx.dataset.label}: ${ctx.parsed.y.toFixed(1)} (−${ctx.raw.reduction})`;
            }
            if (ctx.parsed.y === 0) return null;
            return ` ${ctx.dataset.label}: ${ctx.parsed.y.toFixed(1)}`;
          },
          footer(items) {
            const allocations = items.filter(i => !i.dataset.isCapacity);
            if (!allocations.length) return '';
            const total = allocations.reduce((s, i) => s + i.parsed.y, 0);
            const index = Math.min(allocations[0].dataIndex, state.data.buckets.length - 1);
            const available = state.data.capacity_buckets?.[resource]?.[index];
            return available > 0
              ? `Allocated: ${total.toFixed(1)} / ${available.toFixed(1)} (${(100 * total / available).toFixed(1)}%)`
              : `Allocated: ${total.toFixed(1)}; capacity unknown`;
          },
        },
      },
    },
    scales: {
      x: {
        type: 'time', offset: false,
        afterDataLimits(scale) {
          if (!queueTimeAxis) return;
          scale.min = queueTimeAxis.min;
          scale.max = queueTimeAxis.max;
        },
        afterBuildTicks(scale) {
          if (queueTimeAxis) scale.ticks = queueTimeAxis.ticks.map(tick => ({ ...tick }));
        },
        ticks: { source: 'labels', autoSkip: false, major: { enabled: true },
          color: tickColor, font: fontOpts, maxRotation: 0,
          callback(value) {
            return queueTimeAxis?.ticks.find(tick => tick.value === value)?.label ?? this.getLabelForValue(value);
          },
        },
        grid: { color: gridColor },
      },
      y: {
        stacked: true,
        max: yMax,
        min: 0,
        afterFit: scale => { scale.width = usageYAxisWidth; },
        ticks: { color: tickColor, font: fontOpts, callback: value => allocationTickFormat.format(value) },
        grid: { color: gridColor },
      },
    },
  };
}

function renderCharts() {
  const { data } = state;
  const gpuMax = chartMaximum('gpus', data.users);
  const cpuMax = chartMaximum('cpus', data.cpu_users);

  const gpuDatasets = [...buildDatasets(data.users), ...buildCapacityDatasets('gpus')];
  const cpuDatasets = [...buildDatasets(data.cpu_users), ...buildCapacityDatasets('cpus')];

  if (state.gpuChart) {
    state.gpuChart.data.datasets = gpuDatasets;
    state.gpuChart.data.labels = data.buckets;
    state.gpuChart.options.scales.y.max = gpuMax;
    state.gpuChart.update();
  } else {
    state.gpuChart = new Chart(
      document.getElementById('gpu-chart'),
      { type: 'line', data: { datasets: gpuDatasets }, options: makeChartOptions(gpuMax, 'gpus') }
    );
  }

  if (state.cpuChart) {
    state.cpuChart.data.datasets = cpuDatasets;
    state.cpuChart.data.labels = data.buckets;
    state.cpuChart.options.scales.y.max = cpuMax;
    state.cpuChart.update();
  } else {
    state.cpuChart = new Chart(
      document.getElementById('cpu-chart'),
      { type: 'line', data: { datasets: cpuDatasets }, options: makeChartOptions(cpuMax, 'cpus') }
    );
  }
}

function updateChartsVisibility() {
  if (state.gpuChart) {
    state.gpuChart.data.datasets = [...buildDatasets(state.data.users), ...buildCapacityDatasets('gpus')];
    state.gpuChart.update('none');
  }
  if (state.cpuChart) {
    state.cpuChart.data.datasets = [...buildDatasets(state.data.cpu_users), ...buildCapacityDatasets('cpus')];
    state.cpuChart.update('none');
  }
  if (state.queueChart) {
    state.queueChart.data.datasets = buildQueueDatasets();
    state.queueChart.update('none');
  }
  if (state.waitingChart && state.waitingData) {
    state.waitingChart.data.datasets = buildWaitingDatasets();
    state.waitingChart.update('none');
  }
  renderWaitingTable();
  renderWaitMetrics();
}

// ── Metrics ──────────────────────────────────────────────────

function renderMetrics() {
  const { data, visibleUsers } = state;
  const totalAvailGPU = installedHours('gpus');
  const usedGPU = [...visibleUsers].reduce((s, u) => {
    return s + resourceHours(data.users[u]);
  }, 0);
  const gpuPct = allocationPercent(usedGPU, totalAvailGPU);

  const totalAvailCPU = installedHours('cpus');
  const usedCPU = [...visibleUsers].reduce((s, u) => {
    return s + resourceHours(data.cpu_users[u]);
  }, 0);
  const cpuPct = allocationPercent(usedCPU, totalAvailCPU);

  document.getElementById('gpu-util-pct').textContent = gpuPct == null ? '—' : `${Math.round(gpuPct)}%`;
  document.getElementById('gpu-util-hours').innerHTML =
    `<span>${fmtHours(usedGPU)}</span><span class="hours-sep">/ ${totalAvailGPU == null ? '—' : fmtHours(totalAvailGPU)}</span><span class="hours-unit">GPU-hours</span>`;

  document.getElementById('cpu-util-pct').textContent = cpuPct == null ? '—' : `${Math.round(cpuPct)}%`;
  document.getElementById('cpu-util-hours').innerHTML =
    `<span>${fmtHours(usedCPU)}</span><span class="hours-sep">/ ${totalAvailCPU == null ? '—' : fmtHours(totalAvailCPU)}</span><span class="hours-unit">CPU-hours</span>`;
  const note = document.getElementById('capacity-note');
  const from = data.capacity_known_from_ms;
  note.textContent = data.capacity_coverage_complete
    ? ''
    : from == null
      ? 'Historical capacity is unknown for this range; percentages are unavailable.'
      : `Capacity history starts ${new Date(from).toLocaleString()}. Select a range after that time for percentages; earlier capacity is unknown.`;
}

// ── Wait time chart & metrics ─────────────────────────────────

function queueMeasure() {
  if (document.querySelector('input[name=queue-measure]:checked').value === 'jobs') return 'jobs';
  return document.getElementById('queue-category').value === 'cpu_only' ? 'cpus' : 'gpus';
}

function buildQueueDatasets() {
  const { waitData, visibleUsers } = state;
  const category = document.getElementById('queue-category').value;
  return waitData ? PendingAge.ageBands(waitData, visibleUsers, category, queueMeasure()) : [];
}

const queueBandGlow = {
  id: 'queueBandGlow',
  beforeDatasetDraw(chart, { index }) {
    const { glow } = chart.data.datasets[index];
    if (!glow) return;
    chart.ctx.save();
    chart.ctx.shadowColor = glow;
    chart.ctx.shadowBlur = 8;
  },
  afterDatasetDraw(chart, { index }) {
    if (chart.data.datasets[index].glow) chart.ctx.restore();
  },
};

function renderQueueChart() {
  if (!state.waitData) return;
  const datasets = buildQueueDatasets();
  const { buckets, bucket_durations_ms, bucket_size_ms } = state.data;
  const last = buckets.length - 1;
  const rangeEnd = last >= 0 ? buckets[last] + (bucket_durations_ms?.[last] ?? bucket_size_ms) : undefined;
  const modeLabel = document.getElementById('queue-category').value === 'cpu_only' ? 'CPUs' : 'GPUs';
  document.getElementById('queue-resource-label').textContent = modeLabel;
  const unit = queueMeasure() === 'jobs' ? 'jobs' : modeLabel;
  const gridColor = '#2a2a30';
  const tickColor = '#71717a';
  const fontOpts = { family: '"JetBrains Mono",ui-monospace,monospace', size: 11 };
  const formatDate = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' });
  const formatCount = new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 });
  const options = {
    responsive: true, maintainAspectRatio: false, animation: false,
    interaction: { mode: 'index', intersect: false },
    plugins: {
      legend: { display: true, position: 'top', align: 'end',
        labels: { color: tickColor, font: fontOpts, usePointStyle: true, boxWidth: 8, padding: 10 } },
      tooltip: {
        backgroundColor: '#18181c', borderColor: '#2a2a30', borderWidth: 1,
        titleFont: fontOpts, bodyFont: fontOpts,
        callbacks: {
          title(items) {
            const interval = items[0]?.raw;
            return interval ? formatDate.formatRange(new Date(interval.startTime), new Date(interval.endTime)) : '';
          },
          label(ctx) {
            return ctx.parsed.y == null ? null : `${ctx.dataset.label}: ${formatCount.format(ctx.parsed.y)} ${unit}`;
          },
          footer(items) {
            const interval = items[0]?.raw;
            if (!interval) return [];
            const total = items.reduce((sum, item) => sum + (item.parsed.y ?? 0), 0);
            return [`Average total: ${formatCount.format(total)} ${unit}`, `Samples: ${interval.known}/${interval.expected}`];
          },
        },
      },
    },
    scales: {
      x: { type: 'time', offset: false, stacked: true,
        min: buckets[0] ?? datasets[0]?.data[0]?.startTime,
        max: rangeEnd ?? datasets[0]?.data.at(-1)?.endTime,
        time: { displayFormats: { hour: 'ha', day: 'MMM d' } },
        ticks: { major: { enabled: true }, color: tickColor, font: fontOpts, maxRotation: 0 },
        grid: { color: gridColor, offset: false } },
      y: { min: 0, stacked: true, afterFit: scale => { scale.width = usageYAxisWidth; },
        ticks: { color: tickColor, font: fontOpts }, grid: { color: gridColor } },
    },
  };
  if (state.queueChart) {
    state.queueChart.data.datasets = datasets;
    state.queueChart.options = options;
    state.queueChart.update();
  } else {
    state.queueChart = new Chart(document.getElementById('queue-chart'), {
      type: 'bar', data: { datasets }, options, plugins: [queueBandGlow, queueTimeAxisSync],
    });
  }
}


function renderWaitMetrics() {
  const { waitData, visibleUsers } = state;
  const grid = document.getElementById('wait-stats-grid');
  if (!waitData || !grid) return;
  const last = waitData.buckets.length - 1;
  const stats = last >= 0 ? PendingAge.summarize(waitData, visibleUsers, document.getElementById('queue-category').value, last)
    : { median: null, p90: null, count: null };
  const rows = [
    ['Median', stats.median === null ? '—' : fmtWait(stats.median / 3600000)],
    ['P90', stats.p90 === null ? '—' : fmtWait(stats.p90 / 3600000)],
    ['Pending jobs', stats.count === null ? '—' : stats.count.toLocaleString()],
  ];
  grid.innerHTML = rows.map(([label, value]) => `<span class="wait-stat-label">${label}</span><span class="wait-stat-value">${value}</span>`).join('');
}

// ── Waiting people ──────────────────────────────────────────

function waitingSelection() {
  return state.viewMode === 'users' ? state.visibleUsers : null;
}

function fmtWaitingHours(h) {
  if (h == null) return '—';
  return h === 0 ? '0' : h < 10 ? h.toFixed(1) : Math.round(h).toLocaleString();
}

function fmtGpus(avg, max) {
  if (avg == null) return '—';
  const value = Number.isInteger(avg) ? String(avg) : avg.toFixed(1);
  return max != null && max !== avg ? `${value} / ${max}` : value;
}

function renderWaitingStats() {
  const grid = document.getElementById('waiting-stats-grid');
  const data = state.waitingData;
  if (!grid) return;
  const current = data?.current;
  const asOf = current ? new Date(current.as_of_ms).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' }) : '—';
  const rows = [
    ['Waiting now (2h+)', current ? current.people_2h.toLocaleString() : '—'],
    ['Waiting now (24h+)', current ? current.people_24h.toLocaleString() : '—'],
    ['As of', asOf],
  ];
  grid.innerHTML = rows.map(([label, value]) => `<span class="wait-stat-label">${label}</span><span class="wait-stat-value${value === '—' || label === 'As of' ? ' muted' : ''}">${value}</span>`).join('');
}

function buildWaitingDatasets() {
  const data = state.waitingData;
  if (!data) return [];
  const rows = Waiting.series(data, waitingSelection());
  const point = key => rows.map(row => ({ ...row, x: row.x, y: row[key] }));
  const barColor = color => ctx => hexToRgba(color, ctx.raw?.estimate ? 0.4 : 0.85);
  return [
    { type: 'bar', label: 'Waiting 2h+, nothing running', data: point('idle2'), stack: 'w2', order: 2,
      backgroundColor: barColor('#f43f5e'), borderWidth: 0 },
    { type: 'bar', label: 'Waiting 2h+, already running GPUs', data: point('busy2'), stack: 'w2', order: 2,
      backgroundColor: barColor('#fb923c'), borderWidth: 0 },
    { type: 'line', label: 'Waiting 24h+', data: point('total24'), stack: 'w24', order: 1,
      borderColor: '#facc15', backgroundColor: '#facc15', borderWidth: 2, pointRadius: 0, spanGaps: false, fill: false },
    { type: 'line', label: 'Waited 2h+ at any point', data: point('any2'), stack: 'any', order: 1,
      borderColor: '#a1a1aa', backgroundColor: '#a1a1aa', borderWidth: 1.5, borderDash: [4, 4],
      pointRadius: 0, spanGaps: false, fill: false },
  ];
}

function renderWaitingChart() {
  const unavailable = document.getElementById('waiting-unavailable');
  const canvas = document.getElementById('waiting-chart');
  if (!state.waitingData) {
    if (unavailable) unavailable.hidden = false;
    if (canvas) canvas.hidden = true;
    renderWaitingStats();
    return;
  }
  unavailable.hidden = true;
  canvas.hidden = false;
  document.getElementById('waiting-selection-note').hidden = state.viewMode === 'users';
  renderWaitingStats();
  const datasets = buildWaitingDatasets();
  const { buckets, bucket_durations_ms, bucket_size_ms } = state.data;
  const last = buckets.length - 1;
  const rangeEnd = last >= 0 ? buckets[last] + (bucket_durations_ms?.[last] ?? bucket_size_ms) : undefined;
  const gridColor = '#2a2a30';
  const tickColor = '#71717a';
  const fontOpts = { family: '"JetBrains Mono",ui-monospace,monospace', size: 11 };
  const formatDate = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' });
  const formatCount = new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 });
  const options = {
    responsive: true, maintainAspectRatio: false, animation: false,
    interaction: { mode: 'index', intersect: false },
    plugins: {
      legend: { display: true, position: 'top', align: 'end',
        labels: { color: tickColor, font: fontOpts, usePointStyle: true, boxWidth: 8, padding: 10 } },
      tooltip: {
        backgroundColor: '#18181c', borderColor: '#2a2a30', borderWidth: 1,
        titleFont: fontOpts, bodyFont: fontOpts,
        callbacks: {
          title(items) {
            const interval = items[0]?.raw;
            return interval ? formatDate.formatRange(new Date(interval.startTime), new Date(interval.endTime)) : '';
          },
          label(ctx) {
            if (ctx.parsed.y == null) return null;
            const suffix = ctx.dataset.label === 'Waited 2h+ at any point' ? '' : ' on average';
            return `${ctx.dataset.label}: ${formatCount.format(ctx.parsed.y)} people${suffix}`;
          },
          footer(items) {
            const interval = items[0]?.raw;
            if (!interval) return [];
            const lines = [`Samples: ${interval.known}/${interval.expected}`];
            if (interval.estimate) lines.push('Estimate from accounting, no pending reasons');
            return lines;
          },
        },
      },
    },
    scales: {
      x: { type: 'time', offset: false, stacked: true,
        min: buckets[0] ?? datasets[0]?.data[0]?.startTime,
        max: rangeEnd ?? datasets[0]?.data.at(-1)?.endTime,
        time: { displayFormats: { hour: 'ha', day: 'MMM d' } },
        ticks: { major: { enabled: true }, color: tickColor, font: fontOpts, maxRotation: 0 },
        grid: { color: gridColor, offset: false } },
      y: { min: 0, stacked: true, afterFit: scale => { scale.width = usageYAxisWidth; },
        title: { display: true, text: 'people', color: tickColor, font: fontOpts },
        ticks: { color: tickColor, font: fontOpts, precision: 0 }, grid: { color: gridColor } },
    },
  };
  if (state.waitingChart) {
    state.waitingChart.data.datasets = datasets;
    state.waitingChart.options = options;
    state.waitingChart.update();
  } else {
    state.waitingChart = new Chart(canvas, { type: 'bar', data: { datasets }, options });
  }
}

const waitingColumns = [
  { key: 'user', label: 'Person', format: (row) => escHtml(row.user) },
  { key: 'waiting_h', label: 'Waiting (h)', format: row => fmtWaitingHours(row.waiting_h) },
  { key: 'waiting_24h_h', label: 'of which 24h+ (h)', format: row => fmtWaitingHours(row.waiting_24h_h) },
  { key: 'longest_wait_h', label: 'Longest wait', format: row => row.longest_wait_h == null ? '—' : fmtWait(row.longest_wait_h) },
  { key: 'current_wait_h', label: 'Waiting now', format: row => row.current_wait_h == null ? '' : fmtWait(row.current_wait_h) },
  { key: 'avg_asked_gpus', label: 'GPUs asked', format: row => fmtGpus(row.avg_asked_gpus, row.max_asked_gpus) },
  { key: 'avg_running_gpus', label: 'GPUs running meanwhile', format: row => fmtGpus(row.avg_running_gpus, row.max_running_gpus) },
  { key: 'idle_h', label: 'With 0 running (h)', format: row => fmtWaitingHours(row.idle_h) },
  { key: 'capped_h', label: 'At own cap (h)', format: row => fmtWaitingHours(row.capped_h) },
];

const fmtWaitingDate = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' });

function capsText(caps, defaultCap) {
  const list = Object.entries(caps).map(([qos, cap]) => `${qos} ${cap == null ? 'none' : cap}`).join(', ');
  return `${list}; unknown QOS ${defaultCap ?? 'none'}`;
}

// Only the QOS that differ from the period before, so a dated change reads as one short line.
function capHistoryText(periods) {
  return periods.map((period, i) => {
    if (i === 0) return `Own-cap rule (GPUs per user): ${capsText(period.caps, period.default_cap)}.`;
    const before = periods[i - 1];
    const changed = Object.entries(period.caps).filter(([qos, cap]) => before.caps[qos] !== cap)
      .map(([qos, cap]) => `${qos} ${before.caps[qos] ?? 'none'} → ${cap ?? 'none'}`);
    if (before.default_cap !== period.default_cap) changed.push(`unknown QOS ${before.default_cap} → ${period.default_cap}`);
    return `From ${fmtWaitingDate.format(new Date(period.from_ms))}: ${changed.join(', ') || 'no change'}.`;
  }).join(' ');
}

function renderWaitingWarnings() {
  const data = state.waitingData;
  const drift = document.getElementById('waiting-drift');
  const estimate = document.getElementById('waiting-estimate-note');
  const differs = data?.rules?.cap_drift || [];
  drift.hidden = !differs.length;
  drift.textContent = differs.length
    ? `Live Slurm caps differ from the dashboard config (config.py WAITING_QOS_GPU_CAP_HISTORY): ${differs.map(d => `${d.qos} configured ${d.configured ?? 'none'}, live ${d.live ?? 'none'}`).join('; ')}. Counts after the change use the old cap until a dated period is added.`
    : '';
  const share = data ? Waiting.estimateShare(data) : 0;
  estimate.hidden = share === 0;
  estimate.textContent = share
    ? `${Math.round(share * 100)}% of this range is estimated from Slurm accounting (faded bars). Accounting has no pending reasons, so held jobs, jobs waiting on another job, and jobs over a partition time limit count as waiting there. The table ${state.waitingIncludeEstimates ? 'includes' : 'leaves out'} those hours.`
    : '';
}

function renderWaitingFootnote() {
  const note = document.getElementById('waiting-footnote');
  const data = state.waitingData;
  if (!note) return;
  if (!data) { note.textContent = ''; return; }
  const { excluded_partitions, unmapped_qos } = data.rules;
  const periods = data.rules.cap_history || [{ from_ms: null, caps: data.rules.caps, default_cap: data.rules.default_cap }];
  const parts = [capHistoryText(periods),
    `Excluded partitions: ${excluded_partitions.length ? excluded_partitions.join(', ') : 'none'}.`,
    'Averages divide by recorded samples; hours are samples × step. Faded bars are estimates from accounting.'];
  if (unmapped_qos.length) parts.push(`QOS without a configured cap: ${unmapped_qos.join(', ')}.`);
  const unclassified = Object.entries(data.unclassified_reasons || {});
  if (unclassified.length) {
    parts.push(`Not counted, unclassified reasons (job samples): ${unclassified.map(([reason, count]) => `${reason} ${count}`).join(', ')}.`);
  }
  note.textContent = parts.join(' ');
}

function renderWaitingTable() {
  const container = document.getElementById('waiting-table');
  if (!container) return;
  renderWaitingFootnote();
  renderWaitingWarnings();
  const data = state.waitingData;
  if (!data) { container.innerHTML = ''; return; }
  const { key, descending } = state.waitingSort;
  const rows = Waiting.tableRows(data, waitingSelection(), key, descending, state.waitingIncludeEstimates);
  const head = waitingColumns.map(column => {
    const arrow = column.key === key ? (descending ? ' ▼' : ' ▲') : '';
    return `<th data-key="${column.key}" class="${column.key === key ? 'sorted' : ''}">${column.label}${arrow}</th>`;
  }).join('');
  const body = rows.length ? rows.map(row => {
    const estimate = row.estimate_h > 0;
    const cells = waitingColumns.map((column, i) => {
      const mark = i === 0 && estimate ? `<span class="estimate-mark" title="${fmtWaitingHours(row.estimate_h)} h from the accounting estimate">est.</span>` : '';
      return `<td>${column.format(row)}${mark}</td>`;
    }).join('');
    const open = row.user === state.waitingOpenUser;
    const detail = open ? `<tr class="detail"><td class="waiting-detail" colspan="${waitingColumns.length}" id="waiting-detail">Loading…</td></tr>` : '';
    return `<tr class="person${estimate ? ' estimate' : ''}${open ? ' open' : ''}" data-user="${escHtml(row.user)}">${cells}</tr>${detail}`;
  }).join('') : `<tr><td colspan="${waitingColumns.length}">${state.waitingIncludeEstimates ? 'Nobody waited 2h+ or sat at their cap in this range.' : 'Nobody waited 2h+ or sat at their cap in the recorded part of this range.'}</td></tr>`;
  container.innerHTML = `<table class="waiting-table"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
  container.querySelectorAll('th').forEach(th => {
    th.addEventListener('click', () => {
      const next = th.dataset.key;
      state.waitingSort = next === state.waitingSort.key
        ? { key: next, descending: !state.waitingSort.descending }
        : { key: next, descending: next !== 'user' };
      renderWaitingTable();
    });
  });
  container.querySelectorAll('tr.person').forEach(tr => {
    tr.addEventListener('click', () => {
      state.waitingOpenUser = state.waitingOpenUser === tr.dataset.user ? null : tr.dataset.user;
      renderWaitingTable();
    });
  });
  if (state.waitingOpenUser && document.getElementById('waiting-detail')) loadWaitingDetail(state.waitingOpenUser);
}

const waitingVerdicts = {
  null: 'counts',
  too_young: 'ready for less than 2h',
  over_cap: 'over own GPU cap',
  cap_reason: 'Slurm says own GPU cap (QOSMaxGRESPerUser)',
  dependency_or_hold: 'waits on another job, or held',
  not_cluster_reason: 'own limit, not the cluster (see reason)',
  unclassified_reason: 'reason not classified yet',
  excluded_partition: 'excluded partition',
  no_eligible: 'not eligible yet (dependency, hold or begin time)',
  no_details: 'job details not recorded',
};

// The instant the detail describes: the latest recorder sample if it is inside the range, else the range end.
function waitingDetailTime() {
  const asOf = state.waitingData?.current?.as_of_ms;
  if (asOf != null) return asOf;
  return state.waitingRangeEnd != null ? Math.min(state.waitingRangeEnd, Date.now()) - 1 : null;
}

async function loadWaitingDetail(user) {
  const t = waitingDetailTime();
  const query = `user=${encodeURIComponent(user)}${t != null ? `&t=${t}` : ''}`;
  let detail;
  try {
    const response = await fetch(apiUrl(`api/waiting/explain?${query}`));
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    detail = await response.json();
  } catch (err) {
    console.error('Waiting detail unavailable', err);
    detail = null;
  }
  const cell = document.getElementById('waiting-detail');
  if (!cell || state.waitingOpenUser !== user) return;   // table re-rendered or closed meanwhile
  if (!detail) { cell.textContent = 'Job detail unavailable.'; return; }
  const when = fmtWaitingDate.format(new Date(detail.t_ms));
  const source = detail.source === 'observed' ? 'recorded queue'
    : detail.source === 'accounting' ? 'accounting estimate, no pending reasons' : 'no data';
  const status = { waiting: 'waiting', capped: 'at own cap', not_counted: 'not counted', none: 'no pending GPU jobs' }[detail.status] || detail.status;
  const running = Object.entries(detail.running_by_qos || {}).map(([qos, gpus]) => `${gpus} (${qos === 'null' || qos === 'None' ? 'QOS unknown' : qos})`).join(', ');
  const head = `<div class="waiting-detail-head">${escHtml(user)} at ${escHtml(when)} (${source}): <strong>${status}</strong>. Running GPUs: ${running || 0}.</div>`;
  if (!detail.jobs.length) { cell.innerHTML = head; return; }
  const jobs = [...detail.jobs].sort((a, b) => (b.age_ms ?? 0) - (a.age_ms ?? 0));
  const rows = jobs.map(job => {
    const verdict = waitingVerdicts[job.failed] || job.failed;
    return `<tr><td>${escHtml(job.job_id)}</td><td>${escHtml(job.partition || '')}</td><td>${escHtml(job.qos || '—')}</td>
      <td>${job.gpus}</td><td>${job.age_ms == null ? '—' : fmtWait(job.age_ms / 3_600_000)}</td>
      <td>${escHtml(job.reason || '—')}</td><td class="${job.failed == null ? 'counts' : 'skipped'}">${escHtml(verdict)}</td></tr>`;
  }).join('');
  cell.innerHTML = `${head}<table class="waiting-jobs"><thead><tr><th>Job</th><th>Partition</th><th>QOS</th><th>GPUs</th><th>Ready for</th><th>Slurm reason</th><th>Verdict</th></tr></thead><tbody>${rows}</tbody></table>`;
}

// ── Sidebar ──────────────────────────────────────────────────

function fmtSidebarH(h) {
  if (h === 0) return '—';
  return Math.round(h).toLocaleString();
}

function fmtSidebarPct(p) {
  if (p == null) return '—';
  if (p === 0) return '—';
  return p < 0.1 ? '<0.1%' : p.toFixed(1) + '%';
}

function renderSidebar(filter = '') {
  const { data, allUsers, visibleUsers, userColors } = state;
  const list = document.getElementById('user-list');
  if (!data) return;

  const totalAvailGPU = installedHours('gpus');
  const totalAvailCPU = installedHours('cpus');

  const lf = filter.toLowerCase();
  const filtered = filter ? allUsers.filter(u => u.toLowerCase().includes(lf)) : allUsers;

  const rowsHtml = filtered.map((u, i) => {
    const checked = visibleUsers.has(u);
    const color = userColors[u] || '#888';
    const gpuH = resourceHours(data.users[u]);
    const cpuH = resourceHours((data.cpu_users || {})[u]);
    const gpuP = allocationPercent(gpuH, totalAvailGPU);
    const cpuP = allocationPercent(cpuH, totalAvailCPU);
    return `
      <tr class="user-row${checked ? ' checked' : ''}" data-idx="${i}">
        <td class="col-cb"><input type="checkbox" ${checked ? 'checked' : ''} /></td>
        <td class="col-sw"><span class="user-swatch" style="background:${color}"></span></td>
        <td class="col-name" title="${escHtml(u)}">${escHtml(u)}</td>
        <td class="col-num">${fmtSidebarH(gpuH)}</td>
        <td class="col-pct">${fmtSidebarPct(gpuP)}</td>
        <td class="col-num">${fmtSidebarH(cpuH)}</td>
        <td class="col-pct">${fmtSidebarPct(cpuP)}</td>
      </tr>`;
  }).join('');

  list.innerHTML = `
    <table class="sidebar-table">
      <thead>
        <tr>
          <th class="col-cb"></th>
          <th class="col-sw"></th>
          <th class="col-name">Name</th>
          <th class="col-num">GPU-h</th>
          <th class="col-pct">%</th>
          <th class="col-num">CPU-h</th>
          <th class="col-pct">%</th>
        </tr>
      </thead>
      <tbody>${rowsHtml}</tbody>
    </table>`;

  list.querySelectorAll('.user-row').forEach(tr => {
    const u = filtered[parseInt(tr.dataset.idx)];
    const cb = tr.querySelector('input[type="checkbox"]');
    cb.addEventListener('change', () => {
      if (cb.checked) state.visibleUsers.add(u);
      else state.visibleUsers.delete(u);
      tr.classList.toggle('checked', cb.checked);
      updateChartsVisibility();
      renderMetrics();
      updateSidebarCount();
    });
    tr.addEventListener('click', e => { if (e.target.tagName !== 'INPUT') cb.click(); });
  });

  updateSidebarCount();
}

function updateSidebarCount() {
  const vis = state.visibleUsers.size;
  const total = state.allUsers.length;
  document.getElementById('user-count').textContent = `(${vis} / ${total})`;
}

function updateSidebarLabel() {
  const labels = { users: 'Users', projects: 'Projects', units: 'Units' };
  document.getElementById('sidebar-label').textContent = labels[state.viewMode];
  const search = document.getElementById('user-search');
  search.placeholder = `Filter ${labels[state.viewMode].toLowerCase()}…`;
}

// ── Preset UI ────────────────────────────────────────────────

function applyPreset(preset) {
  const { from, to } = getPresetRange(preset);
  document.getElementById('from-date').value = msToDateInput(from);
  document.getElementById('to-date').value = msToDateInput(to);
}

// ── Fetch & render ───────────────────────────────────────────

async function fetchAndRender() {
  const overlay = document.getElementById('loading-overlay');
  overlay.classList.add('visible');

  const fromVal = document.getElementById('from-date').value;
  const toVal = document.getElementById('to-date').value;
  const from_ms = dateInputToMsStart(fromVal);
  const to_ms = dateInputToMsEnd(toVal);

  if (isNaN(from_ms) || isNaN(to_ms) || from_ms >= to_ms) {
    alert('Invalid date range. Please set a valid from/to date.');
    overlay.classList.remove('visible');
    return;
  }

  const groupBy = state.viewMode === 'users' ? 'user'
                : state.viewMode === 'projects' ? 'project' : 'unit';

  // Separate so a failure here keeps the rest of the page.
  state.waitingRangeEnd = to_ms;
  const waitingPromise = fetch(apiUrl(`api/waiting?from=${from_ms}&to=${to_ms}&tz_offset_min=${new Date(from_ms).getTimezoneOffset()}`))
    .then(response => response.ok ? response.json() : Promise.reject(new Error(`HTTP ${response.status}`)))
    .catch(err => { console.error('Waiting view unavailable', err); return null; });

  try {
    const [usageRes, waitRes] = await Promise.all([
      fetch(apiUrl(`api/usage?from=${from_ms}&to=${to_ms}&group_by=${groupBy}`)),
      fetch(apiUrl(`api/pending-age?from=${from_ms}&to=${to_ms}&group_by=${groupBy}`)),
    ]);
    if (!usageRes.ok) throw new Error(`HTTP ${usageRes.status}`);
    if (!waitRes.ok) throw new Error(`HTTP ${waitRes.status}`);
    [state.data, state.waitData] = await Promise.all([usageRes.json(), waitRes.json()]);
  } catch (err) {
    alert(`Failed to load usage data: ${err.message}`);
    overlay.classList.remove('visible');
    return;
  }

  state.waitingData = await waitingPromise;
  const { data } = state;

  const userSet = new Set([
    ...Object.keys(data.users || {}),
    ...Object.keys(data.cpu_users || {}),
    ...Object.keys(state.waitData.users || {}),
  ]);
  if (groupBy === 'user' && state.waitingData) {
    // A person who only waited, or sat at their own cap, still needs a sidebar row.
    for (const user of Object.keys(state.waitingData.users || {})) userSet.add(user);
    for (const row of state.waitingData.people || []) userSet.add(row.user);
  }
  const allPreviouslySelected = state.allUsers.length > 0 && state.allUsers.every(key => state.visibleUsers.has(key));
  // Sort by GPU usage descending
  state.allUsers = [...userSet].sort((a, b) => {
    const sumA = resourceHours(data.users[a], data);
    const sumB = resourceHours(data.users[b], data);
    return sumB - sumA;
  });

  // Assign colors: use server-provided colors for project/unit mode, PALETTE for user mode
  if (groupBy !== 'user') {
    state.allUsers.forEach(key => {
      state.userColors[key] = data.colors?.[key] || state.waitData.colors?.[key] || '#888888';
    });
  } else {
    state.allUsers.forEach((u, i) => {
      if (!state.userColors[u]) {
        state.userColors[u] = PALETTE[i % PALETTE.length];
      }
    });
  }

  // Reset visible selection when switching view modes
  if (state._lastFetchedMode !== state.viewMode) {
    state.visibleUsers = new Set(state.allUsers);
    state._lastFetchedMode = state.viewMode;
  } else {
    if (allPreviouslySelected || state.visibleUsers.size === 0) {
      state.visibleUsers = new Set(state.allUsers);
    } else {
      for (const u of [...state.visibleUsers]) {
        if (!userSet.has(u)) state.visibleUsers.delete(u);
      }
    }
  }

  updateSidebarLabel();
  renderSidebar(document.getElementById('user-search').value);
  renderQueueChart();
  renderWaitingChart();
  renderWaitingTable();
  renderCharts();
  renderMetrics();
  renderWaitMetrics();

  overlay.classList.remove('visible');
}

// ── Project/Unit editor ───────────────────────────────────────

function openEditor() {
  document.getElementById('editor-modal').hidden = false;
  loadEditorData();
}

function closeEditor() {
  document.getElementById('editor-modal').hidden = true;
}

async function loadEditorData() {
  try {
    const [unitsRes, usersRes, unassignedRes] = await Promise.all([
      fetch(apiUrl('api/units')),
      fetch(apiUrl('api/users')),
      fetch(apiUrl('api/unassigned-jobs')),
    ]);
    state.unitsData = await unitsRes.json();
    const knownUsers = await usersRes.json();
    const unassigned = await unassignedRes.json();

    // Populate datalist for assignment user autocomplete
    const dl = document.getElementById('known-users-list');
    dl.innerHTML = knownUsers.map(u => `<option value="${escHtml(u)}"></option>`).join('');

    renderUnitsTab();
    renderProjectsTab();
    renderUnassignedTab(unassigned);
  } catch (e) {
    console.error('Failed to load editor data', e);
  }
}

function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// ── Units tab ─────────────────────────────────────────────────

function renderUnitsTab() {
  const { units } = state.unitsData;
  const list = document.getElementById('units-list');
  list.innerHTML = '';

  if (units.length === 0) {
    list.innerHTML = '<div class="editor-empty">No units yet.</div>';
  }

  units.forEach(unit => {
    const row = document.createElement('div');
    row.className = 'editor-row';
    row.dataset.id = unit.id;
    row.innerHTML = `
      <span class="editor-color-dot" style="background:${unit.color || '#888'}"></span>
      <span class="editor-row-title">${escHtml(unit.title)}</span>
      <input type="text" class="editor-inline-input" value="${escHtml(unit.title)}" style="display:none" />
      <input type="color" class="editor-inline-color" value="${unit.color || '#3b82f6'}" style="display:none" title="Pick color" />
      <div class="editor-row-actions">
        <button class="editor-btn edit-unit-btn">Edit</button>
        <button class="editor-btn save-unit-btn" style="display:none">Save</button>
        <button class="editor-btn cancel-unit-btn" style="display:none">Cancel</button>
        <button class="editor-btn danger-btn delete-unit-btn">Delete</button>
      </div>
    `;

    // Edit / save / cancel
    const dot = row.querySelector('.editor-color-dot');
    const titleSpan = row.querySelector('.editor-row-title');
    const titleInput = row.querySelector('.editor-inline-input');
    const colorInput = row.querySelector('.editor-inline-color');
    const editBtn = row.querySelector('.edit-unit-btn');
    const saveBtn = row.querySelector('.save-unit-btn');
    const cancelBtn = row.querySelector('.cancel-unit-btn');

    editBtn.addEventListener('click', () => {
      titleSpan.style.display = 'none';
      dot.style.display = 'none';
      titleInput.style.display = '';
      colorInput.style.display = '';
      editBtn.style.display = 'none';
      saveBtn.style.display = '';
      cancelBtn.style.display = '';
      titleInput.focus();
    });

    cancelBtn.addEventListener('click', () => {
      titleSpan.style.display = '';
      dot.style.display = '';
      titleInput.style.display = 'none';
      colorInput.style.display = 'none';
      editBtn.style.display = '';
      saveBtn.style.display = 'none';
      cancelBtn.style.display = 'none';
    });

    saveBtn.addEventListener('click', async () => {
      const newTitle = titleInput.value.trim();
      const newColor = colorInput.value;
      if (!newTitle) return;
      const response = await adminFetch(`api/units/${unit.id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title: newTitle, color: newColor }),
      });
      if (!response) return;
      await loadEditorData();
    });

    row.querySelector('.delete-unit-btn').addEventListener('click', async () => {
      if (!confirm(`Delete unit "${unit.title}"? Its projects will become unaffiliated.`)) return;
      const response = await adminFetch(`api/units/${unit.id}`, { method: 'DELETE' });
      if (!response) return;
      await loadEditorData();
    });

    list.appendChild(row);
  });

  // Set color picker to the next unused palette color
  const usedColors = new Set((state.unitsData.units || []).map(u => u.color).filter(Boolean));
  const nextColor = PALETTE.find(c => !usedColors.has(c)) || PALETTE[0];
  document.getElementById('new-unit-color').value = nextColor;

  // Add unit
  document.getElementById('add-unit-btn').onclick = async () => {
    const titleEl = document.getElementById('new-unit-title');
    const colorEl = document.getElementById('new-unit-color');
    const title = titleEl.value.trim();
    if (!title) { titleEl.focus(); return; }
    const response = await adminFetch('api/units', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title, color: colorEl.value }),
    });
    if (!response) return;
    titleEl.value = '';
    await loadEditorData();  // re-renders tab, which will advance the color picker again
  };
}

// ── Projects tab ──────────────────────────────────────────────

function renderProjectsTab() {
  const { units, unaffiliated } = state.unitsData;
  const list = document.getElementById('projects-list');
  list.innerHTML = '';

  // Update unit dropdown in "add project" form
  const unitSelect = document.getElementById('new-project-unit');
  unitSelect.innerHTML = '<option value="">— No unit —</option>'
    + units.map(u => `<option value="${u.id}">${escHtml(u.title)}</option>`).join('');

  // Build a flat list of all projects, grouped by unit then unaffiliated
  const allProjectGroups = [
    ...units.map(u => ({ label: u.title, color: u.color, projects: u.projects })),
    ...(unaffiliated.length > 0 ? [{ label: 'Unaffiliated', color: null, projects: unaffiliated }] : []),
  ];

  if (allProjectGroups.every(g => g.projects.length === 0)) {
    list.innerHTML = '<div class="editor-empty">No projects yet.</div>';
  }

  allProjectGroups.forEach(group => {
    if (group.projects.length === 0) return;

    const header = document.createElement('div');
    header.className = 'editor-group-header';
    header.innerHTML = group.color
      ? `<span class="editor-color-dot" style="background:${group.color}"></span>${escHtml(group.label)}`
      : `<span class="editor-group-label-plain">${escHtml(group.label)}</span>`;
    list.appendChild(header);

    group.projects.forEach(proj => {
      const row = document.createElement('div');
      row.className = 'editor-row project-row';
      row.dataset.id = proj.id;

      const aCount = proj.assignments.length;
      row.innerHTML = `
        <span class="editor-row-title">${escHtml(proj.title)}</span>
        <input type="text" class="editor-inline-input" value="${escHtml(proj.title)}" style="display:none" />
        <select class="editor-inline-select editor-unit-select" style="display:none">
          <option value="">— No unit —</option>
          ${units.map(u => `<option value="${u.id}"${u.id === proj.unit_id ? ' selected' : ''}>${escHtml(u.title)}</option>`).join('')}
        </select>
        <div class="editor-row-actions">
          <button class="editor-btn toggle-assign-btn">Assignments (${aCount})</button>
          <button class="editor-btn edit-proj-btn">Edit</button>
          <button class="editor-btn save-proj-btn" style="display:none">Save</button>
          <button class="editor-btn cancel-proj-btn" style="display:none">Cancel</button>
          <button class="editor-btn danger-btn delete-proj-btn">Delete</button>
        </div>
        <div class="assignments-panel" hidden></div>
      `;

      const titleSpan = row.querySelector('.editor-row-title');
      const titleInput = row.querySelector('.editor-inline-input');
      const unitSel = row.querySelector('.editor-unit-select');
      const editBtn = row.querySelector('.edit-proj-btn');
      const saveBtn = row.querySelector('.save-proj-btn');
      const cancelBtn = row.querySelector('.cancel-proj-btn');
      const assignPanel = row.querySelector('.assignments-panel');
      const toggleBtn = row.querySelector('.toggle-assign-btn');

      editBtn.addEventListener('click', () => {
        titleSpan.style.display = 'none';
        titleInput.style.display = '';
        unitSel.style.display = '';
        editBtn.style.display = 'none';
        saveBtn.style.display = '';
        cancelBtn.style.display = '';
        titleInput.focus();
      });

      cancelBtn.addEventListener('click', () => {
        titleSpan.style.display = '';
        titleInput.style.display = 'none';
        unitSel.style.display = 'none';
        editBtn.style.display = '';
        saveBtn.style.display = 'none';
        cancelBtn.style.display = 'none';
      });

      saveBtn.addEventListener('click', async () => {
        const newTitle = titleInput.value.trim();
        if (!newTitle) return;
        const newUnitId = unitSel.value ? parseInt(unitSel.value) : null;
        const response = await adminFetch(`api/projects/${proj.id}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ title: newTitle, unit_id: newUnitId }),
        });
        if (!response) return;
        await loadEditorData();
      });

      row.querySelector('.delete-proj-btn').addEventListener('click', async () => {
        if (!confirm(`Delete project "${proj.title}" and all its assignments?`)) return;
        const response = await adminFetch(`api/projects/${proj.id}`, { method: 'DELETE' });
        if (!response) return;
        await loadEditorData();
      });

      toggleBtn.addEventListener('click', () => {
        assignPanel.hidden = !assignPanel.hidden;
        if (!assignPanel.hidden) renderAssignments(assignPanel, proj);
      });

      list.appendChild(row);
    });
  });

  // Add project
  document.getElementById('add-project-btn').onclick = async () => {
    const titleEl = document.getElementById('new-project-title');
    const unitEl = document.getElementById('new-project-unit');
    const title = titleEl.value.trim();
    if (!title) { titleEl.focus(); return; }
    const unit_id = unitEl.value ? parseInt(unitEl.value) : null;
    const response = await adminFetch('api/projects', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title, unit_id }),
    });
    if (!response) return;
    titleEl.value = '';
    await loadEditorData();
  };
}

function renderAssignments(panel, proj) {
  panel.innerHTML = '';

  proj.assignments.forEach(a => {
    const endLabel = a.end_time_ms == null ? '∞' : msToDateStr(a.end_time_ms);
    const row = document.createElement('div');
    row.className = 'assignment-row';
    row.innerHTML = `
      <span class="assign-user">${escHtml(a.user)}</span>
      <span class="assign-dates">${msToDateStr(a.start_time_ms)} → ${endLabel}</span>
      <div class="assignment-edit-form" hidden>
        <input type="text" class="editor-input assign-edit-user" value="${escHtml(a.user)}" list="known-users-list" />
        <input type="date" class="editor-input assign-edit-start" value="${msToDateStr(a.start_time_ms)}" />
        <label class="open-ended-label">
          <input type="checkbox" class="assign-open-ended" ${a.end_time_ms == null ? 'checked' : ''} />
          Open-ended
        </label>
        <input type="date" class="editor-input assign-edit-end" value="${msToDateStr(a.end_time_ms)}" ${a.end_time_ms == null ? 'disabled' : ''} />
      </div>
      <div class="assign-actions">
        <button class="editor-btn edit-assign-btn">Edit</button>
        <button class="editor-btn save-assign-btn" style="display:none">Save</button>
        <button class="editor-btn cancel-assign-btn" style="display:none">Cancel</button>
        <button class="editor-btn danger-btn delete-assign-btn">&#10005;</button>
      </div>
    `;

    const display = row.querySelector('.assign-user');
    const dates = row.querySelector('.assign-dates');
    const editForm = row.querySelector('.assignment-edit-form');
    const editBtn = row.querySelector('.edit-assign-btn');
    const saveBtn = row.querySelector('.save-assign-btn');
    const cancelBtn = row.querySelector('.cancel-assign-btn');
    const openEnded = row.querySelector('.assign-open-ended');
    const endInput = row.querySelector('.assign-edit-end');

    openEnded.addEventListener('change', () => {
      endInput.disabled = openEnded.checked;
      if (openEnded.checked) endInput.value = '';
    });

    editBtn.addEventListener('click', () => {
      display.style.display = 'none';
      dates.style.display = 'none';
      editForm.hidden = false;
      editBtn.style.display = 'none';
      saveBtn.style.display = '';
      cancelBtn.style.display = '';
    });

    cancelBtn.addEventListener('click', () => {
      display.style.display = '';
      dates.style.display = '';
      editForm.hidden = true;
      editBtn.style.display = '';
      saveBtn.style.display = 'none';
      cancelBtn.style.display = 'none';
    });

    saveBtn.addEventListener('click', async () => {
      const user = row.querySelector('.assign-edit-user').value.trim();
      const startStr = row.querySelector('.assign-edit-start').value;
      const endStr = openEnded.checked ? null : endInput.value;
      const start_time_ms = dateStrToMs(startStr);
      const end_time_ms = endStr ? dateStrToMs(endStr) : null;
      if (!user || !start_time_ms) return;
      const response = await adminFetch(`api/projects/${proj.id}/assignments/${a.id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user, start_time_ms, end_time_ms }),
      });
      if (!response) return;
      await loadEditorData();
    });

    row.querySelector('.delete-assign-btn').addEventListener('click', async () => {
      const response = await adminFetch(`api/projects/${proj.id}/assignments/${a.id}`, { method: 'DELETE' });
      if (!response) return;
      await loadEditorData();
    });

    panel.appendChild(row);
  });

  // Add assignment form
  const addRow = document.createElement('div');
  addRow.className = 'assignment-add-row';
  addRow.innerHTML = `
    <input type="text" class="editor-input" id="new-assign-user-${proj.id}" placeholder="Username…" list="known-users-list" />
    <input type="date" class="editor-input" id="new-assign-start-${proj.id}" value="2026-01-01" />
    <label class="open-ended-label">
      <input type="checkbox" id="new-assign-open-${proj.id}" checked /> Open-ended
    </label>
    <input type="date" class="editor-input" id="new-assign-end-${proj.id}" disabled />
    <button class="editor-add-btn" id="new-assign-btn-${proj.id}">+ Add</button>
  `;

  addRow.querySelector(`#new-assign-open-${proj.id}`).addEventListener('change', e => {
    addRow.querySelector(`#new-assign-end-${proj.id}`).disabled = e.target.checked;
  });

  addRow.querySelector(`#new-assign-btn-${proj.id}`).addEventListener('click', async () => {
    const user = addRow.querySelector(`#new-assign-user-${proj.id}`).value.trim();
    const startStr = addRow.querySelector(`#new-assign-start-${proj.id}`).value;
    const openEnded = addRow.querySelector(`#new-assign-open-${proj.id}`).checked;
    const endStr = openEnded ? null : addRow.querySelector(`#new-assign-end-${proj.id}`).value;
    const start_time_ms = dateStrToMs(startStr);
    const end_time_ms = endStr ? dateStrToMs(endStr) : null;
    if (!user || !start_time_ms) {
      addRow.querySelector(`#new-assign-user-${proj.id}`).focus();
      return;
    }
    const response = await adminFetch(`api/projects/${proj.id}/assignments`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ user, start_time_ms, end_time_ms }),
    });
    if (!response) return;
    await loadEditorData();
  });

  panel.appendChild(addRow);
}

// ── Unassigned tab ────────────────────────────────────────────

function renderUnassignedTab(rows) {
  const list = document.getElementById('unassigned-list');
  const badge = document.getElementById('unassigned-badge');

  badge.textContent = rows.length > 0 ? rows.length : '';

  if (rows.length === 0) {
    list.innerHTML = '<div class="editor-empty">All jobs are assigned to a project.</div>';
    return;
  }

  const { units, unaffiliated } = state.unitsData;
  const allProjects = [...units.flatMap(u => u.projects), ...unaffiliated];

  const projOptions = allProjects.length
    ? allProjects.map(p => `<option value="${p.id}">${escHtml(p.title)}</option>`).join('')
    : '<option value="" disabled>— no projects —</option>';

  const rowsHtml = rows.map((r, i) => `
    <tr data-idx="${i}">
      <td class="user-col">${escHtml(r.user || '')}</td>
      <td class="num-col">${r.job_count.toLocaleString()}</td>
      <td class="num-col">${r.gpu_hours.toLocaleString()}</td>
      <td class="num-col">${msToDateStr(r.earliest_ms)}</td>
      <td class="assign-quick-cell">
        <select class="editor-select quick-proj-select">
          <option value="">— pick project —</option>
          ${projOptions}
        </select>
        <button class="editor-add-btn quick-assign-btn" disabled>Assign</button>
      </td>
    </tr>
  `).join('');

  // Set innerHTML on the container div (not on a table element) to avoid
  // browser quirks with innerHTML on table elements.
  list.innerHTML = `
    <table class="unassigned-table">
      <thead>
        <tr>
          <th>User</th>
          <th class="num-col">Jobs</th>
          <th class="num-col">GPU-hours</th>
          <th>Earliest job</th>
          <th>Quick assign</th>
        </tr>
      </thead>
      <tbody>${rowsHtml}</tbody>
    </table>
  `;

  list.querySelectorAll('tr[data-idx]').forEach(tr => {
    const r = rows[parseInt(tr.dataset.idx)];
    const sel = tr.querySelector('.quick-proj-select');
    const btn = tr.querySelector('.quick-assign-btn');

    sel.addEventListener('change', () => { btn.disabled = !sel.value; });

    btn.addEventListener('click', async () => {
      const response = await adminFetch(`api/projects/${parseInt(sel.value)}/assignments`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user: r.user, start_time_ms: r.earliest_ms, end_time_ms: null }),
      });
      if (!response) return;
      await loadEditorData();
    });
  });
}

// ── Bootstrap ────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
  applyPreset('last7');

  document.querySelectorAll('.preset-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.preset === 'last7');
  });

  document.getElementById('apply-btn').addEventListener('click', fetchAndRender);
  document.getElementById('waiting-include-estimates').addEventListener('change', event => {
    state.waitingIncludeEstimates = event.target.checked;
    renderWaitingTable();
  });
  document.getElementById('queue-category').addEventListener('change', () => {
    renderQueueChart();
    renderWaitingChart();
    renderWaitingTable();
    renderWaitMetrics();
  });
  document.querySelectorAll('input[name=queue-measure]').forEach(input => {
    input.addEventListener('change', renderQueueChart);
  });

  document.querySelectorAll('.preset-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.preset-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      applyPreset(btn.dataset.preset);
      fetchAndRender();
    });
  });

  // View toggle
  document.querySelectorAll('.view-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.view-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      state.viewMode = btn.dataset.view;
      fetchAndRender();
    });
  });

  document.getElementById('user-search').addEventListener('input', e => {
    renderSidebar(e.target.value);
  });

  document.getElementById('select-matching').addEventListener('click', () => {
    const filter = document.getElementById('user-search').value.toLowerCase();
    const matching = filter
      ? state.allUsers.filter(u => u.toLowerCase().includes(filter))
      : state.allUsers;
    matching.forEach(u => state.visibleUsers.add(u));
    renderSidebar(document.getElementById('user-search').value);
    updateChartsVisibility();
    renderMetrics();
  });

  document.getElementById('deselect-matching').addEventListener('click', () => {
    const filter = document.getElementById('user-search').value.toLowerCase();
    const matching = filter
      ? state.allUsers.filter(u => u.toLowerCase().includes(filter))
      : state.allUsers;
    matching.forEach(u => state.visibleUsers.delete(u));
    renderSidebar(document.getElementById('user-search').value);
    updateChartsVisibility();
    renderMetrics();
  });

  // Editor modal
  document.getElementById('open-editor').addEventListener('click', openEditor);
  document.getElementById('close-editor').addEventListener('click', closeEditor);
  document.getElementById('editor-modal').addEventListener('click', e => {
    if (e.target === e.currentTarget) closeEditor();
  });

  // Editor tabs
  document.querySelectorAll('.modal-tab').forEach(tab => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('.modal-tab').forEach(t => t.classList.remove('active'));
      tab.classList.add('active');
      const target = tab.dataset.tab;
      document.getElementById('tab-units').hidden = target !== 'units';
      document.getElementById('tab-projects').hidden = target !== 'projects';
      document.getElementById('tab-unassigned').hidden = target !== 'unassigned';
    });
  });

  fetchAndRender();
});
