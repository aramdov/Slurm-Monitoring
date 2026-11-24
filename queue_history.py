"""Observed queue segments, with explicit sampling bounds and outage breaks.

No interval before first_ms or after last_ms is established by an observation.
Changes occurred somewhere between previous_last_ms and first_ms. An unchanged
segment is a polling estimate: changes entirely between polls remain invisible.
Raw array identities are retained; consumers must resolve task membership before
combining changing compressed ranges with individual task records.
"""
import json
import uuid
import pending_age

SESSION = uuid.uuid4().hex


def init(conn):
    pending_age.init(conn)
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS queue_observation_segments (
            id INTEGER PRIMARY KEY,
            job_id TEXT NOT NULL, submit_time_ms INTEGER NOT NULL,
            first_ms INTEGER NOT NULL, last_ms INTEGER NOT NULL,
            previous_last_ms INTEGER,
            session TEXT NOT NULL, signature TEXT NOT NULL,
            state TEXT NOT NULL, reason TEXT NOT NULL,
            eligible_time_ms INTEGER, dependency TEXT,
            details_observed INTEGER NOT NULL,
            start_time_ms INTEGER, partition TEXT,
            req_gpus INTEGER, req_cpus INTEGER, array_task_count INTEGER,
            observations INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_queue_identity_time
            ON queue_observation_segments(job_id, submit_time_ms, first_ms);
        CREATE INDEX IF NOT EXISTS idx_queue_last
            ON queue_observation_segments(last_ms);
        CREATE TABLE IF NOT EXISTS queue_observation_heads (
            job_id TEXT NOT NULL, submit_time_ms INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            PRIMARY KEY(job_id, submit_time_ms)
        ) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS queue_recorder_status (
            id INTEGER PRIMARY KEY CHECK(id=1),
            session TEXT NOT NULL, last_ms INTEGER NOT NULL,
            max_gap_ms INTEGER NOT NULL, successful_polls INTEGER NOT NULL
        );
    ''')


def record(conn, jobs, timestamp_ms, max_gap_ms=90000):
    """Call atomically with each successful squeue snapshot, including empty ones."""
    heads = {(j, s): (i, sig, last, session) for j, s, i, sig, last, session in
             conn.execute('SELECT h.job_id,h.submit_time_ms,s.id,s.signature,s.last_ms,s.session '
                          'FROM queue_observation_heads h JOIN queue_observation_segments s '
                          'ON s.id=h.segment_id')}
    seen = set()
    for job in jobs:
        key = (str(job.get('job_id', '')), job.get('submit_time'))
        if not key[0] or not isinstance(key[1], int) or key[1] <= 0:
            continue
        if key in seen:
            raise ValueError('Duplicate queue identity in one snapshot')
        seen.add(key)
        values = (job.get('state') or '', job.get('pending_reason') or '',
                  job.get('eligible_time'), job.get('dependency'),
                  int(bool(job.get('queue_details_observed'))), job.get('start_time'),
                  job.get('partition') or '', job.get('queue_req_gpus', job.get('req_gpus', 0)),
                  job.get('req_cpus', 0), job.get('array_task_count', 1))
        signature = json.dumps(values, separators=(',', ':'))
        previous = heads.get(key)
        contiguous = previous and previous[3] == SESSION and 0 < timestamp_ms-previous[2] <= max_gap_ms
        if previous and timestamp_ms <= previous[2]:
            raise ValueError('Non-monotonic queue observation')
        if contiguous and previous[1] == signature:
            conn.execute('UPDATE queue_observation_segments SET last_ms=?,observations=observations+1 WHERE id=?',
                         (timestamp_ms, previous[0]))
            continue
        cursor = conn.execute('''INSERT INTO queue_observation_segments
            (job_id,submit_time_ms,first_ms,last_ms,previous_last_ms,session,signature,
             state,reason,eligible_time_ms,dependency,details_observed,start_time_ms,
             partition,req_gpus,req_cpus,array_task_count,observations)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)''',
            (*key, timestamp_ms, timestamp_ms, previous[2] if contiguous else None,
             SESSION, signature, *values))
        conn.execute('INSERT OR REPLACE INTO queue_observation_heads VALUES (?,?,?)',
                     (*key, cursor.lastrowid))
    conn.executemany('DELETE FROM queue_observation_heads WHERE job_id=? AND submit_time_ms=?',
                     [key for key in heads if key not in seen])
    prior = conn.execute('SELECT session,last_ms FROM queue_recorder_status WHERE id=1').fetchone()
    window = conn.execute('SELECT id,session,last_ms FROM queue_observation_windows ORDER BY id DESC LIMIT 1').fetchone()
    if (prior and window and prior[0] == SESSION == window[1] and
            window[2] == prior[1] and 0 < timestamp_ms-prior[1] <= max_gap_ms):
        conn.execute('UPDATE queue_observation_windows SET last_ms=?,polls=polls+1 WHERE id=?',
                     (timestamp_ms,window[0]))
    else:
        conn.execute('INSERT INTO queue_observation_windows(session,first_ms,last_ms,polls) VALUES (?,?,?,1)',
                     (SESSION,timestamp_ms,timestamp_ms))
    conn.execute('''INSERT INTO queue_recorder_status VALUES (1,?,?,?,1)
                    ON CONFLICT(id) DO UPDATE SET session=excluded.session,
                    last_ms=excluded.last_ms,max_gap_ms=excluded.max_gap_ms,
                    successful_polls=queue_recorder_status.successful_polls+1''',
                 (SESSION, timestamp_ms, max_gap_ms))
