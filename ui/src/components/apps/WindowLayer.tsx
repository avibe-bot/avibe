import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';

import { APP_REGISTRY, type AppId } from '../../apps/registry';
import { dockIndexFromShortcut } from '../../apps/dockShortcuts';
import { dockIdToSession, useDock } from '../../context/DockContext';
import { useApi } from '../../context/ApiContext';
import { useWindowManager } from '../../context/WindowManagerContext';
import { useShowPageInventory } from '../useShowPages';
import { AppWindow } from './AppWindow';
import { inTerminalSurface, inTextEntrySurface } from './windowChords';
import { shouldGuardUnload } from './windowUnload';

// The portal layer that hosts app windows. Covers the workbench main area (right
// of the 240px sidebar on desktop). The layer itself is pointer-events-none so
// empty space passes clicks through to the workbench underneath; each AppWindow
// re-enables pointer events on itself (minimized windows stay mounted but inert,
// so their terminal/editor state survives a minimize). Desktop-only — mobile opens
// apps full screen (P5), so no free-floating windows there.
export const WindowLayer: React.FC = () => {
  const { t } = useTranslation();
  const api = useApi();
  const { order, pins } = useDock();
  const { pages } = useShowPageInventory();
  const { windows, close, focus, minimize, openApp, restore, setParams, setTitle, confirmClose } =
    useWindowManager();
  // Any window open and NOT minimized — drives the layer's aria-hidden AND the
  // beforeunload guard (§7.1g).
  const anyShown = shouldGuardUnload(windows);
  const ref = useRef<HTMLDivElement | null>(null);
  const [size, setSize] = useState({ w: 0, h: 0 });
  const windowsRef = useRef(windows);
  const dockRef = useRef({ order, pins, pages });

  useEffect(() => {
    windowsRef.current = windows;
  }, [windows]);

  useEffect(() => {
    dockRef.current = { order, pins, pages };
  }, [order, pages, pins]);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const measure = () => setSize({ w: el.clientWidth, h: el.clientHeight });
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // ⌘W / Ctrl+W closes — ⌘M / Ctrl+M minimizes — the window the keyboard is actually
  // in, resolved from the DOM-focused element's `data-window-id`. NOT the top z-order
  // window: focus can tab or programmatically move into a lower window, and acting on
  // the top one would then close/minimize the wrong window (a dirty editor or terminal
  // the user isn't in). When focus isn't inside any window — the chat composer, another
  // page — the chord falls through to the browser. Inside the terminal only Meta counts,
  // so its Ctrl control-chars (^W/^M) reach the shell (see inTerminalSurface).
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (!(e.metaKey || e.ctrlKey) || e.altKey) return;
      const active = document.activeElement;
      const winEl = active instanceof Element ? active.closest('[data-window-id]') : null;
      if (!winEl || !ref.current?.contains(winEl)) return;
      if (e.ctrlKey && !e.metaKey && inTerminalSurface(active)) return;
      const targetId = winEl.getAttribute('data-window-id');
      if (!targetId) return;
      const key = e.key.toLowerCase();
      if (key === 'w') {
        e.preventDefault();
        if (confirmClose(targetId)) close(targetId);
      } else if (key === 'm') {
        e.preventDefault();
        minimize(targetId);
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [close, minimize, confirmClose]);

  // ⌥W closes the focused in-app window — a browser-safe alternative to ⌘W, which
  // the browser reserves for tab-close (not interceptable). Uses `code` (macOS
  // Option+W emits a special char in `key`); same target resolution + confirmClose
  // guard as the ⌘/Ctrl chord above. Text-entry surfaces (inputs, Monaco, terminal)
  // keep Option+W for character entry — consistent with the Alt+1-9 chord.
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.code !== 'KeyW' || !e.altKey || e.metaKey || e.ctrlKey || e.shiftKey) return;
      const active = document.activeElement;
      if (inTextEntrySurface(active)) return;
      const winEl = active instanceof Element ? active.closest('[data-window-id]') : null;
      if (!winEl || !ref.current?.contains(winEl)) return;
      const targetId = winEl.getAttribute('data-window-id');
      if (!targetId) return;
      e.preventDefault();
      if (confirmClose(targetId)) close(targetId);
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [close, confirmClose]);

  // While any app window is open and visible (not just minimized), guard tab-close
  // / navigation-away with the browser's native confirm — the window state
  // (terminals, unsaved editor buffers, running app iframes) would otherwise vanish
  // silently. No custom copy (browsers ignore it). This is `beforeunload`, distinct
  // from the terminal's `pagehide` keepalive-DELETE, so that cleanup still runs when
  // the user confirms leaving.
  useEffect(() => {
    if (!anyShown) return;
    const onBeforeUnload = (e: BeforeUnloadEvent) => {
      // The window layer is desktop-only (`hidden md:block`); on a narrowed
      // viewport the windows are hidden, so a stale non-minimized window must not
      // prompt on tab-close. Gate on the same md breakpoint, checked at unload time.
      if (!window.matchMedia?.('(min-width: 768px)').matches) return;
      e.preventDefault();
      e.returnValue = '';
    };
    window.addEventListener('beforeunload', onBeforeUnload);
    return () => window.removeEventListener('beforeunload', onBeforeUnload);
  }, [anyShown]);

  // Alt/Option+1..9 focuses or launches the Nth resident tile in the current
  // server-backed Dock order. Use `code`, not `key`: macOS keyboard layouts can
  // turn Option+digit into punctuation. Text inputs and terminals keep the chord
  // for character entry; the Windows layer is desktop-only.
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (!window.matchMedia?.('(min-width: 768px)').matches) return;
      const index = dockIndexFromShortcut(e);
      const target = e.target instanceof Element ? e.target : document.activeElement;
      if (index === null || inTextEntrySurface(target)) return;
      const dockId = dockRef.current.order[index];
      if (!dockId) return;

      const sessionId = dockIdToSession(dockId);
      const appId: AppId = sessionId === null ? (dockId as AppId) : 'showpage';
      if (sessionId === null && !APP_REGISTRY[appId]) return;

      e.preventDefault();
      const own = windowsRef.current.filter((win) =>
        sessionId === null
          ? win.appId === appId
          : win.appId === 'showpage' && win.params?.sessionId === sessionId,
      );
      const visible = own.filter((win) => !win.minimized);
      const focusWindowDom = (id: string) =>
        document.querySelector<HTMLElement>(`[data-window-id="${id}"]`)?.focus({ preventScroll: true });
      if (visible.length > 0) {
        const target = visible.reduce((top, win) => (win.z > top.z ? win : top));
        focus(target.id);
        focusWindowDom(target.id);
        return;
      }
      if (own.length > 0) {
        const target = own.reduce((top, win) => (win.z > top.z ? win : top));
        restore(target.id);
        focusWindowDom(target.id);
        return;
      }
      if (sessionId === null) {
        const id = openApp(appId);
        window.requestAnimationFrame(() => focusWindowDom(id));
        return;
      }
      const page = dockRef.current.pages.find((candidate) => candidate.session_id === sessionId);
      const snapshot = dockRef.current.pins.find((pin) => pin.session_id === sessionId)?.title_snapshot?.trim();
      const title = page ? page.title?.trim() || t('chat.untitled') : snapshot || t('chat.untitled');
      const id = openApp('showpage', { title, params: { sessionId, title } });
      window.requestAnimationFrame(() => focusWindowDom(id));
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [focus, openApp, restore, t]);

  // Session PATCHes already broadcast `session.activity`. Keep every open
  // Show Page window's persisted title/params live when a rename comes from the
  // Library, chat header, CLI, or another browser tab.
  useEffect(
    () =>
      api.connectWorkbenchEvents({
        onSessionActivity: (data) => {
          if (data.event !== 'updated' || !Object.prototype.hasOwnProperty.call(data, 'title')) return;
          const title = data.title?.trim() || t('chat.untitled');
          windowsRef.current
            .filter((win) => win.appId === 'showpage' && win.params?.sessionId === data.session_id)
            .forEach((win) => {
              setTitle(win.id, title);
              setParams(win.id, { title });
            });
        },
      }),
    [api, setParams, setTitle, t],
  );

  // Render every window — minimized ones stay mounted (hidden + inert via AppWindow)
  // so their app body keeps its state. The layer is only aria-hidden when nothing is
  // actually shown (`anyShown`, computed above).

  return (
    <div
      ref={ref}
      aria-hidden={!anyShown}
      // Theme is per-window now (AppWindow sets it from each app's registry `lockTheme`): the File
      // Browser follows the workbench light/dark, while the Editor and Terminal stay dark like a VS
      // Code editor / a terminal. So the layer itself no longer forces a theme — each window opts in.
      // Spans the FULL viewport (no longer offset past the sidebar): windows can move over
      // the sidebar and maximize fills the whole screen. This layer (z-20) sits ABOVE the sidebar
      // (z-10), so a maximized window covers the whole sidebar, Apps launcher included.
      className="pointer-events-none fixed inset-0 z-20 hidden md:block"
    >
      {windows.map((w) => {
        // For a showpage window, join the inventory (already loaded above — no new
        // fetch) to hand its own HTML icon to the title-bar chip (§7.1f/g).
        const sid = w.appId === 'showpage' ? (w.params?.sessionId as string | undefined) : undefined;
        const iconVersion = sid ? pages.find((p) => p.session_id === sid)?.icon_version ?? null : null;
        return <AppWindow key={w.id} win={w} layerWidth={size.w} layerHeight={size.h} iconVersion={iconVersion} />;
      })}
    </div>
  );
};
