import { useEffect, useRef, useState } from 'react';
import { ChevronUp, CodeXml, FolderTree, LayoutGrid, TerminalSquare } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';
import type { LucideIcon } from 'lucide-react';

import { useWindowManager } from '../context/WindowManagerContext';
import type { AppId } from '../apps/registry';

// The "Apps" launcher in the sidebar's bottom-left. Opens on hover (and click,
// for touch/keyboard) and now LAUNCHES WINDOWS via the WindowManager rather than
// navigating to a full page. This is the P1 bridge — P2 replaces it with the full
// Dock (running indicators + minimized-window thumbnails). Open/close timer dance
// mirrors InboxHoverPopover so the menu survives the trigger→panel cursor gap.
type AppItem = { appId: AppId; labelKey: string; descKey: string; icon: LucideIcon };

const APPS: AppItem[] = [
  { appId: 'files', labelKey: 'apps.fileBrowser.label', descKey: 'apps.fileBrowser.desc', icon: FolderTree },
  { appId: 'terminal', labelKey: 'apps.terminal.label', descKey: 'apps.terminal.desc', icon: TerminalSquare },
  { appId: 'editor', labelKey: 'apps.editor.label', descKey: 'apps.editor.desc', icon: CodeXml },
];

export const AppsLauncher: React.FC = () => {
  const { t } = useTranslation();
  const { openApp } = useWindowManager();
  const [open, setOpen] = useState(false);
  const closeTimer = useRef<number | null>(null);

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

  const launch = (appId: AppId) => {
    setOpen(false);
    openApp(appId);
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
          open
            ? 'border-cyan/40 bg-cyan-soft text-foreground shadow-[0_0_16px_-4px_rgba(63,224,229,0.5)]'
            : 'border-border-strong text-foreground hover:bg-foreground/[0.04]',
        )}
      >
        <LayoutGrid className={clsx('size-4', open ? 'text-cyan' : 'text-muted group-hover:text-foreground')} />
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
            return (
              <button
                key={app.appId}
                type="button"
                role="menuitem"
                onClick={() => launch(app.appId)}
                className="flex items-start gap-2.5 rounded-lg px-2.5 py-2 text-left transition hover:bg-foreground/[0.04]"
              >
                <span className="mt-0.5 grid size-8 shrink-0 place-items-center rounded-lg border border-border bg-foreground/[0.03]">
                  <Icon className="size-4 text-mint" />
                </span>
                <span className="flex min-w-0 flex-1 flex-col gap-0.5">
                  <span className="text-[12.5px] font-semibold text-foreground">{t(app.labelKey)}</span>
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
