// The one update panel. The version popover (any Runtime, in a browser or in
// the desktop app) and the desktop app's account footer both render it, so a
// computer's update reads the same on every surface: the same rows, the same
// states, the same words. Containers turn their source into an
// `UpdatePanelModel` (`updatePanelModel.ts`); this view only draws it.
// ui/design: update/panel
import type { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { AlertCircle, ArrowUpCircle, Check, Download, ExternalLink, Info, RefreshCw, X } from 'lucide-react';
import { cn } from '@/lib/utils';
import { formatRelativeTime } from '@/lib/relativeTime';
import { Button } from '../ui/button';
import { badgeVariants } from '../ui/badge-variants';
import { ToggleSwitch } from '../settings/SettingsPrimitives';
import { isPrereleaseVersion, releaseNotesUrl } from './versions';
import type { UpdatePanelModel, UpdatePanelStatus, UpdateSubject } from './updatePanelModel';

/** The same actions on every surface; each container sends them to that computer's updater. */
export type UpdatePanelActions = {
  /** Make the computer's updater check now. */
  check?: { run: () => void; disabled?: boolean };
  /** Install the offered version (from a browser: ask the host to install it). */
  update?: { run: () => void; disabled?: boolean };
  /** The desktop app's own prompt: skip this version, or ignore a remote request. */
  skip?: { run: () => void; ignoreRequest?: boolean; disabled?: boolean };
  setPrereleases?: (enabled: boolean) => void;
  setAutoUpdate?: (enabled: boolean) => void;
  /** A change to either setting is on its way to the updater. */
  settingsBusy?: boolean;
  close?: () => void;
};

type Tone = 'info' | 'success' | 'danger' | 'neutral';

const TONE_CLASS: Record<Tone, string> = {
  info: 'border-cyan/30 bg-cyan/10 text-cyan-ink',
  success: 'border-mint/25 bg-mint/10 text-mint-ink',
  danger: 'border-destructive/30 bg-destructive/10 text-destructive-ink',
  neutral: 'border-edge bg-muted/30 text-muted shadow-elevation-0',
};

type Copy = { tone: Tone; title: string; detail?: string; spin?: boolean; offer?: boolean };

function useStatusCopy(status: UpdatePanelStatus | null, subject: UpdateSubject): Copy | null {
  const { t } = useTranslation();
  const host = subject === 'host-desktop';
  if (!status) return null;
  switch (status.kind) {
    case 'checking':
      return { tone: 'info', spin: true, title: t('update.status.checking') };
    case 'current':
      // Ahead of the latest stable release on a pre-release: not "up to date", and nothing to downgrade to.
      return { tone: 'success', title: t(status.awaitingStable ? 'dashboard.awaitingStableRelease' : 'update.status.current') };
    case 'available':
      // Good news, not a warning: orange and red are for failures only.
      return {
        tone: 'info',
        offer: true,
        title: t('update.status.available'),
        detail: subject === 'runtime' ? undefined : t('update.status.availableDetail'),
      };
    case 'not-checked':
      return { tone: 'neutral', title: t('update.status.notChecked') };
    case 'unknown':
      return { tone: 'neutral', title: t('dashboard.desktopManagedUpdate') };
    case 'disabled':
      return { tone: 'neutral', title: t('update.status.disabled'), detail: t('update.status.disabledDetail') };
    case 'check-failed':
      return {
        tone: 'danger',
        title: t('update.status.checkFailed'),
        detail: t('update.status.checkFailedDetail'),
      };
    case 'requesting':
      return { tone: 'info', spin: true, title: t('update.status.requesting') };
    case 'awaiting-host':
      return { tone: 'neutral', title: t('update.status.awaitingHost'), detail: t('update.status.awaitingHostDetail') };
    case 'downloading':
      return {
        tone: 'info',
        spin: true,
        title: status.progress === null
          ? t('update.status.downloading')
          : t('update.status.downloadingProgress', { progress: status.progress }),
      };
    case 'installing':
      return { tone: 'info', spin: true, title: t('update.status.installing') };
    case 'restarting':
      return { tone: 'info', spin: true, title: t(host ? 'update.status.restartingHost' : 'update.status.restarting') };
    case 'verifying':
      return { tone: 'info', spin: true, title: t('update.status.verifying') };
    case 'updated':
      return {
        tone: 'success',
        title: status.version ? t('update.status.updatedTo', { version: status.version }) : t('update.status.updated'),
        detail: status.restarting ? t('update.status.restarting') : undefined,
      };
    case 'update-failed': {
      const reason = status.reason ?? 'failed';
      return {
        tone: reason === 'busy' ? 'neutral' : 'danger',
        title: t(`update.failure.${reason}.title`),
        detail: t(`update.failure.${reason}.detail`),
      };
    }
  }
}

function Setting({ label, hint, enabled, onChange, busy }: {
  label: string;
  hint: string;
  enabled: boolean;
  /** Absent where this computer's updater cannot take the change: the switch shows its value, disabled. */
  onChange?: (enabled: boolean) => void;
  busy?: boolean;
}) {
  return (
    <div className="flex items-center justify-between gap-3">
      <div className="min-w-0">
        <div className="text-sm text-foreground">{label}</div>
        <div className="text-xs text-muted">{hint}</div>
      </div>
      <ToggleSwitch enabled={enabled} onClick={() => onChange?.(!enabled)} disabled={!onChange || busy} label={label} />
    </div>
  );
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-3 text-sm">
      <span className="shrink-0 text-muted">{label}</span>
      <span className="flex min-w-0 items-center justify-end gap-1.5 text-right">{children}</span>
    </div>
  );
}

