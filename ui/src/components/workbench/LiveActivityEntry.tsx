import { useCallback, useEffect, useRef, useState } from 'react';
import { LockKeyhole, Settings } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Button } from '../ui/button';
import { useToast } from '../../context/ToastContext';
import {
  hasNativeCapability, nativeLiveActivityStatus, nativeLiveActivityStart,
  nativeLiveActivityStop, nativeLiveActivityOpenSettings,
  type NativeLiveActivityStatus,
} from '../../lib/native';

/** One native Turn, with title attributes captured only when the person starts it. */
export function LiveActivityEntry({ sessionId, agentName, sessionTitle, running }: {
  sessionId: string; agentName: string; sessionTitle: string | null; running: boolean;
}) {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const [status, setStatus] = useState<NativeLiveActivityStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const mounted = useRef(false);
  const inFlight = useRef(false);
  const supported = hasNativeCapability('liveActivities');
  const refresh = useCallback(async () => {
    const current = await nativeLiveActivityStatus(sessionId);
    if (mounted.current) setStatus(current);
  }, [sessionId]);

  useEffect(() => {
    mounted.current = true;
    if (!supported || !running) return () => { mounted.current = false; };
    const update = () => {
      if (document.visibilityState !== 'hidden') void refresh().catch(() => {
        if (mounted.current) setStatus(null);
      });
    };
    update();
    document.addEventListener('visibilitychange', update);
    window.addEventListener('pageshow', update);
    window.addEventListener('focus', update);
    return () => {
      mounted.current = false;
      document.removeEventListener('visibilitychange', update);
      window.removeEventListener('pageshow', update);
      window.removeEventListener('focus', update);
    };
  }, [refresh, running, supported]);

  const act = async () => {
    if (inFlight.current || !status) return;
    inFlight.current = true;
    setBusy(true);
    try {
      if (!status.enabled) {
        await nativeLiveActivityOpenSettings();
      } else if (status.tracking) {
        await nativeLiveActivityStop(sessionId);
      } else {
        const result = await nativeLiveActivityStart(sessionId, agentName, sessionTitle);
        if (!result) throw new Error('bridge_unavailable');
        if (!result.ok) {
          if (result.code === 'disabled') {
            if (mounted.current) setStatus({ enabled: false, tracking: false });
          } else if (mounted.current) {
            showToast(t(`liveActivity.${result.code}`), result.code === 'failed' ? 'error' : 'warning');
          }
          return;
        }
      }
      await refresh();
    } catch {
      if (mounted.current) showToast(t('liveActivity.failed'), 'error');
    } finally {
      inFlight.current = false;
      if (mounted.current) setBusy(false);
    }
  };

  if (!supported || !running || !status) return null;
  return (
    <div className="mx-auto w-full max-w-[1080px] px-3 pb-2" aria-busy={busy}>
      {!status.enabled ? (
        <div className="flex items-center gap-2 rounded-2xl border border-edge bg-surface/95 py-2 pr-2 pl-3 shadow-elevation-1">
          <LockKeyhole aria-hidden className="size-4 shrink-0 text-muted" />
          <span className="min-w-0 flex-1 text-[13px] leading-[18px] text-muted">{t('liveActivity.systemOff')}</span>
          <Button type="button" variant="outline" size="sm" disabled={busy} onClick={() => void act()} className="shrink-0 gap-1.5 rounded-full">
            <Settings aria-hidden className="size-3.5" />{t('liveActivity.settings')}
          </Button>
        </div>
      ) : status.tracking ? (
        <div className="flex items-center gap-2 rounded-full border border-mint/30 bg-mint-soft py-1.5 pr-1.5 pl-3 shadow-elevation-1">
          <LockKeyhole aria-hidden className="size-4 shrink-0 text-mint-ink" />
          <span className="min-w-0 flex-1 text-[13px] font-medium text-mint-ink">{t('liveActivity.tracking')}</span>
          <Button type="button" variant="ghost" size="sm" disabled={busy} onClick={() => void act()} className="shrink-0 rounded-full">{t('liveActivity.stop')}</Button>
        </div>
      ) : (
        <div className="flex justify-end">
          <Button type="button" variant="outline" size="sm" disabled={busy} onClick={() => void act()} className="gap-1.5 rounded-full bg-surface/95 shadow-elevation-1">
            <LockKeyhole aria-hidden className="size-3.5" />{t('liveActivity.offer')}
          </Button>
        </div>
      )}
    </div>
  );
}
