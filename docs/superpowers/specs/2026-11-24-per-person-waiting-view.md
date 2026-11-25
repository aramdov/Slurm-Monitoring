# Per-person waiting view (Usage page)

Date: 2026-11-24. Target: worktree `/home/adovlatyan/Slurm-Monitoring-merge`, branch
`merge-upstream-2026-11-24`. Audience for part 2: the implementing agent (Opus 5.5).

## Why

Aram is setting fairness policy on BCM11 (max job time, per-user GPU cap, premium tier).
He needs one number that answers: "On this day or hour, how many people were stuck waiting
for GPUs? Who? For how long?"

The existing Queue chart counts jobs or GPUs. One person with 70 queued 1-GPU jobs fills it.
This view counts people.

---

## 1. Definition

### 1.1 In plain words

> A person is **waiting** when they have a GPU job that has been ready to run for at least
> 2 hours, the cluster is what holds it back, and they still have room under their own GPU cap.

We look at the queue on a fixed time grid (every 5 minutes for a week-long range).
At each look we list the people who are waiting. Each person counts once, however many
jobs they have queued.

Two lines are drawn:

- **Waiting 2h+**: people with such a job ready for 2 hours or more.
- **Waiting 24h+**: the same, with 24 hours or more.

### 1.2 Exact rules

At a sample instant `t`, a pending job **qualifies at threshold T** when all of these hold:

| # | Rule | Field used |
|---|------|-----------|
| J1 | State is `PENDING` and `scontrol` details were captured for it. | `state`, `details_observed` |
| J2 | It asks for at least one GPU. | `req_gpus > 0` |
| J3 | It can run in at least one partition that is not excluded. Default exclusion: `rtx`. | `partition` |
| J4 | Slurm gave it an Eligible time, and `Eligible >= Submit`. | `eligible_time_ms` |
| J5 | It is not blocked by a dependency or a hold. Reuse `pending_age.blocked()`. | `reason`, `dependency` |
| J6 | Its pending reason is on the **cluster-side list** below. | `reason` |
| J7 | Its age `t - Eligible` is at least `T`. | `eligible_time_ms` |

A person **fits under their cap** for job `j` when
`running GPUs of that person under j's QOS + GPUs per task of j <= cap of j's QOS`.
A QOS with no per-user GPU cap always fits.

At instant `t`, for threshold `T`:

- **Waiting**: the person has at least one job that qualifies at `T` and fits under the cap.
- **At own cap**: the person is not waiting at 2 h, and has a GPU job ready for 2 h or more
  that either shows a cluster-side reason but does not fit under the cap, or shows
  `QOSMaxGRESPerUser` (Slurm itself says the cap blocks it). Not counted as waiting.
  Reported in the table only.
- Otherwise the person is not in the count.

Thresholds: `T = 2 h` and `T = 24 h`. Anyone waiting at 24 h is also waiting at 2 h.

There is no "spell" and nothing resets. A job's age is `t - Eligible`, which is Slurm's own
clock. If another job of the same person starts, the person keeps counting while any
qualifying job remains. The "running meanwhile" columns show that case.

### 1.3 Pending reasons

The recorder stores `Reason=` from `scontrol show job -o`. That is one string per job. Spaces
become underscores (the test DB shows `job_requeued_in_held_state`).

Normalise before matching: replace spaces with `_`, then keep the text before the first comma.
So `Nodes_required_for_job_are_DOWN,_DRAINED_or_reserved_...` becomes
`Nodes_required_for_job_are_DOWN`, and `ReqNodeNotAvail,_UnavailableNodes:gpu05` becomes
`ReqNodeNotAvail`.

**Cluster-side (counts). Exact match after normalising:**

| Reason | Meaning |
|--------|---------|
| `Priority` | Other jobs are ahead in the queue. |
| `Resources` | The resources the job asks for are not free right now. |
| `Nodes_required_for_job_are_DOWN` | Nodes are down, drained, or kept for a higher tier. |
| `ReqNodeNotAvail` | A needed node is unavailable or reserved for another job. |
| `NodeDown` | A needed node is down. |
| `PartitionDown` | Admin set the partition down. |
| `PartitionInactive` | Admin set the partition inactive. |

**Not cluster-side (does not count):**

- The person's own limit or a policy limit: anything starting with `QOS`, `Assoc` or `Max`.
  Examples: `QOSMaxGRESPerUser`, `QOSMaxJobsPerUserLimit`, `QOSGrpGRES`, `AssocGrpGRES`,
  `AssocMaxJobsLimit`, `MaxGRESPerAccount`.
