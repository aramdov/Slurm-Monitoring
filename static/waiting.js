/* Per-person waiting view. Pure helpers over /api/waiting; averages divide by known samples. */
(function (root) {
  function keys(data) {
    const thresholds = data.thresholds_h || [2, 24];
    return { first: `w${thresholds[0]}`, last: `w${thresholds[thresholds.length - 1]}` };
  }

  function selectedUsers(data, selected) {
    const all = Object.keys(data.users || {});
    return selected ? all.filter(user => selected.has(user)) : all;
  }

  // -> [{x, startTime, endTime, known, expected, estimate, idle2, busy2, total24, any2}]
  function series(data, selected) {
    const { first, last } = keys(data);
    const users = selectedUsers(data, selected);
    const { expected, observed, accounting } = data.samples;
    return data.buckets.map((startTime, i) => {
      const endTime = startTime + data.bucket_ms;
      const known = observed[i] + accounting[i];
      const row = { x: Math.floor((startTime + endTime) / 2), startTime, endTime, known,
        expected: expected[i], estimate: accounting[i] > 0,
        idle2: null, busy2: null, total24: null, any2: null };
      if (!known) return row;
      let idle = 0, busy = 0, long = 0, any = 0;
      for (const user of users) {
        const cells = data.users[user];
        const waiting = cells[first][i];
        idle += cells[`${first}_idle`][i];
        busy += waiting - cells[`${first}_idle`][i];
        long += cells[last][i];
        if (waiting > 0) any++;
      }
      return { ...row, idle2: idle / known, busy2: busy / known, total24: long / known, any2: any };
    });
  }

  // Default: numbers from observed samples only (pending reasons known). With estimates: every sample,
  // including accounting estimates where held and dependent jobs look like waiting.
  function tableRows(data, selected, sortKey = 'waiting_h', descending = true, includeEstimates = false) {
    let rows = (data.people || []).filter(row => !selected || selected.has(row.user));
    if (!includeEstimates) {
      rows = rows.map(row => ({ ...row, ...(row.observed || {}) }))
        .filter(row => row.waiting_h > 0 || row.capped_h > 0);
    }
    const direction = descending ? -1 : 1;
    return rows.sort((a, b) => {
      const x = a[sortKey], y = b[sortKey];
      if (x !== y) {
        if (x == null) return 1;   // missing values always last
        if (y == null) return -1;
        if (typeof x === 'string') return direction * x.localeCompare(y);
        return direction * (x - y);
      }
      return a.user.localeCompare(b.user);
    });
  }

  // Fraction of known samples in the range that came from the accounting estimate.
  function estimateShare(data) {
    const sum = values => (values || []).reduce((a, b) => a + b, 0);
    const accounting = sum(data.samples.accounting);
    const known = accounting + sum(data.samples.observed);
    return known ? accounting / known : 0;
  }

  const api = { series, tableRows, estimateShare };
  root.Waiting = api;
  if (typeof module !== 'undefined') module.exports = api;
})(globalThis);
