"""Per-person waiting view (docs/superpowers/specs/2026-11-24-per-person-waiting-view.md)."""
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

os.environ.setdefault('SLURM_DASHBOARD_DISABLE_RECORDER', '1')

import history
import waiting as w

M = 60_000
H = 3_600_000
D = 24 * H
T0 = 100 * D  # midnight UTC, a multiple of every step
CAPS = {'researcher': 35, 'pi': 16, 'student': 1, 'admin': 64, 'golf': 16, 'normal': None}
GAP = 10 * M


def job(job_id, user, **kw):
    base = dict(job_id=str(job_id), submit_time=1, state='PENDING', pending_reason='Priority',
                eligible_time=T0 - 3 * H, dependency='(null)', queue_details_observed=True,
                req_gpus=1, qos='researcher', partition='research', user=user)
    base.update(kw)
    return base


def running(job_id, user, gpus, **kw):
    return job(job_id, user, state='RUNNING', pending_reason='None', req_gpus=gpus, **kw)


class ReasonClass(unittest.TestCase):
    def test_every_listed_reason(self):
        cluster = ['Priority', 'Resources', 'Nodes required for job are DOWN, DRAINED or reserved for jobs in higher priority partitions',
                   'Nodes_required_for_job_are_DOWN,_DRAINED_or_reserved_for_jobs_in_higher_priority_partitions',
                   'ReqNodeNotAvail', 'ReqNodeNotAvail, May be reserved for other job',
                   'ReqNodeNotAvail,_UnavailableNodes:gpu05', 'NodeDown', 'PartitionDown',
                   'PartitionInactive', 'Priority,Resources']
        not_cluster = ['QOSMaxGRESPerUser', 'QOSMaxJobsPerUserLimit', 'QOSGrpGRES', 'AssocGrpGRES',
                       'AssocMaxJobsLimit', 'MaxGRESPerAccount', 'JobArrayTaskLimit', 'BeginTime',
                       'Reservation', 'DeadLine', 'BadConstraints', 'Dependency',
                       'DependencyNeverSatisfied', 'JobHeldUser', 'JobHeldAdmin',
                       'job requeued in held state', 'job_requeued_in_held_state',
                       'launch failed requeued held', 'launch_failed_requeued_held',
                       'JobHoldMaxRequeue', 'PartitionTimeLimit', 'PartitionConfig',
                       'InvalidAccount', 'InvalidQOS', 'AccountNotAllowed', 'QOSNotAllowed',
                       'uid 1234 not in group permitted to use this partition (research). groups allowed: x',
                       'uid_1234_not_in_group_permitted_to_use_this_partition_(research)']
        unclassified = ['None', 'Prolog', 'Cleaning', 'Licenses', 'SystemFailure',
                        'PartitionNodeLimit', 'SomethingNew', '', None]
        for reason in cluster:
            self.assertEqual(w.reason_class(reason), 'cluster', reason)
        for reason in not_cluster:
            self.assertEqual(w.reason_class(reason), 'not_cluster', reason)
        for reason in unclassified:
            self.assertEqual(w.reason_class(reason), 'unclassified', reason)
        self.assertEqual(w.normalise_reason('ReqNodeNotAvail,_UnavailableNodes:gpu05'), 'ReqNodeNotAvail')
        self.assertEqual(w.normalise_reason('job requeued in held state'), 'job_requeued_in_held_state')


class ClassifyPerson(unittest.TestCase):
    def test_unknown_qos_gpus_are_not_counted_twice(self):
        pending = [dict(eligible_ms=0, gpus_total=1, tasks=1, qos=None, cap_reason=False)]
        result = w.classify_person(pending, {None: 18}, 3 * H, CAPS, 35, (2 * H, 24 * H))
        self.assertTrue(result['waiting'][2 * H])
        result = w.classify_person(pending, {None: 18, 'researcher': 17}, 3 * H, CAPS, 35, (2 * H, 24 * H))
        self.assertFalse(result['waiting'][2 * H])
        self.assertTrue(result['capped'])
        self.assertEqual(result['running_gpus'], 35)

    def test_cap_reason_only_makes_capped(self):
        pending = [dict(eligible_ms=0, gpus_total=4, tasks=1, qos='researcher', cap_reason=True)]
        result = w.classify_person(pending, {}, 3 * H, CAPS, 35, (2 * H, 24 * H))
        self.assertEqual(result['waiting'], {2 * H: False, 24 * H: False})
        self.assertTrue(result['capped'])
        self.assertEqual(result['asked_gpus'], 0)
        self.assertIsNone(result['max_age_ms'])


