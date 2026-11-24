const assert = require('node:assert/strict');
const { ageBands, summarize } = require('../static/pending-age.js');
const hour = 3_600_000;
const data = {
  buckets: [hour, 2 * hour, 3 * hour], bucket_size_ms: hour,
  sources: ['observed', 'unknown', 'accounting'],
  age_samples: { user: { gte8: [[[168 * hour, 2]], [], []], cpu_only: [[[2 * hour, 4]], [], []] } },
  gpu_age_samples: { user: { gte8: [[[168 * hour, 16]], [], []] } },
  cpu_age_samples: { user: { cpu_only: [[[2 * hour, 32]], [], []] } },
};
const values = (category, measure, sample) => ageBands(data, ['user'], category, measure).map(b => b.data[sample].y);
assert.deepEqual(values('gte8', 'gpus', 0), [0, 0, 0, 0, 16]);
assert.deepEqual(values('cpu_only', 'cpus', 0), [0, 32, 0, 0, 0]);
assert.deepEqual(values('cpu_only', 'jobs', 0), [0, 4, 0, 0, 0]);
assert.deepEqual(values('', 'jobs', 1), [null, null, null, null, null]);
assert.deepEqual(values('', 'jobs', 2), [0, 0, 0, 0, 0]);
assert.equal(summarize(data, ['user'], 'gte8', 0).count, 2);
const long = { buckets: Array.from({ length: 360 }, (_, i) => (i + 1) * hour), bucket_size_ms: hour,
  sources: Array(360).fill('observed'), gpu_age_samples: { user: { gte8: Array.from({ length: 360 }, () => []) } } };
long.sources[1] = 'unknown';
long.gpu_age_samples.user.gte8[0] = [[0, 10]];
const bars = ageBands(long, ['user'], 'gte8', 'gpus');
assert.equal(bars[0].data.length, 60);
assert.equal(bars[0].data[0].y, 2);
assert.equal(bars[0].data[0].known, 5);
console.log('Queue demand, age bands, averaging, and missing data checks passed');
