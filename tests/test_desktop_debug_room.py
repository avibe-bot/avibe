"""The maintenance job may retire only replaceable, blocking master generations."""
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from scripts.ci_desktop_debug_room import LIMIT, MASTER, reconcile, victim

PROFILE = 'desktop-pr-debug-v1-macOS-ARM64-' + 'a' * 64
START = datetime(2026, 11, 28, tzinfo=timezone.utc)


def entry(**updates):
    return dict({'id': 10, 'key': PROFILE.replace('a' * 64, 'b' * 64) + '-9-1',
                 'ref': MASTER, 'version': 'v1', 'size_in_bytes': 300_000_000,
                 'created_at': '2026-11-27T00:00:00Z'}, **updates)


@pytest.mark.parametrize('size,expected', [(250_000_000, False), (250_000_001, True),
                                           (LIMIT, True), (LIMIT + 1, False), (0, False)])
def test_replacement_must_fit_without_blocking_generation(size, expected):
    assert bool(victim([entry()], PROFILE, size, START)) is expected


@pytest.mark.parametrize('updates', [{'ref': 'refs/pull/7/merge'}, {'key': 'release'},
                                     {'key': PROFILE.replace('macOS-ARM64', 'Windows-X64') + '-7-1'},
                                     {'key': PROFILE + '-not-a-generation'}, {'created_at': START.isoformat()}])
def test_not_this_old_master_generation(updates):
    assert victim([entry(**updates)], PROFILE, 300_000_000, START) is None


def test_global_capacity_and_newer_writer_are_rechecked():
    old = entry()
    extra = entry(id=22, key='other', size_in_bytes=8_350_000_001)
    assert victim([old, extra], PROFILE, 300_000_000, START) is None
    extra['size_in_bytes'] -= 1
    assert victim([old, extra], PROFILE, 300_000_000, START) == old
    newer = entry(id=23, key=PROFILE + '-11-1', created_at=START.isoformat())
    assert victim([old, newer], PROFILE, 300_000_000, START) is None


@pytest.mark.parametrize('field,value', [('id', 11), ('version', 'v2'), ('key', PROFILE + '-8-3'), ('ref', 'refs/heads/topic')])
def test_changed_identity_not_deleted(field, value):
    inventories = iter([[entry()], [entry(**{field: value})]])
    reconcile(PROFILE, 300_000_000, START, lambda: next(inventories), lambda _: pytest.fail('unexpected delete'))


def test_removes_only_as_much_as_needed():
    rows = [entry(), entry(id=11, size_in_bytes=50_000_000, created_at='2026-11-27T10:00:00Z')]
    deleted = []
    def remove(i):
        deleted.append(i)
        rows[:] = [r for r in rows if r['id'] != i]
        return True
    reconcile(PROFILE, 300_000_000, START, lambda: list(rows), remove)
    assert deleted == [10]


def test_only_successful_master_lint_can_invoke_write_job():
    workflows = Path(__file__).resolve().parents[1] / '.github/workflows'
    wf = yaml.safe_load((workflows / 'desktop-debug-cache-room.yml').read_bytes())
    assert wf[True] == {'workflow_run': {'workflows': ['lint'], 'branches': ['master'], 'types': ['completed']}}
    job = wf['jobs']['retire']
    for predicate in ("event == 'push'", "conclusion == 'success'", "head_branch == 'master'", 'head_repository.full_name == github.repository'):
        assert predicate in job['if']
    assert job['steps'][0]['with']['ref'] == '${{ github.sha }}'
    assert job['permissions'] == {'contents': 'read', 'actions': 'write'}
