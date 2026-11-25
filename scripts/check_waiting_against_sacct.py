"""Read-only cross-check of the waiting view against sacct for one day.

Usage:
  scripts/check_waiting_against_sacct.py --db data/copy.db --day 2026-11-25 --tz-offset-min -240

At each whole hour t of the day (in the viewer's time zone; JS sign, Yerevan = -240):
  A(t): users with a GPU job outside the excluded partitions, Eligible <= t - 2h,
        and not started or ended by t (sacct, no reason filter, no cap rule).
  W(t): users with status 'waiting' from waiting.explain(conn, t).
Check 1: W(t) is a subset of A(t), apart from listed exceptions (requeued or duplicate
         sacct records, pending arrays sacct has not split, and jobs whose Start or
         Eligible + 2h is within one poll gap of t).
Check 2: every user in A(t) but not in W(t) has a cause from explain().
Hours without recorder or accounting coverage, and hours after the last poll, are skipped.
Exit status 1 on any unexplained difference. The DB is opened read-only; sacct only reads.
"""
import argparse
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config  # noqa: E402
import waiting  # noqa: E402

H = 3_600_000
EXPLAINED = {'not_cluster_reason', 'dependency_or_hold', 'cap_reason', 'over_cap', 'no_details',
             'unclassified_reason'}


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')


def parse_ts(value):
    try:
        return int(datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp()) * 1000
    except ValueError:
        return None


def gpus_of(tres):
    resources = {}
    for token in tres.split(','):
        key, sep, value = token.partition('=')
        if sep and value.isdigit():
            resources[key] = int(value)
    return resources.get('gres/gpu', sum(v for k, v in resources.items() if k.startswith('gres/gpu:')))


def load_sacct(start_ms, end_ms, psv=None):
    if psv:
        output = Path(psv).read_text()
    else:
        command = ['sacct', '-a', '-X', '-D', '--array', '-n', '-P', '-S', iso(start_ms), '-E', iso(end_ms),
                   '--format=JobID,User,Submit,Eligible,Start,End,State,ReqTRES,Partition,QOS']
        output = subprocess.run(command, env=dict(os.environ, TZ='UTC', SLURM_TIME_FORMAT='standard'),
                                capture_output=True, text=True, check=True, timeout=600).stdout
    records, submissions, uncertain, unsplit = defaultdict(set), defaultdict(set), set(), []
    for line in output.splitlines():
        fields = [v.strip() for v in line.split('|')]
        if len(fields) != 10:
            continue
        jid, user, submit, eligible, start, end, state, tres, partition, qos = fields
        sub = parse_ts(submit)
        if sub is None:
            continue
        row = (user, parse_ts(eligible), parse_ts(start), parse_ts(end), state, gpus_of(tres), partition, qos)
        if not re.fullmatch(r'\d+(?:_\d+)?', jid):
            unsplit.append((jid, *row))  # e.g. 123_[0-47]: pending array records not yet split
            continue
        records[jid, sub].add(row)
        submissions[jid].add(sub)
        if 'REQUEU' in state:
            uncertain.add(jid)
    for (jid, sub), variants in records.items():
        if len(variants) != 1 or len(submissions[jid]) != 1:
            uncertain.add(jid)
    jobs = [(jid, *next(iter(variants))) for (jid, sub), variants in records.items()]
    return jobs, uncertain, unsplit


