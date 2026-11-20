// The page's side of a reset-card use: the confirm step, the one request a
// confirmed press sends, and what its answer leaves on screen.
//
// Only the user's own 「确认使用」 sends a use, and never twice: a press while one
// is in flight for the account is dropped here, and the server holds its own lock
// beside it. Nothing is retried. An answer that never arrived may still have spent
// the card, so it reads as unconfirmed and holds the account's cards until a
// refresh lands; the quota read that follows shows the server's own lock.
import * as React from 'react';

import { ApiCallError, modelsApi } from './modelsApi';
import type { ResetCardIntent, ResetCardPhase } from './ResetCards';
import type { QuotaSummary, ResetCardKind } from './types';

type Phases = Readonly<Record<string, ResetCardPhase>>;

/** The server's fresh count for the kind is not the one the user confirmed on; nothing was sent. */
const COUNT_CHANGED = 'reset_card_count_changed';

/** What a failed request says about the card. */
const failurePhase = (error: unknown, kind: ResetCardKind): ResetCardPhase => {
  if (error instanceof ApiCallError && error.serverNamed) {
    const status = error.responseStatus;
    if (status === 409 && error.code === COUNT_CHANGED) return { phase: 'recounted', kind };
    // `reset_card_locked` / `reset_card_unavailable`: refused before anything was sent (the account
    // is busy, its lock could not be recorded, or it holds no usable card of this kind).
    if (status === 409) return { phase: 'not_used', kind, reason: 'held' };
    // Refusals the server names before anything is sent: a malformed body, the reader's
    // role, the feature switched off.
    if ((status === 400 && error.code === 'invalid_parameter') || status === 401 || status === 403
      || (status === 404 && error.code === 'feature_disabled')) {
      return { phase: 'not_used', kind, reason: null };
    }
  }
  // No answer, the server failed partway, or an answer that names no outcome: the card may be spent.
  return { phase: 'unconfirmed', kind };
};

export function useResetCardActions({ land, reread, afterUse }: {
  /** Installs the summary the use answered with, as the newest quota reading. */
  land: (quota: QuotaSummary) => Promise<void>;
  /** Reads the quota again without asking the vendors (the server's lock and counts). */
  reread: () => Promise<void>;
  /** A use can resume an account paused for quota: read the providers behind it. */
  afterUse: () => void;
}) {
  const [phases, setPhases] = React.useState<Phases>({});
  const inFlight = React.useRef(new Set<string>());
  const alive = React.useRef(true);
  React.useEffect(() => {
    alive.current = true;
    return () => { alive.current = false; };
  }, []);
  const set = React.useCallback((sourceId: string, phase: ResetCardPhase | null) => {
    if (!alive.current) return;
    setPhases((previous) => {
      const next = { ...previous };
      if (phase) next[sourceId] = phase;
      else delete next[sourceId];
      return next;
    });
  }, []);

  const use = React.useCallback(async (sourceId: string, kind: ResetCardKind, stated: boolean, count: number) => {
    if (inFlight.current.has(sourceId)) return;
    inFlight.current.add(sourceId);
    set(sourceId, { phase: 'using', kind, stated });
    try {
      const { quota, use: answer } = await modelsApi.redeemResetCard(sourceId, kind, count);
      await land(quota);
      set(sourceId, answer.outcome === 'used'
        ? { phase: 'used', kind, stated, cleared: answer.cleared }
        : answer.outcome === 'not_used'
          ? { phase: 'not_used', kind, reason: answer.reason }
          : { phase: 'unconfirmed', kind });
    } catch (error) {
      set(sourceId, failurePhase(error, kind));
      // A refusal carries the fresh reading to redraw from; anything else is read again.
      if (error instanceof ApiCallError && error.quota) await land(error.quota);
      else await reread().catch(() => undefined);
    } finally {
      inFlight.current.delete(sourceId);
      if (alive.current) afterUse();
    }
  }, [afterUse, land, reread, set]);

  const onIntent = React.useCallback((sourceId: string, intent: ResetCardIntent) => {
    if (inFlight.current.has(sourceId)) return;
    if (intent.type === 'ask') {
      set(sourceId, { phase: 'confirm', kind: intent.kind, stated: intent.stated, count: intent.count });
      return;
    }
    const current = phases[sourceId];
    if (current?.phase !== 'confirm') return;
    if (intent.type === 'cancel') {
      set(sourceId, null);
      return;
    }
    if (intent.kind === current.kind) void use(sourceId, current.kind, current.stated, current.count);
  }, [phases, set, use]);

  /**
   * A refresh landed with a current reading: what it shows replaces this page's
   * last outcome for the account (every account when `sourceId` is null). A step
   * the user is in, or a use still in flight, stays.
   */
  const settleRefresh = React.useCallback((sourceId: string | null) => {
    if (!alive.current) return;
    setPhases((previous) => Object.fromEntries(Object.entries(previous).filter(([id, phase]) => (
      (sourceId !== null && id !== sourceId) || phase.phase === 'confirm' || phase.phase === 'using'
    ))));
  }, []);

  /**
   * A reading landed (a use's answer, a refresh, a poll): a confirm step whose card it no
   * longer lists as usable, at the count confirmed on, or whose account it shows locked,
   * is dropped, so a later reading never brings back a stale question.
   */
  const settleReading = React.useCallback((summary: QuotaSummary) => {
    if (!alive.current) return;
    const bySource = new Map(summary.sources.map((source) => [source.source_id, source.reset_cards]));
    setPhases((previous) => {
      let changed = false;
      const next: Record<string, ResetCardPhase> = { ...previous };
      for (const [sourceId, phase] of Object.entries(previous)) {
        if (phase.phase !== 'confirm') continue;
        const cards = bySource.get(sourceId);
        const card = cards?.cards.find((row) => row.kind === phase.kind);
        if (!cards || cards.lock !== null || !card?.usable || card.count !== phase.count) {
          delete next[sourceId];
          changed = true;
        }
      }
      return changed ? next : previous;
    });
  }, []);

  return { phases, onIntent, settleRefresh, settleReading };
}
