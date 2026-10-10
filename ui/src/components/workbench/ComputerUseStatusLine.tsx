import { useEffect, useSyncExternalStore } from 'react';
import type { FC } from 'react';
import { CircleAlert, CircleCheck, LoaderCircle, ShieldAlert } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { apiFetch } from '../../lib/apiFetch';
import {
  computerUseStatusCopy,
  type ComputerUseCopyKey,
  type ComputerUseStatus,
  type StatusTone,
} from './computerUseStatusCopy';

const REFRESH_INTERVAL_MS = 5_000;

let statusSnapshot: ComputerUseStatus | null = null;
const listeners = new Set<() => void>();

const subscribe = (listener: () => void) => {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0) statusSnapshot = null;
  };
};

const getStatusSnapshot = () => statusSnapshot;

const publishStatus = (next: ComputerUseStatus | null) => {
  if (
    statusSnapshot?.status === next?.status
    && statusSnapshot?.reason === next?.reason
  ) {
    return;
  }
  statusSnapshot = next;
  for (const listener of listeners) listener();
};

const refreshComputerUseStatus = async () => {
  try {
    const response = await apiFetch('/api/desktop/computer-use/status', { cache: 'no-store' });
    if (!response.ok) return;
    const payload = await response.json() as Partial<ComputerUseStatus> & { supported?: boolean };
    if (payload.supported === false) {
      publishStatus(null);
      return;
    }
    if (typeof payload.status !== 'string') return;
    publishStatus({
      status: payload.status,
      reason: typeof payload.reason === 'string' ? payload.reason : null,
    });
  } catch {
    // A transient UI response is not a Computer Use state. Keep the last
    // observed line until the next no-store refresh.
  }
};

const toneClass: Record<StatusTone, string> = {
  muted: 'border-border bg-surface text-muted',
  mint: 'border-mint/30 bg-mint-soft text-mint-ink',
  gold: 'border-gold/35 bg-gold/[0.08] text-gold-ink',
  destructive: 'border-destructive/35 bg-destructive/[0.06] text-destructive-ink',
};

const StatusIcon: FC<{ status: string }> = ({ status }) => {
  if (status === 'ready') return <CircleCheck className="size-4 shrink-0" aria-hidden="true" />;
  if (status === 'needs_permission' || status === 'needs_runtime') {
    return <ShieldAlert className="size-4 shrink-0" aria-hidden="true" />;
  }
  if (status === 'starting') return <LoaderCircle className="size-4 shrink-0 animate-spin" aria-hidden="true" />;
  if (status === 'error' || status === 'unavailable') {
    return <CircleAlert className="size-4 shrink-0" aria-hidden="true" />;
  }
  return <CircleCheck className="size-4 shrink-0" aria-hidden="true" />;
};

export const ComputerUseStatusLine: FC = () => {
  const { t } = useTranslation();
  const status = useSyncExternalStore(subscribe, getStatusSnapshot, getStatusSnapshot);

  useEffect(() => {
    void refreshComputerUseStatus();
    const interval = window.setInterval(() => void refreshComputerUseStatus(), REFRESH_INTERVAL_MS);
    const onVisibilityChange = () => {
      if (document.visibilityState === 'visible') void refreshComputerUseStatus();
    };
    document.addEventListener('visibilitychange', onVisibilityChange);
    const onFocus = () => {
      void refreshComputerUseStatus();
    };
    window.addEventListener('focus', onFocus);
    return () => {
      window.clearInterval(interval);
      document.removeEventListener('visibilitychange', onVisibilityChange);
      window.removeEventListener('focus', onFocus);
    };
  }, []);

  if (!status) return null;
  const copy = computerUseStatusCopy(status, (key: ComputerUseCopyKey) => t(key));
  return (
    <div
      aria-label={copy.label}
      className={`flex w-full items-start gap-2.5 rounded-[10px] border px-4 py-2.5 text-[12px] ${toneClass[copy.tone]}`}
      data-computer-use-status={status.status}
      data-testid="computer-use-status"
    >
      <StatusIcon status={status.status} />
      <div className="min-w-0">
        <p className="font-semibold">{copy.label}</p>
        <p className="mt-0.5 text-[11px] opacity-80">{copy.detail}</p>
      </div>
    </div>
  );
};
