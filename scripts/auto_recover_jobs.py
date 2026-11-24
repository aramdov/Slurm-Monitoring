"""Recover a bounded monitoring gap from accounting, never modifying Slurm."""
from pathlib import Path
import argparse
import datetime as dt
import fcntl
import json
import os
import shutil
import sqlite3
import subprocess
import time

import _job_recovery as recovery


def run(root, start, end, output):
    os.umask(0o077)
    with (root/'data/auto-recovery.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        return _run_locked(root,start,end,output)


def _run_locked(root, start, end, output):
    if not 0 < start < end <= int(time.time()*1000)+60000:
        raise ValueError('Invalid recovery interval')
    os.umask(0o077)
    with open('/proc/sys/fs/file-nr') as pressure:
        allocated,unused,maximum=map(int,pressure.read().split())
    if (allocated-unused)/maximum>=0.8:
        raise RuntimeError('Deferring accounting recovery during file-table pressure')
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    keys = ['job_id','array','name','user','partition','state','time',
            'allocation_nodes','nodes','tres','required','priority']
    evidence = []
    # Bound each accounting query; duplicate overlap is reviewed by the planner.
    for first in range(start, end, 86400000):
        last = min(first+86400000, end)
        stamp = lambda v: dt.datetime.fromtimestamp(v/1000,dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')
        result = subprocess.run(['sacct','-a','-X','-D','--array','--json',
                                 '-S',stamp(first-1000),'-E',stamp(last+1000)],
                                env={**os.environ,'TZ':'UTC'},capture_output=True,
                                text=True,timeout=180)
        if result.returncode:
            raise RuntimeError('Accounting query failed, exit '+str(result.returncode))
        document = json.loads(result.stdout)
        if document.get('errors'):
            raise RuntimeError('Accounting reported errors')
        if 'jobs' not in document:
            raise RuntimeError('Accounting response missing jobs')
        evidence.extend({k:j.get(k) for k in keys} for j in document['jobs'])
    (output/'accounting-projected.json').write_text(json.dumps(evidence))
    db = root/'data/slurm_history.db'
    recovery.START, recovery.END = start, end
    recovery.prepare(output, db)
    manifest = json.loads((output/'manifest.json').read_text())
    planned = len(manifest['inserts'])+len(manifest['updates'])
    if planned:
        backups = root/'data/auto-recovery/backups'
        backups.mkdir(parents=True, exist_ok=True, mode=0o700)
        if shutil.disk_usage(db.parent).free < db.stat().st_size*2+512*1024**2:
            raise RuntimeError('Insufficient free space for safe recovery backup')
        name = 'before-'+str(time.time_ns())+'.db'
        temp = backups/(name+'.partial')
        source = sqlite3.connect('file:'+str(db)+'?mode=ro',uri=True,timeout=30)
        target = sqlite3.connect(temp)
        try:
            source.backup(target,pages=512)
        finally:
            target.close();source.close()
        temp.rename(backups/name)
        # Rotate only generated automatic backups, never manual repair backups.
        for old in sorted(backups.glob('before-*.db'),key=lambda p:p.name)[:-3]:
            old.unlink()
        applied = recovery.apply(output,db)
    else:
        applied = {}
    result = dict(start_ms=start,end_ms=end,accounting_records=len(evidence),
                  planned=manifest['summary'],applied=applied)
    temp = output/'result.json.partial'
    temp.write_text(json.dumps(result,indent=2));temp.replace(output/'result.json')
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--from-ms',type=int,required=True)
    parser.add_argument('--to-ms',type=int,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    print(json.dumps(run(args.root,args.from_ms,args.to_ms,args.output)))
