import { useEffect, useRef, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { ChevronUp, FolderTree, LayoutGrid, TerminalSquare } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';
import type { LucideIcon } from 'lucide-react';

// The "Apps" layer launcher — a Start-menu-style list that opens on hover (and
// on click, for touch/keyboard) from the sidebar's bottom-left. File Browser and
// Terminal are the first two apps; Show Pages will become pinnable here later.
// Mirrors the InboxHoverPopover open/close timer dance so the menu survives the
// cursor crossing the gap between the trigger and the floating panel.
type AppItem = { to: string; labelKey: string; descKey: string; icon: LucideIcon; soon?: boolean };

const APPS: AppItem[] = [
  { to: '/apps/files', labelKey: 'apps.fileBrowser.label', descKey: 'apps.fileBrowser.desc', icon: FolderTree },
  { to: '/apps/terminal', labelKey: 'apps.terminal.label', descKey: 'apps.terminal.desc', icon: TerminalSquare, soon: true },
];

export const AppsLauncher: React.FC = () => {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const location = useLocation();
  const [open, setOpen] = useState(false);
  const closeTimer = useRef<number | null>(null);
  const active = location.pathname.startsWith('/apps');

  const openMenu = () => {
    if (closeTimer.current !== null) {
      window.clearTimeout(closeTimer.current);
      closeTimer.current = null;
    }
    setOpen(true);
  };
  const queueClose = () => {
    if (closeTimer.current !== null) window.clearTimeout(closeTimer.current);
    closeTimer.current = window.setTimeout(() => {
      setOpen(false);
      closeTimer.current = null;
    }, 180);
  };
  useEffect(
    () => () => {
      if (closeTimer.current !== null) window.clearTimeout(closeTimer.current);
    },
    [],
  );
  // Dismiss when navigation lands on a new route (tapping an app navigates).
  useEffect(() => {
    setOpen(false);
  }, [location.pathname]);

  const go = (to: string) => {
    setOpen(false);
    navigate(to);
  };

  return (
    <div className="relative flex-1" onMouseEnter={openMenu} onMouseLeave={queueClose}>
      <button
        type="button"
        onClick={() => (open ? setOpen(false) : openMenu())}
        aria-haspopup="menu"
        aria-expanded={open}
        className={clsx(
          'group flex w-full items-center gap-2.5 rounded-lg border px-3 py-2.5 text-[13px] font-medium transition-colors',
          active || open
            ? 'border-mint/30 bg-mint/[0.08] text-foreground shadow-[0_0_16px_-4px_rgba(91,255,160,0.5)]'
            : 'border-border-strong text-foreground hover:bg-foreground/[0.04]',
        )}
      >
        <LayoutGrid className={clsx('size-4', active || open ? 'text-mint' : 'text-muted group-hover:text-foreground')} />
        <span className="flex-1 text-left">{t('apps.title')}</span>
        <ChevronUp className={clsx('size-3.5 shrink-0 text-muted transition-transform', !open && 'rotate-180')} />
      </button>

      {open && (
        <div
          role="menu"
          aria-label={t('apps.title')}
          onMouseEnter={openMenu}
          onMouseLeave={queueClose}
          className="absolute bottom-full left-0 z-50 mb-2 flex w-[256px] flex-col gap-1 rounded-2xl border border-border-strong bg-surface-2 p-2 shadow-[0_24px_64px_-12px_rgba(0,0,0,0.6)]"
        >
          <div className="px-2 pb-0.5 pt-1 font-mono text-[10px] font-bold uppercase tracking-[0.18em] text-muted">
            {t('apps.title')}
          </div>
          {APPS.map((app) => {
            const Icon = app.icon;
            const isActive = location.pathname.startsWith(app.to);
            return (
              <button
                key={app.to}
                type="button"
                role="menuitem"
                onClick={() => go(app.to)}
                className={clsx(
                  'flex items-start gap-2.5 rounded-lg px-2.5 py-2 text-left transition',
                  isActive ? 'bg-mint/[0.08]' : 'hover:bg-foreground/[0.04]',
                )}
              >
                <span className="mt-0.5 grid size-8 shrink-0 place-items-center rounded-lg border border-border bg-foreground/[0.03]">
                  <Icon className="size-4 text-mint" />
                </span>
                <span className="flex min-w-0 flex-1 flex-col gap-0.5">
                  <span className="flex items-center gap-1.5 text-[12.5px] font-semibold text-foreground">
                    {t(app.labelKey)}
                    {app.soon && (
                      <span className="rounded-full border border-border bg-foreground/[0.04] px-1.5 py-0.5 font-mono text-[9px] font-medium text-muted">
                        {t('apps.soon')}
                      </span>
                    )}
                  </span>
                  <span className="line-clamp-2 text-[11px] leading-relaxed text-muted">{t(app.descKey)}</span>
                </span>
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
};
