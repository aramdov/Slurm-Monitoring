"""Per-person waiting view: how many people have a GPU job the cluster holds back.

Definition and rules: docs/superpowers/specs/2026-11-24-per-person-waiting-view.md.
A person is waiting at threshold T when one of their pending GPU jobs has been
eligible for at least T, shows a cluster-side reason, and would fit under the
per-user GPU cap of its QOS. Counted on a fixed time grid; each person once.
"""
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
import time

import pending_age

H = 3_600_000
D = 24 * H
STEPS_MS = (60_000, 300_000, 900_000, 3_600_000)
MAX_SAMPLES = 3000
HOURLY_MAX_RANGE_MS = 4 * D
CAP_REASON = 'QOSMaxGRESPerUser'

CLUSTER_REASONS = frozenset({
    'Priority', 'Resources', 'Nodes_required_for_job_are_DOWN', 'ReqNodeNotAvail',
    'NodeDown', 'PartitionDown', 'PartitionInactive',
})
NOT_CLUSTER_REASONS = frozenset({
    'JobArrayTaskLimit', 'BeginTime', 'Reservation', 'DeadLine', 'BadConstraints',
    'job_requeued_in_held_state', 'launch_failed_requeued_held', 'JobHoldMaxRequeue',
    'PartitionTimeLimit', 'PartitionConfig', 'InvalidAccount', 'InvalidQOS',
    'AccountNotAllowed', 'QOSNotAllowed',
})
NOT_CLUSTER_PREFIXES = ('QOS', 'Assoc', 'Max', 'Dependency', 'JobHeld')


def normalise_reason(reason):
    return (reason or '').strip().replace(' ', '_').split(',', 1)[0]


def reason_class(reason):
    """'cluster' | 'not_cluster' | 'unclassified'. Rules in spec 1.3."""
    name = normalise_reason(reason)
    if name in CLUSTER_REASONS:
        return 'cluster'
    if (name in NOT_CLUSTER_REASONS or name.startswith(NOT_CLUSTER_PREFIXES)
            or 'not_in_group_permitted' in name):
        return 'not_cluster'
    return 'unclassified'


def _cap(qos, caps, default_cap):
    return default_cap if qos is None else caps.get(qos)


def _used(qos, running_by_qos):
    # Running GPUs of unknown QOS count against every cap, once.
    if qos is None:
        return sum(running_by_qos.values())
    return running_by_qos.get(qos, 0) + running_by_qos.get(None, 0)