def active(job, t, threshold):
    _, _, eligible, start, end, _, gpus, partition, _ = job
    return (gpus > 0 and eligible is not None and eligible <= t - threshold
            and (start is None or start > t) and (end is None or end > t)
            and waiting._partition_allowed(partition, set(config.WAITING_EXCLUDED_PARTITIONS)))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--db', required=True)
    parser.add_argument('--day', required=True, help='YYYY-MM-DD in the viewer time zone')
    parser.add_argument('--tz-offset-min', type=int, default=0, help='JS getTimezoneOffset sign; Yerevan = -240')
    parser.add_argument('--sacct-psv', help='use a saved sacct -P output instead of running sacct')
    args = parser.parse_args()

    day = datetime.strptime(args.day, '%Y-%m-%d').replace(tzinfo=timezone.utc)
    day_start = int(day.timestamp() * 1000) + args.tz_offset_min * 60_000
    day_end = day_start + 24 * H
    threshold = min(config.WAITING_THRESHOLDS_H) * H
    rules = dict(caps=config.WAITING_QOS_GPU_CAPS, default_cap=config.WAITING_DEFAULT_GPU_CAP,
                 excluded_partitions=config.WAITING_EXCLUDED_PARTITIONS, thresholds_h=config.WAITING_THRESHOLDS_H)

    conn = sqlite3.connect('file:' + str(Path(args.db).resolve()) + '?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    status = conn.execute('SELECT last_ms,max_gap_ms FROM queue_recorder_status WHERE id=1').fetchone()
    last_poll = status['last_ms'] if status else None
    max_gap = status['max_gap_ms'] if status else 90_000

    jobs, uncertain, unsplit = load_sacct(day_start - 21 * 24 * H, day_end, args.sacct_psv)
    uncertain_base = {jid.split('_')[0] for jid in uncertain}
    unexplained = 0
    checked = 0
    for hour in range(24):
        t = day_start + hour * H
        label = datetime.fromtimestamp(t / 1000, timezone.utc) - timedelta(minutes=args.tz_offset_min)
        label = label.strftime('%Y-%m-%d %H:%M')
        if last_poll is not None and t > last_poll:
            print(f'{label}  skipped: after the last poll')
            continue
        result = waiting.explain(conn, t, **rules)
        if result['source'] == 'unknown':
            print(f'{label}  skipped: no recording and no accounting estimate')
            continue
        checked += 1
        explained = result['users']
        W = {user for user, info in explained.items() if info['status'] == 'waiting'}
        A_jobs = defaultdict(list)
        for job in jobs:
            if active(job, t, threshold):
                A_jobs[job[1]].append(job)
        A = set(A_jobs)
        notes = defaultdict(list)
        for job in jobs:
            jid, user, eligible, start, *_ = job
            if jid.split('_')[0] in uncertain_base and job[6] > 0:
                notes[user].append(f'uncertain:{jid}')
            near = [x for x in (start, eligible + threshold if eligible is not None else None)
                    if x is not None and abs(x - t) <= max_gap]
            if near and job[6] > 0:
                notes[user].append(f'boundary:{jid}')
        for jid, user, eligible, start, end, state, gpus, partition, qos in unsplit:
            if gpus > 0 and state.startswith('PENDING'):
                notes[user].append(f'unsplit_array:{jid}')
        print(f'{label}  source={result["source"]:<10} |W|={len(W):>3} |A|={len(A):>3}')
        for user in sorted(W - A):
            if notes[user]:
                print(f'    W-A exception {user}: {", ".join(sorted(set(notes[user]))[:4])}')
            else:
                unexplained += 1
                jobs_w = [j['job_id'] for j in explained[user]['jobs'] if j['failed'] is None]
                print(f'    W-A UNEXPLAINED {user}: waiting jobs {jobs_w[:5]}')
        for user in sorted(A - W):
            info = explained.get(user)
            causes = set()
            if info and info['status'] == 'capped':
                causes.add('at_own_cap')
            if info:
                causes |= {j['failed'] + (':' + waiting.normalise_reason(j['reason'])
                                          if j['failed'] in ('not_cluster_reason', 'unclassified_reason') else '')
                           for j in info['jobs'] if j['failed']}
            ok = 'at_own_cap' in causes or any(c.split(':')[0] in EXPLAINED for c in causes)
            if ok:
                print(f'    A-W {user}: {", ".join(sorted(causes)[:4])}')
            elif notes[user]:
                print(f'    A-W exception {user}: {", ".join(sorted(causes) + sorted(set(notes[user]))[:4])}')
            else:
                unexplained += 1
                print(f'    A-W UNEXPLAINED {user}: {", ".join(sorted(causes)) or "not in the recorded queue"}')
    print(f'hours checked: {checked}, unexplained differences: {unexplained}')
    return 1 if unexplained else 0


if __name__ == '__main__':
    sys.exit(main())
