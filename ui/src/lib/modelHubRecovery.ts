import { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { startVisibleTicker } from './useVisibleNow';

const REVEAL_AFTER_MS = 5000;

type RecoveryProgress = {
  requestId: string;
  phase: 'waiting' | 'attempting';
  startedAt: number;
  nextEligibleAt: number | null;
};

function utcTimestamp(value: unknown): number | null {
  if (typeof value !== 'string'
    || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)$/.test(value)) return null;
  const parsed = Date.parse(value);
  // Date.parse normalizes impossible calendar dates such as February 30.
  return Number.isFinite(parsed) && new Date(parsed).toISOString().slice(0, 19) === value.slice(0, 19)
    ? parsed : null;
}

function selectRecovery(snapshot: unknown): RecoveryProgress | null {
  if (!Array.isArray(snapshot)) return null;
  let selected: RecoveryProgress | null = null;
  for (const entry of snapshot) {
    if (!entry || typeof entry !== 'object'
      || typeof entry.request_id !== 'string' || !entry.request_id.trim()
      || (entry.phase !== 'waiting' && entry.phase !== 'attempting')
      || !Number.isSafeInteger(entry.attempt_count) || entry.attempt_count < 0) continue;
    const startedAt = utcTimestamp(entry.started_at);
    const windowEnd = utcTimestamp(entry.window_end);
    if (startedAt === null || windowEnd === null || windowEnd < startedAt) continue;
    // One label follows the oldest pending request, independent of array order.
    // Source/reason are diagnostic fields and never participate in display text.
    if (!selected || startedAt < selected.startedAt
      || (startedAt === selected.startedAt && entry.request_id < selected.requestId)) {
      selected = {
        requestId: entry.request_id,
        phase: entry.phase,
        startedAt,
        nextEligibleAt: utcTimestamp(entry.next_eligible_at),
      };
    }
  }
  return selected;
}

/** A display clock only: server snapshots own attempts, clear, and settlement. */
export function useModelHubRecovery(snapshot: unknown, working: boolean): {
  active: boolean;
  label: string | null;
} {
  const { t } = useTranslation();
  const recovery = useMemo(() => working ? selectRecovery(snapshot) : null, [snapshot, working]);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!recovery) return;
    const tick = () => setNow(Date.now());
    const stop = startVisibleTicker(tick, 1000);
    // Reveal at the server start + 5s even when hydration lands between ticks.
    const remaining = recovery.startedAt + REVEAL_AFTER_MS - Date.now();
    const reveal = remaining > 0 && remaining <= REVEAL_AFTER_MS
      ? window.setTimeout(tick, remaining) : null;
    return () => {
      stop();
      if (reveal !== null) window.clearTimeout(reveal);
    };
  }, [recovery]);

  if (!recovery || now - recovery.startedAt < REVEAL_AFTER_MS) {
    return { active: recovery !== null, label: null };
  }
  if (recovery.phase === 'attempting') {
    return { active: true, label: t('chat.modelRecovery.attempting') };
  }
  const seconds = recovery.nextEligibleAt === null ? 0 : Math.ceil((recovery.nextEligibleAt - now) / 1000);
  return {
    active: true,
    label: seconds > 0
      ? t('chat.modelRecovery.waitingSeconds', { seconds })
      : t('chat.modelRecovery.waiting'),
  };
}
