// Proposed 2026-11-20: the update panel while the desktop app shows an instance on another
// computer. One panel, two sections: the instance in use, by its name, then this computer's
// desktop app; each with its own version, state and button, so no version is shown without
// saying whose it is. The owner updated this computer's app while meaning to update the
// instance beside whose name the version stood (ui/design: update/instance-and-app).
//
// A section is the shipped panel's body (`UpdatePanel`) with the subject's name in place of
// "Current version" and its button right under its state, so a button is never between two
// subjects; the subject's name opens its last check and its switches. The rows, the status
// note, the switches and the button keep the shipped classes and the shipped words
// (`update.*`), and each subject's state comes through the shipped mapping
// (`updatePanelModel.ts`). When this ships, `UpdatePanel` draws the section itself and this
// copy is deleted. Static: no handlers, no effects.
import type { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { AlertCircle, ArrowUpCircle, Check, ChevronDown, Download, ExternalLink, Info, RefreshCw, WifiOff, X } from 'lucide-react';

import { SidebarStatusRow, StatusDot, StatusGlyph } from '@/components/SidebarStatusRow';
import { VersionChip } from '@/components/VersionChip';
import { ToggleSwitch } from '@/components/settings/SettingsPrimitives';
import { Button } from '@/components/ui/button';
import { badgeVariants } from '@/components/ui/badge-variants';
import type { UpdatePanelModel, UpdatePanelStatus } from '@/components/update/updatePanelModel';
import { isPrereleaseVersion, releaseNotesUrl } from '@/components/update/versions';
import { formatRelativeTime } from '@/lib/relativeTime';
import { cn } from '@/lib/utils';

export type SubjectLang = 'zh' | 'en';
type Draft = (zh: string, en: string) => string;
const draft = (lang: SubjectLang): Draft => (zh, en) => (lang === 'en' ? en : zh);

export type UpdateSubjectSection = {
  /** The instance's own name. */
  name: string;
  /** What the shipped mapping makes of this subject's updater; none while the instance cannot be reached. */
  model: UpdatePanelModel | null;
  /** Its last check and its switches are shown (its name opens and closes them). */
  open?: boolean;
  /** Whether the signed-in account may install here (an instance member without that right gets no button). */
  canUpdate?: boolean;
};

type Tone = 'info' | 'success' | 'danger' | 'neutral';
// UpdatePanel's TONE_CLASS.
const TONE_CLASS: Record<Tone, string> = {
  info: 'border-cyan/30 bg-cyan/10 text-cyan-ink',
  success: 'border-mint/25 bg-mint/10 text-mint-ink',
  danger: 'border-destructive/30 bg-destructive/10 text-destructive-ink',
  neutral: 'border-edge bg-muted/30 text-muted shadow-elevation-0',
};
type Copy = { tone: Tone; title: string; detail?: string; spin?: boolean; offer?: boolean };

type Subject = UpdateSubjectSection & { kind: 'instance' | 'app' };

/** UpdatePanel's `useStatusCopy`, with two changes: an offer says what installing it does to this subject, and a
 *  finished update names what was updated. */
function useStatusCopy(status: UpdatePanelStatus | null, section: Subject, d: Draft): Copy | null {
  const { t } = useTranslation();
  const subject = section.model?.subject;
  if (!status) return null;
  switch (status.kind) {
    case 'checking': return { tone: 'info', spin: true, title: t('update.status.checking') };
    case 'current': return { tone: 'success', title: t(status.awaitingStable ? 'dashboard.awaitingStableRelease' : 'update.status.current') };
    case 'available':
      return {
        tone: 'info',
        offer: true,
        title: t('update.status.available'),
        // New for the instance, in its own words: it restarts (about a minute and a half on the owner's computer,
        // the install included), sessions and settings stay, and a Turn that is running then may be cut
        // (Vibey continues its own after the restart; a Claude Code or Codex Turn ends with a Retry notice).
        detail: section.kind === 'instance'
          ? d(
            `更新时 ${section.name} 会重新启动，通常一两分钟。会话和设置都会保留，正在进行的工作可能中断。`,
            `${section.name} restarts to finish, usually within a minute or two. Sessions and settings are kept; work in progress may be interrupted.`,
          )
          : t('update.status.availableDetail'),
      };
    case 'not-checked': return { tone: 'neutral', title: t('update.status.notChecked') };
    case 'unknown': return { tone: 'neutral', title: t('dashboard.desktopManagedUpdate') };
    case 'disabled': return { tone: 'neutral', title: t('update.status.disabled'), detail: t('update.status.disabledDetail') };
    case 'check-failed':
      return status.reason === 'unreachable'
        ? { tone: 'danger', title: t('update.failure.unreachable.title'), detail: t('update.failure.unreachable.detail') }
        : { tone: 'danger', title: t('update.status.checkFailed'), detail: t('update.status.checkFailedDetail') };
    case 'requesting': return { tone: 'info', spin: true, title: t('update.status.requesting') };
    case 'awaiting-host': return { tone: 'neutral', title: t('update.status.awaitingHost'), detail: t('update.status.awaitingHostDetail') };
    case 'downloading':
      return { tone: 'info', spin: true, title: status.progress === null ? t('update.status.downloading') : t('update.status.downloadingProgress', { progress: status.progress }) };
    case 'installing': return { tone: 'info', spin: true, title: t('update.status.installing') };
    case 'restarting': return { tone: 'info', spin: true, title: t(subject === 'host-desktop' ? 'update.status.restartingHost' : 'update.status.restarting') };
    case 'verifying': return { tone: 'info', spin: true, title: t('update.status.verifying') };
    case 'updated':
      return {
        tone: 'success',
        // New: the result names what was updated.
        title: status.version
          ? d(`${section.name} 已更新到 ${status.version}`, `${section.name} updated to ${status.version}`)
          : d(`${section.name} 已更新`, `${section.name} updated`),
        detail: status.restarting ? t('update.status.restarting') : undefined,
      };
    case 'update-failed': {
      const reason = status.reason ?? 'failed';
      return { tone: reason === 'busy' ? 'neutral' : 'danger', title: t(`update.failure.${reason}.title`), detail: t(`update.failure.${reason}.detail`) };
    }
  }
}

// UpdatePanel's Row and Setting.
function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-3 text-sm">
      <span className="shrink-0 text-muted">{label}</span>
      <span className="flex min-w-0 items-center justify-end gap-1.5 text-right">{children}</span>
    </div>
  );
}
function Setting({ label, hint, enabled }: { label: string; hint: string; enabled: boolean }) {
  return (
    <div className="flex items-center justify-between gap-3">
      <div className="min-w-0">
        <div className="text-sm text-foreground">{label}</div>
        <div className="text-xs text-muted">{hint}</div>
      </div>
      <ToggleSwitch enabled={enabled} onClick={() => {}} label={label} />
    </div>
  );
}

