"""Scoped, evidence-backed terminal-job recovery for a monitoring outage.

Preparation reads a consistent backup and projected sacct evidence. Apply uses
an explicit manifest and optimistic row guards, preserving attribution/telemetry.
No scheduler changes and no imports of the serving Flask application.
"""
from pathlib import Path
import argparse
import collections
import datetime as dt
import hashlib
import json
import sqlite3
import time

START = 0
END = 0
TERMINAL = {'COMPLETED', 'FAILED', 'CANCELLED', 'TIMEOUT', 'OUT_OF_MEMORY',
            'NODE_FAIL', 'PREEMPTED', 'BOOT_FAIL', 'DEADLINE', 'REVOKED'}


def identity(j):
    array = j['array']
    if array['job_id']:
        task = array['task_id']
        if not task['set'] or task['infinite']:
            return None
        return str(array['job_id'])+'_'+str(task['number'])
    return str(j['job_id'])


def counters(items):
    return {(x['type'], x['name']): x['count'] for x in items or [] if x['count'] >= 0}


def gpu(t):
    if ('gres', 'gpu') in t:
        return t['gres', 'gpu']
    return sum(v for (kind, name), v in t.items() if kind=='gres' and name.startswith('gpu:'))


def digest_tables(conn, tables):
    result = {}
    for name in tables:
        h = hashlib.sha256()
        for row in conn.execute('SELECT * FROM "'+name+'" ORDER BY 1,2'):
            h.update(json.dumps(tuple(row), separators=(',', ':')).encode())
        result[name] = h.hexdigest()
    return result


