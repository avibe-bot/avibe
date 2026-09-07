import { describe, expect, it } from 'vitest';

import { sourceStatePresentation, type SourceStateSurface } from './sourceStatePresentation';
import { SOURCE_STATUSES } from './types';
import type { SourceState, SourceStatus, SupplyChannel } from './types';

/**
 * How each supply channel reaches the `native` flag both callers pass — stated as
 * a total Record so a channel added later has to say which reading it takes.
 */
const CHANNEL_IS_NATIVE: Readonly<Record<SupplyChannel, boolean>> = {
  native_cli: true,
  hub: false,
};

const state = (status: SourceStatus, over: Partial<SourceState> = {}): SourceState => ({
  status,
  retry_at: null,
  detail_key: null,
  ...over,
});

describe('sourceStatePresentation', () => {
  it.each(['card', 'detail'] as const)('keeps unverified %s sources distinct from adopted healthy sources', (surface) => {
    for (const status of SOURCE_STATUSES) {
      const current = state(status);
      const adoption = { known: true, backends: ['Codex'], native: false };
      const presentation = sourceStatePresentation(current, surface, 'en', 0, { ...adoption, verificationPending: true });
      if (status === 'active' || status === 'standby') {
        expect(presentation.key).toBe('settings.models.sourceDetail.status.unverified');
        expect(presentation.dotClass).toBe('bg-gold');
      } else {
        expect(presentation).toEqual(sourceStatePresentation(current, surface, 'en', 0, adoption));
      }
    }
  });

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

  it('uses persisted route adoption for healthy gateway sources in standby', () => {
    expect(sourceStatePresentation(state('standby'), 'card', 'zh', 0, {
      known: true,
      backends: ['Claude Code', 'Codex'],
      native: false,
    })).toMatchObject({
      key: 'settings.models.upstream.state.supplying',
      values: { backends: 'Claude Code、Codex' },
      dotClass: 'bg-mint',
    });
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

  // The hint promises one transition, so it is honest only if every supply
  // channel can make it — and both can, for the same reason: adoption is read off
  // route hops, and a source of either channel is a route candidate exactly while
  // its agent is in Gateway mode. 「Supplying … (native)」 is that transition's own
  // landing state, which is why naming the mode is what keeps the sentence true
  // for a native source rather than what excludes it.
  it('promises a transition every supply channel can make', () => {
    for (const native of Object.values(CHANNEL_IS_NATIVE)) {
      expect(sourceStatePresentation(state('standby'), 'detail', 'en', 0, {
        known: true,
        backends: [],
        native,
      }).hint).toBeTruthy();

      const adopted = sourceStatePresentation(state('active'), 'card', 'en', 0, {
        known: true,
        backends: ['Claude Code'],
        native,
      });
      expect(adopted.key).toBe(native
        ? 'settings.models.upstream.state.supplyingNative'
        : 'settings.models.upstream.state.supplying');
      expect(adopted.hint).toBeUndefined();
    }
  });

  it('maps each needs-action cause to the copy register', () => {
    expect(sourceStatePresentation(state('needs_action', {
      detail_key: 'models.source.needs_action.balance_exhausted',
    }), 'detail', 'en', 0).key).toBe('settings.models.sourceDetail.status.needsAction.balanceExhausted');
  });
});
