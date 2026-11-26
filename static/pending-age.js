/* Exact pooled, job-weighted quantiles. Never average subgroup percentiles. */
(function (root) {
  const classes = ['cpu_only', 'gpu_1_7', 'gte8'];
  function summarize(data, selected, prefix, index) {
    if (data.sources[index] === 'unknown') return { median: null, p90: null, count: null };
    const values = [];
    for (const key of selected) {
      for (const category of prefix ? [prefix] : classes) {
        for (const pair of data.age_samples[key]?.[category]?.[index] || []) values.push(pair);
      }
    }
    values.sort((a, b) => a[0] - b[0]);
    const count = values.reduce((sum, pair) => sum + pair[1], 0);
    if (!count) return { median: null, p90: null, count: 0 };
    function rank(position) {
      let seen = 0;
      for (const [age, weight] of values) {
        seen += weight;
        if (position < seen) return age;
      }
      return null;
    }
    return { count, median: (rank(Math.floor((count - 1) / 2)) + rank(Math.floor(count / 2))) / 2,
      p90: rank(Math.ceil(count * 0.9) - 1) };
  }
  function series(data, selected, prefix, metric) {
    return data.buckets.map((time, i) => {
      const stats = summarize(data, selected, prefix, i);
      return { x: time, y: stats[metric] === null ? null : stats[metric] / 3600000,
        pendingCount: stats.count, source: data.sources[i] };
    });
  }
const queueBands = [
  { label: '<2h', hours: 2, color: '#38bdf8' },
  { label: '2–24h', hours: 24, color: '#34d399' },
  { label: '1–3 days', hours: 72, color: '#fbbf24' },
  { label: '3–7 days', hours: 168, color: '#fb923c' },
  { label: '7+ days', hours: Infinity, color: '#ff303f', glow: '#ff303f88' },
];

function ageBands(data, selected, prefix, measure) {
  const groups = measure === 'gpus' ? data.gpu_age_samples
    : measure === 'cpus' ? data.cpu_age_samples : data.age_samples;
  const categories = prefix ? [prefix] : ['cpu_only', 'gpu_1_7', 'gte8'];
  const barCount = Math.min(data.buckets.length, 60);
  const intervals = Array.from({ length: barCount }, (_, index) => {
    const first = Math.floor(index * data.buckets.length / barCount);
    const end = Math.floor((index + 1) * data.buckets.length / barCount);
    const startTime = first > 0 ? data.buckets[first - 1] + 1
      : data.buckets[first] + 1 - data.bucket_size_ms;
    const endTime = data.buckets[end - 1] + 1;
    const sums = queueBands.map(() => 0);
    let known = 0;
    for (let i = first; i < end; i++) {
      if (data.sources[i] === 'unknown') continue;
      known++;
      for (const key of selected) {
        for (const category of categories) {
          for (const [age, weight] of groups?.[key]?.[category]?.[i] || []) {
            const band = queueBands.findIndex(item => age < item.hours * 3600000);
            sums[band] += weight;
          }
        }
      }
    }
    return { x: Math.floor((startTime + endTime) / 2), startTime, endTime, known,
      expected: end - first, means: sums.map(sum => known ? sum / known : null) };
  });
  return queueBands.map(({ label, color, glow }, band) => ({
    label, backgroundColor: color, glow,
    data: intervals.map(({ means, ...interval }) => ({ ...interval, y: means[band] })),
  })).reverse();
}


  const api = { summarize, series, ageBands };
  root.PendingAge = api;
  if (typeof module !== 'undefined') module.exports = api;
})(globalThis);
