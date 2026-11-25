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

  function tableRows(data, selected, sortKey = 'waiting_h', descending = true) {
    const rows = (data.people || []).filter(row => !selected || selected.has(row.user));
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

  const api = { series, tableRows };
  root.Waiting = api;
  if (typeof module !== 'undefined') module.exports = api;
})(globalThis);