export function UpdatePanel({ model, actions, extra, title }: {
  model: UpdatePanelModel;
  actions: UpdatePanelActions;
  /** Rows only one kind of Runtime has: a source revision, the auto-update switch. */
  extra?: ReactNode;
  title?: string;
}) {
  const { t } = useTranslation();
  const { status, subject } = model;
  const copy = useStatusCopy(status, subject);
  // A desktop app too old to take these from here gets one line; its container offers only what it accepts.
  const legacy = Boolean(model.legacyHost);
  const offered = model.latest;
  const checkLabel = t('update.action.check');
  const checking = status?.kind === 'checking';

  // One lead action per state: install what is offered, or check again after a failure.
  const lead = status?.kind === 'available' && actions.update
    ? { label: t('update.action.update'), run: actions.update.run, disabled: actions.update.disabled, icon: <Download /> }
    : status?.kind === 'update-failed' && status.retry && actions.update
      ? { label: t('update.action.retry'), run: actions.update.run, disabled: actions.update.disabled, icon: <RefreshCw /> }
      // Without an install action (the desktop app dropped the failed offer), trying again starts with a check.
      : (status?.kind === 'check-failed' || status?.kind === 'not-checked'
        || (status?.kind === 'update-failed' && !actions.update)) && actions.check
        ? {
          label: status?.kind !== 'not-checked' ? t('update.action.retry') : checkLabel,
          run: actions.check.run,
          disabled: actions.check.disabled,
          icon: <RefreshCw />,
        }
        : null;

  return (
    <div data-update-panel data-update-status={status?.kind ?? 'none'} className="text-popover-foreground">
      <div className="flex items-center justify-between gap-2 border-b border-border px-4 py-3">
        {/* One title on every surface: the panel never describes itself differently by where it is opened. */}
        <p className="min-w-0 text-sm font-medium text-foreground">{title ?? t('dashboard.versionAndUpdate')}</p>
        <div className="flex shrink-0 items-center gap-1">
          {actions.check && (
            <Button
              variant="ghost"
              size="icon"
              className="h-7 w-7 text-muted hover:text-foreground"
              onClick={actions.check.run}
              disabled={actions.check.disabled || checking}
              aria-label={checkLabel}
              title={checkLabel}
            >
              <RefreshCw size={14} className={checking ? 'animate-spin' : ''} />
            </Button>
          )}
          {actions.close && (
            <Button
              variant="ghost"
              size="icon"
              className="h-7 w-7 text-muted hover:text-foreground"
              onClick={actions.close}
              aria-label={t('common.close')}
            >
              <X size={14} />
            </Button>
          )}
        </div>
      </div>

      <div className="space-y-3 p-4">
        {model.current && (
          <Row label={t('update.row.current')}>
            <span className="break-all font-mono font-medium text-foreground">{model.current}</span>
          </Row>
        )}
        {offered && (
          <Row label={t(model.latestIsStable ? 'update.row.latestStable' : 'update.row.offered')}>
            {isPrereleaseVersion(offered) && (
              <span className={badgeVariants({ variant: 'info' })}>{t('dashboard.prereleaseBadge')}</span>
            )}
            <a
              href={releaseNotesUrl(offered)}
              target="_blank"
              rel="noreferrer"
              className="inline-flex min-w-0 items-center gap-1 rounded-sm font-mono font-medium text-foreground hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              title={t('dashboard.releaseNotes')}
              aria-label={t('dashboard.releaseNotesFor', { version: offered })}
            >
              <span className="break-all">{offered}</span>
              <ExternalLink size={12} className="shrink-0 text-muted" aria-hidden="true" />
            </a>
          </Row>
        )}
        {model.checkedAt && (
          <Row label={t('update.row.checkedAt')}>
            <time dateTime={model.checkedAt} className="text-foreground">{formatRelativeTime(model.checkedAt, t)}</time>
          </Row>
        )}

        {copy && <div role={status?.kind === 'check-failed' || status?.kind === 'update-failed' ? 'alert' : 'status'} className={cn('flex items-start gap-2 rounded-md border px-3 py-2 text-sm', TONE_CLASS[copy.tone])}>
          {copy.spin ? <RefreshCw size={16} className="mt-0.5 shrink-0 animate-spin" />
            : copy.offer ? <ArrowUpCircle size={16} className="mt-0.5 shrink-0" />
            : copy.tone === 'success' ? <Check size={16} className="mt-0.5 shrink-0" />
              : copy.tone === 'neutral' ? <Info size={16} className="mt-0.5 shrink-0" />
                : <AlertCircle size={16} className="mt-0.5 shrink-0" />}
          <div className="min-w-0 space-y-0.5">
            <p className="font-medium">{copy.title}</p>
            {copy.detail && <p className="text-xs opacity-90">{copy.detail}</p>}
          </div>
        </div>}
        {status?.kind === 'downloading' && status.progress !== null && (
          <div className="h-1 overflow-hidden rounded-full bg-muted/40" aria-hidden="true">
            <div className="h-full rounded-full bg-cyan transition-[width]" style={{ width: `${status.progress}%` }} />
          </div>
        )}

        {legacy && (
          <p className="rounded-md border border-edge px-3 py-2 text-xs leading-5 text-muted shadow-elevation-0">
            {t('update.legacyHost')}
          </p>
        )}
        {(model.prereleases || model.autoUpdate) && (
          <div className="space-y-3 border-t border-border pt-3">
            {model.prereleases && (
              <Setting
                label={t('update.setting.prereleases')}
                hint={t('update.setting.prereleasesHint')}
                enabled={model.prereleases.enabled}
                onChange={actions.setPrereleases}
                busy={actions.settingsBusy}
              />
            )}
            {model.autoUpdate && (
              <Setting
                label={t('update.setting.autoUpdate')}
                hint={t('update.setting.autoUpdateHint')}
                enabled={model.autoUpdate.enabled}
                onChange={actions.setAutoUpdate}
                busy={actions.settingsBusy}
              />
            )}
          </div>
        )}
        {extra}
      </div>

      {(lead || actions.skip) && (
        <div className="flex items-center justify-end gap-2 border-t border-border px-4 py-3">
          {actions.skip && (
            <Button variant="ghost" size="xs" onClick={actions.skip.run} disabled={actions.skip.disabled}>
              {t(actions.skip.ignoreRequest ? 'dashboard.desktopRemoteUpdateIgnore' : 'dashboard.skipUpdate')}
            </Button>
          )}
          {lead && (
            <Button variant="brand" size="xs" onClick={lead.run} disabled={lead.disabled}>
              {lead.icon}
              {lead.label}
            </Button>
          )}
        </div>
      )}
    </div>
  );
}
