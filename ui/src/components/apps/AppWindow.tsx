import { useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';

import { APP_REGISTRY } from '../../apps/registry';
import { useWindowManager, type WindowInstance } from '../../context/WindowManagerContext';
import { clampToLayer, resizeBounds, type ResizeDir } from '../../lib/windowBounds';

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
  // Play the scale-down exit before the window actually leaves (minimize → Dock,
  // or close); the animation's end drives the real action, so the CSS owns the
  // timing. The entrance animation covers open + restore via remount.
  const [exitKind, setExitKind] = useState<'min' | 'close' | null>(null);

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

  const lights: { key: string; color: string; glyph: string; onClick: () => void; label: string }[] = [
    { key: 'close', color: '#ff5f57', glyph: '×', onClick: () => setExitKind((k) => k ?? 'close'), label: t('common.close') },
    { key: 'min', color: '#febc2e', glyph: '–', onClick: () => setExitKind((k) => k ?? 'min'), label: t('apps.window.minimize') },
    { key: 'max', color: '#28c840', glyph: '+', onClick: () => wm.toggleMaximize(win.id), label: t('apps.window.maximize') },
  ];

  return (
    <div
      ref={rootRef}
      role="dialog"
      aria-label={t(def.titleKey)}
      tabIndex={-1}
      onPointerDown={(e) => {
        wm.focus(win.id);
        // Give the window DOM focus (so ⌘W/⌘M target it) — but don't steal focus
        // from an inner input/editor the click is actually landing in.
        const tgt = e.target as HTMLElement;
        if (!tgt.closest('input,textarea,select,button,a,[contenteditable="true"],.monaco-editor')) {
          rootRef.current?.focus({ preventScroll: true });
        }
      }}
      onAnimationEnd={(e) => {
        // Only the root's own exit animation drives the action (ignore the
        // entrance, and any child animation bubbling up).
        if (e.target !== e.currentTarget || !exitKind) return;
        if (exitKind === 'close') wm.close(win.id);
        else wm.minimize(win.id);
      }}
      className={clsx(
        'group/win pointer-events-auto absolute flex flex-col overflow-hidden rounded-xl border bg-surface-2 outline-none',
        win.maximized ? 'rounded-none' : 'rounded-xl',
        exitKind ? 'animate-appwindow-out' : 'animate-appwindow-in',
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
              <span className="opacity-0 transition-opacity group-hover/win:opacity-100">{l.glyph}</span>
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
        <Body windowId={win.id} params={win.params} />
      </div>

      {!win.maximized &&
        RESIZE_HANDLES.map((h) => (
          <div
            key={h.dir}
            onPointerDown={(e) => startGesture(e, h.dir)}
            className={clsx('absolute z-10', h.className)}
          />
        ))}
    </div>
  );
};
