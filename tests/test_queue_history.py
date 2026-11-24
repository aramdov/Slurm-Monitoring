import sqlite3
import unittest
import queue_history as q
import slurm
import history
import tempfile
from pathlib import Path
from unittest.mock import patch


class Observations(unittest.TestCase):
    def setUp(self):
        self.c = sqlite3.connect(':memory:')
        q.init(self.c)
        self.j = dict(job_id='42', submit_time=1000, state='PENDING',
                      pending_reason='Dependency', queue_details_observed=True,
                      dependency='afterok:41(unfulfilled)', req_gpus=8)

    def rows(self):
        return self.c.execute('SELECT first_ms,last_ms,previous_last_ms,reason,observations '
                              'FROM queue_observation_segments ORDER BY id').fetchall()

    def test_dependency_change_preserves_bounds(self):
        q.record(self.c,[self.j],2000)
        q.record(self.c,[self.j],12000)
        self.j.update(pending_reason='Priority',dependency='(null)',eligible_time=15000)
        q.record(self.c,[self.j],22000)
        self.assertEqual(self.rows(),[(2000,12000,None,'Dependency',2),(22000,22000,12000,'Priority',1)])

    def test_outage_and_restart_are_unknown(self):
        q.record(self.c,[self.j],2000,max_gap_ms=30000)
        q.record(self.c,[self.j],32001,max_gap_ms=30000)
        original = q.SESSION
        try:
            q.SESSION='restarted'
            q.record(self.c,[self.j],42001)
        finally:
            q.SESSION=original
        self.assertEqual([r[2] for r in self.rows()],[None,None,None])

    def test_disappearance_and_reappearance(self):
        q.record(self.c,[self.j],2000)
        q.record(self.c,[],12000)
        q.record(self.c,[self.j],22000)
        self.assertEqual([r[2] for r in self.rows()],[None,None])

    def test_resource_or_array_changes_remain_separate(self):
        self.j.update(job_id='42_[0-3]',array_task_count=4,req_gpus=32)
        q.record(self.c,[self.j],2000)
        self.j.update(job_id='42_[1-3]',array_task_count=3,req_gpus=24)
        q.record(self.c,[self.j],12000)
        self.assertEqual(len(self.rows()),2)
        self.assertEqual(self.c.execute('SELECT count(*) FROM queue_observation_heads').fetchone()[0],1)

    def test_clock_rollback_is_rejected(self):
        q.record(self.c,[self.j],12000)
        with self.assertRaises(ValueError): q.record(self.c,[self.j],2000)

    def test_real_upsert_is_atomic_and_retains_jobs(self):
        with tempfile.TemporaryDirectory() as directory:
            db = str(Path(directory)/'history.db')
            history.init_db(db)
            history.upsert_jobs(db,[self.j],2000)
            changed = dict(self.j,pending_reason='Priority')
            history.upsert_jobs(db,[changed],12000)
            with sqlite3.connect(db) as c:
                self.assertEqual(c.execute('SELECT count(*) FROM jobs').fetchone()[0],1)
                self.assertEqual(c.execute('SELECT count(*) FROM queue_observation_segments').fetchone()[0],2)
            history.upsert_jobs(db,[],22000)
            with sqlite3.connect(db) as c:
                self.assertEqual(c.execute('SELECT count(*) FROM jobs').fetchone()[0],1)
                self.assertEqual(c.execute('SELECT count(*) FROM queue_observation_heads').fetchone()[0],0)

    def test_array_detail_matching_and_unknown_details(self):
        submit='2026-11-01T12:00:00'
        job=dict(job_id='42_[0-3%2]',state='PENDING',submit_time=slurm._parse_ts(submit))
        other=dict(job_id='43',state='RUNNING',submit_time=slurm._parse_ts(submit))
        line='JobId=42 ArrayJobId=42 ArrayTaskId=0-3%2 SubmitTime='+submit+' JobState=PENDING Reason=Dependency EligibleTime=Unknown Dependency=afterok:41(unfulfilled)'
        with patch.object(slurm,'_run',return_value=line) as run:
            slurm._fill_queue_details([job,other])
        self.assertTrue(run.call_args.kwargs['require_success'])
        self.assertTrue(job['queue_details_observed'])
        self.assertEqual(job['pending_reason'],'Dependency')
        self.assertIsNone(job['eligible_time'])
        self.assertFalse(other['queue_details_observed'])


if __name__ == '__main__': unittest.main()
