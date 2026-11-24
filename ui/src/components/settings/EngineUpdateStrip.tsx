// The strip under the Model Hub engine's row in Settings › Dependencies: one sentence, the
// automatic-update switch, one button (ui/design: settings/engine-updates/*). It draws what
// `engineUpdateView` folds the engine's twenty update states into, and nothing else.
import type { ComponentType } from 'react';
import { useTranslation } from 'react-i18next';
import { AlertCircle, AlertTriangle, ArrowUpCircle, Check, Clock3, Download, Info, Loader2, RefreshCw, WifiOff } from 'lucide-react';
import clsx from 'clsx';

import type { DependencyItem } from '@/context/ApiContext';
import { formatRelativeTime } from '@/lib/relativeTime';
import { Button } from '../ui/button';
import { engineBuildVersion } from './engineUpdateView';
import type { EngineUpdateButton, EngineUpdateSentence, EngineUpdateView } from './engineUpdateView';
import { ToggleSwitch } from './SettingsPrimitives';

type Tone = 'quiet' | 'plain' | 'stopped';

// One spinner at a time, and it is the button's: the sentence keeps a still icon while work runs.
const SENTENCE: Record<EngineUpdateSentence, { icon: ComponentType<{ className?: string }>; iconClassName: string; tone: Tone; alert?: boolean }> = {
  current: { icon: Check, iconClassName: 'text-mint-ink', tone: 'quiet' },
  notChecked: { icon: Info, iconClassName: 'text-muted', tone: 'quiet' },
  checking: { icon: RefreshCw, iconClassName: 'text-muted', tone: 'quiet' },
  // Good news, not a warning (as the update panel has it): orange is for what went wrong.
  available: { icon: ArrowUpCircle, iconClassName: 'text-cyan-ink', tone: 'plain' },
  updating: { icon: ArrowUpCircle, iconClassName: 'text-cyan-ink', tone: 'plain' },
  waitingForIdle: { icon: Clock3, iconClassName: 'text-muted', tone: 'plain' },
  undone: { icon: AlertTriangle, iconClassName: 'text-gold-ink', tone: 'plain', alert: true },
  unfinished: { icon: AlertTriangle, iconClassName: 'text-gold-ink', tone: 'plain', alert: true },
  replaced: { icon: AlertTriangle, iconClassName: 'text-gold-ink', tone: 'plain', alert: true },
  needsAvibe: { icon: Info, iconClassName: 'text-gold-ink', tone: 'plain' },
  cannotCheck: { icon: WifiOff, iconClassName: 'text-muted', tone: 'quiet' },
  withdrawing: { icon: AlertTriangle, iconClassName: 'text-gold-ink', tone: 'plain' },
  stopped: { icon: AlertCircle, iconClassName: 'text-destructive-ink', tone: 'stopped', alert: true },
  cannotStart: { icon: AlertCircle, iconClassName: 'text-destructive-ink', tone: 'stopped', alert: true },
};

const TONE_CLASS: Record<Tone, string> = {
  quiet: 'text-muted',
  plain: 'text-foreground',
  stopped: 'text-destructive-ink',
};

