import { useTranslation } from 'react-i18next';
import clsx from 'clsx';

import { APP_LIST, APP_REGISTRY, type AppId } from '../../apps/registry';
import { useWindowManager } from '../../context/WindowManagerContext';

// The unified Dock panel: app launcher + running indicators + minimized-window
// thumbnails. Headless of how it's revealed — the AppsLauncher trigger floats it
// above the bottom-left Apps button (hover = preview, click = pin).
export const Dock: React.FC = () => {
  const { t } = useTranslation();
  const wm = useWindowManager();
  const { windows, focusedId } = wm;
  const focusedApp = windows.find((w) => w.id === focusedId)?.appId;
  const minimized = windows.filter((w) => w.minimized);

  // Click an app: focus its front window, else un-minimize its top window, else
  // launch a fresh one.
  const activate = (appId: AppId) => {
    const own = windows.filter((w) => w.appId === appId);
    const visible = own.filter((w) => !w.minimized);
    if (visible.length > 0) {
      const top = visible.reduce((a, b) => (b.z > a.z ? b : a));
      wm.focus(top.id);
      // Pull DOM focus straight to it: if it was already the top window, wm.focus is a
      // no-op so the focusedId-keyed focus effect won't re-fire, and the chord (routed by
      // document.activeElement) would otherwise stay on this Dock button. The element is
      // already rendered + visible here, so focusing it directly is safe.
      (document.querySelector(`[data-window-id="${top.id}"]`) as HTMLElement | null)?.focus?.({ preventScroll: true });
    } else if (own.length > 0) {
      // Restored from minimized: the element is inert until the next render, so DOM focus is
      // handled by AppWindow's focus-on-becoming-top effect rather than a direct call here.
      wm.restore(own.reduce((a, b) => (b.z > a.z ? b : a)).id);
    } else {
      wm.openApp(appId);
    }
  };

  return (
    <div className="flex items-center gap-2 rounded-2xl border border-border-strong bg-surface-2/95 p-2 shadow-[0_24px_64px_-12px_rgba(0,0,0,0.65)] backdrop-blur-xl">
      {APP_LIST.map((app) => {
        const Icon = app.icon;
        const running = windows.some((w) => w.appId === app.id);
        const active = focusedApp === app.id;
        return (
          <button
            key={app.id}
            type="button"
            title={running ? `${t(app.titleKey)} · ${t('apps.dock.newInstanceHint')}` : t(app.titleKey)}
            aria-label={t(app.titleKey)}
            // Plain click focuses/restores the existing window; a modifier-click
            // (⌘/Ctrl/Alt) always opens another instance — windows are multi-instance,
            // but otherwise there'd be no UI path to a second Files/Terminal window.
            onClick={(e) => (e.metaKey || e.ctrlKey || e.altKey ? wm.openApp(app.id) : activate(app.id))}
            className="flex flex-col items-center gap-1.5"
          >
            <span
              className={clsx(
                'grid size-12 place-items-center rounded-xl border transition',
                active ? 'border-2 bg-foreground/[0.07]' : 'border-border bg-foreground/[0.03] hover:bg-foreground/[0.07]',
              )}
              style={active ? { borderColor: `var(${app.accent})` } : undefined}
            >
              <Icon className="size-6" style={{ color: `var(${app.accent})` }} />
            </span>
            <span className="size-1.5 rounded-full" style={{ backgroundColor: running ? `var(${app.accent})` : 'transparent' }} />
          </button>
        );
      })}

      {minimized.length > 0 && <div className="mx-1 h-11 w-px shrink-0 bg-border-strong" />}

      {minimized.map((w) => {
        const def = APP_REGISTRY[w.appId];
        const Icon = def.icon;
        return (
          <button
            key={w.id}
            type="button"
            title={w.title ?? t(def.titleKey)}
            aria-label={t('apps.window.restore', { defaultValue: 'Restore' })}
            onClick={() => wm.restore(w.id)}
            className="flex h-[52px] w-[76px] shrink-0 flex-col overflow-hidden rounded-lg border border-border-strong bg-surface transition hover:border-foreground/30"
          >
            <span className="flex items-center gap-1 bg-surface-2 px-1.5 py-1">
              {['#ff5f57', '#febc2e', '#28c840'].map((c) => (
                <span key={c} className="size-1 rounded-full" style={{ backgroundColor: c }} />
              ))}
            </span>
            <span className="flex flex-1 items-center justify-center">
              <Icon className="size-4" style={{ color: `var(${def.accent})` }} />
            </span>
          </button>
        );
      })}
    </div>
  );
};
