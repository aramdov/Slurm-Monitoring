"""The recorder stores each job's QOS, so per-QOS limits can be applied later."""
import sqlite3
import unittest
from unittest.mock import patch

import queue_history as q
import slurm


class QueueQos(unittest.TestCase):
    def test_scontrol_qos_is_captured(self):
        submit = '2026-11-01T12:00:00'
        job = dict(job_id='42', state='PENDING', submit_time=slurm._parse_ts(submit))
        line = ('JobId=42 SubmitTime=' + submit + ' JobState=PENDING Reason=Priority '
                'EligibleTime=' + submit + ' Dependency=(null) QOS=researcher '
                'ReqTRES=cpu=8,gres/gpu=1')
        with patch.object(slurm, '_run', return_value=line):
            slurm._fill_queue_details([job])
        self.assertEqual(job['qos'], 'researcher')

    def test_old_segments_table_gains_qos_column(self):
        c = sqlite3.connect(':memory:')
        c.execute('''CREATE TABLE queue_observation_segments (
            id INTEGER PRIMARY KEY, job_id TEXT NOT NULL, submit_time_ms INTEGER NOT NULL,
            first_ms INTEGER NOT NULL, last_ms INTEGER NOT NULL, previous_last_ms INTEGER,
            session TEXT NOT NULL, signature TEXT NOT NULL, state TEXT NOT NULL,
            reason TEXT NOT NULL, eligible_time_ms INTEGER, dependency TEXT,
            details_observed INTEGER NOT NULL, start_time_ms INTEGER, partition TEXT,
            req_gpus INTEGER, req_cpus INTEGER, array_task_count INTEGER,
            observations INTEGER NOT NULL)''')
        q.init(c)
        columns = {row[1] for row in c.execute('PRAGMA table_info(queue_observation_segments)')}
        self.assertIn('qos', columns)
        q.init(c)  # idempotent

    def test_qos_is_stored_and_a_change_starts_a_segment(self):
        c = sqlite3.connect(':memory:')
        q.init(c)
        job = dict(job_id='42', submit_time=1000, state='PENDING', pending_reason='Priority',
                   queue_details_observed=True, dependency='(null)', req_gpus=1, qos='researcher')
        q.record(c, [job], 2000)
        q.record(c, [job], 12000)
        q.record(c, [dict(job, qos='pi')], 22000)
        rows = c.execute('SELECT qos,first_ms,last_ms,previous_last_ms FROM queue_observation_segments '
                         'ORDER BY id').fetchall()
        self.assertEqual(rows, [('researcher', 2000, 12000, None), ('pi', 22000, 22000, 12000)])


if __name__ == '__main__':
    unittest.main()
