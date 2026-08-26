import { describe, expect, it } from 'vitest';

import { sourceStatePresentation, type SourceStateSurface } from './sourceStatePresentation';
import { SOURCE_STATUSES } from './types';
import type { SourceState, SourceStatus } from './types';

const state = (status: SourceStatus, over: Partial<SourceState> = {}): SourceState => ({
  status,
  retry_at: null,
  detail_key: null,
  ...over,
});

describe('sourceStatePresentation', () => {
  it('omits attribution when the source projection does not carry it', () => {
    expect(sourceStatePresentation(state('active'), 'card', 'en', 0).key).toBeNull();
    expect(sourceStatePresentation(state('active'), 'detail', 'en', 0).key).toBeNull();
    expect(sourceStatePresentation(state('active'), 'card', 'en', 0, {
      known: true,
      backends: [],
      native: false,
    }).key).toBe('settings.models.upstream.state.standby');
  });

  it('uses a creation response adoption without reconstructing it from chains', () => {
    expect(sourceStatePresentation(state('active'), 'card', 'en', 0, {
      known: true,
      backends: ['Claude Code', 'Codex'],
      native: false,
    })).toMatchObject({
      key: 'settings.models.upstream.state.supplying',
      values: { backends: 'Claude Code, Codex' },
      dotClass: 'bg-mint',
    });
    expect(sourceStatePresentation(state('active'), 'detail', 'en', 0, {
      known: true,
      backends: ['Claude Code'],
      native: true,
    })).toMatchObject({ key: 'settings.models.sourceDetail.status.inUse', dotClass: 'bg-mint' });
  });

  it('renders a future cooldown as a localized duration', () => {
    const view = sourceStatePresentation(
      state('cooldown', { retry_at: '2026-08-11T10:05:00Z' }),
      'detail',
      'en',
      Date.parse('2026-08-11T10:00:00Z'),
    );
    expect(view).toMatchObject({
      key: 'settings.models.upstream.state.unavailableRetry',
      values: { delay: '5 minutes' },
    });
  });

  it('does not imply another request ran after a cooldown deadline passed', () => {
    expect(sourceStatePresentation(
      state('cooldown', { retry_at: '2026-08-11T09:59:00Z' }),
      'detail',
      'en',
      Date.parse('2026-08-11T10:00:00Z'),
    ).key).toBe('settings.models.upstream.state.unavailableDue');
  });

  // The explanation belongs to the reading, not to one surface: every path that
  // produces 备用 carries it and no other state borrows it. Stated over the whole
  // status union and both surfaces so a status added later has to decide rather
  // than inherit silence, and so a surface cannot quietly drop the sentence.
  it('explains the standby reading wherever it is produced, and nothing else', () => {
    const surfaces: SourceStateSurface[] = ['card', 'detail'];
    const adoptions = [
      undefined,
      { known: true, backends: [], native: false },
      { known: true, backends: ['Claude Code'], native: false },
    ];
    const readings = SOURCE_STATUSES.flatMap((status) => surfaces.flatMap((surface) => adoptions.map(
      (adoption) => sourceStatePresentation(state(status), surface, 'en', 0, adoption),
    )));

    for (const reading of readings) {
      expect(Boolean(reading.hint)).toBe(reading.key === 'settings.models.upstream.state.standby');
    }
    expect(readings.filter((reading) => reading.hint).length).toBeGreaterThan(0);
    expect(sourceStatePresentation(state('standby'), 'detail', 'en', 0).hint).toEqual({
      labelKey: 'settings.models.sourceDetail.status.standbyHintLabel',
      bodyKey: 'settings.models.sourceDetail.status.standbyHint',
    });
  });

  it('maps each needs-action cause to the copy register', () => {
    expect(sourceStatePresentation(state('needs_action', {
      detail_key: 'models.source.needs_action.balance_exhausted',
    }), 'detail', 'en', 0).key).toBe('settings.models.sourceDetail.status.needsAction.balanceExhausted');
  });
});
