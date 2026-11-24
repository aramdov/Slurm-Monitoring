import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import monitor_watchdog as watchdog
import _job_recovery as recovery
import auto_recover_jobs as automatic


class GapTests(unittest.TestCase):
    def test_full_automatic_worker_recovers_accounting_and_is_repeatable(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'data').mkdir()
            db=root/'data/slurm_history.db'
            conn=sqlite3.connect(db)
            fields=['job_id TEXT','user TEXT','job_name TEXT','state TEXT',
                    'start_time_ms INTEGER','end_time_ms INTEGER','submit_time_ms INTEGER',
                    'req_gpus INTEGER','req_cpus INTEGER','req_cpuonly INTEGER','req_mem INTEGER',
                    'time_limit_ms INTEGER','priority INTEGER','array_task_count INTEGER',
                    'allocations_json TEXT','pending_reason TEXT','partition TEXT','last_seen_ms INTEGER']
            conn.execute('create table jobs(id integer primary key,'+','.join(fields)+')')
            conn.execute("create table attribution(value text)")
            conn.execute("insert into attribution values('preserve')");conn.commit();conn.close()
            now=int(time.time());start=(now-100)*1000;end=(now-5)*1000
            number=lambda n:{'set':True,'infinite':False,'number':n}
            resources=[{'type':'cpu','name':'','count':8},{'type':'gres','name':'gpu','count':8}]
            job=dict(job_id=42,array={'job_id':0},name='example',user='sample-user',
                     partition='batch',state={'current':['COMPLETED']},
                     time={'submission':now-95,'start':now-90,'end':now-10,'limit':number(60)},
                     allocation_nodes=1,nodes='node01',tres={'requested':resources,'allocated':resources},
                     required={'CPUs':8},priority=number(100))
            response=SimpleNamespace(returncode=0,stdout=json.dumps({'jobs':[job]}))
            with patch.object(automatic.subprocess,'run',return_value=response):
                first=automatic.run(root,start,end,root/'first')
                second=automatic.run(root,start,end,root/'second')
            self.assertEqual(first['applied']['inserted'],1)
            self.assertEqual(second['planned']['insert_count'],0)
            conn=sqlite3.connect(db)
            self.assertEqual(conn.execute('select count(*) from jobs').fetchone()[0],1)
            self.assertEqual(conn.execute('select value from attribution').fetchone()[0],'preserve')

    def test_short_crash_is_recovered_and_queue_is_durable(self):
        state={'last_meta_ms':1000,'outage_start_ms':1000,'gaps':[]}
        watchdog.observe(state,12000)
        state=json.loads(json.dumps(state))
        self.assertEqual((state['gaps'][0]['start_ms'],state['gaps'][0]['end_ms']),(1000,12000))
        watchdog.observe(state,12000)
        self.assertEqual(len(state['gaps']),1)
        self.assertNotIn('outage_start_ms',state)

    def test_normal_polls_do_not_make_gaps_but_stalled_collection_does(self):
        state={'gaps':[]}
        watchdog.observe(state,1000);watchdog.observe(state,11000)
        self.assertEqual(state['gaps'],[])
        watchdog.observe(state,100000)
        self.assertEqual(state['gaps'][0]['start_ms'],11000)

    def test_failed_recovery_retries_and_three_successes_are_required(self):
        gap={'passes':0,'failures':0}
        watchdog.completed(gap,False,1000)
        self.assertEqual(gap['passes'],0)
        self.assertGreater(gap['next_try'],1000)
        for n in range(3):
            watchdog.completed(gap,True,2000+n)
            self.assertEqual(gap['passes'],n+1)

    def test_recovery_insert_is_idempotent_and_concurrent_change_is_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder);db=p/'jobs.db'
            conn=sqlite3.connect(db)
            conn.execute('create table jobs(id integer primary key,job_id text,submit_time_ms integer,state text)')
            conn.execute("insert into jobs values(1,'10',1000,'FINISHED_UNKNOWN')")
            conn.commit();conn.close()
            manifest={'inserts':[{'job_id':'20','submit_time_ms':2000,'state':'COMPLETED'}],
                      'updates':[{'before':{'id':1,'job_id':'10','submit_time_ms':1000,'state':'FINISHED_UNKNOWN'},
                                  'changes':{'state':'COMPLETED'}}]}
            (p/'manifest.json').write_text(json.dumps(manifest))
            conn=sqlite3.connect(db)
            conn.execute("update jobs set state='RUNNING' where id=1");conn.commit();conn.close()
            first=recovery.apply(p,db);second=recovery.apply(p,db)
            self.assertEqual(first['inserted'],1)
            self.assertEqual(first['update_skipped_concurrent_change'],1)
            self.assertEqual(second['insert_skipped_existing'],1)
            conn=sqlite3.connect(db)
            self.assertEqual(conn.execute('select state from jobs where id=1').fetchone()[0],'RUNNING')


if __name__=='__main__':unittest.main()