function Section({ section, lang, first }: { section: Subject; lang: SubjectLang; first: boolean }) {
  const { t } = useTranslation();
  const d = draft(lang);
  const { model, open = false, canUpdate = true } = section;
  const status = model?.status ?? null;
  const copy = useStatusCopy(status, section, d);
  const offered = model?.latest ?? null;
  const lead = !canUpdate ? null
    // Two buttons in one panel each say what they update; one section alone keeps the shipped "Update".
    : status?.kind === 'available'
      ? { label: section.kind === 'instance' ? d(`更新 ${section.name}`, `Update ${section.name}`) : d('更新桌面应用', 'Update this app'), icon: <Download /> }
      : (status?.kind === 'update-failed' && status.retry) || status?.kind === 'check-failed' ? { label: t('update.action.retry'), icon: <RefreshCw /> }
        : null;
  const failed = status?.kind === 'check-failed' || status?.kind === 'update-failed';
  return (
    // The shipped panel keeps 16 px under its body and 12 px under its button; a section ends in one or the other.
    <section data-update-subject className={cn('space-y-3 px-4 pt-4', lead && !open ? 'pb-3' : 'pb-4', !first && 'border-t border-border')}>
      {/* The subject's name where the shipped panel says "Current version": every version has its owner beside it.
          The name opens and closes this subject's last check and switches. */}
      <div className="flex items-center justify-between gap-3 text-sm">
        <button type="button" aria-expanded={open} title={section.name}
          className="flex min-w-0 items-center gap-1 rounded-sm font-medium text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
          <span className="truncate">{section.name}</span>
          <ChevronDown size={14} aria-hidden="true" className={cn('shrink-0 text-muted transition-transform', open && 'rotate-180')} />
        </button>
        {model?.current && <span className="shrink-0 font-mono font-medium text-foreground">{model.current}</span>}
      </div>
      {offered && (
        <Row label={t(model?.latestIsStable ? 'update.row.latestStable' : 'update.row.offered')}>
          {isPrereleaseVersion(offered) && <span className={badgeVariants({ variant: 'info' })}>{t('dashboard.prereleaseBadge')}</span>}
          <a href={releaseNotesUrl(offered)} target="_blank" rel="noreferrer" title={t('dashboard.releaseNotes')}
            className="inline-flex min-w-0 items-center gap-1 rounded-sm font-mono font-medium text-foreground hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
            <span className="break-all">{offered}</span>
            <ExternalLink size={12} className="shrink-0 text-muted" aria-hidden="true" />
          </a>
        </Row>
      )}

      {/* An instance that cannot be reached shows the footer's own words for it, and nothing it cannot know. */}
      {!model && (
        <div role="status" className={cn('flex items-start gap-2 rounded-md border px-3 py-2 text-sm', TONE_CLASS.neutral)}>
          <WifiOff size={16} className="mt-0.5 shrink-0" />
          <p className="min-w-0 font-medium">{t('desktopClient.disconnected')}</p>
        </div>
      )}
      {copy && (
        <div role={failed ? 'alert' : 'status'} className={cn('flex items-start gap-2 rounded-md border px-3 py-2 text-sm', TONE_CLASS[copy.tone])}>
          {copy.spin ? <RefreshCw size={16} className="mt-0.5 shrink-0 animate-spin" />
            : copy.offer ? <ArrowUpCircle size={16} className="mt-0.5 shrink-0" />
              : copy.tone === 'success' ? <Check size={16} className="mt-0.5 shrink-0" />
                : copy.tone === 'neutral' ? <Info size={16} className="mt-0.5 shrink-0" />
                  : <AlertCircle size={16} className="mt-0.5 shrink-0" />}
          <div className="min-w-0 space-y-0.5">
            <p className="font-medium">{copy.title}</p>
            {copy.detail && <p className="text-xs opacity-90">{copy.detail}</p>}
          </div>
        </div>
      )}
      {status?.kind === 'downloading' && status.progress !== null && (
        <div className="h-1 overflow-hidden rounded-full bg-muted/40" aria-hidden="true">
          <div className="h-full rounded-full bg-cyan" style={{ width: `${status.progress}%` }} />
        </div>
      )}

      {/* The button sits under the state it acts on, in its own subject's section. */}
      {lead && (
        <div className="flex items-center justify-end gap-2">
          {/* A long instance name truncates inside its button. */}
          <Button variant="brand" size="xs" className="max-w-full min-w-0" title={lead.label}>
            {lead.icon}
            <span className="truncate">{lead.label}</span>
          </Button>
        </div>
      )}
      {open && model && (model.checkedAt || model.prereleases || model.autoUpdate) && (
        <div className="space-y-3 border-t border-border pt-3">
          {model.checkedAt && (
            <Row label={t('update.row.checkedAt')}>
              <time dateTime={model.checkedAt} className="text-foreground">{formatRelativeTime(model.checkedAt, t)}</time>
            </Row>
          )}
          {model.prereleases && <Setting label={t('update.setting.prereleases')} hint={t('update.setting.prereleasesHint')} enabled={model.prereleases.enabled} />}
          {model.autoUpdate && <Setting label={t('update.setting.autoUpdate')} hint={t('update.setting.autoUpdateHint')} enabled={model.autoUpdate.enabled} />}
        </div>
      )}
    </section>
  );
}

