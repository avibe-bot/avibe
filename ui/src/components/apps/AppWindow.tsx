import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';
import { Minus, Plus, X, type LucideIcon } from 'lucide-react';

import { APP_REGISTRY } from '../../apps/registry';
import { useWindowManager, type WindowInstance } from '../../context/WindowManagerContext';
import { clampToLayer, resizeBounds, type ResizeDir } from '../../lib/windowBounds';
import { ErrorBoundary } from '../ui/error-boundary';

const RESIZE_HANDLES: { dir: ResizeDir; className: string }[] = [
  { dir: 'n', className: 'left-2 right-2 top-0 h-1.5 cursor-ns-resize' },
  { dir: 's', className: 'left-2 right-2 bottom-0 h-1.5 cursor-ns-resize' },
  { dir: 'e', className: 'top-2 bottom-2 right-0 w-1.5 cursor-ew-resize' },
  { dir: 'w', className: 'top-2 bottom-2 left-0 w-1.5 cursor-ew-resize' },
  { dir: 'ne', className: 'top-0 right-0 size-3 cursor-nesw-resize' },
  { dir: 'nw', className: 'top-0 left-0 size-3 cursor-nwse-resize' },
  { dir: 'se', className: 'bottom-0 right-0 size-3 cursor-nwse-resize' },
  { dir: 'sw', className: 'bottom-0 left-0 size-3 cursor-nesw-resize' },
];

