"""Private projected accounting backfill for derived pending-age charts only."""
import argparse
from collections import Counter,defaultdict
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time

os.umask(0o077)
parser=argparse.ArgumentParser()
parser.add_argument('--apply',action='store_true')
parser.add_argument('--cached',action='store_true')
parser.add_argument('--output-dir',default='data/queue-accounting-import')
parser.add_argument('--db',default='data/slurm_history.db')
args=parser.parse_args()
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
root=Path(args.output_dir).resolve()
root.mkdir(parents=True, exist_ok=True, mode=0o700)
with sqlite3.connect('file:'+args.db+'?mode=ro',uri=True) as c:
    cohort=c.execute('SELECT min(submit_time_ms) FROM jobs WHERE submit_time_ms>0').fetchone()[0]
cutoff=int(time.time()*1000)//1000*1000
def iso(ms): return datetime.fromtimestamp(ms/1000,timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')
if args.cached:
    settings=json.loads((root/'query.json').read_text());cohort=settings['cohort_ms'];cutoff=settings['cutoff_ms']
    output=(root/'accounting.psv').read_text()
else:
    fields='JobID%100,User%100,Submit,Eligible,Start,End,State%40,ReqTRES%1000,Partition%100,JobName%1024'
    command=['sacct','-a','-X','-D','--array','-n','-P','-S',iso(cohort),'-E',iso(cutoff),'--format='+fields]
    output=subprocess.run(command,env=dict(os.environ,TZ='UTC',SLURM_TIME_FORMAT='standard'),
                          capture_output=True,text=True,check=True,timeout=300).stdout
    (root/'query.json').write_text(json.dumps({'cohort_ms':cohort,'cutoff_ms':cutoff,'command':command},indent=2))
    (root/'accounting.psv').write_text(output)

def timestamp(value):
    try:
        t=int(datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp())*1000
        return t if 0<t<=cutoff else None
    except ValueError: return None

records=defaultdict(set);submissions=defaultdict(set);uncertain=set();stats=Counter()
for line in output.splitlines():
    fields=[v.strip() for v in line.split('|',9)]
    if len(fields)!=10:
        stats['malformed_rows']+=1;continue
    jid,user,submit,eligible,start,end,state,tres,partition,name=fields
    sub=timestamp(submit)
    if not re.fullmatch(r'\d+(?:_\d+)?',jid) or sub is None or sub<cohort:
        stats['outside_cohort_or_unsupported_id']+=1;continue
    records[jid,sub].add((user,timestamp(eligible),timestamp(start),timestamp(end),state,tres,partition,name))
    submissions[jid].add(sub)
    if 'REQUEU' in state:uncertain.add(jid)
for (jid,sub),variants in records.items():
    if len(variants)!=1 or len(submissions[jid])!=1:uncertain.add(jid)

rows=[]
for (jid,sub),variants in records.items():
    if jid in uncertain:stats['ambiguous_or_requeue_identities']+=1;continue
    user,eligible,start,end,state,tres,partition,name=next(iter(variants))
    if eligible is None or eligible<sub:stats['missing_or_invalid_eligible']+=1;continue
    if start is not None:
        if start<eligible or (end is not None and end<start):stats['invalid_start_order']+=1;continue
        pending_end=start
    elif state.startswith('COMPLETED'):
        stats['completed_missing_start']+=1;continue
    elif end is not None:
        pending_end=end
    elif state.startswith('PENDING'):
        pending_end=cutoff+1
    else:stats['unknown_pending_end']+=1;continue
    if pending_end<eligible:stats['invalid_end_order']+=1;continue
    resources={}
    for token in tres.split(','):
        k,sep,v=token.partition('=')
        if sep and v.isdigit():resources[k]=int(v)
    gpus=resources.get('gres/gpu',sum(v for k,v in resources.items() if k.startswith('gres/gpu:')))
    rows.append((jid,sub,user,name,partition,eligible,pending_end,state,gpus,cutoff,resources.get('cpu')))
    stats['importable_identities']+=1
    stats['started' if start is not None else 'never_started']+=1
    stats['zero_pending_interval']+=int(eligible==pending_end)
report={'cohort_start_utc':iso(cohort),'cutoff_utc':iso(cutoff),'stats':dict(stats),'applied':False}
if args.apply:
    # Point only this new derived table at the new snapshot. Original jobs,
    # observations, projects, assignments and allocation tables are untouched.
    import pending_age
    with sqlite3.connect(args.db,timeout=30) as c:
        pending_age.init(c)
        c.execute('BEGIN IMMEDIATE')
        c.execute('DELETE FROM queue_accounting')
        c.executemany('''INSERT INTO queue_accounting
            (job_id,submit_time_ms,user,job_name,partition,eligible_time_ms,pending_end_ms,state,req_gpus,cutoff_ms,req_cpus)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)''', rows)
        c.execute('INSERT OR REPLACE INTO queue_accounting_meta VALUES (1,?,?,?)',(cohort,cutoff,len(rows)))
        c.commit()
    report['applied']=True
(root/'import-report.json').write_text(json.dumps(report,indent=2))
print(json.dumps(report,indent=2))
