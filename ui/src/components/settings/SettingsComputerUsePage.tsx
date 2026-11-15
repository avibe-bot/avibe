import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { FC, ReactNode } from 'react';
import { AppWindow, Check, Hand, CircleAlert, CircleCheck, CircleStop, ExternalLink, Info, LoaderCircle, Monitor, Power, RotateCw, ShieldAlert, ShieldX } from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { computerUseShellActions, isDesktopShell, type ComputerUsePermissionPane } from '@/lib/desktopShell';
import type { TranslationKey } from '@/i18n/types';
import {
  COMPUTER_USE_GUIDE_REFRESH_INTERVAL_MS,
  COMPUTER_USE_GUIDE_STEPS,
  computerUseGuideProgress,
  computerUseToneClass,
  readComputerUseStatus,
  type ComputerUseGuideStep,
  type ComputerUseGuideStepState,
  type ComputerUseStatusRead,
} from '../workbench/computerUseStatus';
import { computerUseStatusCopy, type ComputerUseCopyKey } from '../workbench/computerUseStatusCopy';
import { SettingsPageShell } from './SettingsPageShell';
import { SettingsPanel } from './SettingsPrimitives';

const STEP_COPY: Record<ComputerUseGuideStep, { title: TranslationKey; detail: TranslationKey }> = {
  enable: {
    title: 'settings.computerUse.steps.enable.title',
    detail: 'settings.computerUse.steps.enable.detail',
  },
  accessibility: {
    title: 'settings.computerUse.steps.accessibility.title',
    detail: 'settings.computerUse.steps.accessibility.detail',
  },
  screenRecording: {
    title: 'settings.computerUse.steps.screenRecording.title',
    detail: 'settings.computerUse.steps.screenRecording.detail',
  },
};

/** The privacy pane each permission step opens; `enable` turns the feature on instead. */
const STEP_PANE: Partial<Record<ComputerUseGuideStep, ComputerUsePermissionPane>> = {
  accessibility: 'accessibility',
  screenRecording: 'screen_recording',
};

/**
 * Windows grants no per-app privacy permissions, so its guide is the switch
 * alone. The macOS Accessibility and Screen Recording steps never appear there.
 */
const WINDOWS_GUIDE_STEPS: readonly ComputerUseGuideStep[] = ['enable'];

const WINDOWS_NOTES: { testId: string; icon: LucideIcon; text: TranslationKey }[] = [
  { testId: 'computer-use-windows-apps', icon: AppWindow, text: 'settings.computerUse.windows.apps' },
  { testId: 'computer-use-windows-elevated', icon: ShieldX, text: 'settings.computerUse.windows.elevated' },
  { testId: 'computer-use-windows-launch', icon: Hand, text: 'settings.computerUse.windows.launch' },
  { testId: 'computer-use-windows-stop', icon: CircleStop, text: 'settings.computerUse.windows.stop' },
];

const PreviewBadge: FC = () => {
  const { t } = useTranslation();
  return (
    <Badge variant="warning" data-testid="computer-use-preview-badge">
      {t('settings.computerUse.windows.previewBadge')}
    </Badge>
  );
};

const STEP_STATE_LABEL: Record<ComputerUseGuideStepState, TranslationKey> = {
  complete: 'settings.computerUse.stepState.complete',
  current: 'settings.computerUse.stepState.current',
  pending: 'settings.computerUse.stepState.pending',
};

