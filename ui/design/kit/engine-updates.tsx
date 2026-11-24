// Model Hub engine · its own updates, in the engine's row of Settings › Dependencies
// (docs/plans/model-hub-engine-channel.md, section 5; ui/design: settings/engine-updates/*).
//
// The engine updates by itself between Avibe releases. The row keeps its line (name, running build,
// Reinstall as repair) and gains one strip under it: one sentence, one switch, one button. The
// backend reports twenty states; `engineUpdateView` folds them into the few sentences a person
// sees, and is the only place a state is read.
import type { ComponentType } from 'react';
import { AlertCircle, AlertTriangle, ArrowUpCircle, Check, Clock3, Download, Info, Loader2, RefreshCw, WifiOff } from 'lucide-react';
import clsx from 'clsx';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { engineBuildVersion } from '@/components/settings/SettingsDependenciesPage.logic';
import { ToggleSwitch } from '@/components/settings/SettingsPrimitives';
import type { DependencyItem } from '@/context/ApiContext';
import { formatRelativeTime } from '@/lib/relativeTime';

import { useDraft } from './draft';

/** What the strip says. Several backend states share one. */
export type EngineUpdateSentence =
  | 'current' | 'notChecked' | 'checking' | 'available' | 'updating' | 'waitingForIdle'
  | 'undone' | 'unfinished' | 'replaced' | 'needsAvibe' | 'cannotCheck' | 'withdrawing' | 'stopped' | 'cannotStart';

/** The strip's one button: what it does, and whether it is doing it now. */
export type EngineUpdateButton = { does: 'check' | 'update' | 'retryUpdate' | 'retryCheck'; busy?: boolean };

/** `blocked` because the running build was withdrawn and the one build named to replace it cannot run here. */
const WITHDRAWN_REASONS = new Set(['engine_withdrawn', 'engine_withdrawn_without_fallback']);

export type EngineUpdateView = { sentence: EngineUpdateSentence; button: EngineUpdateButton };

/** The twenty states as the sentences a person sees; `null` where the row stays as it was. */
export function engineUpdateView(dep: DependencyItem): EngineUpdateView | null {
  // A candidate is `available_release` and nothing else; whether it may be installed is the state's to say.
  const offered = Boolean(dep.available_release) && dep.available_release !== dep.active_release;
  // No field names the build that failed, and `rolled_back` lasts only until the next check: the
  // last recorded result keeps the notice until another update is tried.
  const undone: EngineUpdateView = { sentence: 'undone', button: { does: 'retryUpdate' } };
  const unfinished: EngineUpdateView = { sentence: 'unfinished', button: { does: 'retryUpdate' } };
  switch (dep.update_state) {
    case undefined:
    case 'uncommissioned':
    case 'opted_out':
      return null;
    case 'recovering':
    case 'checking':
      return { sentence: 'checking', button: { does: 'check', busy: true } };
    case 'updating':
    case 'rolling_back':
      return { sentence: 'updating', button: { does: 'update', busy: true } };
    case 'withdrawing':
      return { sentence: 'withdrawing', button: { does: 'update', busy: true } };
    case 'available':
      return { sentence: 'available', button: { does: 'update' } };
    case 'deferred':
      // Nothing failed: the button is the same "do it now" as when the build was first offered.
      return { sentence: 'waitingForIdle', button: { does: 'update' } };
    case 'rolled_back':
      // A candidate that failed its check before the switch was never running: nothing was put back.
      return dep.last_result === 'preflight_failed' ? unfinished : undone;
    case 'failed':
      // A fresh read cannot tell a failed check from a failed update, so the safe next step is a check.
      return { sentence: 'unfinished', button: { does: 'check' } };
    case 'requires_avibe_update':
      return { sentence: 'needsAvibe', button: { does: 'check' } };
    case 'offline':
    case 'unavailable':
    case 'rejected':
    case 'waiting_for_controller':
      return { sentence: 'cannotCheck', button: { does: 'retryCheck' } };
    case 'blocked':
      // Only the one build named with the withdrawal can replace it, never a newer release; a check
      // switches to it as soon as it can run here. Any other cause is not cleared by a release, so
      // that sentence promises nothing and points at Diagnostics.
      return { sentence: WITHDRAWN_REASONS.has(dep.update_reason ?? '') ? 'stopped' : 'cannotStart', button: { does: 'check' } };
    case 'idle':
    case 'up_to_date':
    case 'applied':
      if (offered) return { sentence: 'available', button: { does: 'update' } };
      if (dep.last_result === 'rolled_back') return undone;
      if (dep.last_result === 'preflight_failed') return unfinished;
      // A withdrawal is said when it happens, not beside the switch: what happened, and that it works.
      if (dep.last_result === 'withdrawn_to_fallback') return { sentence: 'replaced', button: { does: 'check' } };
      return { sentence: dep.last_checked_at ? 'current' : 'notChecked', button: { does: 'check' } };
  }
}