def _fits(job, running_by_qos, caps, default_cap):
    if job['cap_reason']:
        return False
    cap = _cap(job['qos'], caps, default_cap)
    gpus_per_task = -(-job['gpus_total'] // max(1, job['tasks'] or 1))
    return cap is None or _used(job['qos'], running_by_qos) + gpus_per_task <= cap


def classify_person(pending, running_by_qos, t_ms, caps, default_cap, thresholds_ms):
    """Pure. One person at one instant.

    pending items: {eligible_ms, gpus_total, tasks, qos, cap_reason}, already past J1-J5,
    with a cluster-side reason or (cap_reason=True) QOSMaxGRESPerUser.
    running_by_qos: {qos_or_None: gpus}.
    """
    first = min(thresholds_ms)
    waiting = {threshold: False for threshold in thresholds_ms}
    ready = False
    max_age = None
    asked = 0
    for job in pending:
        age = t_ms - job['eligible_ms']
        if age < first:
            continue
        ready = True
        if not job['cap_reason']:
            asked += job['gpus_total']
        if _fits(job, running_by_qos, caps, default_cap):
            for threshold in thresholds_ms:
                if age >= threshold:
                    waiting[threshold] = True
            max_age = age if max_age is None else max(max_age, age)
    return {'waiting': waiting, 'capped': ready and not waiting[first], 'max_age_ms': max_age,
            'asked_gpus': asked, 'running_gpus': sum(running_by_qos.values())}


def _partition_allowed(partition, excluded):
    names = [name for name in (partition or '').split(',') if name]
    return not names or any(name not in excluded for name in names)


def _check_segment(row, excluded):
    """First failed rule among J1-J6 for a segment row: (failed or None, cap_reason)."""
    if row['state'] != 'PENDING':
        return 'not_pending', False
    if not row['details_observed']:
        return 'no_details', False
    if not row['req_gpus'] or row['req_gpus'] <= 0:
        return 'no_gpus', False
    if not _partition_allowed(row['partition'], excluded):
        return 'excluded_partition', False
    eligible = row['eligible_time_ms']
    if not isinstance(eligible, int) or eligible < row['submit_time_ms']:
        return 'no_eligible', False
    if pending_age.blocked(row['reason'], row['dependency']):
        return 'dependency_or_hold', False
    if normalise_reason(row['reason']) == CAP_REASON:
        return None, True
    kind = reason_class(row['reason'])
    if kind == 'not_cluster':
        return 'not_cluster_reason', False
    if kind == 'unclassified':
        return 'unclassified_reason', False
    return None, False


def _entry(eligible, gpus, tasks, qos, cap_reason):
    return {'eligible_ms': eligible, 'gpus_total': gpus, 'tasks': max(1, tasks or 1),
            'qos': qos, 'cap_reason': cap_reason}


def _step_ms(from_ms, to_ms):
    for step in STEPS_MS:
        if _grid_count(from_ms, to_ms, step) <= MAX_SAMPLES:
            return step
    return STEPS_MS[-1]


def _grid_count(from_ms, to_ms, step):
    first = -(-from_ms // step) * step
    return max(0, -(-(to_ms - first) // step))


def _segment_lo(row):
    return row['previous_last_ms'] + 1 if row['previous_last_ms'] is not None else row['first_ms']


_SEGMENT_SQL = '''SELECT s.job_id,s.submit_time_ms,s.first_ms,s.last_ms,s.previous_last_ms,s.state,
    s.reason,s.eligible_time_ms,s.dependency,s.details_observed,s.partition,s.req_gpus,
    s.array_task_count,{qos} AS qos,j.user FROM queue_observation_segments s
    LEFT JOIN jobs j ON j.job_id=s.job_id AND j.submit_time_ms=s.submit_time_ms
    WHERE COALESCE(s.previous_last_ms+1,s.first_ms)<=? AND s.last_ms>=?'''


def _segment_sql(conn):
    # Rows recorded before the qos column existed read as unknown QOS (default cap).
    columns = {row[1] for row in conn.execute('PRAGMA table_info(queue_observation_segments)')}
    return _SEGMENT_SQL.format(qos='s.qos' if 'qos' in columns else 'NULL')


def _schedule(caps, default_cap, cap_history):
    """Sorted [{from_ms, caps, default_cap}]; the first period starts at None (always)."""
    if not cap_history:
        cap_history = [{'from_ms': None, 'caps': caps or {}, 'default_cap': default_cap}]
    periods = sorted(({'from_ms': p['from_ms'], 'caps': dict(p['caps'] or {}),
                       'default_cap': p.get('default_cap')} for p in cap_history),
                     key=lambda p: -1 if p['from_ms'] is None else p['from_ms'])
    periods[0]['from_ms'] = None
    return periods


def _caps_at(schedule, t_ms):
    """(caps, default_cap) in force at t_ms."""
    period = schedule[0]
    for candidate in schedule[1:]:
        if candidate['from_ms'] > t_ms:
            break
        period = candidate
    return period['caps'], period['default_cap']


def _rules(caps, default_cap, excluded_partitions, thresholds_h, cap_history=None):
    thresholds_ms = tuple(sorted(int(h * H) for h in thresholds_h))
    return (_schedule(caps, default_cap, cap_history), tuple(excluded_partitions or ()),
            thresholds_ms)


def _instant_observed(conn, t_ms, excluded, first_threshold):
    """Pending entries, running GPUs and per-job verdicts at one observed instant."""
    pending, running, verdicts = defaultdict(list), defaultdict(Counter), defaultdict(list)
    for row in conn.execute(_segment_sql(conn), (t_ms, t_ms)):
        user = row['user'] or 'unknown'
        if row['state'] == 'RUNNING':
            if (row['req_gpus'] or 0) > 0:
                running[user][row['qos']] += row['req_gpus']
            continue
        failed, cap_reason = _check_segment(row, excluded)
        age = t_ms - row['eligible_time_ms'] if isinstance(row['eligible_time_ms'], int) else None
        if failed is None and age < first_threshold:
            failed = 'too_young'
        entry = None
        if failed is None:
            entry = _entry(row['eligible_time_ms'], row['req_gpus'], row['array_task_count'],
                           row['qos'], cap_reason)
            pending[user].append(entry)
        if row['state'] == 'PENDING' and (row['req_gpus'] or 0) > 0:
            verdicts[user].append({'job_id': row['job_id'], 'reason': row['reason'],
                                   'age_ms': age, 'gpus': row['req_gpus'], 'qos': row['qos'],
                                   'partition': row['partition'], 'failed': failed,
                                   '_entry': entry})
    return pending, running, verdicts


def _instant_accounting(conn, t_ms, excluded, first_threshold):
    pending, running, verdicts = defaultdict(list), defaultdict(Counter), defaultdict(list)
    for row in conn.execute('SELECT job_id,user,partition,eligible_time_ms,req_gpus FROM queue_accounting '
                            'WHERE eligible_time_ms<=? AND pending_end_ms>? AND req_gpus>0', (t_ms, t_ms)):
        age = t_ms - row['eligible_time_ms']
        failed = ('excluded_partition' if not _partition_allowed(row['partition'], excluded)
                  else 'too_young' if age < first_threshold else None)
        entry = None
        if failed is None:
            entry = _entry(row['eligible_time_ms'], row['req_gpus'], 1, None, False)
            pending[row['user']].append(entry)
        verdicts[row['user']].append({'job_id': row['job_id'], 'reason': None, 'age_ms': age,
                                      'gpus': row['req_gpus'], 'qos': None,
                                      'partition': row['partition'], 'failed': failed,
                                      '_entry': entry})
    for row in _running_jobs(conn, t_ms, t_ms):
        if row['start_time_ms'] <= t_ms and (row['end_time_ms'] is None or row['end_time_ms'] > t_ms):
            running[row['user'] or 'unknown'][None] += row['req_gpus']
    return pending, running, verdicts


def _running_jobs(conn, first_ms, last_ms):
    return conn.execute('''SELECT user,start_time_ms,
            CASE WHEN end_time_ms IS NULL OR end_time_ms<=0 THEN NULL ELSE end_time_ms END AS end_time_ms,
            req_gpus FROM jobs
        WHERE req_gpus>0 AND start_time_ms>0 AND start_time_ms<=?
          AND (end_time_ms>? OR ((end_time_ms IS NULL OR end_time_ms<=0) AND state='RUNNING'))''',
                        (last_ms, first_ms))


def explain(conn, t_ms, caps=None, default_cap=None, excluded_partitions=(), thresholds_h=(2, 24),
            cap_history=None):
    """Per user at one instant: status ('waiting' | 'capped' | 'not_counted'), and for
    each pending GPU job the first rule it failed (None = counts)."""
    schedule, excluded, thresholds_ms = _rules(caps, default_cap, excluded_partitions, thresholds_h,
                                               cap_history)
    caps, default_cap = _caps_at(schedule, t_ms)
    meta = conn.execute('SELECT cohort_start_ms,cutoff_ms FROM queue_accounting_meta WHERE id=1').fetchone()
    merged = pending_age.observed_ranges(conn, t_ms, t_ms + 1)
    source = pending_age.sources_for([t_ms], merged, meta)[0]
    users = {}
    if source != 'unknown':
        gather = _instant_observed if source == 'observed' else _instant_accounting
        pending, running, verdicts = gather(conn, t_ms, excluded, thresholds_ms[0])
        for user, jobs in verdicts.items():
            run = dict(running.get(user, {}))
            result = classify_person(pending.get(user, []), run, t_ms, caps, default_cap, thresholds_ms)
            for job in jobs:
                entry = job.pop('_entry')
                if entry is not None and not _fits(entry, run, caps, default_cap):
                    job['failed'] = 'cap_reason' if entry['cap_reason'] else 'over_cap'
            status = ('waiting' if result['waiting'][thresholds_ms[0]]
                      else 'capped' if result['capped'] else 'not_counted')
            users[user] = {'status': status, 'running_gpus': result['running_gpus'],
                           'running_by_qos': {str(k): v for k, v in run.items()},
                           'max_age_ms': result['max_age_ms'], 'jobs': jobs}
    return {'t_ms': t_ms, 'source': source, 'caps': caps, 'default_cap': default_cap, 'users': users}


def get_waiting(conn, from_ms, to_ms, tz_offset_min=0, now_ms=None, caps=None, default_cap=None,
                excluded_partitions=(), thresholds_h=(2, 24), cap_history=None):
    schedule, excluded, thresholds_ms = _rules(caps, default_cap, excluded_partitions, thresholds_h,
                                               cap_history)
    known_qos = set().union(*(p['caps'] for p in schedule))
    first_threshold = thresholds_ms[0]
    keys = [f'w{h:g}' for h in sorted(thresholds_h)]
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    to_ms = min(to_ms, now_ms)
    step = _step_ms(from_ms, to_ms)
    bucket_ms = H if to_ms - from_ms <= HOURLY_MAX_RANGE_MS else D
    first_point = -(-from_ms // step) * step
    points = list(range(first_point, to_ms, step))
    n = len(points)
    offset = tz_offset_min * 60_000
    buckets, bucket_of = [], []
    if points:
        base = (points[0] - offset) // bucket_ms
        buckets = [(k * bucket_ms + offset) for k in range(base, (points[-1] - offset) // bucket_ms + 1)]
        bucket_of = [(t - offset) // bucket_ms - base for t in points]
    nb = len(buckets)

    meta = conn.execute('SELECT cohort_start_ms,cutoff_ms FROM queue_accounting_meta WHERE id=1').fetchone()
    segments = conn.execute(_segment_sql(conn), (points[-1], points[0])).fetchall() if points else []
    merged = pending_age.observed_ranges(conn, from_ms, to_ms, segments)
    sources = pending_age.sources_for(points, merged, meta)

    pending = [None] * n
    running = [None] * n
    unclassified = Counter()
    unmapped = set()

    def add_pending(i, user, entry):
        slot = pending[i]
        if slot is None:
            slot = pending[i] = defaultdict(list)
        slot[user].append(entry)

    def add_running(i, user, qos, gpus):
        slot = running[i]
        if slot is None:
            slot = running[i] = defaultdict(Counter)
        slot[user][qos] += gpus

    for row in segments:
        i0, i1 = bisect_left(points, _segment_lo(row)), bisect_right(points, row['last_ms'])
        if i0 >= i1:
            continue
        user = row['user'] or 'unknown'
        qos = row['qos']
        if row['state'] == 'RUNNING':
            if (row['req_gpus'] or 0) > 0:
                if qos is not None and qos not in known_qos:
                    unmapped.add(qos)
                for i in range(i0, i1):
                    if sources[i] == 'observed':
                        add_running(i, user, qos, row['req_gpus'])
            continue
        failed, cap_reason = _check_segment(row, excluded)
        if failed not in (None, 'unclassified_reason'):
            continue
        i0 = max(i0, bisect_left(points, row['eligible_time_ms'] + first_threshold))
        if failed == 'unclassified_reason':
            label = normalise_reason(row['reason']) or '(empty)'
            unclassified[label] += sum(1 for i in range(i0, i1) if sources[i] == 'observed')
            continue
        if qos is not None and qos not in known_qos:
            unmapped.add(qos)
        entry = _entry(row['eligible_time_ms'], row['req_gpus'], row['array_task_count'], qos, cap_reason)
        for i in range(i0, i1):
            if sources[i] == 'observed':
                add_pending(i, user, entry)

    accounting = [i for i, source in enumerate(sources) if source == 'accounting']
    if accounting:
        lo_t, hi_t = points[accounting[0]], points[accounting[-1]]
        for row in conn.execute('''SELECT user,partition,eligible_time_ms,pending_end_ms,req_gpus
                FROM queue_accounting WHERE eligible_time_ms<=? AND pending_end_ms>? AND req_gpus>0''',
                                (hi_t - first_threshold, lo_t)):
            if not _partition_allowed(row['partition'], excluded):
                continue
            entry = _entry(row['eligible_time_ms'], row['req_gpus'], 1, None, False)
            for i in range(bisect_left(points, row['eligible_time_ms'] + first_threshold),
                           bisect_left(points, row['pending_end_ms'])):
                if sources[i] == 'accounting':
                    add_pending(i, row['user'], entry)
        for row in _running_jobs(conn, lo_t, hi_t):
            end = bisect_left(points, row['end_time_ms']) if row['end_time_ms'] is not None else n
            for i in range(bisect_left(points, row['start_time_ms']), end):
                if sources[i] == 'accounting' and pending[i] is not None:
                    add_running(i, row['user'] or 'unknown', None, row['req_gpus'])

    expected, observed, from_accounting = [0] * nb, [0] * nb, [0] * nb
    per_user = {}
    def new_stats():
        return {'wait': Counter(), 'idle': 0, 'capped': 0, 'estimate': 0, 'longest': 0,
                'asked_sum': 0, 'asked_max': 0, 'run_sum': 0, 'run_max': 0}
    people = defaultdict(new_stats)
    people_observed = defaultdict(new_stats)  # same numbers, observed samples only
    empty = {}
    for i, t in enumerate(points):
        b = bucket_of[i]
        expected[b] += 1
        source = sources[i]
        if source == 'unknown':
            continue
        (observed if source == 'observed' else from_accounting)[b] += 1
        if pending[i] is None:
            continue
        run_at = running[i] or empty
        caps, default_cap = _caps_at(schedule, t)
        for user, jobs in pending[i].items():
            run = run_at.get(user, empty)
            result = classify_person(jobs, run, t, caps, default_cap, thresholds_ms)
            targets = [people[user]] + ([people_observed[user]] if source == 'observed' else [])
            if result['capped']:
                for stats in targets:
                    stats['capped'] += 1
            if not result['waiting'][first_threshold]:
                continue
            cells = per_user.get(user)
            if cells is None:
                cells = per_user[user] = {k: [0] * nb for key in keys for k in (key, key + '_idle')}
            idle = result['running_gpus'] == 0
            for key, threshold in zip(keys, thresholds_ms):
                if result['waiting'][threshold]:
                    cells[key][b] += 1
                    if idle:
                        cells[key + '_idle'][b] += 1
            for stats in targets:
                for threshold in thresholds_ms:
                    stats['wait'][threshold] += result['waiting'][threshold]
                stats['idle'] += idle
                stats['estimate'] += source == 'accounting'
                stats['longest'] = max(stats['longest'], result['max_age_ms'])
                stats['asked_sum'] += result['asked_gpus']
                stats['asked_max'] = max(stats['asked_max'], result['asked_gpus'])
                stats['run_sum'] += result['running_gpus']
                stats['run_max'] = max(stats['run_max'], result['running_gpus'])

    current = None
    current_age = {}
    status = conn.execute('SELECT last_ms,max_gap_ms FROM queue_recorder_status WHERE id=1').fetchone()
    if status and 0 <= now_ms - status[0] <= status[1] and to_ms >= status[0]:
        as_of = status[0]
        now_pending, now_running, _ = _instant_observed(conn, as_of, excluded, first_threshold)
        counts = [0] * len(thresholds_ms)
        for user, jobs in now_pending.items():
            result = classify_person(jobs, dict(now_running.get(user, {})), as_of,
                                     *_caps_at(schedule, as_of), thresholds_ms)
            for k, threshold in enumerate(thresholds_ms):
                counts[k] += result['waiting'][threshold]
            if result['waiting'][first_threshold]:
                current_age[user] = result['max_age_ms']
        current = {'as_of_ms': as_of}
        for key, count in zip(keys, counts):
            current['people_' + key[1:] + 'h'] = count

    hours = step / H

    def numbers(stats):
        waited = stats['wait'][first_threshold]
        return {
            'waiting_h': round(waited * hours, 4),
            'waiting_24h_h': round(stats['wait'][thresholds_ms[-1]] * hours, 4),
            'idle_h': round(stats['idle'] * hours, 4),
            'capped_h': round(stats['capped'] * hours, 4),
            'longest_wait_h': round(stats['longest'] / H, 4) if waited else None,
            'avg_asked_gpus': round(stats['asked_sum'] / waited, 2) if waited else None,
            'max_asked_gpus': stats['asked_max'] if waited else None,
            'avg_running_gpus': round(stats['run_sum'] / waited, 2) if waited else None,
            'max_running_gpus': stats['run_max'] if waited else None,
            'estimate_h': round(stats['estimate'] * hours, 4),
        }

    rows = []
    for user, stats in people.items():
        if not stats['wait'][first_threshold] and not stats['capped']:
            continue
        row = {'user': user, **numbers(stats)}
        row['current_wait_h'] = (round(current_age[user] / H, 4)
                                 if current is not None and user in current_age else None)
        row['observed'] = numbers(people_observed[user]) if user in people_observed else numbers(new_stats())
        rows.append(row)
    rows.sort(key=lambda row: (-row['waiting_h'], row['user']))

    return {
        'methodology': 'waiting_people_v1',
        'thresholds_h': list(sorted(thresholds_h)),
        'step_ms': step,
        'bucket_ms': bucket_ms,
        'buckets': buckets,
        'samples': {'expected': expected, 'observed': observed, 'accounting': from_accounting},
        'users': per_user,
        'people': rows,
        'current': current,
        'rules': {'cluster_reasons': sorted(CLUSTER_REASONS),
                  'caps': _caps_at(schedule, to_ms)[0], 'default_cap': _caps_at(schedule, to_ms)[1],
                  'cap_history': schedule,
                  'excluded_partitions': list(excluded), 'unmapped_qos': sorted(unmapped)},
        'unclassified_reasons': {k: v for k, v in sorted(unclassified.items()) if v},
    }
