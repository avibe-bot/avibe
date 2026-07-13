import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Link as LinkIcon, LogOut } from 'lucide-react';
import clsx from 'clsx';

import { useApi } from '../../context/ApiContext';
import { useStatus } from '../../context/StatusContext';
import { useAuthAccount } from '../../lib/useAuthAccount';
import { LanguageSwitcher } from '../LanguageSwitcher';
import { ThemeToggle } from '../ThemeToggle';
import { VersionBadge } from '../VersionBadge';

// The former mobile "More" page has been absorbed into the mobile Dock drawer
// (§7.1b): its Apps list is now the drawer's tile grid, its Control Panel bridge
// is the drawer's 设置 chip, and the remaining bits — appearance, account, and the
// read-only connection/status — are these three reusable sections, surfaced from
// the drawer footer chips. Extracting them keeps the capability intact while the
// `/more` route retires (redirects home). No standalone page component remains.

/** Appearance controls (theme + language). The drawer's 外观 chip. */
export const MoreAppearanceSection: React.FC = () => {
  const { t } = useTranslation();
  return (
    <div className="rounded-xl border border-border bg-surface">
      <div className="flex items-center gap-3 px-4 py-3">
        <span className="flex-1 text-sm font-medium">{t('more.appearance')}</span>
        <ThemeToggle />
        <LanguageSwitcher openUpward />
      </div>
    </div>
  );
};

/** Signed-in account + sign out. Renders null when this isn't a remote,
 *  authenticated session (local setups have no account). The drawer's 账号 chip. */
export const MoreAccountSection: React.FC = () => {
  const { t } = useTranslation();
  const { email, signingOut, signOut } = useAuthAccount();

  if (!email) return null;

  return (
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
  );
};

/** Read-only service status + version, plus the host address. The drawer's 更多
 *  overflow — everything that didn't earn its own chip. */
export const MoreConnectionSection: React.FC = () => {
  const { t } = useTranslation();
  const { status } = useStatus();
  const api = useApi();
  const [config, setConfig] = useState<{ runtime?: { hostname?: string } } | null>(null);

  useEffect(() => {
    api.getConfig().then(setConfig).catch(() => {});
  }, [api]);

  const isRunning = status.state === 'running';
  const hostname = config?.runtime?.hostname;

  return (
    <div className="flex flex-col gap-3">
      {/* Read-only service status — control lives in the Control Panel. */}
      <div
        className={clsx(
          'flex items-center gap-2.5 rounded-xl border px-4 py-3.5',
          isRunning ? 'border-mint/30 bg-mint/[0.08]' : 'border-border bg-surface',
        )}
      >
        <span
          className={clsx(
            'size-2.5 shrink-0 rounded-full',
            isRunning ? 'bg-mint shadow-[0_0_9px_rgba(91,255,160,0.9)]' : 'bg-muted',
          )}
        />
        <span className="flex-1 text-sm font-semibold">
          {isRunning ? t('common.running') : t('common.stopped')}
        </span>
        <VersionBadge />
      </div>

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
