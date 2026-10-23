import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { FC, ReactNode } from 'react';
import { Check, ExternalLink, Info, LoaderCircle, Monitor, Power, RotateCw } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { computerUseShellActions, isDesktopShell, type ComputerUsePermissionPane } from '@/lib/desktopShell';
import type { TranslationKey } from '@/i18n/types';
import { ComputerUseStatusIcon } from '../workbench/ComputerUseStatusLine';
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

const STEP_STATE_LABEL: Record<ComputerUseGuideStepState, TranslationKey> = {
  complete: 'settings.computerUse.stepState.complete',
  current: 'settings.computerUse.stepState.current',
  pending: 'settings.computerUse.stepState.pending',
};

const StepMarker: FC<{ index: number; state: ComputerUseGuideStepState }> = ({ index, state }) => (
  <span
    aria-hidden="true"
    className={clsx(
      'grid size-6 shrink-0 place-items-center rounded-full border text-[11px] font-semibold',
      state === 'complete' && 'border-mint/40 bg-mint-soft text-mint-ink',
      state === 'current' && 'border-gold/60 bg-gold/[0.12] text-gold-ink',
      state === 'pending' && 'border-border bg-surface text-muted',
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
 * visible. Inside the desktop shell each current step carries its action; any
 * other host follows the same live status read-only.
 */
export const SettingsComputerUsePage: FC = () => {
  const { t } = useTranslation();
  const desktopShell = isDesktopShell();
  const actions = useMemo(() => computerUseShellActions(), []);
  const [read, setRead] = useState<ComputerUseStatusRead | null>(null);
  const [checking, setChecking] = useState(false);
  const [runningStep, setRunningStep] = useState<ComputerUseGuideStep | null>(null);
  const [actionFailed, setActionFailed] = useState(false);
  const [screenRecordingRequested, setScreenRecordingRequested] = useState(false);
  // A poll that started before a Recheck must not overwrite the newer answer.
  const latestRequest = useRef(0);

  // Resolves to the read, or `undefined` once a newer read has started.
  const readLatest = useCallback(async (): Promise<ComputerUseStatusRead | null | undefined> => {
    const request = ++latestRequest.current;
    const next = await readComputerUseStatus();
    return request === latestRequest.current ? next : undefined;
  }, []);

  // A transient failure (`null`) keeps the last observed state.
  const poll = useCallback(() => {
    void readLatest().then((next) => {
      if (next) setRead(next);
    });
  }, [readLatest]);

  const recheck = () => {
    setChecking(true);
    void readLatest().then((next) => {
      setChecking(false);
      if (next) setRead(next);
    });
  };

  useEffect(() => {
    // Polls only while the page is on screen; returning to it reads at once,
    // since granting happens in System Settings.
    let interval: number | undefined;
    const stop = () => {
      window.clearInterval(interval);
      interval = undefined;
    };
    const start = () => {
      if (interval !== undefined) return;
      poll();
      interval = window.setInterval(poll, COMPUTER_USE_GUIDE_REFRESH_INTERVAL_MS);
    };
    const onVisibilityChange = () => {
      if (document.visibilityState === 'hidden') stop();
      else start();
    };
    if (document.visibilityState === 'hidden') poll();
    else start();
    window.addEventListener('focus', poll);
    document.addEventListener('visibilitychange', onVisibilityChange);
    return () => {
      // Invalidate a read still in flight so it lands nowhere.
      latestRequest.current += 1;
      stop();
      window.removeEventListener('focus', poll);
      document.removeEventListener('visibilitychange', onVisibilityChange);
    };
  }, [poll]);

  const runStep = (step: ComputerUseGuideStep) => {
    if (!actions) return;
    const pane = STEP_PANE[step];
    setRunningStep(step);
    setActionFailed(false);
    void (pane ? actions.openPermission(pane) : actions.enable())
      .then(
        () => {
          if (step === 'screenRecording') setScreenRecordingRequested(true);
        },
        () => setActionFailed(true),
      )
      .finally(() => {
        setRunningStep(null);
        poll();
      });
  };

  if (read?.kind === 'unsupported') {
    return (
      <SettingsPageShell
        activeTab="computer-use"
        title={t('settings.computerUse.title')}
        subtitle={t('settings.computerUse.subtitle')}
      >
        <SettingsPanel variant="preference">
          <div className="flex items-start gap-3" data-testid="computer-use-unsupported">
            <Monitor className="mt-0.5 size-5 shrink-0 text-muted" aria-hidden="true" />
            <div className="flex min-w-0 flex-col gap-1">
              <p className="text-[14px] font-semibold text-foreground">
                {t('settings.computerUse.unsupportedTitle')}
              </p>
              <p className="text-[12px] leading-relaxed text-muted">
                {t('settings.computerUse.unsupportedDetail')}
              </p>
            </div>
          </div>
        </SettingsPanel>
      </SettingsPageShell>
    );
  }

  const status = read?.kind === 'status' ? read.status : null;
  const copy = status ? computerUseStatusCopy(status, (key: ComputerUseCopyKey) => t(key)) : null;
  const progress = status ? computerUseGuideProgress(status) : null;

  return (
    <SettingsPageShell
      activeTab="computer-use"
      title={t('settings.computerUse.title')}
      subtitle={t('settings.computerUse.subtitle')}
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
            <p className="text-[13px] font-medium text-foreground">{t('settings.computerUse.remoteNotice')}</p>
          </div>
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
          {COMPUTER_USE_GUIDE_STEPS.map((step, index) => {
            const state = progress?.[step] ?? 'pending';
            const actionable = actions !== null && state === 'current';
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
                    {t(STEP_COPY[step].detail)}
                  </p>
                  {actionable && actionFailed && (
                    <GuideNote testId="computer-use-action-failed" tone="destructive">
                      {t('settings.computerUse.actionFailed')}
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