- The person's own choice: `JobArrayTaskLimit`, `BeginTime`, `Reservation`, `DeadLine`,
  `BadConstraints`, anything starting with `Dependency` (`Dependency`,
  `DependencyNeverSatisfied`), anything starting with `JobHeld` (`JobHeldUser`,
  `JobHeldAdmin`), `job_requeued_in_held_state`, `launch_failed_requeued_held`,
  `JobHoldMaxRequeue`.
- A request the partition can never run: `PartitionTimeLimit`,
  `PartitionConfig`, `InvalidAccount`, `InvalidQOS`, `AccountNotAllowed`, `QOSNotAllowed`,
  and the free-text `uid_<n>_not_in_group_permitted_to_use_this_partition_...`
  (match on `not_in_group_permitted`).

**Anything else** (`None`, `Prolog`, `Cleaning`, `Licenses`, `SystemFailure`,
`PartitionNodeLimit`, new strings) is `unclassified`. `PartitionNodeLimit` is here because
Slurm uses it both for a request that is too large and for nodes that are down. It does not count. The API returns each unclassified string with a sample
count, so a new reason is visible instead of silently dropped.

The displayed reason is Slurm's label, not proof of the only cause. "Cluster-side" means
"the reason Slurm shows is one the cluster controls".

Why a short allow-list: the question is "who is stuck because the cluster is full or
broken". A wrong inclusion inflates the count with people who blocked themselves. A wrong
exclusion shows up in `unclassified_reasons` and is easy to fix.

Source of the strings: `strings` on
`/cm/images/default-image/cm/local/apps/slurm/25.05/lib64/slurm/libslurmfull.so` and
`man squeue` (JOB REASON CODES) on 25.05.5. The live `jobs` table (17,570 rows) has seen
`Resources`, `Priority`, `JobArrayTaskLimit`, `Dependency`, the `Nodes required...` text,
`QOSMaxGRESPerUser`, `DependencyNeverSatisfied`, `BeginTime`, `JobHeldUser`,
`ReqNodeNotAvail, May be reserved for other job`, `PartitionTimeLimit`, `PartitionConfig`,
`job requeued in held state`, and the `uid ... not in group permitted` text.

### 1.4 Why the cap rule exists as well as the reason list