// One spinner at a time, and it is the button's: the sentence keeps a still icon while work runs.
const SENTENCE_ICON: Record<EngineUpdateSentence, { icon: ComponentType<{ className?: string }>; className: string; quiet?: boolean }> = {
  current: { icon: Check, className: 'text-mint-ink', quiet: true },
  notChecked: { icon: Info, className: 'text-muted', quiet: true },
  checking: { icon: RefreshCw, className: 'text-muted', quiet: true },
  // Good news, not a warning (as the update panel has it): orange is for what went wrong.
  available: { icon: ArrowUpCircle, className: 'text-cyan-ink' },
  updating: { icon: ArrowUpCircle, className: 'text-cyan-ink' },
  waitingForIdle: { icon: Clock3, className: 'text-muted' },
  undone: { icon: AlertTriangle, className: 'text-gold-ink' },
  unfinished: { icon: AlertTriangle, className: 'text-gold-ink' },
  replaced: { icon: AlertTriangle, className: 'text-gold-ink' },
  needsAvibe: { icon: Info, className: 'text-gold-ink' },
  cannotCheck: { icon: WifiOff, className: 'text-muted', quiet: true },
  withdrawing: { icon: AlertTriangle, className: 'text-gold-ink' },
  stopped: { icon: AlertCircle, className: 'text-destructive-ink' },
  cannotStart: { icon: AlertCircle, className: 'text-destructive-ink' },
};

