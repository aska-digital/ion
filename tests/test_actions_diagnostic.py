import importlib.util
from pathlib import Path
import pytest

spec = importlib.util.spec_from_file_location('actions_diagnostic', Path(__file__).parents[1] / 'scripts/diagnose_actions.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def test_jobless_startup_failure_does_not_invent_cause():
    run = dict(head_sha='abc', run_attempt=1, status='completed', conclusion='startup_failure', html_url='https://example.invalid/run')
    result = m.diagnose('owner/repo', 1, lambda p: {'total_count': 0, 'jobs': []} if '/jobs?' in p else run)
    assert result['execution'] == 'blocked_before_jobs'
    assert result['cause'] == 'unestablished'


def test_incomplete_inventory_is_not_zero_jobs():
    def get(path):
        if '/jobs?' in path:
            return {'total_count': 1, 'jobs': []}
        return dict(head_sha='abc', status='completed', conclusion='failure')
    with pytest.raises(RuntimeError, match='incomplete'):
        m.diagnose('owner/repo', 1, get)


def test_moved_attempt_rejected():
    attempts = iter([1, 2])
    def get(path):
        if '/jobs?' in path:
            return {'total_count': 0, 'jobs': []}
        return dict(head_sha='abc', run_attempt=next(attempts), status='completed', conclusion='startup_failure')
    with pytest.raises(RuntimeError, match='changed'):
        m.diagnose('owner/repo', 1, get)