export const AppWindow: React.FC<{ win: WindowInstance; layerWidth: number; layerHeight: number }> = ({
  win,
  layerWidth,
  layerHeight,
}) => {
  const { t } = useTranslation();
  const wm = useWindowManager();
  const def = APP_REGISTRY[win.appId];
  const rootRef = useRef<HTMLDivElement | null>(null);
  const draggingRef = useRef(false);
  // Closing plays a scale-down exit whose animationEnd drives the real unmount (the
  // CSS owns the timing). Minimizing is a pure mounted hide (see className) so the
  // window body — terminal session, editor buffer — stays alive and intact.
  const [exitKind, setExitKind] = useState<'close' | null>(null);
  // Animate window GEOMETRY (maximize/restore) but NOT during a drag/resize, which must track the
  // pointer instantly. `dragging` is state (not a ref) so the transition is enabled in the SAME render
  // that changes the bounds — otherwise the geometry jumps before the transition class arrives and
  // maximize/restore don't animate at all.
  const [dragging, setDragging] = useState(false);

  // Keep a visible window reachable when the geometry around it changes without a
  // drag: the layer shrinking, or the window being restored / un-maximized after the
  // layer shrank (the gesture clamps can't fire in those cases). Deliberately not
  // keyed on win.bounds — a drag clamps itself, and re-running here would fight it.
  useEffect(() => {
    // Skip while hidden, and when the layer reports 0×0 — it's `hidden md:block`, so
    // below the md breakpoint the ResizeObserver sees no size; clamping to a zero layer
    // would shove a normal window to a negative origin and persist that off-screen.
    if (win.minimized || win.maximized || layerWidth === 0 || layerHeight === 0) return;
    const c = clampToLayer(win.bounds, layerWidth, layerHeight);
    if (c.x !== win.bounds.x || c.y !== win.bounds.y) wm.setBounds(win.id, c);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [win.minimized, win.maximized, layerWidth, layerHeight]);

  // Pull DOM focus to this window when it BECOMES the focused (top) window without a
  // pointer click already placing focus inside it — i.e. opened via openApp, or
  // activated / restored from the Dock (which only raise z-order). The keyboard chord
  // routes by document.activeElement, so otherwise a Dock-activated or freshly-opened
  // window wouldn't receive ⌘W/⌘M. The `contains` guard avoids yanking focus from an
  // inner field the user just clicked (pointerdown already focuses those).
  useEffect(() => {
    if (win.minimized || wm.focusedId !== win.id) return;
    if (rootRef.current?.contains(document.activeElement)) return;
    rootRef.current?.focus({ preventScroll: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wm.focusedId, win.minimized]);

  // One pointer gesture (move or resize): attach window-level listeners on down,
  // tear them down on up. Capturing `win.bounds` at gesture start keeps the math
  // stable even as state updates re-render mid-drag.
  const startGesture = (e: React.PointerEvent, kind: 'move' | ResizeDir) => {
    if (win.maximized) return;
    e.preventDefault();
    e.stopPropagation();
    wm.focus(win.id);
    // Give the window real DOM focus so keyboard chords (⌘W/⌘M) target it — the
    // titlebar/handle stops propagation, so the root's own focus handler won't run.
    rootRef.current?.focus({ preventScroll: true });
    draggingRef.current = true;
    setDragging(true);
    const startX = e.clientX;
    const startY = e.clientY;
    const start = { ...win.bounds };
    const onMove = (ev: PointerEvent) => {
      const dx = ev.clientX - startX;
      const dy = ev.clientY - startY;
      if (kind === 'move') {
        wm.setBounds(win.id, clampToLayer({ ...start, x: start.x + dx, y: start.y + dy }, layerWidth, layerHeight));
      } else {
        // Clamp resize results too: dragging the N/W edge inward past the min size
        // moves the origin, which could otherwise push the titlebar off-screen.
        wm.setBounds(win.id, clampToLayer(resizeBounds(start, kind, dx, dy), layerWidth, layerHeight));
      }
    };
    const onUp = () => {
      draggingRef.current = false;
      setDragging(false);
      window.removeEventListener('pointermove', onMove);
      window.removeEventListener('pointerup', onUp);
    };
    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', onUp);
  };

  const Body = def.Component;
  const Icon = def.icon;
  const focused = wm.focusedId === win.id;

  const style: React.CSSProperties = win.maximized
    ? { left: 0, top: 0, width: layerWidth, height: layerHeight, zIndex: win.z }
    : { left: win.bounds.x, top: win.bounds.y, width: win.bounds.width, height: win.bounds.height, zIndex: win.z };
  // Minimize morph: shrink the window and fly it toward the Dock (bottom-left, above the sidebar's
  // Apps button) so it's visually clear where it went — a CSS approximation of macOS's genie (a
  // literal liquid warp needs WebGL). Restoring reverses the same transition. Exact landing doesn't
  // matter; anchoring near the bottom-left is enough to read as "went down to the Dock".
  if (win.minimized) {
    const left = win.maximized ? 0 : win.bounds.x;
    const top = win.maximized ? 0 : win.bounds.y;
    const targetX = 96;
    const targetY = Math.max(0, layerHeight - 44);
    style.transform = `translate(${targetX - left}px, ${targetY - top}px) scale(0.08)`;
    style.transformOrigin = 'top left';
  }

  const lights: { key: string; color: string; Glyph: LucideIcon; onClick: () => void; label: string }[] = [
    {
      key: 'close',
      color: '#ff5f57',
      Glyph: X,
      // A dirty editor body can veto the close (confirm) before the exit animation.
      onClick: () => {
        // Mark closing so a reload during the ~300ms exit animation doesn't re-persist (and
        // resurrect) this window — it's removed from manager state only at animationEnd.
        if (wm.confirmClose(win.id)) {
          wm.markClosing(win.id);
          setExitKind((k) => k ?? 'close');
        }
      },
      label: t('common.close'),
    },
    // Minimize is a mounted hide (no exit animation): the body keeps running and
    // `inert` on the root pulls focus out, so nothing is trapped in a hidden window.
    { key: 'min', color: '#febc2e', Glyph: Minus, onClick: () => wm.minimize(win.id), label: t('apps.window.minimize') },
    { key: 'max', color: '#28c840', Glyph: Plus, onClick: () => wm.toggleMaximize(win.id), label: t('apps.window.maximize') },
  ];

  return (
    <div
      ref={rootRef}
      role="dialog"
      aria-label={t(def.titleKey)}
      // Keyboard chords resolve their target window from the DOM-focused element via
      // this id, so they act on the window you're actually typing in (not just the top).
      data-window-id={win.id}
      // Per-app theme: the File Browser follows the global light/dark; the Editor and Terminal
      // lock to dark (VS Code-style) via their registry `lockTheme`. `data-theme` re-cascades that
      // token set to this window's subtree; an omitted (undefined) value inherits the global theme.
      data-theme={def.lockTheme}
      // Minimized windows stay mounted (to preserve their body state) but go fully
      // inert: hidden from assistive tech, out of the tab order, non-interactive —
      // and React/the browser moves focus out automatically.
      inert={win.minimized}
      tabIndex={-1}
      onPointerDown={(e) => {
        wm.focus(win.id);
        // Give the window DOM focus (so ⌘W/⌘M target it) — but don't steal focus from
        // an inner control/editor/terminal the click lands in (xterm's screen isn't a
        // textarea, so it needs an explicit exemption or terminal input would break).
        const tgt = e.target as HTMLElement;
        if (!tgt.closest('input,textarea,select,button,a,[contenteditable="true"],.monaco-editor,.xterm')) {
          rootRef.current?.focus({ preventScroll: true });
        }
      }}
      onAnimationEnd={(e) => {
        // Only the root's own close animation drives the unmount (ignore the
        // entrance, and any child animation bubbling up). Minimize doesn't animate
        // here — it's a CSS transition on the className, not a keyframe.
        if (e.target !== e.currentTarget || exitKind !== 'close') return;
        wm.close(win.id);
      }}
      className={clsx(
        // origin-center drives the open/close keyframe; the minimize morph overrides transform-origin
        // to top-left inline (so its translate+scale math lands the window at the Dock).
        'group/win absolute flex flex-col overflow-hidden border bg-surface-2 outline-none origin-center',
        // Geometry + the minimize/restore morph animate, except during a drag/resize (which must track
        // the pointer instantly). 300ms so the fly-to-Dock reads clearly.
        dragging
          ? 'transition-[transform,opacity] duration-200 ease-out'
          : 'transition-[left,top,width,height,transform,opacity] duration-300 ease-out',
        win.maximized ? 'rounded-none' : 'rounded-xl',
        exitKind === 'close' ? 'animate-appwindow-out' : 'animate-appwindow-in',
        // Minimize = mounted hide: the body stays alive (terminal/editor state preserved) while the
        // window shrinks toward the Dock (inline transform above) and stops taking pointer events.
        win.minimized ? 'pointer-events-none opacity-0' : 'pointer-events-auto',
        focused
          ? 'border-border-strong shadow-[0_28px_60px_-12px_rgba(0,0,0,0.7)]'
          : 'border-border shadow-[0_16px_40px_-16px_rgba(0,0,0,0.6)]',
      )}
      style={style}
    >
      {/* Titlebar: traffic lights (left) + centered title. Drag handle = the bar. */}
      <div
        onPointerDown={(e) => startGesture(e, 'move')}
        onDoubleClick={() => wm.toggleMaximize(win.id)}
        className="flex h-9 shrink-0 select-none items-center gap-3 border-b border-border px-3.5"
      >
        <div className="flex items-center gap-2">
          {lights.map((l) => (
            <button
              key={l.key}
              type="button"
              aria-label={l.label}
              title={l.label}
              onPointerDown={(e) => e.stopPropagation()}
              onClick={l.onClick}
              className="grid size-3 place-items-center rounded-full text-[9px] font-bold leading-none text-black/55 opacity-100"
              style={{ backgroundColor: l.color }}
            >
              <l.Glyph className="size-2 opacity-0 transition-opacity group-hover/win:opacity-100" strokeWidth={3.5} />
            </button>
          ))}
        </div>
        <div className="flex flex-1 items-center justify-center gap-1.5 overflow-hidden">
          <Icon className="size-3.5 shrink-0" style={{ color: `var(${def.accent})` }} />
          <span className="truncate text-[13px] font-semibold text-foreground">{win.title ?? t(def.titleKey)}</span>
        </div>
        {/* Right spacer balances the traffic lights so the title stays centered. */}
        <div className="w-[52px] shrink-0" />
      </div>

      <div className="min-h-0 flex-1 overflow-hidden">
        {/* A crashing app only takes down its own window — the shell + other windows stay usable, and
            "Retry" remounts just this app. */}
        <ErrorBoundary variant="inline">
          <Body windowId={win.id} params={win.params} />
        </ErrorBoundary>
      </div>

      {!win.maximized &&
        RESIZE_HANDLES.map((h) => (
          <div
            key={h.dir}
            onPointerDown={(e) => startGesture(e, h.dir)}
            // z-30 keeps the edge/corner grips ABOVE any app-body content (a full-bleed preview,
            // image, or overlay), so the window edges stay grabbable no matter what the app renders.
            className={clsx('absolute z-30', h.className)}
          />
        ))}
    </div>
  );
};
