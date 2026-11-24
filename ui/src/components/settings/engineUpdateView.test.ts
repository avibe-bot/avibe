import { describe, expect, it } from 'vitest';

import type { DependencyItem, EngineUpdateState } from '@/context/ApiContext';
import { engineBuildVersion, engineUpdateIsWorking, engineUpdatesApply, engineUpdateView } from './engineUpdateView';

const RUNNING = 'model-hub-engine-v8.0.63-2';
const NEXT = 'model-hub-engine-v8.0.70-1';

const engine = (extra: Partial<DependencyItem> = {}): DependencyItem => ({
  id: 'model-hub-engine',
  kind: 'tool',
  required: true,
  installed: true,
  version: '8.0.63',
  status: 'ready',
  active_release: RUNNING,
  available_release: null,
  auto_update: true,
  update_state: 'up_to_date',
  last_checked_at: '2026-11-01T07:55:00+00:00',
  last_result: null,
  ...extra,
});

const seen = (extra: Partial<DependencyItem>, context = {}) => {
  const view = engineUpdateView(engine(extra), context);
  return view && `${view.sentence} / ${view.button.label}${view.button.does === 'update' ? ' (update)' : ''}${view.button.busy ? ' busy' : ''}`;
};

describe('engineUpdateView', () => {
  it('folds every reported state into one sentence and one button', () => {
    const states: Record<EngineUpdateState, string | null> = {
      idle: 'current / check',
      recovering: 'checking / check busy',
      checking: 'checking / check busy',
      up_to_date: 'current / check',
      available: 'available / update (update)',
      requires_avibe_update: 'needsAvibe / check',
      updating: 'updating / update (update) busy',
      deferred: 'waitingForIdle / update (update)',
      applied: 'current / check',
      rolling_back: 'updating / update (update) busy',
      rolled_back: 'undone / retry (update)',
      withdrawing: 'withdrawing / update (update) busy',
      blocked: 'cannotStart / check',
      failed: 'unfinished / check',
      offline: 'cannotCheck / retry',
      uncommissioned: null,
      opted_out: null,
      unavailable: 'cannotCheck / retry',
      rejected: 'cannotCheck / retry',
      waiting_for_controller: 'cannotCheck / retry',
    };
    for (const [state, expected] of Object.entries(states)) {
      const offer = state === 'available' || state === 'deferred' ? NEXT : null;
      expect([state, seen({ update_state: state as EngineUpdateState, available_release: offer })]).toEqual([state, expected]);
    }
  });

  it('keeps the row as it was when the runtime reports no update state', () => {
    expect(engineUpdateView(engine({ update_state: undefined }))).toBeNull();
    expect(engineUpdatesApply({ id: 'avault', update_state: 'up_to_date' })).toBe(false);
  });

  it('takes a candidate only from available_release, and offers it only where the state allows', () => {
    // Repair's fields never make an offer.
    expect(seen({ latest_version: '8.0.70', has_update: true })).toBe('current / check');
    // The running build is not a candidate for itself.
    expect(seen({ update_state: 'available', available_release: RUNNING })).toBe('current / check');
    // With routine updates off the candidate is still reported, and still offered.
    expect(seen({ update_state: 'available', available_release: NEXT, auto_update: false })).toBe('available / update (update)');
    expect(seen({ update_state: 'up_to_date', available_release: NEXT })).toBe('available / update (update)');
    // A cached or fallback target beside a failed refresh or a stopped engine is not an upgrade.
    for (const update_state of ['unavailable', 'rejected', 'offline', 'blocked', 'withdrawing', 'recovering', 'requires_avibe_update', 'waiting_for_controller'] as const) {
      expect(seen({ update_state, available_release: NEXT })).not.toContain('available');
    }
  });

  it('says what the last update came to until another one is tried', () => {
    expect(seen({ update_state: 'up_to_date', last_result: 'rolled_back' })).toBe('undone / retry (update)');
    expect(seen({ update_state: 'idle', last_result: 'preflight_failed' })).toBe('unfinished / retry (update)');
    // A candidate that failed before the switch was never running: nothing was put back.
    expect(seen({ update_state: 'rolled_back', last_result: 'preflight_failed' })).toBe('unfinished / retry (update)');
    expect(seen({ update_state: 'applied', last_result: 'withdrawn_to_fallback' })).toBe('replaced / check');
    expect(seen({ update_state: 'up_to_date', last_result: 'applied' })).toBe('current / check');
    expect(seen({ update_state: 'idle', last_result: 'restarted' })).toBe('current / check');
    // A newer offer replaces the notice about the last one.
    expect(seen({ update_state: 'available', available_release: NEXT, last_result: 'rolled_back' })).toBe('available / update (update)');
  });

  it('names a withdrawal only when that is why the engine stopped', () => {
    expect(seen({ update_state: 'blocked', update_reason: 'engine_withdrawn' })).toBe('stopped / check');
    expect(seen({ update_state: 'blocked', update_reason: 'engine_withdrawn_without_fallback' })).toBe('stopped / check');
    for (const update_reason of ['engine_journal_unreadable', 'engine_rollback_failed', 'engine_no_authorized_recovery_target', null]) {
      expect(seen({ update_state: 'blocked', update_reason })).toBe('cannotStart / check');
    }
  });

  it('shows this page\'s own operation while it runs, and what it knows about its failure', () => {
    expect(seen({ update_state: 'up_to_date' }, { working: 'check' })).toBe('checking / check busy');
    expect(seen({ update_state: 'available', available_release: NEXT }, { working: 'update' })).toBe('updating / update (update) busy');
    expect(seen({ update_state: 'failed' }, { updateFailed: true })).toBe('unfinished / retry (update)');
    expect(seen({ update_state: 'uncommissioned' }, { working: 'check' })).toBeNull();
  });

  it('has never checked until a check is recorded', () => {
    expect(seen({ update_state: 'idle', last_checked_at: null })).toBe('notChecked / check');
  });

  it('reads again by itself only while the engine owner is at work', () => {
    const working = (['idle', 'recovering', 'checking', 'up_to_date', 'available', 'updating', 'deferred', 'rolling_back', 'withdrawing', 'blocked', 'uncommissioned'] as const)
      .filter((update_state) => engineUpdateIsWorking({ id: 'model-hub-engine', update_state }));
    expect(working).toEqual(['recovering', 'checking', 'updating', 'rolling_back', 'withdrawing']);
  });

  it('shows a build tag without its fixed prefix and keeps the build revision', () => {
    expect(engineBuildVersion(RUNNING)).toBe('8.0.63-2');
    expect(engineBuildVersion('v8.0.63')).toBe('8.0.63');
    expect(engineBuildVersion(null)).toBeNull();
  });
});
