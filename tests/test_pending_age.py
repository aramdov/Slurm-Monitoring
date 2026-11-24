import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import history
import pending_age as p
import queue_history as q
import slurm
from unittest.mock import patch

H=3600000


class PendingAge(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.db=str(Path(self.temp.name)/'data.db')
        history.init_db(self.db)
        self.c=sqlite3.connect(self.db)
        self.c.row_factory=sqlite3.Row
        self.c.execute('INSERT INTO queue_accounting_meta VALUES (1,1,?,0)',(10*H,))

    def tearDown(self):
        self.c.close();self.temp.cleanup()

    def account(self,jid,eligible,end,gpus=0,user='alice',name='experiment'):
        self.c.execute('INSERT INTO queue_accounting VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                       (jid,1,user,name,'research',eligible,end,'COMPLETED',gpus,10*H,32))

    def data(self,start,end,group='user',resolve=lambda user,t,name:('project','#abc')):
        return p.get_buckets(self.c,start,end,group,resolve,now_ms=10*H)

    def test_pending_population_uses_eligible_and_excludes_started_and_cancelled(self):
        self.account('1',H,5*H,8)
        self.account('2',H,2*H,8)  # started before sample
        self.account('3',2*H,4*H,0)  # cancelled later, still pending at sample
        self.account('4',4*H,6*H,8)  # dependency not released yet
        result=self.data(3*H,3*H+1)
        self.assertEqual(result['age_samples']['alice']['gte8'][0],[(2*H,1)])
        self.assertEqual(result['age_samples']['alice']['cpu_only'][0],[(H,1)])
        self.assertEqual(result['users']['alice'],[2])
        later=self.data(5*H,5*H+1)
        self.assertEqual(later['users']['alice'],[1])

    def test_observed_arrays_per_task_class_and_blocked_jobs_hidden(self):
        base=dict(job_id='10_[0-3]',submit_time=1,state='PENDING',pending_reason='Priority',
                  eligible_time=H,dependency='(null)',queue_details_observed=True,
                  req_gpus=28,array_task_count=4,user='alice')
        jobs=[base,dict(base,job_id='11',pending_reason='Dependency',req_gpus=8,array_task_count=1),
              dict(base,job_id='12',pending_reason='Priority',dependency='afterok:8(unfulfilled)'),
              dict(base,job_id='13',pending_reason='JobHeldUser'),
              dict(base,job_id='14',eligible_time=None),
              dict(base,job_id='15',queue_details_observed=False)]
        self.c.commit()
        history.upsert_jobs(self.db,jobs,2*H)
        result=self.data(2*H,2*H+1)
        self.assertEqual(result['sources'],['observed'])
        self.assertEqual(result['age_samples']['alice']['gpu_1_7'][0],[(H,4)])
        self.assertEqual(result['users']['alice'],[4])
        self.assertEqual(result['age_samples']['alice']['gte8'][0],[])

    def test_outage_gap_is_unknown_without_accounting(self):
        self.c.execute('DELETE FROM queue_accounting_meta');self.c.commit()
        job=dict(job_id='1',submit_time=1,state='RUNNING')
        q.record(self.c,[job],2000,max_gap_ms=30000)
        q.record(self.c,[job],12000,max_gap_ms=30000)
        q.record(self.c,[job],120000,max_gap_ms=30000)
        result=self.data(60000,60001)
        self.assertEqual(result['sources'],['unknown'])
        self.assertEqual(self.c.execute('SELECT count(*) FROM queue_observation_windows').fetchone()[0],2)

    def test_observed_overrides_accounting_without_double_counting(self):
        self.account('1',H,8*H,8)
        self.c.commit()
        job=dict(job_id='1',submit_time=1,state='PENDING',pending_reason='Dependency',eligible_time=H,
                 dependency='afterok:9(unfulfilled)',queue_details_observed=True,req_gpus=8,user='alice')
        history.upsert_jobs(self.db,[job],3*H)
        result=self.data(3*H,3*H+1)
        self.assertEqual(result['sources'],['observed'])
        self.assertEqual(result['users'],{})

    def test_assignment_resolved_at_each_pending_sample(self):
        self.account('1',H,8*H,8)
        calls=[]
        def resolve(user,t,name):
            calls.append((user,t,name))
            return ('old' if t<3*H else 'new','#abc')
        result=self.data(2*H,4*H+1,'project',resolve)
        self.assertEqual(set(result['users']),{'old','new'})
        self.assertTrue(all(user=='alice' and name=='experiment' for user,t,name in calls))

    def test_final_sample_uses_completed_poll_and_does_not_extend_it(self):
        self.c.execute('DELETE FROM queue_accounting_meta');self.c.commit()
        job=dict(job_id='1',submit_time=1,state='PENDING',pending_reason='Priority',eligible_time=H,
                 dependency='(null)',queue_details_observed=True,user='alice')
        history.upsert_jobs(self.db,[job],2*H)
        result=self.data(2*H,2*H+10000)
        self.assertEqual(result['buckets'],[2*H])
        self.assertEqual(result['sources'],['observed'])
        stale=self.data(2*H+100000,2*H+100001)
        self.assertEqual(stale['sources'],['unknown'])

    def test_cached_import_rejects_requeues_and_preserves_pipe_in_name(self):
        directory=Path(self.temp.name)/'import';directory.mkdir()
        source=Path(__file__).resolve().parent
        root=source.parent
        for name in ('pending_age.py',):shutil.copy2(root/name,directory.parent/name)
        shutil.copy2(root/'scripts/import_queue_accounting.py',directory/'import_accounting.py')
        # Only the existing jobs table is read. Existing jobs remain untouched.
        self.c.execute('INSERT INTO jobs(job_id,submit_time_ms,last_seen_ms) VALUES (?,?,?)',('1',1000,1000));self.c.commit()
        (directory/'query.json').write_text(json.dumps({'cohort_ms':1000,'cutoff_ms':100000}))
        (directory/'accounting.psv').write_text(
            '1|alice|1970-01-01T00:00:01|1970-01-01T00:00:02|1970-01-01T00:01:00|1970-01-01T00:01:10|COMPLETED|gres/gpu=8,gres/gpu:h100=8|research|name|with pipe\n'
            '2|alice|1970-01-01T00:00:01|Unknown|Unknown|1970-01-01T00:01:10|CANCELLED|gres/gpu=8|research|x\n'
            '3|alice|1970-01-01T00:00:01|1970-01-01T00:00:02|1970-01-01T00:01:00|1970-01-01T00:01:10|REQUEUED|gres/gpu=8|research|x\n')
        subprocess.run([sys.executable,str(directory/'import_accounting.py'),'--db',self.db,'--output-dir',str(directory),'--cached','--apply'],
                       capture_output=True,text=True,check=True)
        result=self.c.execute('SELECT job_id,job_name,req_gpus FROM queue_accounting').fetchall()
        self.assertEqual([tuple(r) for r in result],[('1','name|with pipe',8)])
        self.assertEqual(self.c.execute('SELECT count(*) FROM jobs').fetchone()[0],1)

    def test_observed_gpu_class_uses_total_tres_per_array_task(self):
        submit='2026-11-23T12:00:00'
        job=dict(job_id='42_[0-3]',submit_time=slurm._parse_ts(submit),state='PENDING',
                 req_gpus=16,array_task_count=4)
        output='JobId=42 ArrayJobId=42 ArrayTaskId=0-3 SubmitTime='+submit+' JobState=PENDING Reason=Priority EligibleTime='+submit+' Dependency=(null) ReqTRES=cpu=8,gres/gpu=8,gres/gpu:h100=8'
        with patch.object(slurm,'_run',return_value=output):slurm._fill_queue_details([job])
        self.assertEqual(job['queue_req_gpus'],32)


if __name__=='__main__':unittest.main()
