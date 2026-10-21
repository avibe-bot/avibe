import { useCallback, useEffect, useRef, useState } from 'react';
import type { FC } from 'react';
import { Check, LoaderCircle, Monitor, RotateCw } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';

import { Button } from '@/components/ui/button';
import { isDesktopShell } from '@/lib/desktopShell';
import type { TranslationKey } from '@/i18n/types';
import { ComputerUseStatusIcon } from '../workbench/ComputerUseStatusLine';
import {
  COMPUTER_USE_GUIDE_STEPS,
  COMPUTER_USE_REFRESH_INTERVAL_MS,
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
  recheck: {
    title: 'settings.computerUse.steps.recheck.title',
    detail: 'settings.computerUse.steps.recheck.detail',
  },
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

/**
 * Settings → Computer Use: the macOS permission setup as one numbered guide
 * whose progress is read from the desktop status endpoint. Only the desktop
 * shell lists this page; any other host reaching it gets a short notice.
 */
export const SettingsComputerUsePage: FC = () => {
  const { t } = useTranslation();
  const desktopShell = isDesktopShell();
  const [read, setRead] = useState<ComputerUseStatusRead | null>(null);
  const [checking, setChecking] = useState(false);
  // A poll that started before a Recheck must not overwrite the newer answer.
  const latestRequest = useRef(0);

  // Resolves to the read, or `undefined` once a newer read has started.
  const readLatest = useCallback(async (): Promise<ComputerUseStatusRead | null | undefined> => {
    const request = ++latestRequest.current;
    const next = await readComputerUseStatus();
    return request === latestRequest.current ? next : undefined;
  }, []);

  // A transient failure (`null`) keeps the last observed state.
  const apply = (next: ComputerUseStatusRead | null | undefined) => {
    if (next) setRead(next);
  };

  const recheck = () => {
    setChecking(true);
    void readLatest().then((next) => {
      setChecking(false);
      apply(next);
    });
  };

  useEffect(() => {
    if (!desktopShell) return undefined;
    const poll = () => {
      void readLatest().then((next) => {
        if (next) setRead(next);
      });
    };
    poll();
    const interval = window.setInterval(poll, COMPUTER_USE_REFRESH_INTERVAL_MS);
    // Granting a permission happens in System Settings; returning to the
    // window is the moment the answer is most likely to have changed.
    const onVisibilityChange = () => {
      if (document.visibilityState === 'visible') poll();
    };
    window.addEventListener('focus', poll);
    document.addEventListener('visibilitychange', onVisibilityChange);
    return () => {
      // Invalidate a read still in flight so it lands nowhere.
      latestRequest.current += 1;
      window.clearInterval(interval);
      window.removeEventListener('focus', poll);
      document.removeEventListener('visibilitychange', onVisibilityChange);
    };
  }, [desktopShell, readLatest]);

  const unsupported = !desktopShell || read?.kind === 'unsupported';
  const status = read?.kind === 'status' ? read.status : null;

  if (unsupported) {
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
          variant="secondary"
          size="xs"
          disabled={checking}
          onClick={recheck}
        >
          <RotateCw className={clsx('size-3.5', checking && 'animate-spin')} aria-hidden="true" />
          {checking ? t('settings.computerUse.checking') : t('settings.computerUse.recheck')}
        </Button>
      }
    >
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
                <div className="flex min-w-0 flex-col gap-0.5">
                  <p
                    className={clsx(
                      'text-[13px] font-medium',
                      state === 'pending' ? 'text-muted' : 'text-foreground',
                    )}
                  >
                    {t(STEP_COPY[step].title)}
                    <span className="sr-only">{` — ${t(STEP_STATE_LABEL[state])}`}</span>
                  </p>
                  <p className="max-w-2xl text-[12px] leading-relaxed text-muted">
                    {t(STEP_COPY[step].detail)}
                  </p>
                </div>
              </li>
            );
          })}
        </ol>
      </SettingsPanel>
    </SettingsPageShell>
  );
};