class Waiting(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / 'data.db')
        history.init_db(self.db)
        self.c = sqlite3.connect(self.db)
        self.c.row_factory = sqlite3.Row

    def tearDown(self):
        self.c.close()
        self.temp.cleanup()

    def poll(self, jobs, ts):
        history.upsert_jobs(self.db, jobs, ts, queue_max_gap_ms=GAP)

    def polls(self, jobs, start, end, every=5 * M):
        for ts in range(start, end + 1, every):
            self.poll(jobs, ts)

    def run_view(self, start, end, now=None, tz=0, **kw):
        rules = dict(caps=CAPS, default_cap=35, excluded_partitions=('rtx',), thresholds_h=(2, 24))
        rules.update(kw)
        return w.get_waiting(self.c, start, end, tz_offset_min=tz, now_ms=end if now is None else now, **rules)

    def person(self, result, user):
        rows = [p for p in result['people'] if p['user'] == user]
        return rows[0] if rows else None

    # 2
    def test_counts_people_not_jobs(self):
        jobs = [job(f'a{i}', 'alice') for i in range(70)] + [job('b1', 'bob')]
        self.polls(jobs, T0, T0 + H)
        result = self.run_view(T0, T0 + H)
        self.assertEqual(result['current']['people_2h'], 2)
        self.assertEqual(sorted(result['users']), ['alice', 'bob'])
        self.assertEqual(result['users']['alice']['w2'], [60])
        self.assertEqual(result['samples'], {'expected': [60], 'observed': [60], 'accounting': [0]})
        self.assertEqual(self.person(result, 'alice')['avg_asked_gpus'], 70.0)

    # 3
    def test_thresholds(self):
        jobs = [job('1', 'young', eligible_time=T0 - H), job('2', 'mid', eligible_time=T0 - 3 * H),
                job('3', 'old', eligible_time=T0 - 25 * H)]
        self.polls(jobs, T0, T0 + 10 * M)
        result = self.run_view(T0, T0 + 10 * M)
        self.assertIsNone(self.person(result, 'young'))
        self.assertGreater(self.person(result, 'mid')['waiting_h'], 0)
        self.assertEqual(self.person(result, 'mid')['waiting_24h_h'], 0)
        self.assertEqual(self.person(result, 'old')['waiting_24h_h'], self.person(result, 'old')['waiting_h'])
        self.assertEqual(result['current']['people_2h'], 2)
        self.assertEqual(result['current']['people_24h'], 1)

    # 4
    def test_not_counted(self):
        jobs = [job('0', 'control'), job('1', 'u1', req_gpus=0), job('2', 'u2', eligible_time=None),
                job('3', 'u3', queue_details_observed=False),
                job('4', 'u4', pending_reason='QOSMaxGRESPerUser'),
                job('5', 'u5', pending_reason='JobArrayTaskLimit'),
                job('6', 'u6', pending_reason='BeginTime'), job('7', 'u7', pending_reason='JobHeldUser'),
                job('8', 'u8', pending_reason='PartitionTimeLimit'),
                job('9', 'u9', pending_reason='Dependency'),
                job('10', 'u10', dependency='afterok:8(unfulfilled)')]
        self.polls(jobs, T0, T0 + 10 * M)
        result = self.run_view(T0, T0 + 10 * M)
        self.assertEqual(list(result['users']), ['control'])
        self.assertEqual(result['current']['people_2h'], 1)
        # QOSMaxGRESPerUser is at own cap, so only that person is in the table besides control.
        self.assertEqual(sorted(p['user'] for p in result['people']), ['control', 'u4'])
        self.assertEqual(self.person(result, 'u4')['waiting_h'], 0)

    # 5
    def test_cap_rule(self):
        jobs = [
            running('ra', 'a', 35), job('pa', 'a'),
            running('rb', 'b', 27), job('pb1', 'b', req_gpus=8), job('pb2', 'b'),
            running('rc', 'c', 30), job('pc', 'c', req_gpus=8),
            job('pc2', 'c2', req_gpus=4, pending_reason='QOSMaxGRESPerUser'),
            running('rc3', 'c3', 18, qos=None), job('pc3', 'c3', qos=None),
            running('rd', 'd', 100, qos='normal'), job('pd', 'd', qos='normal', req_gpus=8),
            job('50_[0-47]', 'e', req_gpus=48, array_task_count=48),
            running('rf', 'f', 35, qos='admin'), job('pf', 'f'),
        ]
        self.polls(jobs, T0, T0 + 10 * M)
        result = self.run_view(T0, T0 + 10 * M)
        waiting = {u for u in result['users']}
        self.assertEqual(waiting, {'b', 'c3', 'd', 'e', 'f'})
        for user in ('a', 'c', 'c2'):
            row = self.person(result, user)
            self.assertEqual(row['waiting_h'], 0, user)
            self.assertGreater(row['capped_h'], 0, user)
        self.assertIsNone(self.person(result, 'c2')['avg_asked_gpus'])
        self.assertEqual(self.person(result, 'f')['max_running_gpus'], 35)
        self.assertEqual(self.person(result, 'b')['max_asked_gpus'], 9)

    # 6
    def test_running_in_other_partition_counts_toward_cap(self):
        jobs = [running('r', 'a', 35, partition='rtx'), job('p', 'a')]
        self.polls(jobs, T0, T0 + 10 * M)
        result = self.run_view(T0, T0 + 10 * M)
        self.assertEqual(result['users'], {})
        self.assertGreater(self.person(result, 'a')['capped_h'], 0)

    # 7
    def test_excluded_partition(self):
        jobs = [job('1', 'rtxonly', partition='rtx'), job('2', 'both', partition='research,rtx')]
        self.polls(jobs, T0, T0 + 10 * M)
        result = self.run_view(T0, T0 + 10 * M)
        self.assertEqual(list(result['users']), ['both'])
        self.assertEqual(result['rules']['excluded_partitions'], ['rtx'])

    # 8
    def test_unclassified_reason_is_reported_not_counted(self):
        self.polls([job('1', 'a', pending_reason='Licenses')], T0, T0 + 10 * M)
        result = self.run_view(T0, T0 + 10 * M)
        self.assertEqual(result['users'], {})
        self.assertGreater(result['unclassified_reasons']['Licenses'], 0)

    # 9
    def test_recording_gap_is_unknown(self):
        jobs = [job('1', 'a')]
        self.polls(jobs, T0, T0 + 10 * M)
        self.polls(jobs, T0 + 190 * M, T0 + 4 * H)
        result = self.run_view(T0, T0 + 4 * H)
        self.assertEqual(result['bucket_ms'], H)
        self.assertEqual(result['samples']['expected'], [60, 60, 60, 60])
        self.assertEqual(result['samples']['observed'], [11, 0, 0, 50])
        self.assertEqual(result['users']['a']['w2'], [11, 0, 0, 50])
        self.assertAlmostEqual(self.person(result, 'a')['waiting_h'], 61 / 60, places=3)

    # 10
    def test_reason_flip_between_grid_points(self):
        jobs = [job('1', 'a')]
        for k, ts in enumerate(range(T0 - 30_000, T0 + H + 30_000 + 1, M)):
            self.poll([dict(jobs[0], pending_reason='Priority' if k % 2 else 'Resources')], ts)
        result = self.run_view(T0, T0 + H)
        self.assertEqual(result['samples']['observed'], [60])
        self.assertEqual(result['users']['a']['w2'], [60])
        self.assertEqual(self.person(result, 'a')['waiting_h'], 1.0)

    def test_segment_starting_after_last_sample_covers_it(self):
        base = job('1', 'a')
        for ts in range(T0 - 30_000, T0 + 59 * M, M):  # last at T0+58.5 min
            self.poll([base], ts)
        self.poll([dict(base, pending_reason='Resources')], T0 + 59 * M + 30_000)
        result = self.run_view(T0, T0 + H, now=T0 + 60 * M)
        self.assertEqual(result['samples']['observed'], [60])
        self.assertEqual(result['users']['a']['w2'], [60])

    # 11
    def accounting(self):
        self.c.execute('INSERT INTO queue_accounting_meta VALUES (1,1,?,1)', (T0 + 10 * D,))
        self.c.execute('INSERT INTO queue_accounting VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                       ('1', 1, 'carol', 'x', 'research', T0 - 3 * H, T0 + 2 * H, 'COMPLETED', 4,
                        T0 + 10 * D, 8))
        self.c.commit()

    def test_accounting_estimate(self):
        self.accounting()
        result = self.run_view(T0, T0 + H)
        self.assertEqual(result['samples'], {'expected': [60], 'observed': [0], 'accounting': [60]})
        self.assertEqual(result['users']['carol']['w2'], [60])
        self.assertEqual(self.person(result, 'carol')['estimate_h'], 1.0)
        self.assertIsNone(result['current'])

    def test_observed_rules_win_over_accounting(self):
        self.accounting()
        self.polls([job('1', 'carol', req_gpus=4, pending_reason='JobHeldUser')], T0, T0 + H)
        held = self.run_view(T0, T0 + H)
        self.assertEqual(held['samples']['accounting'], [0])
        self.assertEqual(held['people'], [])
        self.c.execute('DELETE FROM queue_observation_segments')
        self.c.execute('DELETE FROM queue_observation_windows')
        self.c.execute('DELETE FROM queue_observation_heads')
        self.c.execute('DELETE FROM queue_recorder_status')
        self.c.commit()
        self.polls([job('1', 'carol', req_gpus=4)], T0, T0 + H)
        seen = self.run_view(T0, T0 + H)
        row = self.person(seen, 'carol')
        self.assertEqual((row['waiting_h'], row['estimate_h']), (1.0, 0.0))
        self.assertEqual(seen['users']['carol']['w2'], [60])

    # 12
    def test_buckets_and_steps(self):
        self.assertEqual(self.run_view(T0, T0 + 4 * D)['bucket_ms'], H)
        self.assertEqual(self.run_view(T0, T0 + 5 * D)['bucket_ms'], D)
        week = self.run_view(T0, T0 + 7 * D, tz=-240)
        self.assertTrue(week['buckets'])
        self.assertTrue(all(b % D == 20 * H for b in week['buckets']))
        self.assertEqual(week['buckets'][0], T0 - 4 * H)
        self.assertEqual(sum(week['samples']['expected']), 7 * 24 * 12)
        for days, step in ((1, M), (7, 5 * M), (30, 15 * M), (90, H)):
            self.assertEqual(self.run_view(T0, T0 + days * D)['step_ms'], step, days)

    # 13
    def test_table_numbers(self):
        pending = [job('p', 'dave', req_gpus=4, eligible_time=T0 - 23 * H), running('re', 'erin', 35),
                   job('pe', 'erin')]
        self.polls(pending, T0, T0 + 55 * M)
        self.polls(pending + [running('r', 'dave', 2)], T0 + 60 * M, T0 + 2 * H)
        result = self.run_view(T0, T0 + 2 * H)
        dave = self.person(result, 'dave')
        self.assertEqual(dave['waiting_h'], 2.0)
        self.assertEqual(dave['waiting_24h_h'], 1.0)
        self.assertEqual(dave['idle_h'], 1.0)
        self.assertEqual(dave['capped_h'], 0.0)
        self.assertAlmostEqual(dave['longest_wait_h'], 23 + 119 / 60, places=3)
        self.assertEqual(dave['avg_asked_gpus'], 4.0)
        self.assertEqual(dave['avg_running_gpus'], 1.0)
        self.assertEqual(dave['max_running_gpus'], 2)
        self.assertAlmostEqual(dave['current_wait_h'], 25.0, places=3)
        self.assertEqual(result['users']['dave'], {'w2': [60, 60], 'w2_idle': [60, 0],
                                                   'w24': [0, 60], 'w24_idle': [0, 0]})
        self.assertEqual(self.person(result, 'erin')['capped_h'], 2.0)
        self.assertEqual(result['people'][0]['user'], 'dave')

    # 14
    def test_current(self):
        self.polls([job('1', 'a')], T0, T0 + 2 * H)
        reaching = self.run_view(T0, T0 + 2 * H)
        self.assertEqual(reaching['current'], {'as_of_ms': T0 + 2 * H, 'people_2h': 1, 'people_24h': 0})
        earlier = self.run_view(T0, T0 + H, now=T0 + 2 * H)
        self.assertIsNone(earlier['current'])
        self.assertIsNone(self.person(earlier, 'a')['current_wait_h'])
        stale = self.run_view(T0, T0 + 3 * H)
        self.assertIsNone(stale['current'])

    def test_explain(self):
        jobs = [job('1', 'a'), job('2', 'b', pending_reason='BeginTime'), running('r', 'c', 35),
                job('3', 'c'), job('4', 'd', eligible_time=T0 - H)]
        self.polls(jobs, T0, T0 + 10 * M)
        result = w.explain(self.c, T0 + 5 * M, caps=CAPS, default_cap=35,
                           excluded_partitions=('rtx',), thresholds_h=(2, 24))
        self.assertEqual(result['source'], 'observed')
        users = result['users']
        self.assertEqual(users['a']['status'], 'waiting')
        self.assertEqual(users['b']['status'], 'not_counted')
        self.assertEqual(users['b']['jobs'][0]['failed'], 'not_cluster_reason')
        self.assertEqual(users['c']['status'], 'capped')
        self.assertEqual(users['c']['jobs'][0]['failed'], 'over_cap')
        self.assertEqual(users['d']['jobs'][0]['failed'], 'too_young')

    def test_unmapped_qos_listed(self):
        self.polls([job('1', 'a', qos='mystery')], T0, T0 + 10 * M)
        result = self.run_view(T0, T0 + 10 * M)
        self.assertEqual(result['rules']['unmapped_qos'], ['mystery'])
        self.assertEqual(list(result['users']), ['a'])


class Route(unittest.TestCase):
    def setUp(self):
        import app as dashboard_app
        import config
        self.config = config
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / 'data.db')
        history.init_db(self.db)
        self.saved = config.HISTORY_DB_PATH
        config.HISTORY_DB_PATH = self.db
        self.client = dashboard_app.app.test_client()

    def tearDown(self):
        self.config.HISTORY_DB_PATH = self.saved
        self.temp.cleanup()

    def test_route(self):
        self.assertEqual(self.client.get('/api/waiting').status_code, 400)
        self.assertEqual(self.client.get(f'/api/waiting?from={T0}&to={T0}').status_code, 400)
        response = self.client.get(f'/api/waiting?from={T0}&to={T0 + H}&tz_offset_min=-9999')
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        for key in ('methodology', 'thresholds_h', 'step_ms', 'bucket_ms', 'buckets', 'samples',
                    'users', 'people', 'current', 'rules', 'unclassified_reasons'):
            self.assertIn(key, data)
        self.assertEqual(data['methodology'], 'waiting_people_v1')
        self.assertEqual(data['thresholds_h'], [2, 24])
        self.assertEqual(data['rules']['default_cap'], 35)


if __name__ == '__main__':
    unittest.main()