const ComputerUseStatusIcon: FC<{ status: string }> = ({ status }) => {
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

const StepMarker: FC<{ index: number; state: ComputerUseGuideStepState }> = ({ index, state }) => (
  <span
    aria-hidden="true"
    className={clsx(
      'grid size-6 shrink-0 place-items-center rounded-full border text-[11px] font-semibold',
      state === 'complete' && 'border-mint/40 bg-mint-soft text-mint-ink',
      state === 'current' && 'border-gold/60 bg-gold/[0.12] text-gold-ink',
      state === 'pending' && 'border-edge text-muted bg-field',
    )}
  >
    {state === 'complete' ? <Check className="size-3.5" /> : index + 1}
  </span>
);

const GuideNote: FC<{ testId: string; tone?: 'muted' | 'destructive'; children: ReactNode }> = ({
  testId,
  tone = 'muted',
  children,
}) => (
  <p
    data-testid={testId}
    className={clsx(
      'mt-1.5 flex max-w-2xl items-start gap-1.5 text-[12px] leading-relaxed',
      tone === 'destructive' ? 'text-destructive-ink' : 'text-foreground',
    )}
  >
    <Info className="mt-0.5 size-3.5 shrink-0" aria-hidden="true" />
    <span>{children}</span>
  </p>
);

/**
 * Settings → Computer Use: the macOS permission setup as one numbered guide
 * whose progress is read from the desktop status endpoint while the page is
 * visible. Inside the desktop shell incomplete permission steps can open their
 * settings even while the driver is unavailable; other hosts follow read-only.
 * A Windows host (the server's `host_platform`, in the shell or any browser)
 * shows the Preview notes and the switch alone, without macOS permission steps.
 */
export const SettingsComputerUsePage: FC = () => {
  const { t } = useTranslation();
  const desktopShell = isDesktopShell();
  const actions = useMemo(() => computerUseShellActions(), []);
  const [read, setRead] = useState<ComputerUseStatusRead | null>(null);
  const [checking, setChecking] = useState(false);
  const [runningStep, setRunningStep] = useState<ComputerUseGuideStep | null>(null);
  const [failedStep, setFailedStep] = useState<ComputerUseGuideStep | null>(null);
  const [screenRecordingRequested, setScreenRecordingRequested] = useState(false);
  // At most one status read is in flight. Every caller shares it rather than
  // starting a newer one: a read is never invalidated, so even reads slower
  // than the refresh interval (`ready` can wait on the Controller) land.
  const inFlight = useRef<Promise<void> | null>(null);
  const mounted = useRef(true);

  // A transient failure (`null`) keeps the last observed state.
  const readStatus = useCallback((): Promise<void> => {
    if (!inFlight.current) {
      inFlight.current = readComputerUseStatus()
        .then((next) => {
          if (mounted.current && next) setRead(next);
        })
        .finally(() => {
          inFlight.current = null;
        });
    }
    return inFlight.current;
  }, []);

  const recheck = () => {
    setChecking(true);
    void readStatus().then(() => {
      if (mounted.current) setChecking(false);
    });
  };

  useEffect(() => {
    mounted.current = true;
    // One polling chain while the page is visible: the next read is scheduled
    // only after the current one settles. `chained` is true while the chain
    // owns either a pending timer or a read it is waiting for.
    let active = true;
    let chained = false;
    let timer: number | undefined;
    const visible = () => document.visibilityState !== 'hidden';
    const step = () => {
      timer = undefined;
      chained = true;
      void readStatus().then(() => {
        if (!active || !visible()) {
          chained = false;
          return;
        }
        timer = window.setTimeout(step, COMPUTER_USE_GUIDE_REFRESH_INTERVAL_MS);
      });
    };
    const onVisibilityChange = () => {
      if (visible()) {
        // Returning reads at once, since granting happens in System Settings.
        if (!chained) step();
      } else if (timer !== undefined) {
        window.clearTimeout(timer);
        timer = undefined;
        chained = false;
      }
    };
    const onFocus = () => void readStatus();
    if (visible()) step();
    else void readStatus();
    window.addEventListener('focus', onFocus);
    document.addEventListener('visibilitychange', onVisibilityChange);
    return () => {
      active = false;
      mounted.current = false;
      window.clearTimeout(timer);
      window.removeEventListener('focus', onFocus);
      document.removeEventListener('visibilitychange', onVisibilityChange);
    };
  }, [readStatus]);

  const runStep = (step: ComputerUseGuideStep) => {
    if (!actions) return;
    const pane = STEP_PANE[step];
    setRunningStep(step);
    setFailedStep(null);
    void (pane ? actions.openPermission(pane) : actions.enable())
      .then(
        () => {
          if (step === 'screenRecording') setScreenRecordingRequested(true);
        },
        () => setFailedStep(step),
      )
      .finally(() => {
        setRunningStep(null);
        void readStatus();
      });
  };

  // The Runtime host's OS, as the server reports it. The desktop shell only
  // decides whether this page can run the native actions, never the host.
  const host = read?.host ?? 'macos';
  const windows = host === 'windows';

  if (read?.kind === 'unsupported') {
    return (
      <SettingsPageShell
        activeTab="computer-use"
        title={t('settings.computerUse.title')}
        subtitle={windows ? t('settings.computerUse.windows.subtitle') : t('settings.computerUse.subtitle')}
      >
        <SettingsPanel variant="preference">
          <div className="flex items-start gap-3" data-testid="computer-use-unsupported">
            <Monitor className="mt-0.5 size-5 shrink-0 text-muted" aria-hidden="true" />
            <div className="flex min-w-0 flex-col gap-1">
              <p className="flex items-center gap-2 text-[14px] font-semibold text-foreground">
                {t('settings.computerUse.unsupportedTitle')}
                {windows && <PreviewBadge />}
              </p>
              <p className="text-[12px] leading-relaxed text-muted">
                {windows
                  ? t('settings.computerUse.windows.unsupportedDetail')
                  : t('settings.computerUse.unsupportedDetail')}
              </p>
            </div>
          </div>
        </SettingsPanel>
      </SettingsPageShell>
    );
  }

  const status = read?.kind === 'status' ? read.status : null;
  const copy = status ? computerUseStatusCopy(status, (key: ComputerUseCopyKey) => t(key), host) : null;
  const progress = status ? computerUseGuideProgress(status) : null;
  const steps = windows ? WINDOWS_GUIDE_STEPS : COMPUTER_USE_GUIDE_STEPS;

  return (
    <SettingsPageShell
      activeTab="computer-use"
      title={t('settings.computerUse.title')}
      subtitle={windows ? t('settings.computerUse.windows.subtitle') : t('settings.computerUse.subtitle')}
      actions={
        <Button
          type="button"
          variant="ghost"
          size="xs"
          disabled={checking}
          onClick={recheck}
        >
          <RotateCw className={clsx('size-3.5', checking && 'animate-spin')} aria-hidden="true" />
          {checking ? t('settings.computerUse.checking') : t('settings.computerUse.recheck')}
        </Button>
      }
    >
      {!desktopShell && (
        <SettingsPanel variant="preference">
          <div className="flex items-start gap-3" data-testid="computer-use-remote-notice">
            <Monitor className="mt-0.5 size-5 shrink-0 text-muted" aria-hidden="true" />
            <p className="text-[13px] font-medium text-foreground">
              {windows ? t('settings.computerUse.windows.remoteNotice') : t('settings.computerUse.remoteNotice')}
            </p>
          </div>
        </SettingsPanel>
      )}

      {windows && (
        <SettingsPanel
          title={(
            <span className="flex items-center gap-2">
              {t('settings.computerUse.windows.title')}
              <PreviewBadge />
            </span>
          )}
          description={t('settings.computerUse.windows.previewDetail')}
        >
          <ul className="flex flex-col gap-2.5 px-5 py-4" data-testid="computer-use-windows-notes">
            {WINDOWS_NOTES.map(({ testId, icon: Icon, text }) => (
              <li
                key={testId}
                data-testid={testId}
                className="flex max-w-2xl items-start gap-2 text-[12px] leading-relaxed text-foreground"
              >
                <Icon className="mt-0.5 size-3.5 shrink-0 text-muted" aria-hidden="true" />
                <span>{t(text)}</span>
              </li>
            ))}
          </ul>
        </SettingsPanel>
      )}

      <div
        role="status"
        aria-live="polite"
        className={clsx(
          'flex items-start gap-2.5 rounded-[10px] border px-4 py-3 text-[12px]',
          computerUseToneClass[copy?.tone ?? 'muted'],
        )}
        data-testid="computer-use-guide-status"
        data-computer-use-status={status?.status ?? 'loading'}
      >
        {status
          ? <ComputerUseStatusIcon status={status.status} />
          : <LoaderCircle className="size-4 shrink-0 animate-spin" aria-hidden="true" />}
        <div className="min-w-0">
          <p className="font-semibold">{copy?.label ?? t('settings.computerUse.loading')}</p>
          {copy && <p className="mt-0.5 text-[11px] opacity-80">{copy.detail}</p>}
        </div>
      </div>

      <SettingsPanel title={t('settings.computerUse.stepsTitle')}>
        <ol className="flex flex-col" data-testid="computer-use-steps">
          {steps.map((step, index) => {
            const state = progress?.[step] ?? 'pending';
            const actionable = actions !== null && progress !== null
              && (step === 'enable' ? state === 'current' : state !== 'complete');
            const running = runningStep === step;
            return (
              <li
                key={step}
                aria-current={state === 'current' ? 'step' : undefined}
                data-step={step}
                data-step-state={state}
                className={clsx(
                  'flex items-start gap-3 border-b border-border px-5 py-3.5 last:border-b-0',
                  state === 'current' && 'bg-gold/[0.05]',
                )}
              >
                <StepMarker index={index} state={state} />
                <div className="flex min-w-0 flex-1 flex-col gap-0.5">
                  <p
                    className={clsx(
                      'flex items-center gap-2 text-[13px] font-medium',
                      state === 'pending' ? 'text-muted' : 'text-foreground',
                    )}
                  >
                    {t(STEP_COPY[step].title)}
                    {state === 'complete' && (
                      <Badge variant="success" aria-hidden="true">{t(STEP_STATE_LABEL.complete)}</Badge>
                    )}
                    <span className="sr-only">{` — ${t(STEP_STATE_LABEL[state])}`}</span>
                  </p>
                  <p className="max-w-2xl text-[12px] leading-relaxed text-muted">
                    {windows && step === 'enable'
                      ? t('settings.computerUse.windows.enableDetail')
                      : t(STEP_COPY[step].detail)}
                  </p>
                  {actionable && failedStep === step && (
                    <GuideNote testId="computer-use-action-failed" tone="destructive">
                      {windows
                        ? t('settings.computerUse.windows.actionFailed')
                        : t('settings.computerUse.actionFailed')}
                    </GuideNote>
                  )}
                  {step === 'screenRecording' && screenRecordingRequested && state !== 'complete' && (
                    <GuideNote testId="computer-use-relaunch-notice">
                      {t('settings.computerUse.relaunchNotice')}
                    </GuideNote>
                  )}
                </div>
                {actionable && (
                  <Button
                    type="button"
                    variant={step === 'enable' ? 'default' : 'secondary'}
                    size="xs"
                    className="shrink-0"
                    disabled={runningStep !== null}
                    onClick={() => runStep(step)}
                    data-testid={`computer-use-action-${step}`}
                  >
                    {running
                      ? <LoaderCircle className="animate-spin" aria-hidden="true" />
                      : step === 'enable'
                        ? <Power aria-hidden="true" />
                        : <ExternalLink aria-hidden="true" />}
                    {step === 'enable'
                      ? t('settings.computerUse.actions.enable')
                      : t('settings.computerUse.actions.openSettings')}
                  </Button>
                )}
              </li>
            );
          })}
        </ol>
      </SettingsPanel>
    </SettingsPageShell>
  );
};
