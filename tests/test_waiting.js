'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { series, tableRows, estimateShare } = require('../static/waiting.js');

const hour = 3_600_000;
const data = {
  thresholds_h: [2, 24], step_ms: 300_000, bucket_ms: hour,
  buckets: [0, hour, 2 * hour],
  samples: { expected: [12, 12, 12], observed: [12, 0, 0], accounting: [0, 0, 6] },
  users: {
    alice: { w2: [12, 0, 6], w2_idle: [12, 0, 0], w24: [6, 0, 0], w24_idle: [6, 0, 0] },
    bob: { w2: [6, 0, 0], w2_idle: [0, 0, 0], w24: [0, 0, 0], w24_idle: [0, 0, 0] },
    carol: { w2: [0, 0, 3], w2_idle: [0, 0, 3], w24: [0, 0, 0], w24_idle: [0, 0, 0] },
  },
  people: [
    { user: 'alice', waiting_h: 1.5, capped_h: 0, longest_wait_h: 30, estimate_h: 0.5,
      observed: { estimate_h: 0, waiting_h: 1, capped_h: 0, longest_wait_h: 30 } },
    { user: 'bob', waiting_h: 0.5, capped_h: 2, longest_wait_h: null, estimate_h: 0,
      observed: { estimate_h: 0, waiting_h: 0.5, capped_h: 2, longest_wait_h: null } },
    { user: 'carol', waiting_h: 0.5, capped_h: 0, longest_wait_h: 3, estimate_h: 0.5,
      observed: { estimate_h: 0, waiting_h: 0, capped_h: 0, longest_wait_h: null } },
  ],
};

test('series sums selected users only and divides by known samples', () => {
  const [first] = series(data, new Set(['alice']));
  assert.equal(first.idle2, 1);
  assert.equal(first.busy2, 0);
  assert.equal(first.total24, 0.5);
  assert.equal(first.known, 12);
  assert.equal(first.expected, 12);
  assert.equal(first.startTime, 0);
  assert.equal(first.endTime, hour);
  const all = series(data, null)[0];
  assert.equal(all.idle2 + all.busy2, 1.5);
});

test('a bucket without known samples is null, not zero', () => {
  const gap = series(data, null)[1];
  assert.equal(gap.known, 0);
  for (const key of ['idle2', 'busy2', 'total24', 'any2']) assert.equal(gap[key], null);
});

test('any2 counts users with a non-zero cell; idle2 + busy2 is the 2h average', () => {
  const rows = series(data, null);
  assert.equal(rows[0].any2, 2);
  assert.equal(rows[2].any2, 2);
  assert.equal(rows[0].busy2, 0.5);
  assert.equal(rows[2].idle2 + rows[2].busy2, (6 + 3) / 6);
});

test('estimate is true when a bucket has accounting samples', () => {
  assert.deepEqual(series(data, null).map(row => row.estimate), [false, false, true]);
});

test('tableRows shows observed numbers by default and drops estimate-only people', () => {
  const rows = tableRows(data, null);
  assert.deepEqual(rows.map(r => r.user), ['alice', 'bob']);
  assert.equal(rows[0].waiting_h, 1);
  assert.equal(rows[0].estimate_h, 0);
  assert.equal(data.people[0].waiting_h, 1.5, 'input rows are not modified');
});

test('estimateShare is the accounting fraction of known samples', () => {
  assert.equal(estimateShare(data), 6 / 18);
  assert.equal(estimateShare({ samples: { observed: [0], accounting: [0] } }), 0);
});

test('tableRows with estimates filters by selection and sorts both ways, ties by user', () => {
  const all = (d, sel, key, desc) => tableRows(d, sel, key, desc, true);
  assert.deepEqual(all(data, null).map(r => r.user), ['alice', 'bob', 'carol']);
  assert.deepEqual(all(data, null, 'waiting_h', false).map(r => r.user), ['bob', 'carol', 'alice']);
  assert.deepEqual(all(data, new Set(['bob', 'carol'])).map(r => r.user), ['bob', 'carol']);
  assert.deepEqual(all(data, null, 'capped_h').map(r => r.user), ['bob', 'alice', 'carol']);
  assert.deepEqual(all(data, null, 'longest_wait_h').map(r => r.user), ['alice', 'carol', 'bob']);
  assert.deepEqual(all(data, null, 'longest_wait_h', false).map(r => r.user), ['carol', 'alice', 'bob']);
  assert.deepEqual(all(data, null, 'user', false).map(r => r.user), ['alice', 'bob', 'carol']);
});
