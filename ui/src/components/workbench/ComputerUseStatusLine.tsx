import { useCallback, useEffect, useState } from 'react';
import { CircleAlert, CircleCheck, LoaderCircle, ShieldAlert } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { apiFetch } from '../../lib/apiFetch';

type ComputerUseStatus = {
  status: string;
  reason: string | null;
};

type ComputerUseCopyKey =
  | 'workbench.home.computerUse.ready'
  | 'workbench.home.computerUse.readyDetail'
  | 'workbench.home.computerUse.starting'
  | 'workbench.home.computerUse.startingDetail'
  | 'workbench.home.computerUse.accessibilityDetail'
  | 'workbench.home.computerUse.screenRecordingDetail'
  | 'workbench.home.computerUse.unknownPermissionDetail'
  | 'workbench.home.computerUse.needsPermission'
  | 'workbench.home.computerUse.runtimeTooOldDetail'
  | 'workbench.home.computerUse.runtimeUnavailableDetail'
  | 'workbench.home.computerUse.needsRuntime'
  | 'workbench.home.computerUse.error'
  | 'workbench.home.computerUse.errorDetail'
  | 'workbench.home.computerUse.unavailable'
  | 'workbench.home.computerUse.unavailableDetail'
  | 'workbench.home.computerUse.off'
  | 'workbench.home.computerUse.offDetail';

type StatusTone = 'muted' | 'mint' | 'gold' | 'destructive';

const REFRESH_INTERVAL_MS = 5_000;

export const computerUseStatusCopy = (
  status: ComputerUseStatus,
  t: (key: ComputerUseCopyKey) => string,
): { label: string; detail: string; tone: StatusTone } => {
  if (status.status === 'ready') {
    return { label: t('workbench.home.computerUse.ready'), detail: t('workbench.home.computerUse.readyDetail'), tone: 'mint' };
  }
  if (status.status === 'starting') {
    return { label: t('workbench.home.computerUse.starting'), detail: t('workbench.home.computerUse.startingDetail'), tone: 'gold' };
  }
  if (status.status === 'needs_permission') {
    const detail = status.reason === 'accessibility'
      ? t('workbench.home.computerUse.accessibilityDetail')
      : status.reason === 'screen_recording'
        ? t('workbench.home.computerUse.screenRecordingDetail')
        : t('workbench.home.computerUse.unknownPermissionDetail');
    return { label: t('workbench.home.computerUse.needsPermission'), detail, tone: 'gold' };
  }
  if (status.status === 'needs_runtime') {
    const detail = status.reason === 'runtime_too_old'
      ? t('workbench.home.computerUse.runtimeTooOldDetail')
      : t('workbench.home.computerUse.runtimeUnavailableDetail');
    return { label: t('workbench.home.computerUse.needsRuntime'), detail, tone: 'gold' };
  }
  if (status.status === 'error') {
    return { label: t('workbench.home.computerUse.error'), detail: t('workbench.home.computerUse.errorDetail'), tone: 'destructive' };
  }
  if (status.status === 'unavailable') {
    return { label: t('workbench.home.computerUse.unavailable'), detail: t('workbench.home.computerUse.unavailableDetail'), tone: 'destructive' };
  }
  return { label: t('workbench.home.computerUse.off'), detail: t('workbench.home.computerUse.offDetail'), tone: 'muted' };
};

const toneClass: Record<StatusTone, string> = {
  muted: 'border-border bg-surface text-muted',
  mint: 'border-mint/30 bg-mint-soft text-mint-ink',
  gold: 'border-gold/35 bg-gold/[0.08] text-gold-ink',
  destructive: 'border-destructive/35 bg-destructive/[0.06] text-destructive-ink',
};

const StatusIcon: React.FC<{ status: string }> = ({ status }) => {
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

export const ComputerUseStatusLine: React.FC = () => {
  const { t } = useTranslation();
  const [status, setStatus] = useState<ComputerUseStatus | null>(null);

  const refresh = useCallback(async () => {
    try {
      const response = await apiFetch('/api/desktop/computer-use/status', { cache: 'no-store' });
      if (!response.ok) return;
      const payload = await response.json() as Partial<ComputerUseStatus>;
      if (typeof payload.status !== 'string') return;
      setStatus({
        status: payload.status,
        reason: typeof payload.reason === 'string' ? payload.reason : null,
      });
    } catch {
      // A transient UI response is not a Computer Use state. Keep the last
      // observed line until the next no-store refresh.
    }
  }, []);

  useEffect(() => {
    void refresh();
    const interval = window.setInterval(() => void refresh(), REFRESH_INTERVAL_MS);
    const onVisibilityChange = () => {
      if (document.visibilityState === 'visible') void refresh();
    };
    document.addEventListener('visibilitychange', onVisibilityChange);
    const onFocus = () => {
      void refresh();
    };
    window.addEventListener('focus', onFocus);
    return () => {
      window.clearInterval(interval);
      document.removeEventListener('visibilitychange', onVisibilityChange);
      window.removeEventListener('focus', onFocus);
    };
  }, [refresh]);

  if (!status) return null;
  const copy = computerUseStatusCopy(status, t);
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
