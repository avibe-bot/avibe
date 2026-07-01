import { useEffect } from 'react';
import clsx from 'clsx';

// A cursor-positioned right-click menu, shared by the editor's file tree and the File Browser app.
// Rendered inline (not portaled) so it inherits the surrounding window's theme scope, exactly like
// the original FileTree menu it was extracted from. A full-viewport backdrop closes it on any click
// (or right-click), and Escape closes it too.

export const ContextMenuItem: React.FC<{
  icon?: React.ReactNode;
  label: string;
  danger?: boolean;
  onClick: () => void;
}> = ({ icon, label, danger, onClick }) => (
  <button
    type="button"
    role="menuitem"
    onClick={onClick}
    className={clsx(
      'flex w-full items-center gap-2.5 rounded-md px-2.5 py-1.5 text-left text-[12.5px] transition',
      danger ? 'text-destructive hover:bg-destructive/[0.1]' : 'text-foreground hover:bg-cyan-soft',
    )}
  >
    {icon !== undefined && <span className="grid size-4 shrink-0 place-items-center">{icon}</span>}
    {label}
  </button>
);

export const ContextMenu: React.FC<{
  x: number;
  y: number;
  onClose: () => void;
  children: React.ReactNode;
  /** Menu width in px (default 196), used for horizontal viewport clamping. */
  width?: number;
  /** Number of items, used to estimate height for vertical viewport clamping. */
  itemCount?: number;
}> = ({ x, y, onClose, children, width = 196, itemCount = 4 }) => {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  // Clamp inside the viewport: a cursor near the right/bottom edge would otherwise overflow. The
  // height is an estimate (item count × row height + padding) — good enough to nudge it back in.
  const height = itemCount * 34 + 12;
  const left = Math.max(8, Math.min(x, window.innerWidth - width - 8));
  const top = Math.max(8, Math.min(y, window.innerHeight - height - 8));

  return (
    <>
      <div
        className="fixed inset-0 z-40"
        onClick={onClose}
        onContextMenu={(e) => {
          e.preventDefault();
          onClose();
        }}
        aria-hidden
      />
      <div
        role="menu"
        style={{ left, top, width }}
        className="fixed z-50 rounded-lg border border-border bg-surface-3 p-1 shadow-[0_12px_30px_-8px_rgba(0,0,0,0.7)]"
      >
        {children}
      </div>
    </>
  );
};