/** The panel's content, for the popup the desktop app draws (`UpdatePopoverView`): the shipped header, then the
 *  instance in use under its own name, then this computer's desktop app. */
export function UpdateSubjectsPanel({ lang, instance, app }: {
  lang: SubjectLang;
  instance: UpdateSubjectSection;
  app: Omit<UpdateSubjectSection, 'name'>;
}) {
  const sections: Subject[] = [
    { ...instance, kind: 'instance' },
    { ...app, kind: 'app', name: draft(lang)('这台电脑上的桌面应用', "This computer's app") },
  ];
  const { t } = useTranslation();
  const checking = sections.some((section) => section.model?.status?.kind === 'checking');
  return (
    <div data-update-panel lang={lang} className="text-popover-foreground">
      <div className="flex items-center justify-between gap-2 border-b border-border px-4 py-3">
        <p className="min-w-0 text-sm font-medium text-foreground">{t('dashboard.versionAndUpdate')}</p>
        <div className="flex shrink-0 items-center gap-1">
          {/* One check asks both updaters. */}
          <Button variant="ghost" size="icon" className="h-7 w-7 text-muted hover:text-foreground" aria-label={t('update.action.check')} title={t('update.action.check')}>
            <RefreshCw size={14} className={checking ? 'animate-spin' : ''} />
          </Button>
          <Button variant="ghost" size="icon" className="h-7 w-7 text-muted hover:text-foreground" aria-label={t('common.close')}>
            <X size={14} />
          </Button>
        </div>
      </div>
      {sections.map((section, index) => <Section key={section.name} section={section} lang={lang} first={index === 0} />)}
    </div>
  );
}

