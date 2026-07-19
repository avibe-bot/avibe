import { describe, expect, it } from 'vitest';

import type { SessionActivityState } from '../context/ApiContext';
import {
  activityItemKind,
  activityKindI18nKey,
  harnessItemNativeId,
  harnessNavPath,
  isHarnessActivity,
  resolveActivityLabel,
  sortBackgroundActivities,
} from './backgroundActivity';

// Minimal item factory — only the fields the pure helpers read.
function item(partial: Partial<SessionActivityState>): SessionActivityState {
  return {
    id: 'x',
    backend: '',
    runtime_key: '',
    session_id: null,
    kind: '',
    status: 'running',
    description: null,
    started_at: '2026-07-16T00:00:00Z',
    updated_at: '2026-07-16T00:00:00Z',
    ...partial,
  };
}

describe('activityItemKind', () => {
  it('returns the harness kind for watch / task / agent_run', () => {
    expect(activityItemKind({ item_kind: 'watch' })).toBe('watch');
    expect(activityItemKind({ item_kind: 'task' })).toBe('task');
    expect(activityItemKind({ item_kind: 'agent_run' })).toBe('agent_run');
  });

  it('defaults missing / legacy / unknown item_kind to backend_activity', () => {
    expect(activityItemKind({})).toBe('backend_activity');
    expect(activityItemKind({ item_kind: 'backend_activity' })).toBe('backend_activity');
    // A payload from before the union carried no item_kind at all.
    expect(activityItemKind({ item_kind: undefined })).toBe('backend_activity');
    // An unexpected value still degrades to a backend activity row.
    expect(activityItemKind({ item_kind: 'mystery' as never })).toBe('backend_activity');
  });
});

describe('activityKindI18nKey', () => {
  it('classifies one-shot and recurring tasks from schedule_type', () => {
    expect(activityKindI18nKey({ item_kind: 'task', schedule_type: 'at' })).toBe('taskOneShot');
    expect(activityKindI18nKey({ item_kind: 'task', schedule_type: 'cron' })).toBe(
      'taskRecurring',
    );
  });

  it('does not infer recurrence from labels or other activity kinds', () => {
    expect(activityKindI18nKey({ item_kind: 'task', schedule_type: null })).toBe('task');
    expect(activityKindI18nKey({ item_kind: 'watch' })).toBe('watch');
    expect(activityKindI18nKey({ item_kind: 'agent_run' })).toBe('agentRun');
    expect(activityKindI18nKey({ item_kind: 'backend_activity' })).toBe('backendActivity');
  });
});

describe('isHarnessActivity', () => {
  it('is true only for harness rows', () => {
    expect(isHarnessActivity({ item_kind: 'watch' })).toBe(true);
    expect(isHarnessActivity({ item_kind: 'task' })).toBe(true);
    expect(isHarnessActivity({ item_kind: 'agent_run' })).toBe(true);
    expect(isHarnessActivity({ item_kind: 'backend_activity' })).toBe(false);
    expect(isHarnessActivity({})).toBe(false);
  });
});

describe('resolveActivityLabel', () => {
  it('prefers label, then description, then the fallback', () => {
    expect(resolveActivityLabel({ label: 'deploy watch', description: 'desc' }, 'Watch')).toBe(
      'deploy watch',
    );
    expect(resolveActivityLabel({ label: '', description: 'from desc' }, 'Watch')).toBe('from desc');
    expect(resolveActivityLabel({ label: null, description: null }, 'Watch')).toBe('Watch');
    expect(resolveActivityLabel({}, 'Scheduled task')).toBe('Scheduled task');
  });

  it('treats a whitespace-only label as empty', () => {
    expect(resolveActivityLabel({ label: '   ', description: '' }, 'Agent run')).toBe('Agent run');
  });
});

describe('harnessItemNativeId', () => {
  it('strips the kind namespace prefix', () => {
    expect(harnessItemNativeId({ id: 'watch:abc123' })).toBe('abc123');
    expect(harnessItemNativeId({ id: 'agent_run:run-9' })).toBe('run-9');
    expect(harnessItemNativeId({ id: 'plain' })).toBe('plain');
  });
});

describe('sortBackgroundActivities', () => {
  it('orders running items first, then by start time descending', () => {
    const items = [
      item({ id: 'watch:w', item_kind: 'watch', status: 'enabled', since: '2026-07-16T05:00:00Z' }),
      item({ id: 'run:new', item_kind: 'agent_run', status: 'running', since: '2026-07-16T09:00:00Z' }),
      item({ id: 'run:old', item_kind: 'agent_run', status: 'running', since: '2026-07-16T02:00:00Z' }),
      item({ id: 'task:t', item_kind: 'task', status: 'scheduled', since: '2026-07-16T08:00:00Z' }),
    ];
    expect(sortBackgroundActivities(items).map((i) => i.id)).toEqual([
      'run:new', // running, newest
      'run:old', // running, older
      'task:t', // pending, newest
      'watch:w', // pending, older
    ]);
  });

  it('does not mutate the input array', () => {
    const items = [item({ id: 'a', status: 'enabled' }), item({ id: 'b', status: 'running' })];
    const copy = [...items];
    sortBackgroundActivities(items);
    expect(items).toEqual(copy);
  });

  it('treats the raw "processing" status as in-progress', () => {
    const items = [
      item({ id: 'task', status: 'scheduled', since: '2026-07-16T09:00:00Z' }),
      item({ id: 'run', status: 'processing', since: '2026-07-16T02:00:00Z' }),
    ];
    // The processing run ranks above the (newer) scheduled task.
    expect(sortBackgroundActivities(items).map((i) => i.id)).toEqual(['run', 'task']);
  });
});

describe('harnessNavPath', () => {
  it('sends watch/task rows to the matching tab with a session filter', () => {
    expect(harnessNavPath({ id: 'watch:w1', item_kind: 'watch' }, 'ses-1')).toBe(
      '/harness?tab=watches&session=ses-1',
    );
    expect(harnessNavPath({ id: 'task:t1', item_kind: 'task' }, 'ses-1')).toBe(
      '/harness?tab=tasks&session=ses-1',
    );
  });

  it('anchors a delegated run by id (no session filter — it runs elsewhere)', () => {
    expect(harnessNavPath({ id: 'agent_run:r1', item_kind: 'agent_run' }, 'ses-1')).toBe(
      '/harness?tab=runs&run=r1',
    );
  });

  it('omits the session param when no session is given', () => {
    expect(harnessNavPath({ id: 'watch:w1', item_kind: 'watch' }, null)).toBe('/harness?tab=watches');
  });
});
