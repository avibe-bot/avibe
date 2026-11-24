import { describe, expect, it } from 'vitest';

import type { DependencyItem, EngineUpdateState } from '@/context/ApiContext';
import { engineBuildVersion, engineUpdateRereadMs, engineUpdatesApply, engineUpdateView } from './engineUpdateView';

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
      recovering: 'recovering / check',
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
      failed: 'attemptUnfinished / check',
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
    // A tag left beside a quiet state is not a fresh decision; one beside a failed refresh or a
    // stopped engine is a cache or a fallback, not an upgrade.
    for (const update_state of ['idle', 'up_to_date', 'applied', 'unavailable', 'rejected', 'offline', 'blocked', 'withdrawing', 'recovering', 'requires_avibe_update', 'waiting_for_controller'] as const) {
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
    expect(seen({ update_state: 'failed' }, { failed: 'update' })).toBe('unfinished / retry (update)');
    // A failed check is not reported as an update that did not finish.
    expect(seen({ update_state: 'failed' }, { failed: 'check' })).toBe('cannotCheck / retry');
    expect(seen({ update_state: 'failed' })).toBe('attemptUnfinished / check');
    expect(seen({ update_state: 'uncommissioned' }, { working: 'check' })).toBeNull();
  });

  it('has never checked until a check is recorded', () => {
    expect(seen({ update_state: 'idle', last_checked_at: null })).toBe('notChecked / check');
  });

  it('reads again by itself only where the engine owner will change the row unasked', () => {
    const delay = (update_state: EngineUpdateState, auto_update = true) => engineUpdateRereadMs({ id: 'model-hub-engine', update_state, auto_update });
    for (const state of ['checking', 'updating', 'rolling_back', 'withdrawing'] as const) expect([state, delay(state)]).toEqual([state, 3_000]);
    // Recovery can wait a long time for Model Hub to be idle: read slowly, and keep Check usable.
    expect(delay('recovering')).toBe(10_000);
    // The owner tries a deferred update again by itself, so an open page must catch up with it.
    expect(delay('deferred')).toBe(30_000);
    expect(delay('deferred', false)).toBe(30_000);
    // An offered build is installed by the next routine pass only while routine updates are on.
    expect(delay('available')).toBe(60_000);
    expect(delay('available', false)).toBeNull();
    for (const state of ['idle', 'up_to_date', 'applied', 'rolled_back', 'failed', 'blocked', 'unavailable', 'requires_avibe_update', 'uncommissioned', 'opted_out'] as const) {
      expect([state, delay(state)]).toEqual([state, null]);
    }
    expect(engineUpdateRereadMs({ id: 'avault', update_state: 'updating' })).toBeNull();
  });

  it('shows a build tag without its fixed prefix and keeps the build revision', () => {
    expect(engineBuildVersion(RUNNING)).toBe('8.0.63-2');
    expect(engineBuildVersion('v8.0.63')).toBe('8.0.63');
    expect(engineBuildVersion(null)).toBeNull();
  });
});