def prepare(folder, db_path=None):
    evidence = json.loads((folder/'accounting-projected.json').read_text())
    conn = sqlite3.connect('file:' + str(db_path or folder/'before.db') + '?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    groups = collections.defaultdict(list)
    skips = collections.Counter()
    for j in evidence:
        name = identity(j)
        if name is None:
            skips['unexpanded_array_or_invalid_task'] += 1
            continue
        submit = j['time']['submission']
        if not submit or submit*1000 > END:
            skips['outside_gap_or_missing_submit'] += 1
            continue
        groups[name, submit*1000].append(j)
    inserts, updates = [], []
    states = collections.Counter()
    for (name, submit), jobs in sorted(groups.items()):
        # Multiple actual starts or active/terminal versions imply an ambiguous run.
        actual_starts = {j['time']['start'] for j in jobs if j['time']['start']}
        states_here = {j['state']['current'][0] for j in jobs}
        if len(actual_starts)>1 or (states_here & TERMINAL and states_here-TERMINAL):
            skips['ambiguous_requeue_or_run'] += 1
            continue
        terminal = [j for j in jobs if j['state']['current'][0] in TERMINAL]
        if not terminal:
            skips['active_jobs_preserved'] += 1
            continue
        signatures = {json.dumps({k:j[k] for k in ['name','user','partition','state','time','nodes','tres','required']},sort_keys=True)
                      for j in terminal}
        if len(signatures)>1:
            skips['conflicting_accounting_duplicates'] += 1
            continue
        j = terminal[0]
        start = (j['time']['start'] or 0)*1000 or None
        end = (j['time']['end'] or 0)*1000 or None
        if not end or end<START or (start and (start>end or start>=END)):
            skips['outside_gap_or_invalid_times'] += 1
            continue
        state = j['state']['current'][0]
        requested = counters(j['tres']['requested'])
        allocated = counters(j['tres']['allocated'])
        reqcpu = requested.get(('cpu',''), j['required']['CPUs'] or 0)
        reqgpu = gpu(requested)
        allocations = []
        node = j['nodes'] or ''
        # All observed allocated records in this outage are single-node. Refuse
        # to divide resources across nodes without per-node accounting evidence.
        if start:
            if j['allocation_nodes'] != 1 or not node or node in {'None assigned','(null)','Unknown'} or any(x in node for x in '[], '):
                skips['unsupported_or_missing_node_allocation'] += 1
                continue
            allocations = [dict(node=node, gpus=gpu(allocated), cpus=allocated.get(('cpu',''), reqcpu))]
        limit = j['time']['limit']
        priority = j['priority']
        record = dict(job_id=name, user=j['user'], job_name=j['name'], state=state,
                      start_time_ms=start, end_time_ms=end, submit_time_ms=submit,
                      req_gpus=reqgpu, req_cpus=reqcpu,
                      req_cpuonly=requested.get(('gres','cpuonly'),0),
                      req_mem=requested.get(('mem',''),0),
                      time_limit_ms=limit['number']*60000 if limit['set'] and not limit['infinite'] else None,
                      priority=priority['number'] if priority['set'] and not priority['infinite'] else 0,
                      array_task_count=1, allocations_json=json.dumps(allocations),
                      pending_reason='', partition=j['partition'], last_seen_ms=int(time.time()*1000))
        existing = conn.execute('select * from jobs where job_id=? and submit_time_ms=?',(name,submit)).fetchone()
        if existing is None:
            inserts.append(record)
            states[state] += 1
            continue
        old = dict(existing)
        if old['user'] != record['user'] or (old['start_time_ms'] is not None and old['start_time_ms']!=start):
            skips['existing_identity_or_start_conflict'] += 1
            continue
        changes = {}
        if old['state']=='FINISHED_UNKNOWN' or (old['state'] in {'RUNNING','PENDING','COMPLETING'} and old['last_seen_ms']<=END):
            changes.update(state=state, start_time_ms=start, end_time_ms=end, pending_reason='')
        elif old['state'] != state or old['end_time_ms'] != end:
            skips['existing_authoritative_or_fresh_state_preserved'] += 1
            continue
        if not json.loads(old['allocations_json'] or '[]') and allocations:
            changes['allocations_json'] = record['allocations_json']
        if not old['partition'] and record['partition']:
            changes['partition'] = record['partition']
        changes = {k:v for k,v in changes.items() if old[k]!=v}
        if changes:
            updates.append(dict(before=old, changes=changes))
        else:
            skips['already_complete'] += 1
    manifest = dict(gap_start_ms=START, gap_end_ms=END,
                    inserts=inserts, updates=updates,
                    summary=dict(insert_count=len(inserts),update_count=len(updates),
                                 inserted_states=dict(states), skipped=dict(skips)))
    (folder/'manifest.json').write_text(json.dumps(manifest,indent=2))
    conn.close()
    print(json.dumps(manifest['summary'],indent=2))


def apply(folder, path):
    manifest = json.loads((folder/'manifest.json').read_text())
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('BEGIN IMMEDIATE')
    counts = collections.Counter()
    try:
        for item in manifest['inserts']:
            if conn.execute('select 1 from jobs where job_id=? and submit_time_ms=?',
                            (item['job_id'],item['submit_time_ms'])).fetchone():
                counts['insert_skipped_existing'] += 1
                continue
            cols = list(item)
            conn.execute('insert into jobs ('+','.join(cols)+') values ('+','.join('?' for _ in cols)+')',
                         [item[k] for k in cols])
            counts['inserted'] += 1
        for item in manifest['updates']:
            old, changes = item['before'], item['changes']
            current = conn.execute('select * from jobs where id=?',(old['id'],)).fetchone()
            if current is None or dict(current)!=old:
                counts['update_skipped_concurrent_change'] += 1
                continue
            conn.execute('update jobs set '+','.join(k+'=?' for k in changes)+' where id=?',
                         list(changes.values())+[old['id']])
            counts['updated'] += 1
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    print(json.dumps(dict(counts)))
    return dict(counts)


def verify(folder, path):
    before = sqlite3.connect(folder/'before.db');before.row_factory=sqlite3.Row
    after = sqlite3.connect(path);after.row_factory=sqlite3.Row
    manifest = json.loads((folder/'manifest.json').read_text())
    proposed = {x['before']['id']: x['changes'] for x in manifest['updates']}
    altered = 0
    for row in before.execute('select * from jobs order by id'):
        old = dict(row)
        actual = after.execute('select * from jobs where id=?',(old['id'],)).fetchone()
        assert actual is not None
        expected = {**old, **proposed.get(old['id'],{})}
        assert dict(actual)==expected, ('Unexpected change to existing job',old['id'])
        altered += dict(actual)!=old
    tables = [r[0] for r in before.execute("select name from sqlite_master where type='table' and name not in ('jobs','sqlite_sequence')")]
    assert digest_tables(before,tables)==digest_tables(after,tables), 'Unrelated table modified'
    assert after.execute('select count(*) from jobs').fetchone()[0]==before.execute('select count(*) from jobs').fetchone()[0]+len(manifest['inserts'])
    check = after.execute('pragma integrity_check').fetchone()[0]
    assert check=='ok',check
    print(json.dumps(dict(existing_jobs_verified=True,existing_jobs_changed=altered,
                         unrelated_tables_unchanged=tables, integrity=check)))
