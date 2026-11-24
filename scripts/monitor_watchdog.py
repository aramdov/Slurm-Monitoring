"""Unprivileged dashboard watchdog; durable gap queue, backoff and backfill."""
from pathlib import Path
from contextlib import closing
import argparse
import fcntl
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.request


def observe(state, timestamp):
    previous=state.get('last_meta_ms',0)
    start=state.get('outage_start_ms') or previous
    if timestamp>previous:
        if start and (state.get('outage_start_ms') or timestamp-previous>60000):
            key=f'{start}-{timestamp}'
            if not any(g['key']==key for g in state['gaps']):
                state['gaps'].append(dict(key=key,start_ms=start,end_ms=timestamp,
                                          passes=0,failures=0,next_try=time.time()+15))
        state['last_meta_ms']=timestamp
        state.pop('outage_start_ms',None)


def completed(gap, success, now):
    if success:
        gap['passes']+=1
        gap['failures']=0
        gap['next_try']=now+(300 if gap['passes']==1 else 900)
    else:
        gap['failures']+=1
        gap['next_try']=now+min(1800,30*2**min(gap['failures'],6))


def main(root, interval):
    os.umask(0o077)
    data=root/'data'
    data.mkdir(exist_ok=True)
    lock=(data/'watchdog.lock').open('a')
    try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError: return
    path=data/'watchdog-state.json'
    state=json.loads(path.read_text()) if path.exists() else {'gaps':[]}
    log=(data/'watchdog.log').open('a',buffering=1)
    def record(event,**values):
        try:
            print(json.dumps(dict(time=time.time(),event=event,**values)),file=log,flush=True)
        except OSError:
            pass  # Preserve supervision even if the filesystem/log is unavailable.
    def save():
        tmp=path.with_suffix('.json.partial')
        tmp.write_text(json.dumps(state,indent=2));tmp.replace(path)
    worker=None;active=None;recovery_log=None
    restart_at=0;restart_failures=0;bad_http=0
    record('watchdog_started',pid=os.getpid())
    while True:
        try:
            paused=(data/'watchdog.paused').exists()
            if worker and worker.poll() is not None:
                success=worker.returncode==0 and (active['output']/'result.json').exists()
                completed(active['gap'],success,time.time())
                record('recovery_finished',gap=active['gap']['key'],success=success,
                       passes=active['gap']['passes'],exit_code=worker.returncode)
                recovery_log.close();worker=None;active=None
                save()
            if paused:
                if state.get('last_meta_ms'):
                    state.setdefault('outage_start_ms',state['last_meta_ms'])
                    save()
                if worker:
                    worker.terminate();worker.wait(timeout=15)
                time.sleep(interval)
                continue
            meta=0;healthy=False
            try:
                with closing(sqlite3.connect('file:'+str(data/'slurm_history.db')+'?mode=ro',uri=True,timeout=2)) as conn:
                    row=conn.execute('select updated_at_ms from meta where id=1').fetchone()
                    meta=row[0] if row else 0
                with urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('SLURM_DASHBOARD_PORT','19526')+'/api/config',timeout=3) as response:
                    healthy=response.status==200
            except Exception:
                pass
            if healthy and meta and time.time()*1000-meta<900000:
                previous=state.get('last_meta_ms',0)
                marker=data/'watchdog.recovery-needed'
                if marker.exists() and previous:
                    state.setdefault('outage_start_ms',previous)
                observe(state,meta)
                if meta>previous and marker.exists():
                    save()
                    marker.unlink(missing_ok=True)
                restart_failures=0;bad_http=0
            else:
                if state.get('last_meta_ms'):
                    state.setdefault('outage_start_ms',state['last_meta_ms'])
                bad_http+=1
                session=subprocess.run(['tmux','-L','slurm-monitoring','has-session','-t','dashboard'],
                                       stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0
                if time.time()>=restart_at and (not session or bad_http>=3):
                    if session:
                        stopped=subprocess.run([str(root/'monitor-tmux.sh'),'stop'],
                                               stdout=subprocess.DEVNULL,stderr=log,timeout=20)
                        if stopped.returncode: raise RuntimeError('Monitor stop failed')
                        # Resume after this watchdog's own maintenance stop.
                        (data/'watchdog.paused').unlink(missing_ok=True)
                    result=subprocess.run([str(root/'monitor-tmux.sh'),'start'],
                                          stdout=subprocess.DEVNULL,stderr=log,timeout=20)
                    restart_failures+=1
                    restart_at=time.time()+min(300,15*2**min(restart_failures-1,5))
                    record('restart_attempt',exit_code=result.returncode)
            save()
            # Delayed repeated passes cover accounting propagation delay. The
            # child is independent, so slow accounting never blocks supervision.
            pending=[g for g in state['gaps'] if g['passes']<3 and g['next_try']<=time.time()]
            if healthy and worker is None and pending:
                gap=pending[0]
                output=data/'auto-recovery'/gap['key']/str(time.time_ns())
                output.mkdir(parents=True,mode=0o700)
                recovery_log=(output/'worker.log').open('a')
                worker=subprocess.Popen([sys.executable,str(root/'scripts/auto_recover_jobs.py'),
                    '--root',str(root),'--from-ms',str(gap['start_ms']),
                    '--to-ms',str(gap['end_ms']),'--output',str(output)],
                    stdout=recovery_log,stderr=subprocess.STDOUT)
                active={'gap':gap,'output':output}
                record('recovery_started',gap=gap['key'],pid=worker.pid)
        except Exception as exc:
            record('watchdog_error',kind=type(exc).__name__,message=str(exc)[:200])
            restart_failures+=1
            restart_at=max(restart_at,time.time()+min(300,15*2**min(restart_failures-1,5)))
        time.sleep(interval)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--interval',type=float,default=15)
    args=parser.parse_args()
    if args.interval<1:parser.error('Interval must be >=1 second')
    main(args.root.resolve(),args.interval)