/** The sidebar's bottom row while the desktop app shows another computer's instance: the version beside the
 *  instance's name is the instance's. It turns gold with a dot when either subject can be updated, and spins
 *  while the instance updates, whether or not the panel is open. The trigger is DesktopAccountRowView's. */
export function InstanceVersionRow({ lang, name, version, attention = false, updating = false, offline = false }: {
  lang: SubjectLang;
  name: string;
  /** The instance's version; none while it has never been reached. */
  version: string | null;
  attention?: boolean;
  updating?: boolean;
  offline?: boolean;
}) {
  const { t } = useTranslation();
  const d = draft(lang);
  const shown = version ? `${t('dashboard.versionPrefix')}${version}` : null;
  const trigger = (
    <Button data-account-trigger type="button" variant="ghost" title={name}
      className="h-8 min-w-0 flex-1 justify-start gap-1.5 rounded-md border border-transparent pl-4 pr-1.5 text-left text-xs [--press-scale:1]">
      <StatusGlyph label={name}>
        {/* The icon fills its box where the dot leaves 2 px, so it sits 2 px left: the same clear space before the
            name in both states, and the name does not move. The shipped row draws it flush, touching the name. */}
        {offline ? <WifiOff data-status-indicator className="-ml-0.5 size-3 shrink-0 text-gold-ink" aria-label={t('desktopClient.disconnected')} />
          : <StatusDot tone="running" label={t('desktopClient.connected')} />}
      </StatusGlyph>
      <span data-account-label className="min-w-0 truncate">{name}</span>
    </Button>
  );
  const chip = shown && (
    <VersionChip attention={attention && !updating} updateDot={attention && !updating}
      title={d(`${name} 的版本 ${shown}`, `${name} is on ${shown}`)}>
      {updating && <RefreshCw size={12} aria-hidden="true" className="animate-spin" />}
      {shown}
    </VersionChip>
  );
  return <div data-desktop-account-row className="relative min-w-0"><SidebarStatusRow status={trigger} version={chip} /></div>;
}