function useSentence(dep: DependencyItem, sentence: EngineUpdateSentence): string {
  const { t } = useTranslation();
  const next = engineBuildVersion(dep.available_release);
  switch (sentence) {
    case 'current':
      return t('settings.dependencies.engineUpdates.current', { time: formatRelativeTime(dep.last_checked_at, t) });
    case 'notChecked':
      return t('settings.dependencies.engineUpdates.notChecked');
    case 'checking':
      return t('settings.dependencies.engineUpdates.checking');
    case 'available':
      return t('settings.dependencies.engineUpdates.available', { version: next });
    case 'updating':
      return next
        ? t('settings.dependencies.engineUpdates.updatingTo', { version: next })
        : t('settings.dependencies.engineUpdates.updating');
    case 'waitingForIdle':
      // With the switch off nothing retries by itself, so the sentence does not promise it.
      if (dep.auto_update === false) return t('settings.dependencies.engineUpdates.waitingForIdleManual');
      return next
        ? t('settings.dependencies.engineUpdates.waitingForIdleTo', { version: next })
        : t('settings.dependencies.engineUpdates.waitingForIdle');
    // What happened last is history and names no build; the badge above says what runs now.
    case 'undone':
      return t('settings.dependencies.engineUpdates.undone');
    case 'unfinished':
      return t('settings.dependencies.engineUpdates.unfinished');
    case 'replaced':
      return t('settings.dependencies.engineUpdates.replaced');
    case 'needsAvibe':
      return t('settings.dependencies.engineUpdates.needsAvibe', { version: dep.min_avibe_version });
    case 'cannotCheck':
      return t('settings.dependencies.engineUpdates.cannotCheck');
    case 'withdrawing':
      return t('settings.dependencies.engineUpdates.withdrawing');
    case 'stopped':
      return t('settings.dependencies.engineUpdates.stopped');
    case 'cannotStart':
      return t('settings.dependencies.engineUpdates.cannotStart');
  }
}

function StripButton({ button, disabled, onClick }: { button: EngineUpdateButton; disabled?: boolean; onClick: () => void }) {
  const { t } = useTranslation();
  if (button.busy) {
    return (
      <Button variant="secondary" size="xs" disabled>
        <Loader2 className="size-3.5 animate-spin" />
        {button.does === 'check'
          ? t('settings.dependencies.engineUpdates.checkingButton')
          : t('settings.dependencies.engineUpdates.updatingButton')}
      </Button>
    );
  }
  // The offer is the row's one lead action; a second try after a failure, and a check, are not.
  const lead = button.label === 'update';
  return (
    <Button variant={lead ? 'brand' : 'secondary'} size="xs" disabled={disabled} onClick={onClick}>
      {lead ? <Download className="size-3.5" /> : <RefreshCw className="size-3.5" />}
      {button.label === 'update' ? t('update.action.update') : button.label === 'retry' ? t('update.action.retry') : t('update.action.check')}
    </Button>
  );
}

export type EngineUpdateStripProps = {
  dep: DependencyItem;
  view: EngineUpdateView;
  /** Another operation on this page is running: the button waits for it. */
  disabled?: boolean;
  /** The switch's change is on its way. */
  autoUpdateBusy?: boolean;
  onCheck: () => void;
  onUpdate: () => void;
  onAutoUpdateChange: (enabled: boolean) => void;
};

export function EngineUpdateStrip({ dep, view, disabled, autoUpdateBusy, onCheck, onUpdate, onAutoUpdateChange }: EngineUpdateStripProps) {
  const { t } = useTranslation();
  const sentence = useSentence(dep, view.sentence);
  const { icon: Icon, iconClassName, tone, alert } = SENTENCE[view.sentence];
  const automatic = dep.auto_update !== false;
  const label = t('settings.dependencies.engineUpdates.autoUpdate');
  return (
    <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between md:gap-6" data-engine-updates={view.sentence}>
      <div className="flex min-w-0 items-start gap-2 text-[12px] leading-snug">
        <Icon className={clsx('mt-px size-3.5 shrink-0', iconClassName)} />
        <p role={alert ? 'alert' : 'status'} className={TONE_CLASS[tone]}>{sentence}</p>
      </div>
      {/* One order at every width: the switch, then the button at the row's right edge. */}
      <div className="flex shrink-0 items-center justify-between gap-3 md:justify-end">
        <div className="flex items-center gap-2">
          <span className="whitespace-nowrap text-[12px] text-muted">{label}</span>
          <ToggleSwitch
            enabled={automatic}
            onClick={() => onAutoUpdateChange(!automatic)}
            disabled={autoUpdateBusy}
            label={label}
          />
        </div>
        <StripButton button={view.button} disabled={disabled} onClick={view.button.does === 'update' ? onUpdate : onCheck} />
      </div>
    </div>
  );
}
