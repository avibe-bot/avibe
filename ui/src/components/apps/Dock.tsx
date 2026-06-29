import { useEffect, useState } from 'react';
import { Copy, Plus, SquarePlus } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';

import { APP_LIST, APP_REGISTRY, type AppId } from '../../apps/registry';
import { useWindowManager } from '../../context/WindowManagerContext';

// The unified Dock panel: app launcher + running indicators + minimized windows.
// Headless of how it's revealed — the AppsLauncher trigger floats it above the bottom-left
// Apps button (hover = preview, click = pin). A running app's tile reveals a ＋ (and right-click
// menu) to open another window — windows are multi-instance, so this is the explicit path to a
// second Files/Terminal/Editor. Each app tile carries its name (macOS-style label) and each
// minimized window shows its title so the user can tell several open windows apart.
export const Dock: React.FC = () => {
  const { t } = useTranslation();
  const wm = useWindowManager();
  const { windows, focusedId } = wm;
  const focusedApp = windows.find((w) => w.id === focusedId)?.appId;
  const minimized = windows.filter((w) => w.minimized);
  const [menuApp, setMenuApp] = useState<AppId | null>(null);

  // The per-tile context menu is a transient popover — Esc dismisses it (the backdrop handles
  // click-outside). Without this it lingered on screen after the user moved on (Codex).
  useEffect(() => {
    if (!menuApp) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setMenuApp(null);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [menuApp]);

  // Click an app: focus its front window, else un-minimize its top window, else launch one.
  const activate = (appId: AppId) => {
    setMenuApp(null); // any tile interaction dismisses a lingering context menu
    const own = windows.filter((w) => w.appId === appId);
    const visible = own.filter((w) => !w.minimized);
    if (visible.length > 0) {
      const top = visible.reduce((a, b) => (b.z > a.z ? b : a));
      wm.focus(top.id);
      (document.querySelector(`[data-window-id="${top.id}"]`) as HTMLElement | null)?.focus?.({ preventScroll: true });
    } else if (own.length > 0) {
      wm.restore(own.reduce((a, b) => (b.z > a.z ? b : a)).id);
    } else {
      wm.openApp(appId);
    }
  };

  const openNew = (appId: AppId) => {
    wm.openApp(appId);
    setMenuApp(null);
  };
  const showAll = (appId: AppId) => {
    const own = windows.filter((w) => w.appId === appId);
    own.filter((w) => w.minimized).forEach((w) => wm.restore(w.id));
    const visible = own.filter((w) => !w.minimized);
    if (visible.length > 0) wm.focus(visible.reduce((a, b) => (b.z > a.z ? b : a)).id);
    setMenuApp(null);
  };

  return (
    <div className="relative">
      {menuApp && <div className="fixed inset-0 z-0" onClick={() => setMenuApp(null)} aria-hidden />}
      <div className="relative flex items-end gap-2 rounded-2xl border border-border-strong bg-surface-2/95 p-2 shadow-[0_24px_64px_-12px_rgba(0,0,0,0.65)] backdrop-blur-xl">
        {APP_LIST.map((app) => {
          const Icon = app.icon;
          const running = windows.some((w) => w.appId === app.id);
          const active = focusedApp === app.id;
          return (
            <div key={app.id} className="group/tile relative flex w-[60px] flex-col items-center gap-1">
              {/* Icon wrapper is the positioning context for the ＋ affordance so it pins to the
                  icon's corner (not the wider labelled tile). */}
              <div className="relative">
                <button
                  type="button"
                  title={t(app.titleKey)}
                  aria-label={t(app.titleKey)}
                  onClick={(e) => (e.metaKey || e.ctrlKey || e.altKey ? openNew(app.id) : activate(app.id))}
                  onContextMenu={(e) => {
                    e.preventDefault();
                    setMenuApp((cur) => (cur === app.id ? null : app.id));
                  }}
                  className={clsx(
                    'grid size-10 place-items-center rounded-xl border transition',
                    active ? 'border-2 bg-foreground/[0.07]' : 'border-border bg-foreground/[0.03] hover:bg-foreground/[0.07]',
                  )}
                  style={active ? { borderColor: `var(${app.accent})` } : undefined}
                >
                  <Icon className="size-5" style={{ color: `var(${app.accent})` }} />
                </button>

                {/* ＋ reveals on hover and directly opens another window (the one-click affordance the
                    label advertises). The New Window / Show All Windows menu is the tile's right-click. */}
                <button
                  type="button"
                  title={t('apps.dock.newWindow')}
                  aria-label={t('apps.dock.newWindow')}
                  onClick={(e) => {
                    e.stopPropagation();
                    openNew(app.id);
                  }}
                  className={clsx(
                    'absolute -right-1 -top-1 grid size-4 place-items-center rounded-full border border-border-strong bg-surface-2 text-muted shadow transition hover:text-foreground',
                    menuApp === app.id ? 'opacity-100' : 'opacity-0 group-hover/tile:opacity-100',
                  )}
                >
                  <Plus className="size-2.5" strokeWidth={3} />
                </button>
              </div>

              {/* App name (macOS-style labelled tile) + running indicator. The name also surfaces on
                  hover via the icon button's title, in case it's truncated. */}
              <span className="max-w-full truncate text-[10px] leading-tight text-muted">{t(app.titleKey)}</span>
              <span className="size-1 rounded-full" style={{ backgroundColor: running ? `var(${app.accent})` : 'transparent' }} />

              {menuApp === app.id && (
                <div
                  role="menu"
                  // Anchor to the tile's LEFT edge (opens rightward), not centered: the Dock sits at
                  // the bottom-left, so a centered menu's left half spilled past the viewport edge and
                  // got clipped (Codex). Left-anchored keeps the whole menu on-screen.
                  className="absolute bottom-full left-0 z-10 mb-2 w-44 rounded-xl border border-border bg-surface-3 p-1.5 shadow-[0_10px_28px_-8px_rgba(0,0,0,0.7)]"
                >
                  <button
                    type="button"
                    role="menuitem"
                    onClick={() => openNew(app.id)}
                    className="flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-left text-[12.5px] font-medium text-foreground transition hover:bg-cyan-soft"
                  >
                    <SquarePlus className="size-[15px] text-cyan" />
                    {t('apps.dock.newWindow')}
                  </button>
                  <button
                    type="button"
                    role="menuitem"
                    onClick={() => showAll(app.id)}
                    disabled={!running}
                    className="flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-left text-[12.5px] text-muted transition hover:bg-foreground/[0.05] disabled:opacity-40 disabled:hover:bg-transparent"
                  >
                    <Copy className="size-[15px]" />
                    {t('apps.dock.showAllWindows')}
                  </button>
                </div>
              )}
            </div>
          );
        })}

        {minimized.length > 0 && <div className="mx-1 h-11 w-px shrink-0 self-center bg-border-strong" />}

        {/* Minimized windows: an icon + the window's title so several open windows are
            distinguishable at a glance (a true live thumbnail isn't practical — the editor/terminal
            render to canvas — so the title carries the identification). */}
        {minimized.map((w) => {
          const def = APP_REGISTRY[w.appId];
          const Icon = def.icon;
          const label = w.title ?? t(def.titleKey);
          return (
            <button
              key={w.id}
              type="button"
              title={label}
              aria-label={t('apps.window.restore', { defaultValue: 'Restore' })}
              onClick={() => wm.restore(w.id)}
              className="flex h-10 w-[136px] shrink-0 items-center gap-2 self-center rounded-lg border border-border-strong bg-surface px-2.5 text-left transition hover:border-foreground/30"
            >
              <Icon className="size-4 shrink-0" style={{ color: `var(${def.accent})` }} />
              <span className="min-w-0 flex-1 truncate text-[11px] text-foreground">{label}</span>
            </button>
          );
        })}
      </div>
    </div>
  );
};
