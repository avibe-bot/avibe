// 「优化线路」 as the tunnel's own recovery reports it: evaluating now, pressable again only a
// moment after the last attempt, and when the automatic one comes next. A press that did not start
// an evaluation says why, and never claims that one began.
import { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import type { TFunction } from 'i18next';

import type { RemoteAccessStatus } from '@/context/ApiContext';
import type { TranslationKey } from '@/i18n/types';
import { useDeadlineClock } from '../settings/models/useDeadlineClock';
import { devicesApi } from './api';
import type { OptimizeState } from './types';

/** A manual optimization can be asked for again this long after the last attempt ended. */
const MANUAL_DEBOUNCE_MS = 10_000;
/** A quality sample older than this says nothing about now. */
const QUALITY_FRESH_MS = 150_000;

const problemKeys = {
  remote_access_not_running: 'errors.remote_access_not_running',
  route_optimization_evaluating: 'errors.route_optimization_evaluating',
  route_optimization_evidence_unavailable: 'errors.route_optimization_evidence_unavailable',
  remote_access_busy: 'errors.remote_access_busy',
  route_optimization_start_failed: 'errors.route_optimization_start_failed',
} as const satisfies Record<string, TranslationKey>;

function problemText(t: TFunction, answer: RemoteAccessStatus): string {
  const code = answer.error ?? '';
  if (code === 'route_optimization_debounce') return t('errors.route_optimization_debounce', { seconds: answer.retry_after_seconds ?? 0 });
  return code in problemKeys ? t(problemKeys[code as keyof typeof problemKeys]) : t('errors.remote_access_unknown');
}

export function useOptimizeRoute({ status, statusAt, onStatus, refresh }: {
  status: RemoteAccessStatus | null;
  /** When `status` was read. */
  statusAt: number;
  /** The answer to a press carries the status as it is now. */
  onStatus: (status: RemoteAccessStatus) => void;
  refresh: () => Promise<unknown> | void;
}): { optimize: OptimizeState; recovering: boolean; run: () => void } {
  const { t, i18n } = useTranslation();
  const [pressed, setPressed] = useState<{ busy?: boolean; started?: boolean; problem?: string }>({});
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => { alive.current = false; };
  }, []);

  const quality = status?.tunnel_quality;
  const recovering = quality?.recovery.state === 'evaluating' || quality?.recovery.state === 'draining';
  const lastAttempt = Date.parse(quality?.recovery.last_attempt_at || '');
  const manualRetryAt = status?.error === 'route_optimization_debounce' && status.retry_at
    ? status.retry_at
    : Number.isFinite(lastAttempt) ? new Date(lastAttempt + MANUAL_DEBOUNCE_MS).toISOString() : null;
  const autoRetryAt = quality?.recovery.next_attempt_at ?? null;
  const manualClock = useDeadlineClock(manualRetryAt);
  const automaticClock = useDeadlineClock(autoRetryAt);
  const clock = Math.max(manualClock, automaticClock);
  const clockTime = (value: string) => new Date(value).toLocaleTimeString(i18n.language);
  const manualWait = Boolean(manualRetryAt && Date.parse(manualRetryAt) > clock);
  const fresh = quality ? statusAt - Date.parse(quality.sampled_at) <= QUALITY_FRESH_MS : false;
  const wait: OptimizeState['wait'] = !status?.running || !fresh || recovering ? undefined
    : manualWait && manualRetryAt ? { kind: 'manual', time: clockTime(manualRetryAt) }
      : status.settings?.auto_recovery && autoRetryAt
        ? Date.parse(autoRetryAt) > clock ? { kind: 'automatic', time: clockTime(autoRetryAt) } : { kind: 'due' }
        : undefined;

  const run = useCallback(() => {
    setPressed({ busy: true });
    void (async () => {
      try {
        const answer = await devicesApi.optimizeRoute();
        if (!alive.current) return;
        if (typeof answer.running === 'boolean') onStatus(answer);
        setPressed(answer.ok === true ? { started: true } : { problem: problemText(t, answer) });
      } catch {
        if (alive.current) setPressed({ problem: t('errors.remote_access_unknown') });
      }
      await refresh();
    })();
  }, [onStatus, refresh, t]);

  return {
    optimize: { ...pressed, busy: pressed.busy || recovering, disabled: !status?.running || manualWait, wait },
    recovering,
    run,
  };
}
