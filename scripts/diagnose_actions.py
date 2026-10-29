#!/usr/bin/env python3
"""Read-only, head-pinned Actions evidence. Does not infer a billing/policy cause."""
from __future__ import annotations
import argparse
import json
import re
import subprocess


def gh_get(path):
    proc = subprocess.run(['gh', 'api', '--method', 'GET', path], capture_output=True, text=True)
    if proc.returncode:
        raise RuntimeError('GitHub GET failed for ' + path)
    return json.loads(proc.stdout)


def diagnose(repo, run_id, get=gh_get):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo) or not str(run_id).isdigit():
        raise ValueError('expected owner/repo and numeric run id')
    prefix = f'repos/{repo}/actions/runs/{run_id}'
    run = get(prefix)
    jobs, expected = [], None
    page = 1
    while True:
        data = get(prefix + f'/jobs?filter=latest&per_page=100&page={page}')
        total = data['total_count']
        if expected is not None and expected != total:
            raise RuntimeError('job inventory changed during pagination; retry')
        expected = total
        batch = data['jobs']
        jobs.extend(batch)
        if len(jobs) >= expected:
            break
        if not batch:
            raise RuntimeError('incomplete job inventory')
        page += 1
    if len(jobs) != expected or len({j['id'] for j in jobs}) != expected:
        raise RuntimeError('inconsistent job inventory')
    fresh = get(prefix)
    for key in ('head_sha', 'run_attempt', 'status', 'conclusion'):
        if fresh.get(key) != run.get(key):
            raise RuntimeError('run changed during collection; retry')
    return {'repository': repo, 'run_id': int(run_id), 'head_sha': run['head_sha'],
            'attempt': run.get('run_attempt'), 'status': run['status'], 'conclusion': run.get('conclusion'),
            'url': run['html_url'], 'job_count': len(jobs),
            'execution': 'blocked_before_jobs' if run.get('conclusion') == 'startup_failure' and not jobs else 'inspect_jobs',
            'cause': 'unestablished',
            'jobs': [{k: j.get(k) for k in ('id', 'name', 'status', 'conclusion', 'html_url')} for j in jobs]}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('repository')
    parser.add_argument('run_id', type=int)
    args = parser.parse_args()
    print(json.dumps(diagnose(args.repository, args.run_id), indent=2))