function useSentence(dep: DependencyItem, sentence: EngineUpdateSentence): string {
  const d = useDraft();
  const { t } = useTranslation();
  const running = engineBuildVersion(dep.active_release) ?? dep.version ?? '';
  const next = engineBuildVersion(dep.available_release);
  switch (sentence) {
    case 'current':
      return d(`已是最新 · ${formatRelativeTime(dep.last_checked_at, t)}检查`, `Up to date · checked ${formatRelativeTime(dep.last_checked_at, t).toLowerCase()}`);
    case 'notChecked':
      return d('还没有检查过更新', 'Not checked for updates yet');
    case 'checking':
      return d('正在检查更新…', 'Checking for updates…');
    case 'available':
      return d(`可以更新到 v${next}`, `v${next} is available`);
    case 'updating':
      return next ? d(`正在更新到 v${next}…`, `Updating to v${next}…`) : d('正在更新…', 'Updating…');
    case 'waitingForIdle':
      // With the switch off nothing retries by itself, so the sentence does not promise it.
      return dep.auto_update === false
        ? d('模型网关正在工作，这次没有更新，等它空闲后再试', "Model Hub is busy, so it wasn't updated. Try again once it is idle.")
        : d(`模型网关正在工作，空闲后会自动更新${next ? `到 v${next}` : ''}`, `Model Hub is busy. ${next ? `v${next}` : 'The update'} installs by itself once it is idle.`);
    case 'undone':
      return d(`上次更新没有成功，已换回 v${running}，模型网关照常可用`, `The last update didn't work, so v${running} was put back. Model Hub works as before.`);
    case 'unfinished':
      return d(`上次更新没有完成，仍在使用 v${running}，模型网关照常可用`, `The last update didn't finish. Still on v${running}; Model Hub works as before.`);
    case 'replaced':
      return d(`之前的版本已被撤回，已换成 v${running}，模型网关照常可用`, `The earlier version was withdrawn; v${running} replaced it. Model Hub works as before.`);
    case 'needsAvibe':
      return d(`新版引擎需要 Avibe ${dep.min_avibe_version} 或更高版本，请先更新 Avibe`, `A newer engine needs Avibe ${dep.min_avibe_version} or later. Update Avibe first.`);
    case 'cannotCheck':
      return d('暂时无法检查更新，当前版本不受影响', "Can't check for updates right now. The current version keeps working.");
    case 'withdrawing':
      return d('这个版本已被撤回，正在换成安全的版本，模型网关稍后恢复', 'This version was withdrawn. Switching to a safe one; Model Hub is back shortly.');
    case 'stopped':
      return d('模型网关已停止：这个引擎版本已被撤回。替换它的版本能在本机运行后会自动恢复。', 'Model Hub is stopped: the engine was withdrawn. It restarts once its replacement can run here.');
    case 'cannotStart':
      return d('模型网关已停止：引擎无法启动。请到「诊断与日志」查看原因。', "Model Hub is stopped: the engine can't start. See Diagnostics & Logs for the cause.");
  }
}

const ALERTS = new Set<EngineUpdateSentence>(['undone', 'unfinished', 'replaced', 'stopped', 'cannotStart']);

function StripButton({ button }: { button: EngineUpdateButton }) {
  const d = useDraft();
  if (button.busy) {
    return (
      <Button variant="secondary" size="xs" disabled>
        <Loader2 className="size-3.5 animate-spin" />
        {button.does === 'check' ? d('检查中…', 'Checking…') : d('更新中…', 'Updating…')}
      </Button>
    );
  }
  if (button.does === 'update') {
    return (
      <Button variant="brand" size="xs">
        <Download className="size-3.5" />
        {d('更新', 'Update')}
      </Button>
    );
  }
  return (
    <Button variant="secondary" size="xs">
      <RefreshCw className="size-3.5" />
      {button.does === 'check' ? d('检查更新', 'Check for updates') : d('重试', 'Try again')}
    </Button>
  );
}

/** The strip under the engine's row: one sentence, the automatic-update switch, one button. */
export function EngineUpdateStrip({ dep }: { dep: DependencyItem }) {
  const d = useDraft();
  const view = engineUpdateView(dep);
  const sentence = useSentence(dep, view?.sentence ?? 'current');
  if (!view) return null;
  const { icon: Icon, className, quiet } = SENTENCE_ICON[view.sentence];
  const automatic = dep.auto_update !== false;
  const label = d('自动更新', 'Auto-update');
  return (
    <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between md:gap-6" data-engine-updates={view.sentence}>
      <div className="flex min-w-0 items-start gap-2 text-[12px] leading-snug">
        <Icon className={clsx('mt-px size-3.5 shrink-0', className)} />
        <p
            role={ALERTS.has(view.sentence) ? 'alert' : 'status'}
            className={view.sentence === 'stopped' || view.sentence === 'cannotStart' ? 'text-destructive-ink' : quiet ? 'text-muted' : 'text-foreground'}
          >
          {sentence}
        </p>
      </div>
      <div className="flex shrink-0 items-center justify-between gap-3 md:justify-end">
        <div className="flex items-center gap-2">
          <span className="whitespace-nowrap text-[12px] text-muted">{label}</span>
          <ToggleSwitch enabled={automatic} onClick={() => undefined} label={label} />
        </div>
        <StripButton button={view.button} />
      </div>
    </div>
  );
}
