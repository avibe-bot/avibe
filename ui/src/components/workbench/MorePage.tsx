import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { ArrowRight, FolderTree, Link as LinkIcon, LogOut, SlidersHorizontal, SquareTerminal } from 'lucide-react';
import clsx from 'clsx';

import { useApi } from '../../context/ApiContext';
import { useStatus } from '../../context/StatusContext';
import { useAuthAccount } from '../../lib/useAuthAccount';
import { LanguageSwitcher } from '../LanguageSwitcher';
import { ThemeToggle } from '../ThemeToggle';
import { VersionBadge } from '../VersionBadge';

// Mobile-only "More" tab (workbench). The bridge to the Control Panel plus
// appearance / connection / account. Per product decision the service
// start/stop control lives ONLY in the Control Panel, so this screen shows a
// read-only status line. Design: design.pen `Nxnja`.
export const MorePage: React.FC = () => {
  const { t } = useTranslation();
  const { status } = useStatus();
  const api = useApi();
  const { email, signingOut, signOut } = useAuthAccount();
  const [config, setConfig] = useState<any>(null);

  useEffect(() => {
    api.getConfig().then(setConfig).catch(() => {});
  }, [api]);

  const isRunning = status.state === 'running';
  const hostname = config?.runtime?.hostname as string | undefined;

  return (
    <div className="mx-auto flex max-w-xl flex-col gap-4">
      <h1 className="text-xl font-bold">{t('more.title')}</h1>

      {/* Read-only service status — control lives in the Control Panel. */}
      <div
        className={clsx(
          'flex items-center gap-2.5 rounded-xl border px-4 py-3.5',
          isRunning ? 'border-mint/30 bg-mint/[0.08]' : 'border-border bg-surface'
        )}
      >
        <span
          className={clsx(
            'size-2.5 shrink-0 rounded-full',
            isRunning ? 'bg-mint shadow-[0_0_9px_rgba(91,255,160,0.9)]' : 'bg-muted'
          )}
        />
        <span className="flex-1 text-sm font-semibold">
          {isRunning ? t('common.running') : t('common.stopped')}
        </span>
        <VersionBadge />
      </div>

      {/* Bridge to the Control Panel (admin shell). */}
      <Link
        to="/admin/dashboard"
        className="flex items-center gap-3 rounded-xl border border-cyan/35 bg-surface px-4 py-3.5 transition hover:bg-foreground/[0.04]"
      >
        <span className="grid size-9 shrink-0 place-items-center rounded-lg bg-cyan/[0.14]">
          <SlidersHorizontal className="size-[18px] text-cyan" />
        </span>
        <span className="min-w-0 flex-1">
          <span className="block text-[15px] font-semibold">{t('more.controlPanel')}</span>
          <span className="block truncate text-[11.5px] text-muted">{t('more.controlPanelDesc')}</span>
        </span>
        <ArrowRight className="size-[18px] shrink-0 text-cyan" />
      </Link>

      {/* Apps — the desktop sidebar's Apps launcher, surfaced for mobile where
          there is no sidebar. */}
      <div className="flex flex-col gap-2">
        <div className="px-1 font-mono text-[10px] font-bold uppercase tracking-[0.18em] text-muted">
          {t('apps.title')}
        </div>
        <Link
          to="/apps/files"
          className="flex items-center gap-3 rounded-xl border border-border bg-surface px-4 py-3.5 transition hover:bg-foreground/[0.04]"
        >
          <span className="grid size-9 shrink-0 place-items-center rounded-lg bg-mint/[0.12]">
            <FolderTree className="size-[18px] text-mint" />
          </span>
          <span className="min-w-0 flex-1">
            <span className="block text-[15px] font-semibold">{t('apps.fileBrowser.label')}</span>
            <span className="block truncate text-[11.5px] text-muted">{t('apps.fileBrowser.desc')}</span>
          </span>
          <ArrowRight className="size-[18px] shrink-0 text-muted" />
        </Link>
        <Link
          to="/apps/terminal"
          className="flex items-center gap-3 rounded-xl border border-border bg-surface px-4 py-3.5 transition hover:bg-foreground/[0.04]"
        >
          <span className="grid size-9 shrink-0 place-items-center rounded-lg bg-foreground/[0.05]">
            <SquareTerminal className="size-[18px] text-foreground" />
          </span>
          <span className="min-w-0 flex-1">
            <span className="block text-[15px] font-semibold">{t('apps.terminal.label')}</span>
            <span className="block truncate text-[11.5px] text-muted">{t('apps.terminal.desc')}</span>
          </span>
          <ArrowRight className="size-[18px] shrink-0 text-muted" />
        </Link>
      </div>

      {/* Appearance — reuse the existing toggles as touch rows. */}
      <div className="rounded-xl border border-border bg-surface">
        <div className="flex items-center gap-3 px-4 py-3">
          <span className="flex-1 text-sm font-medium">{t('more.appearance')}</span>
          <ThemeToggle />
          <LanguageSwitcher />
        </div>
      </div>

      {/* Account — moved here from the mobile header. Only shown for an
          authenticated remote session (local setups have no sign-out). */}
      {email && (
        <div className="overflow-hidden rounded-xl border border-border bg-surface">
          <div className="flex items-center gap-3 px-4 py-3">
            <span className="grid size-9 shrink-0 place-items-center rounded-full border border-cyan/35 bg-cyan/[0.08] text-[13px] font-semibold text-cyan">
              {(email.split('@')[0]?.[0] ?? '?').toUpperCase()}
            </span>
            <div className="min-w-0 flex-1">
              <div className="text-[10px] font-bold uppercase tracking-[0.16em] text-muted">{t('appShell.signedInAs')}</div>
              <div className="truncate text-sm font-medium">{email}</div>
            </div>
          </div>
          <button
            type="button"
            onClick={signOut}
            disabled={signingOut}
            className="flex w-full items-center gap-2 border-t border-border px-4 py-3 text-left text-sm font-medium text-destructive transition hover:bg-destructive/[0.06] disabled:opacity-60"
          >
            <LogOut className="size-4" />
            {signingOut ? t('appShell.signingOut') : t('appShell.signOut')}
          </button>
        </div>
      )}

      {/* Connection — host only. The version badge already lives in the status
          card above, so a second version row here would be redundant. */}
      {hostname && (
        <div className="rounded-xl border border-border bg-surface">
          <div className="flex items-center gap-3 px-4 py-3">
            <LinkIcon className="size-4 text-muted" />
            <span className="flex-1 text-sm font-medium">{t('more.host')}</span>
            <span className="font-mono text-[12px] text-muted">{hostname}</span>
          </div>
        </div>
      )}
    </div>
  );
};