Slurm shows one reason per job even when several apply (`man squeue`: "only one of those
reasons is displayed"). `Priority` often hides the cap. Live example, 2026-11-24:
`heganyan` runs 27 GPUs under QOS `researcher` (cap 35) and has 41 one-GPU jobs pending.
All 41 show `Priority`. None show `QOSMaxGRESPerUser`. Eight of them could start under the
cap, so this person is waiting. With 35 running, none could start, and the reason would
still say `Priority`.

So the reason list alone cannot tell "the cluster is full" from "I am at my cap". The cap
rule does.

Caps come from a config map, checked against `sacctmgr show qos` on 2026-11-24:
`researcher=35, pi=16, student=1, admin=64, golf=16, normal=none`. In the last 30 days,
56 of 61 GPU users ran under `researcher`.

- Running GPUs are summed over **all partitions**, per QOS, because `MaxTRESPerUser` is a
  per-QOS, cross-partition limit.
- GPUs per task = `ceil(req_gpus / array_task_count)`. A 48-task array of 1-GPU tasks needs
  room for 1 GPU, not 48.
- QOS unknown (old rows, accounting estimate): use the default cap, 35.
- QOS known but not in the map: no cap. The API lists it under `rules.unmapped_qos`.

### 1.5 Thresholds

2 h and 24 h are communication choices. They match the first two band edges of the existing
Queue chart (`static/pending-age.js`, `queueBands`) and of `fair-use/analysis/wait_bands.py`.
They are not service targets. Nothing measured so far proves that 2 h is the "right" wait.
Under 2 h is treated as normal scheduling churn.

### 1.6 Aggregation over time

**Sample grid.** Instants are multiples of `step` since the epoch, inside `[from, to)`.
`step` is the smallest of 1, 5, 15, 60 minutes that gives at most 3,000 samples. If none
does, use 60 minutes. A 7-day range uses 5 minutes. A 30-day range uses 15 minutes.

**Is a job in the queue at `t`?** A segment is a run of polls where nothing about the job
changed. It covers `t` when `lo <= t <= last_ms`, with `lo = previous_last_ms + 1` if
`previous_last_ms` is set, else `first_ms`.

This matters. When a field changes, the recorder starts a new segment. A job whose reason
flips between `Priority` and `Resources` on every poll has segments with
`first_ms == last_ms`. Those cover one instant each, and a grid sample almost never lands on
it. The rule above gives the time between two polls to the newer segment. The real change
happened somewhere in that poll interval (about 30 s), so each transition is placed up to one
poll early.

**Each sample has a source**, same idea as the Queue chart:

| Source | When | Rules applied |
|--------|------|---------------|
| `observed` | `t` is inside a recorded window (`queue_observation_windows` or a segment). | All rules. |
| `accounting` | Not observed, but inside the sacct backfill (`queue_accounting_meta`). | No reason or hold filter (sacct has no reason). Cap rule with the default cap. A rough estimate, drawn differently. |
| `unknown` | Neither. | Nothing. The sample is missing, not zero. |

**Display buckets.** 1 hour when the range is 4 days or less. Otherwise 1 day. Buckets are
aligned to the viewer's clock (the browser sends its UTC offset), so a daily bar is a
calendar day in Yerevan time.

Per bucket and threshold:

- **Average people waiting** = (sum over known samples of people waiting) / (known samples).
  Split in two: people with 0 GPUs running, and people already running some.
- **People waiting at any point** = distinct people waiting in at least one known sample.
- If the bucket has no known samples, every value is `null`. The chart shows a gap.
- The bucket carries `known / expected` sample counts and how many came from accounting.

Averages divide by known samples, never by the bucket length. A day with 6 hours of
recording is averaged over those 6 hours and flagged as partial.

### 1.7 Per-person table (whole selected range)

One row per person with at least one waiting or at-own-cap sample.

| Column | Definition |
|--------|-----------|
| Person | Slurm user. |
| Waiting (h) | Waiting samples at 2 h x `step`. An estimate from samples, not a stopwatch. |
| of which 24h+ (h) | Waiting samples at 24 h x `step`. |
| Longest wait | Largest job age seen on any waiting sample. Can exceed the range, since age counts from Eligible. |
| Waiting now | Age of the person's oldest waiting job at the last recorded poll. Empty if the range does not reach the present. |
| GPUs asked | Average over waiting samples of the total GPUs in the person's qualifying jobs. A request size, not what the cluster could hand out. |
| GPUs running meanwhile | Average over waiting samples of the person's running GPUs (all partitions, all QOS). Also the max. |
| With 0 running (h) | Waiting samples where the person ran 0 GPUs x `step`. |
| At own cap (h) | At-own-cap samples x `step`. |

Default sort: Waiting (h), descending.

### 1.8 What the recorder stores today (checked 2026-11-24)

- `queue_observation_segments`: `job_id`, `submit_time_ms`, `first_ms`, `last_ms`,
  `previous_last_ms`, `state`, `reason`, `eligible_time_ms`, `dependency`,
  `details_observed`, `start_time_ms`, `partition`, `req_gpus`, `req_cpus`,
  `array_task_count`. Running jobs get segments too (`state='RUNNING'`).
  `req_gpus` is `ReqTRES gres/gpu` x task count (`slurm.py`, `_fill_queue_details`).
- **No user column.** The user comes from a join to `jobs` on `(job_id, submit_time_ms)`.
- **No QOS column.** This spec adds one.
- `queue_observation_windows`: one row per unbroken run of polls. A gap longer than
  `max_gap_ms` or a restart starts a new row. Time between rows is unrecorded.
- `queue_accounting`: sacct backfill with `eligible_time_ms`, `pending_end_ms`, `user`,
  `req_gpus`, `partition`. No reason, no QOS.
- **The live DB `/home/adovlatyan/Slurm-Monitoring/data/slurm_history.db` has none of the
  `queue_*` tables yet.** It runs older code. Observed history starts when this branch is
  deployed. Earlier days exist only as the accounting estimate, after
  `scripts/import_queue_accounting.py --apply` is run.

### 1.9 Known limitations

1. **Sampled, not event-exact.** A wait that starts and ends between two grid points is
   missed. With a 2 h threshold and a step of 60 minutes or less this is small.
2. **Numbers shift slightly with the range.** The step grows with the range, so the same day
   can read a little differently in a 7-day and a 90-day view.
3. **One reason per job.** A job that shows `Priority` may also be blocked by something we
   cannot see (a partition QOS, an association limit, a node feature). The cap rule covers
   only the per-user GPU cap of the job's QOS.
4. **Caps are a config snapshot.** If the cap changes, past days are re-read with the new
   cap. The API echoes the caps used so the page can show them.
5. **The accounting estimate is rough, in both directions.** It cannot drop jobs held back
   by `JobArrayTaskLimit`, holds or a QOS limit, so it can count too many. It skips requeued
   jobs and pending array records that sacct has not split, and it uses the default cap for
   everyone, so it can also count too few. It is drawn differently and labelled.
6. **Not a starvation measure.** A person who starts one small job still counts while a big
   one waits. A person can also leave the count by cancelling. Read "GPUs running meanwhile"
   next to the wait.
7. **Eligible can move.** A requeue or a job update gives a new Eligible time and the age
   restarts.
8. **`rtx` is left out.** That partition is one desktop GPU and is not part of the H100 pool.
   Running GPUs on `rtx` still count toward the QOS cap, as in Slurm.
9. **Users with no `jobs` row** appear as `unknown` and count as one person.
10. **Hours are sample counts.** Each known sample stands for one `step`. Next to a recording
    gap this can be off by up to one step.
11. **Running GPUs come from the request** (`ReqTRES`), because that is what a segment
    stores. Slurm enforces the cap on allocated GPUs. On 2026-11-24 all 43 running GPU jobs
    had the same GPU count in `ReqTRES` and `AllocTRES`.

---

## 2. Implementation instructions

Work test-first. Write the tests in 2.8, see them fail, then implement. Leave the existing
Queue chart code and the `/api/pending-age` output format as they are. Do not touch the
live DB.

### 2.1 Files

| File | Change |
|------|--------|
| `waiting.py` | New. All metric logic. |
| `pending_age.py` | Extract the merged-coverage code into a helper. No behaviour change. |
| `slurm.py` | `_fill_queue_details`: also capture `QOS`. |
| `queue_history.py` | Add `qos` column to segments, with migration. Store it. |
| `history.py` | New wrapper `get_waiting_people`. |
| `config.py` | New settings. |
| `app.py` | New route `/api/waiting`. |
| `static/waiting.js` | New. Pure functions, exported like `pending-age.js`. |
| `static/usage.html`, `static/usage.js`, `static/usage.css` | New block, fetch, render. Bump the `?v=` strings. |
| `scripts/check_waiting_against_sacct.py` | New. Read-only cross-check. |
| `tests/test_waiting.py`, `tests/test_waiting.js` | New. |
| `package.json` | Add `node tests/test_waiting.js` and `node --check static/waiting.js` to `test`. |
| `README.md` | Short section: definition, config, cross-check command. |

### 2.2 Recorder: store QOS

`slurm.py`, `_fill_queue_details`: add `QOS` to the regex alternation and set
`job['qos'] = fields.get('QOS')`. `scontrol show job -o` already prints `QOS=researcher`.
No extra command is run.

`queue_history.py`:

- In `init`, after the `CREATE TABLE`, add the column if missing. Follow the pattern at the
  bottom of `pending_age.init` (`PRAGMA table_info`, then `ALTER TABLE ... ADD COLUMN qos TEXT`).
- In `record`, append `job.get('qos')` to `values` and add `qos` to the `INSERT` column list
  and placeholders. `qos` then becomes part of the signature. That is intended. A QOS change
  needs `scontrol update`, so it is rare. When it happens it starts one new segment, like any
  other field change.

Existing tests in `tests/test_queue_history.py` and `tests/test_pending_age.py` must pass
unchanged.

### 2.3 `pending_age.py`: shared coverage helper

Move lines that build `merged` (segments' `(first_ms, last_ms)` plus
`queue_observation_windows`, sorted and unioned) into:

```python
def observed_ranges(conn, from_ms: int, to_ms: int, segments=None) -> list[list[int]]:
    """Unioned [lo, hi] ranges with queue evidence that overlap [from_ms, to_ms)."""

def sources_for(points: list[int], merged: list[list[int]], meta) -> list[str]:
    """'observed' | 'accounting' | 'unknown' per point. meta = (cohort_start_ms, cutoff_ms) or None."""
```

`get_buckets` calls them. Output must be identical; the existing tests prove it.

### 2.4 `config.py`

```python
WAITING_THRESHOLDS_H = (2, 24)
# Per-user GPU cap by QOS (sacctmgr show qos format=name,maxtrespu). None = no cap.
# Checked 2026-11-24. Update when a cap changes.
WAITING_QOS_GPU_CAPS = json.loads(os.environ.get("SLURM_DASHBOARD_WAITING_QOS_GPU_CAPS", "null")) or {
    "researcher": 35, "pi": 16, "student": 1, "admin": 64, "golf": 16, "normal": None,
}
WAITING_DEFAULT_GPU_CAP = 35          # used when a job's QOS is unknown
WAITING_EXCLUDED_PARTITIONS = ("rtx",)
```

### 2.5 `waiting.py`

```python
CLUSTER_REASONS = frozenset({
    'Priority', 'Resources', 'Nodes_required_for_job_are_DOWN', 'ReqNodeNotAvail',
    'NodeDown', 'PartitionDown', 'PartitionInactive',
})

def normalise_reason(reason: str | None) -> str: ...
def reason_class(reason: str | None) -> str:
    """'cluster' | 'not_cluster' | 'unclassified'. Rules in spec 1.3."""

def classify_person(pending: list[dict], running_by_qos: dict, t_ms: int,
                    caps: dict, default_cap: int | None,
                    thresholds_ms: tuple[int, ...]) -> dict:
    """Pure. One person at one instant.
    pending items: {eligible_ms, gpus_total, tasks, qos, cap_reason}
      Already passed J1-J5. Reason is cluster-side, or it is QOSMaxGRESPerUser, in which
      case cap_reason=True and the job can only make the person 'capped'.
    running_by_qos: {qos_or_None: gpus}
    Returns {'waiting': {T_ms: bool}, 'capped': bool, 'max_age_ms': int | None,
             'asked_gpus': int, 'running_gpus': int}
    """

def get_waiting(conn, from_ms: int, to_ms: int, tz_offset_min: int = 0, now_ms: int | None = None,
                caps: dict | None = None, default_cap: int | None = None,
                excluded_partitions: tuple = (), thresholds_h: tuple = (2, 24)) -> dict: ...

def explain(conn, t_ms: int, **rules) -> dict:
    """Per user at one instant: status ('waiting' | 'capped' | 'not_counted'), and for each
    pending GPU job the first rule it failed. Used by the cross-check script and tests."""
```

`classify_person` details:

- `gpus_per_task = ceil(gpus_total / max(1, tasks))`.
- `cap = default_cap if qos is None else caps.get(qos)`; a known QOS missing from `caps`
  means no cap.
- `used` = `sum(running_by_qos.values())` when the job's `qos` is `None`. Otherwise
  `running_by_qos.get(qos, 0) + running_by_qos.get(None, 0)`. Running GPUs of unknown QOS
  count against every cap. Do not add the `None` entry twice.
- `fits = (not cap_reason) and (cap is None or used + gpus_per_task <= cap)`.
- `waiting[T]` = any job with age >= T and `fits`.
- `capped` = not `waiting[2 h]`, and some job has age >= 2 h.
- `asked_gpus` = sum of `gpus_total` over jobs with age >= 2 h and `cap_reason` false.
- `max_age_ms` = max age over jobs with age >= 2 h that fit.
- `running_gpus` = sum of all values in `running_by_qos`.

`get_waiting` data path:

1. `to_ms = min(to_ms, now_ms)`. Pick `step_ms` and `bucket_ms` (1.6). Build `points`.
2. Load segments like `pending_age.get_buckets` does (`LEFT JOIN jobs` for `user`), but
   select on the widened interval from 1.6:
   `COALESCE(previous_last_ms + 1, first_ms) <= :last_point AND last_ms >= :first_point`.
   A segment that starts just after the range can still cover the last sample.
3. `merged = pending_age.observed_ranges(...)`, `sources = pending_age.sources_for(...)`.
4. For each segment, find its point index range with `bisect`, using the widened `lo`.
   `PENDING` rows that pass J1-J5 and have a cluster-side reason or `QOSMaxGRESPerUser` go
   to `pending[i][user]`. `RUNNING` rows with `req_gpus > 0` add to
   `running[i][user][qos]`. Only fill points whose source is `observed`. Count
   `unclassified` reasons here.
5. For `accounting` points: pending from `queue_accounting`
   (`eligible_time_ms <= t < pending_end_ms`, `req_gpus > 0`, partition not excluded,
   `tasks=1`, `qos=None`). Running from `jobs`
   (`start_time_ms <= t AND (end_time_ms > t OR (end_time_ms IS NULL AND state='RUNNING'))`,
   `req_gpus > 0`), all under `qos=None`.
6. For each known point and each user with pending entries, call `classify_person` and add
   to the per-user, per-bucket counters and the per-person totals.
7. "Waiting now": read `queue_recorder_status`. If `last_ms` is within `max_gap_ms` of
   `now_ms` and `to_ms >= last_ms`, classify once more at `t = last_ms` and fill
   `current_wait_h` and the `current` block. Otherwise both are `null`.

Bucket of a sample: `off = tz_offset_min * 60000`; `index = (t - off) // bucket_ms`;
`bucket start = index * bucket_ms + off`. `tz_offset_min` uses the JavaScript sign
(`Date.prototype.getTimezoneOffset`): Yerevan is `-240`.

Keep the whole call under 2 s for a 30-day range on a DB the size of the live one. The app
runs one gunicorn worker and `history._db_lock` blocks the recorder while a query runs.

### 2.6 API

`history.py`:

```python
def get_waiting_people(db_path: str, from_ms: int, to_ms: int, tz_offset_min: int = 0) -> dict:
```

Same shape as `get_pending_age_buckets`: take `_db_lock`, open with `row_factory = sqlite3.Row`,
call `waiting.get_waiting` with the `config.WAITING_*` values, close.

`app.py`: `GET /api/waiting?from=<ms>&to=<ms>&tz_offset_min=<int>`.
Return 400 `{"error": "A valid from/to range is required"}` when `from`/`to` are missing or
`from >= to` (same as `/api/pending-age`). Clamp `tz_offset_min` to `[-840, 720]`, default 0.
There is no `group_by`. This view always counts users.

Response:

```json
{
  "methodology": "waiting_people_v1",
  "thresholds_h": [2, 24],
  "step_ms": 300000,
  "bucket_ms": 3600000,
  "buckets": [1795500000000],
  "samples": {"expected": [12], "observed": [12], "accounting": [0]},
  "users": {
    "alice": {"w2": [12], "w2_idle": [12], "w24": [0], "w24_idle": [0]}
  },
  "people": [
    {"user": "alice", "waiting_h": 1.0, "waiting_24h_h": 0.0, "idle_h": 1.0, "capped_h": 0.0,
     "longest_wait_h": 5.5, "current_wait_h": 5.5,
     "avg_asked_gpus": 70.0, "max_asked_gpus": 70,
     "avg_running_gpus": 0.0, "max_running_gpus": 0,
     "estimate_h": 0.0}
  ],
  "current": {"as_of_ms": 1795503600000, "people_2h": 1, "people_24h": 0},
  "rules": {"cluster_reasons": ["Priority", "Resources"], "caps": {"researcher": 35},
            "default_cap": 35, "excluded_partitions": ["rtx"], "unmapped_qos": []},
  "unclassified_reasons": {"Prolog": 3}
}
```

- `users[u].w2[i]` = number of known samples in bucket `i` where `u` was waiting at 2 h.
  `w2_idle` = the subset with 0 running GPUs. `w24`, `w24_idle` likewise. Omit users with
  all zeros.
- `people` is sorted by `waiting_h` descending. `estimate_h` = waiting hours that came from
  `accounting` samples. `current` is `null` when rule 7 above does not apply.
- Hours = samples x `step_ms / 3_600_000`.

The client derives the chart from `users` so the sidebar selection can filter it:
`avg[i] = sum_u w2[u][i] / known[i]`, `any[i] = count of u with w2[u][i] > 0`,
`known[i] = observed[i] + accounting[i]`.

### 2.7 Front end

`static/waiting.js`, same wrapper as `pending-age.js` (`root.Waiting = api`,
`module.exports = api`):

```js
series(data, selectedUsers /* Set or null = all */)
// -> [{x, startTime, endTime, known, expected, estimate /* accounting > 0 */,
//      idle2, busy2, total24, any2}]   values are null when known === 0
tableRows(data, selectedUsers, sortKey = 'waiting_h', descending = true)
```

`usage.html`: add one `hero-block` after the Queue block, and a table block under it.

- Left column: label "Waiting people" with a new accent dot, then one sentence:
  "People with a GPU job ready for 2+ hours that the cluster is holding back." Below it a
  small grid like `wait-stats-grid`: "Waiting now (2h+)", "Waiting now (24h+)", "As of".
- Chart canvas `waiting-chart`.
- Under the chart: `<div id="waiting-table">` and a one-line footnote built from `rules`
  ("Own-cap rule: researcher 35, pi 16, ... Excluded partitions: rtx.") plus any
  `unclassified_reasons`.

`usage.js`:

- Fetch `api/waiting?from=..&to=..&tz_offset_min=${new Date(from_ms).getTimezoneOffset()}`
  alongside the two existing calls. If only this call fails, keep the rest of the page and
  show "Waiting view unavailable" in the block. Store the result in `state.waitingData`.
- `renderWaitingChart()`: Chart.js `bar`, following `renderQueueChart` (same `fontOpts`,
  grid and tick colours, tooltip styling, `animation: false`, time x-axis with the same
  `min`/`max`, `afterFit` with `usageYAxisWidth`). Datasets:
  1. Bar, stacked: "Waiting 2h+, nothing running" (`idle2`).
  2. Bar, stacked: "Waiting 2h+, already running GPUs" (`busy2`).
  3. Line: "Waiting 24h+" (`total24`), `spanGaps: false`.
  4. Line, dashed, no fill: "Waited 2h+ at any point" (`any2`), `spanGaps: false`.
  Y axis: "people", `min: 0`, integer ticks. Estimate buckets get half opacity on the bars.
  Tooltip title: the bucket's time range. Tooltip footer: `Samples: known/expected`, and
  "Estimate from accounting, no pending reasons" when `estimate` is true. Buckets with
  `known === 0` draw nothing.
- `renderWaitingTable()`: columns from 1.7. Click a header to sort; click again to flip.
  Reuse `fmtWait` for durations and `escHtml` for names. Mark rows with `estimate_h > 0`.
- Selection: in Users view, chart and table use `state.visibleUsers`. In Projects and Units
  views they show all users, with a note "Counts people. Sidebar selection applies in Users
  view." Call both render functions wherever `renderQueueChart()` is called.
- In Users view, add the waiting users to the sidebar's user set in `fetchAndRender`
  (`Object.keys(state.waitingData.users)` and every `people[].user`). Otherwise a person who
  only waited, or was only at their cap, would be filtered out.

### 2.8 Tests to write first

Python, `tests/test_waiting.py`. Follow `tests/test_pending_age.py`: temp DB,
`history.init_db`, feed snapshots through `history.upsert_jobs(db, jobs, ts)`, pass `now_ms`.
Use a base job dict with `queue_details_observed=True`, `dependency='(null)'`,
`pending_reason='Priority'`, `qos='researcher'`, `partition='research'`.

1. `reason_class`: every string in 1.3, in both the spaced and underscored forms, plus the
   comma forms (`ReqNodeNotAvail,_UnavailableNodes:gpu05`, `Priority,Resources`).
2. People, not jobs: alice has 70 one-GPU jobs, bob has one. Count is 2.
3. Thresholds: Eligible 1 h ago is not waiting. 3 h ago is waiting at 2 h only. 25 h ago is
   waiting at both.
4. Not counted: `req_gpus=0`; `eligible_time=None`; `queue_details_observed=False`;
   reasons `QOSMaxGRESPerUser`, `JobArrayTaskLimit`, `BeginTime`, `JobHeldUser`,
   `PartitionTimeLimit`, `Dependency`; dependency text `afterok:8(unfulfilled)` with reason
   `Priority`.
5. Cap rule: (a) 35 running + one 1-GPU job with `Priority`: not waiting, `capped`.
   (b) 27 running + an 8-GPU and a 1-GPU job: waiting. (c) 30 running + one 8-GPU job:
   capped. (c2) 0 running + one job with reason `QOSMaxGRESPerUser`: capped, not waiting,
   and `avg_asked_gpus` ignores it. (c3) QOS unknown on both sides, 18 running, cap 35, one
   1-GPU job: waiting (guards against counting unknown-QOS GPUs twice).
   (d) QOS `normal`: waiting at any running count. (e) 48-task array of 1-GPU tasks
   with 0 running: waiting. (f) 35 running under `admin` do not block a `researcher` job,
   and `max_running_gpus` is 35.
6. Running in another partition under the same QOS counts toward the cap.
7. Excluded partition: a job in `rtx` only does not count. A job in `research,rtx` does.
8. Unclassified reason: not counted, and appears in `unclassified_reasons`.
9. Gap: polls at 0-10 min, none for 3 h, polls again. Samples in the gap are unknown. A
   bucket fully inside the gap has `expected > 0`, `observed == 0`, and the person's
   `waiting_h` excludes the gap.
10. Reason flip: the reason alternates `Priority` / `Resources` on every poll for an hour, with
    poll times that never land on a grid point. The person counts on every sample in that
    hour. Also: a segment whose `first_ms` is after the last sample but whose
    `previous_last_ms` is before it covers that sample.
11. Accounting estimate: no segments, one `queue_accounting` row, meta set. The person
    counts, `samples.accounting > 0`, `estimate_h > 0`. Add a segment window over the same
    time: the observed rules win and nothing is counted twice.
12. Buckets: range <= 4 days gives `bucket_ms == 3_600_000`; longer gives 86,400,000. With
    `tz_offset_min=-240`, daily bucket starts are at 20:00 UTC. Step selection for 1, 7, 30
    and 90 days.
13. Table numbers for one hand-built person: `waiting_h`, `waiting_24h_h`, `idle_h`,
    `capped_h`, `longest_wait_h`, `avg_asked_gpus`, `avg_running_gpus`.
14. `current`: filled when the range reaches the last poll, `null` when the range ends earlier.
15. Recorder: `_fill_queue_details` sets `job['qos']` from a patched `scontrol` line (see
    `test_array_detail_matching_and_unknown_details`). A segments table created without `qos`
    gains the column on `queue_history.init`. A QOS change starts a new segment.
16. Route: with `app.test_client()`, `/api/waiting` without `from` returns 400; a valid call
    returns the keys in 2.6. Set `SLURM_DASHBOARD_DISABLE_RECORDER=1` and point
    `config.HISTORY_DB_PATH` at a temp DB (see `tests/test_security_boundaries.py`).

JavaScript, `tests/test_waiting.js`. Use `node:test` and `node:assert/strict`, so both
`node tests/test_waiting.js` and `node --test tests/test_waiting.js` run it.

1. `series` sums selected users only and divides by known samples.
2. A bucket with `observed + accounting === 0` returns `null` values, not 0.
3. `any2` counts users with a non-zero cell. `idle2 + busy2` equals the 2 h average.
4. `estimate` is true when a bucket has accounting samples.
5. `tableRows` filters by selection and sorts both ways. Ties sort by user name.

### 2.9 Acceptance checks

1. `SLURM_DASHBOARD_DISABLE_RECORDER=1 .venv/bin/python -m unittest discover -s tests` and
   `npm test` pass. No existing test was edited.
2. Trial run on a **copy** of a DB, with the recorder on, for at least 3 hours. Never point a
   trial at `/home/adovlatyan/Slurm-Monitoring/data`. Then:
   - `sqlite3 copy.db "select qos,count(*) from queue_observation_segments group by 1"` shows
     real QOS names.
   - The page shows the block. Bars only where there was recording. "Waiting now" matches a
     hand count from `squeue -h -a -t PD -o "%u|%r|%b|%q"` plus `squeue -t R` for running GPUs.
3. Caps in `config.py` equal `sacctmgr -nP show qos format=name,maxtrespu` on the day of
   the trial.
4. A 30-day `/api/waiting` call on a copy of the live DB returns in under 2 s.
5. Sacct cross-check for one fully recorded day, with
   `scripts/check_waiting_against_sacct.py --db <copy> --day YYYY-MM-DD --tz-offset-min -240`.
   The script opens the DB with `mode=ro` and runs `sacct` read-only.

   Method:
   - Pull `sacct -a -X -D --array -n -P -S <day minus 21 d> -E <day end>
     --format=JobID,User,Submit,Eligible,Start,End,State,ReqTRES,Partition,QOS` with `TZ=UTC`
     and `SLURM_TIME_FORMAT=standard`. Same flags as `scripts/import_queue_accounting.py`.
     `-D` keeps requeued records. Reuse that script's rules for requeues and duplicate
     records: mark those job IDs `uncertain`. The 21-day lookback gives jobs that were
     already pending their true age.
   - At each whole hour `t` of the day, build set **A(t)**: users with a GPU job, outside
     excluded partitions, where `Eligible <= t - 2 h` and the job had not started or ended
     by `t`.
   - Build set **W(t)** from `waiting.explain(conn, t)`: users with status `waiting`.
   - **Check 1:** every user in W(t) should be in A(t), because A has no reason filter and
     no cap rule. Allowed exceptions, listed separately and not failures: `uncertain` jobs,
     pending arrays sacct has not split, and jobs whose sacct `Start` or `Eligible + 2 h` is
     within one `max_gap_ms` of `t` (the recorder places changes up to one poll early).
     Anything else in W but not in A is a bug.
   - **Check 2:** every user in A(t) but not in W(t) has a stated cause from `explain`:
     a not-cluster reason, a hold or dependency, at own cap, or no captured details. Zero
     unexplained users.
   - Print one line per hour: `t`, `|W|`, `|A|`, and each difference with its cause. Exit
     non-zero on any unexplained difference.
6. In the browser: switching presets redraws the block, sorting works, deselecting a user in
   the sidebar lowers the bars, and Projects view shows the note.

---

## 3. Out of scope

- Starvation spells, reset or pause logic, "time since last start".
- "How many GPUs would clear the queue" or any capped-demand number.
- Final wait of started jobs (`Start - Eligible`). The page had that before; not here.
- Grouping by project or unit.
- CPU-only jobs and the `rtx` partition.
- Reading caps live from `sacctmgr`, or a history of cap changes.
- Partition QOS, association limits, node features and reservations in the cap rule.
- Changes to the existing Queue chart, `/api/pending-age` output, or `/api/wait`.
- Alerts, Slack messages, the weekly fairness report.
- Deploying to the live instance, migrating the live DB, running the accounting import.
- Any `scontrol`, `sacctmgr` or other command that changes the cluster.
