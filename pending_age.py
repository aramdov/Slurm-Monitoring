"""Point-in-time pending ages. Accounting estimates and observed coverage stay distinct."""
from bisect import bisect_left
from collections import Counter
import time

CLASSES = ('cpu_only', 'gpu_1_7', 'gte8')


def init(conn):
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS queue_accounting (
            job_id TEXT NOT NULL, submit_time_ms INTEGER NOT NULL,
            user TEXT NOT NULL, job_name TEXT, partition TEXT,
            eligible_time_ms INTEGER NOT NULL, pending_end_ms INTEGER NOT NULL,
            state TEXT NOT NULL, req_gpus INTEGER NOT NULL,
            cutoff_ms INTEGER NOT NULL,
            PRIMARY KEY(job_id,submit_time_ms)
        ) WITHOUT ROWID;
        CREATE INDEX IF NOT EXISTS idx_queue_accounting_interval
            ON queue_accounting(pending_end_ms,eligible_time_ms);
        CREATE TABLE IF NOT EXISTS queue_accounting_meta (
            id INTEGER PRIMARY KEY CHECK(id=1),
            cohort_start_ms INTEGER NOT NULL, cutoff_ms INTEGER NOT NULL,
            imported_rows INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS queue_observation_windows (
            id INTEGER PRIMARY KEY, session TEXT NOT NULL,
            first_ms INTEGER NOT NULL,last_ms INTEGER NOT NULL,polls INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_queue_window_time
            ON queue_observation_windows(last_ms,first_ms);
    ''')
    columns = {row[1] for row in conn.execute('PRAGMA table_info(queue_accounting)')}
    if 'req_cpus' not in columns:
        conn.execute('ALTER TABLE queue_accounting ADD COLUMN req_cpus INTEGER')


def blocked(reason, dependency):
    reason = (reason or '').lower()
    dependency = (dependency or '').lower()
    return (reason.startswith('dependency') or 'held' in reason or
            '(unfulfilled)' in dependency or '(failed)' in dependency)


def get_buckets(conn, from_ms, to_ms, group_by, resolve_group, now_ms=None):
    """Return per-group weighted age samples, so any visible selection pools exactly.

    Each bucket is sampled at its right edge, not averaged over time. Every job
    is counted once at a sample; array ranges are weighted by task count and
    classified by requested GPUs PER TASK. Never substitute Submit for Eligible.
    """
    now_ms = int(time.time()*1000) if now_ms is None else now_ms
    to_ms = min(to_ms, now_ms)
    duration = max(0, to_ms - from_ms)
    count = min(360, max(1, duration // 60_000)) if duration else 0
    step = (duration + count - 1) // count if count else 0
    points = [from_ms + duration * i // count - 1 for i in range(1, count + 1)]
    n = len(points)
    status = conn.execute('SELECT last_ms,max_gap_ms FROM queue_recorder_status WHERE id=1').fetchone()
    if points and status and status[0] <= points[-1] and points[-1]-status[0] <= status[1]:
        # The final point is the latest completed poll, never an extrapolation.
        if status[0] >= from_ms and (len(points) == 1 or status[0] > points[-2]):
            points[-1] = status[0]
    meta = conn.execute('SELECT cohort_start_ms,cutoff_ms FROM queue_accounting_meta WHERE id=1').fetchone()
    segments = conn.execute('''SELECT s.*,j.user,j.job_name FROM queue_observation_segments s
        LEFT JOIN jobs j ON j.job_id=s.job_id AND j.submit_time_ms=s.submit_time_ms
        WHERE s.first_ms<? AND s.last_ms>=?''',(to_ms,from_ms)).fetchall()
    windows = [(r['first_ms'],r['last_ms']) for r in segments]
    windows += [tuple(r) for r in conn.execute('SELECT first_ms,last_ms FROM queue_observation_windows '
                                             'WHERE first_ms<? AND last_ms>=?',(to_ms,from_ms))]
    # Old segments provide evidence before explicit empty-queue coverage windows
    # were introduced. Union ranges; never min/max across disjoint outages.
    merged = []
    for lo,hi in sorted(windows):
        if merged and lo<=merged[-1][1]: merged[-1][1]=max(hi,merged[-1][1])
        else: merged.append([lo,hi])
    sources=[]
    cursor=0
    for t in points:
        while cursor<len(merged) and merged[cursor][1]<t: cursor+=1
        observed=cursor<len(merged) and merged[cursor][0]<=t<=merged[cursor][1]
        sources.append('observed' if observed else 'accounting' if meta and meta[0]<=t<=meta[1] else 'unknown')

    samples, gpu_samples, cpu_samples, counts, colors = {}, {}, {}, {}, {}
    def add(row, lo, hi, eligible, total_gpus, weight, source):
        if not isinstance(eligible,int) or eligible<row['submit_time_ms'] or weight<=0:
            return
        gpus_per_task = total_gpus / weight
        prefix = 'gte8' if gpus_per_task >= 8 else 'gpu_1_7' if gpus_per_task >= 1 else 'cpu_only'
        first=bisect_left(points,max(lo,eligible))
        last=bisect_left(points,hi)  # end is exclusive
        for i in range(first,last):
            if sources[i]!=source: continue
            user=row['user'] or 'unknown'
            if group_by=='user': key,color=user,None
            else: key,color=resolve_group(user,points[i],row['job_name'] or '')
            if key not in samples:
                samples[key]={c:[Counter() for _ in points] for c in CLASSES}
                counts[key]=[0]*n
            age = points[i] - eligible
            samples[key][prefix][i][age] += weight
            if total_gpus:
                group = gpu_samples.setdefault(key, {})
                if prefix not in group:
                    group[prefix] = [Counter() for _ in points]
                group[prefix][i][age] += total_gpus
            if prefix == 'cpu_only' and row['req_cpus']:
                if key not in cpu_samples:
                    cpu_samples[key] = [Counter() for _ in points]
                cpu_samples[key][i][age] += row['req_cpus']
            counts[key][i]+=weight
            if color: colors[key]=color

    if points:
        for row in conn.execute('SELECT * FROM queue_accounting WHERE eligible_time_ms<=? AND pending_end_ms>?',
                                (points[-1],points[0])):
            add(row,row['eligible_time_ms'],row['pending_end_ms'],row['eligible_time_ms'],row['req_gpus'],1,'accounting')
        for row in segments:
            if row['state']!='PENDING' or not row['details_observed'] or blocked(row['reason'],row['dependency']):
                continue
            weight=max(1,row['array_task_count'] or 1)
            add(row,row['first_ms'],row['last_ms']+1,row['eligible_time_ms'],(row['req_gpus'] or 0),weight,'observed')
    encoded={key:{c:[sorted(v.items()) for v in series[c]] for c in CLASSES} for key,series in samples.items()}
    return {'methodology':'pending_age_since_eligible_v1', 'buckets':points,'bucket_size_ms':step,
            'age_samples':encoded,
            'gpu_age_samples':{key:{category:[sorted(v.items()) for v in values]
                                   for category,values in series.items()}
                               for key,series in gpu_samples.items()},
            'cpu_age_samples':{key:{'cpu_only':[sorted(v.items()) for v in values]}
                               for key,values in cpu_samples.items()},
            'users':counts,'colors':colors,'group_by':group_by,
            'sources':sources,'accounting_cutoff_ms':meta[1] if meta else None,
            'quantiles':{'median':'middle, averaging two middle jobs for even counts','p90':'nearest rank, ceil(0.9*N)'},
            'excluded':'unresolved dependencies, holds, missing eligibility and ambiguous accounting requeues'}
