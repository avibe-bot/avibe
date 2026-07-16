import { describe, expect, it } from 'vitest';

import { activityItemKind, isHarnessActivity, resolveActivityLabel } from './backgroundActivity';

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
