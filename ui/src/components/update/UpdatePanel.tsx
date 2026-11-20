// The one update panel. The version popover (any Runtime, in a browser or in
// the desktop app) and the desktop app's account footer both render it, so a
// computer's update reads the same on every surface: the same rows, the same
// states, the same words. Containers turn their source into an
// `UpdatePanelModel` (`updatePanelModel.ts`); this view only draws it.
// ui/design: update/panel
//
// While the desktop app shows an instance on another computer, two things update
// separately, so the panel has a section for each (`UpdateSubjectsPanel`): the
// instance under its own name, then this computer's desktop app. No version is
// shown without saying whose it is. ui/design: update/instance-and-app
import { useState, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { AlertCircle, ArrowUpCircle, Check, ChevronDown, Download, ExternalLink, Info, RefreshCw, WifiOff, X } from 'lucide-react';
import { cn } from '@/lib/utils';
import { formatRelativeTime } from '@/lib/relativeTime';
import { Button } from '../ui/button';
import { badgeVariants } from '../ui/badge-variants';
import { ToggleSwitch } from '../settings/SettingsPrimitives';
import { isPrereleaseVersion, releaseNotesUrl } from './versions';
import * as panelModels from './updatePanelModel';
import type { UpdatePanelModel, UpdatePanelStatus, UpdateSubject } from './updatePanelModel';

export type PanelModels = typeof panelModels;

/** The panel with its model built inside this chunk, so a surface's first screen carries neither. */
export function UpdatePanelFor({ build, ...props }: Omit<Parameters<typeof UpdatePanel>[0], 'model'> & {
  build: (models: PanelModels) => UpdatePanelModel;
}) {
  return <UpdatePanel model={build(panelModels)} {...props} />;
}

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

/** One of the two things a two-section panel updates, and the other one's name. */
type Named = { kind: 'instance' | 'app'; name: string; other: string };

function useStatusCopy(status: UpdatePanelStatus | null, subject: UpdateSubject, named?: Named): Copy | null {
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
        // Beside another subject, each says what installing does to it: the instance restarts, under its own
        // name; this computer's Avibe restarts, and the instance is left alone.
        detail: named?.kind === 'instance' ? t('update.status.availableInstanceDetail', { name: named.name })
          : named ? t('update.status.availableAppDetail', { name: named.other })
            : subject === 'runtime' ? undefined : t('update.status.availableDetail'),
      };
    case 'not-checked':
      return { tone: 'neutral', title: t('update.status.notChecked') };
    case 'unknown':
      return { tone: 'neutral', title: t('dashboard.desktopManagedUpdate') };
    case 'disabled':
      return { tone: 'neutral', title: t('update.status.disabled'), detail: t('update.status.disabledDetail') };
    case 'check-failed':
      return status.reason === 'unreachable'
        ? { tone: 'danger', title: t('update.failure.unreachable.title'), detail: t('update.failure.unreachable.detail') }
        : { tone: 'danger', title: t('update.status.checkFailed'), detail: t('update.status.checkFailedDetail') };
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
        // Beside another subject, the result names what was updated.
        title: named
          ? status.version
            ? t('update.status.updatedNamedTo', { name: named.name, version: status.version })
            : t('update.status.updatedNamed', { name: named.name })
          : status.version ? t('update.status.updatedTo', { version: status.version }) : t('update.status.updated'),
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

function Offered({ model }: { model: UpdatePanelModel }) {
  const { t } = useTranslation();
  const offered = model.latest;
  if (!offered) return null;
  return (
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
  );
}

function CheckedAt({ model }: { model: UpdatePanelModel }) {
  const { t } = useTranslation();
  if (!model.checkedAt) return null;
  return (
    <Row label={t('update.row.checkedAt')}>
      <time dateTime={model.checkedAt} className="text-foreground">{formatRelativeTime(model.checkedAt, t)}</time>
    </Row>
  );
}

function StatusNote({ status, copy }: { status: UpdatePanelStatus | null; copy: Copy | null }) {
  return (
    <>
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
    </>
  );
}

function Settings({ model, actions }: { model: UpdatePanelModel; actions: UpdatePanelActions }) {
  const { t } = useTranslation();
  return (
    <>
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
    </>
  );
}

function Header({ title, check, checking, checkLabel, close }: {
  title: string;
  check?: { run: () => void; disabled?: boolean };
  checking: boolean;
  checkLabel: string;
  close?: () => void;
}) {
  const { t } = useTranslation();
  return (
    <div className="flex items-center justify-between gap-2 border-b border-border px-4 py-3">
      {/* One title on every surface: the panel never describes itself differently by where it is opened. */}
      <p className="min-w-0 text-sm font-medium text-foreground">{title}</p>
      <div className="flex shrink-0 items-center gap-1">
        {check && (
          <Button
            variant="ghost"
            size="icon"
            className="h-7 w-7 text-muted hover:text-foreground"
            onClick={check.run}
            disabled={check.disabled || checking}
            aria-label={checkLabel}
            title={checkLabel}
          >
            <RefreshCw size={14} className={checking ? 'animate-spin' : ''} />
          </Button>
        )}
        {close && (
          <Button
            variant="ghost"
            size="icon"
            className="h-7 w-7 text-muted hover:text-foreground"
            onClick={close}
            aria-label={t('common.close')}
          >
            <X size={14} />
          </Button>
        )}
      </div>
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
  // A source build has no release to check; the button reads its build again.
  const checkLabel = t(status ? 'update.action.check' : 'update.action.refreshBuild');
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
      <Header title={title ?? t('dashboard.versionAndUpdate')} check={actions.check} checking={checking} checkLabel={checkLabel} close={actions.close} />

      <div className="space-y-3 p-4">
        {model.current && (
          // A source build has no release, only the package version its revision was built as.
          <Row label={t(status ? 'update.row.current' : 'update.row.package')}>
            <span className="break-all font-mono font-medium text-foreground">{model.current}</span>
          </Row>
        )}
        <Offered model={model} />
        <CheckedAt model={model} />

        <StatusNote status={status} copy={copy} />

        {legacy && (
          <p className="rounded-md border border-edge px-3 py-2 text-xs leading-5 text-muted shadow-elevation-0">
            {t('update.legacyHost')}
          </p>
        )}
        {(model.prereleases || model.autoUpdate) && (
          <div className="space-y-3 border-t border-border pt-3">
            <Settings model={model} actions={actions} />
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

/** One of the two subjects of `UpdateSubjectsPanel`. */
export type UpdateSubjectSection = {
  /** What its updater says, through the shared mapping; none while the instance cannot be read. */
  model: UpdatePanelModel | null;
  /** Why there is none: the instance does not answer (the default), or it no longer accepts the app's sign-in. */
  unavailable?: 'unreachable' | 'unauthorized';
  /** Install, and the two switches, as far as this subject's updater takes them. The panel's one check and its close are the panel's. */
  actions: Pick<UpdatePanelActions, 'update' | 'setPrereleases' | 'setAutoUpdate' | 'settingsBusy'>;
  /** Shown under the state: a request that could not be sent, a hint for a member who cannot install. */
  extra?: ReactNode;
};

function SubjectSection({ named, section, first, check }: {
  named: Named;
  section: UpdateSubjectSection;
  first: boolean;
  /** The panel's check, for a subject whose state is a check that failed or never ran. */
  check?: { run: () => void; disabled?: boolean };
}) {
  const { t } = useTranslation();
  // Its last check and its switches open from its name; closed, the panel is the two states and their buttons.
  const [open, setOpen] = useState(false);
  const { model, actions } = section;
  const status = model?.status ?? null;
  const copy = useStatusCopy(status, model?.subject ?? 'runtime', named);
  // Two buttons in one panel each say what they update.
  const updateLabel = named.kind === 'instance' ? t('update.action.updateNamed', { name: named.name }) : t('update.action.updateApp');
  const lead = status?.kind === 'available' && actions.update
    ? { label: updateLabel, run: actions.update.run, disabled: actions.update.disabled, icon: <Download /> }
    : status?.kind === 'update-failed' && status.retry && actions.update
      ? { label: t('update.action.retry'), run: actions.update.run, disabled: actions.update.disabled, icon: <RefreshCw /> }
      : (status?.kind === 'check-failed' || status?.kind === 'not-checked') && check
        ? {
          label: t(status.kind === 'not-checked' ? 'update.action.check' : 'update.action.retry'),
          run: check.run,
          disabled: check.disabled,
          icon: <RefreshCw />,
        }
        : null;
  const details = Boolean(model && (model.checkedAt || model.prereleases || model.autoUpdate));
  return (
    <section
      data-update-subject={named.kind}
      data-update-status={status?.kind ?? 'none'}
      // The single panel keeps 16 px under its body and 12 px under its button; a section ends in one or the other.
      className={cn('space-y-3 px-4 pt-4', lead && !(open && details) ? 'pb-3' : 'pb-4', !first && 'border-t border-border')}
    >
      {/* The subject's name where the single panel says "Current version": every version has its owner beside it. */}
      <div className="flex items-center justify-between gap-3 text-sm">
        <button
          type="button"
          aria-expanded={details ? open : undefined}
          disabled={!details}
          onClick={() => setOpen((value) => !value)}
          title={named.name}
          className="flex min-w-0 items-center gap-1 rounded-sm font-medium text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <span className="truncate">{named.name}</span>
          {details && <ChevronDown size={14} aria-hidden="true" className={cn('shrink-0 text-muted transition-transform', open && 'rotate-180')} />}
        </button>
        {model?.current && <span className="shrink-0 font-mono font-medium text-foreground">{model.current}</span>}
      </div>
      {model && <Offered model={model} />}

      {/* An instance that cannot be reached shows the bottom row's own words for it, and nothing it cannot know;
          one that no longer accepts the app's sign-in says that, not "disconnected". */}
      {!model && (
        <div role="status" className={cn('flex items-start gap-2 rounded-md border px-3 py-2 text-sm', TONE_CLASS.neutral)}>
          {section.unavailable === 'unauthorized' ? <Info size={16} className="mt-0.5 shrink-0" /> : <WifiOff size={16} className="mt-0.5 shrink-0" />}
          <p className="min-w-0 font-medium">
            {section.unavailable === 'unauthorized' ? t('update.status.instanceUnauthorized', { name: named.name }) : t('desktopClient.disconnected')}
          </p>
        </div>
      )}
      <StatusNote status={status} copy={copy} />
      {model?.legacyHost && (
        <p className="rounded-md border border-edge px-3 py-2 text-xs leading-5 text-muted shadow-elevation-0">
          {t('update.legacyHost')}
        </p>
      )}
      {section.extra}

      {/* The button sits under the state it acts on, inside its own subject's section. */}
      {lead && (
        <div className="flex items-center justify-end gap-2">
          <Button variant="brand" size="xs" className="max-w-full min-w-0" onClick={lead.run} disabled={lead.disabled} title={lead.label}>
            {lead.icon}
            <span className="truncate">{lead.label}</span>
          </Button>
        </div>
      )}
      {open && model && details && (
        <div className="space-y-3 border-t border-border pt-3">
          <CheckedAt model={model} />
          <Settings model={model} actions={actions} />
        </div>
      )}
    </section>
  );
}

/** The panel while the desktop app shows an instance on another computer: that instance first, under its own
 *  name, then this computer's desktop app. One check asks both updaters. ui/design: update/instance-and-app */
export function UpdateSubjectsPanel({ instanceName, instance, app, check, close }: {
  instanceName: string;
  instance: UpdateSubjectSection;
  app: UpdateSubjectSection;
  check?: { run: () => void; disabled?: boolean };
  close?: () => void;
}) {
  const { t } = useTranslation();
  const checking = instance.model?.status?.kind === 'checking' || app.model?.status?.kind === 'checking';
  return (
    <div data-update-panel data-update-subjects className="text-popover-foreground">
      <Header title={t('dashboard.versionAndUpdate')} check={check} checking={checking} checkLabel={t('update.action.check')} close={close} />
      <SubjectSection named={{ kind: 'instance', name: instanceName, other: t('update.subject.app') }} section={instance} first check={check} />
      <SubjectSection named={{ kind: 'app', name: t('update.subject.app'), other: instanceName }} section={app} first={false} check={check} />
    </div>
  );
}
