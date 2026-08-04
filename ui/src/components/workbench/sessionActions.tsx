import * as React from 'react';
import { useRef } from 'react';
import { Ellipsis, Loader2 } from 'lucide-react';
import clsx from 'clsx';
import type { LucideIcon } from 'lucide-react';

import { Button } from '../ui/button';

// Presentation half of the shared session action menu: the ⋯ trigger and the menu
// body, used by the desktop sidebar row, the mobile projects row and the chat
// header. The actions themselves (labels, writes, pending state) come from
// useSessionActions.tsx — one model, so the surfaces cannot drift again.

export type SessionActionId = 'pin' | 'reference' | 'fork' | 'rename' | 'hide' | 'archive';

/** Menu grouping — rendered with a hairline divider between groups. */
export type SessionActionGroup = 'organize' | 'continue' | 'lifecycle';

export interface SessionActionDescriptor {
  id: SessionActionId;
  group: SessionActionGroup;
  icon: LucideIcon;
  label: string;
  /** Keyboard hint badge (e.g. ``⇧⌘D``), right-aligned in the menu row. */
  hint?: string;
  /** Native tooltip — used to explain a disabled action (fork without a native session). */
  title?: string;
  disabled?: boolean;
  /** A write is in flight: the icon becomes a spinner and the row stops accepting clicks. */
  pending?: boolean;
  danger?: boolean;
  onSelect: () => void;
}

// The ⋯ trigger. Rows reveal it on hover / keyboard focus and keep it visible on
// coarse pointers (touch has no hover) and while its menu is open; the chat
// header always shows it. forwardRef + prop spread so <PopoverTrigger asChild>
// can attach its own ref and handlers.
interface SessionActionsTriggerProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  label: string;
  open: boolean;
  /** ``row``: hover-revealed inside a session row. ``bar``: always visible. */
  variant?: 'row' | 'bar';
}

export const SessionActionsTrigger = React.forwardRef<HTMLButtonElement, SessionActionsTriggerProps>(
  ({ label, open, variant = 'row', className, ...rest }, ref) => (
    <Button
      ref={ref}
      type="button"
      variant="ghost"
      size="icon"
      aria-label={label}
      aria-haspopup="menu"
      aria-expanded={open}
      title={label}
      className={clsx(
        'shrink-0 text-muted transition hover:text-foreground',
        variant === 'row'
          ? [
              'size-6 opacity-0 group-hover/sess:opacity-100 group-focus-within/sess:opacity-100 pointer-coarse:opacity-100',
              open && 'opacity-100',
            ]
          : 'size-7',
        className,
      )}
      {...rest}
    >
      <Ellipsis className="size-3.5" />
    </Button>
  ),
);
SessionActionsTrigger.displayName = 'SessionActionsTrigger';

const GROUP_ORDER: SessionActionGroup[] = ['organize', 'continue', 'lifecycle'];

// The menu body. Shared by all trigger sites so the rows, the divider between
// groups and the danger styling can't drift again.
export const SessionActionMenu: React.FC<{
  actions: SessionActionDescriptor[];
  /** Close the surface's popover — called after any row fires. */
  onAction?: (id: SessionActionId) => void;
  label?: string;
}> = ({ actions, onAction, label }) => {
  const itemsRef = useRef<Array<HTMLButtonElement | null>>([]);

  // Roving arrow-key focus: PopoverContent is generic content, so the menu owns
  // its own keyboard model (Home/End included). Esc + focus trapping stay with
  // the popover primitive.
  const moveFocus = (from: number, delta: number) => {
    const enabled = actions
      .map((action, index) => ({ action, index }))
      .filter(({ action }) => !action.disabled);
    if (enabled.length === 0) return;
    const current = enabled.findIndex(({ index }) => index === from);
    const nextPos = current === -1 ? 0 : (current + delta + enabled.length) % enabled.length;
    itemsRef.current[enabled[nextPos].index]?.focus();
  };

  return (
    <div role="menu" aria-label={label} className="flex flex-col">
      {actions.map((action, index) => {
        const Icon = action.icon;
        const previous = actions[index - 1];
        const newGroup =
          previous != null && GROUP_ORDER.indexOf(previous.group) !== GROUP_ORDER.indexOf(action.group);
        return (
          <div key={action.id} className={clsx(newGroup && 'mt-1 border-t border-border pt-1')}>
            <button
              ref={(node) => {
                itemsRef.current[index] = node;
              }}
              type="button"
              role="menuitem"
              disabled={action.disabled}
              title={action.title}
              onClick={() => {
                if (action.disabled) return;
                onAction?.(action.id);
                action.onSelect();
              }}
              onKeyDown={(event) => {
                if (event.key === 'ArrowDown') {
                  event.preventDefault();
                  moveFocus(index, 1);
                } else if (event.key === 'ArrowUp') {
                  event.preventDefault();
                  moveFocus(index, -1);
                } else if (event.key === 'Home') {
                  event.preventDefault();
                  moveFocus(-1, 0);
                } else if (event.key === 'End') {
                  event.preventDefault();
                  moveFocus(-1, -1);
                }
              }}
              className={clsx(
                'flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-[12px] transition',
                'focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-cyan/60',
                'disabled:cursor-not-allowed disabled:text-muted disabled:hover:bg-transparent',
                action.danger
                  ? 'text-pink hover:bg-pink/[0.08]'
                  : 'text-foreground hover:bg-foreground/[0.04]',
              )}
            >
              {action.pending ? (
                <Loader2 className="size-3 shrink-0 animate-spin text-muted" aria-hidden="true" />
              ) : (
                <Icon
                  className={clsx('size-3 shrink-0', action.danger ? '' : 'text-muted')}
                  aria-hidden="true"
                />
              )}
              <span className="flex-1 truncate">{action.label}</span>
              {action.hint && (
                <span className="shrink-0 font-mono text-[10px] text-muted" aria-hidden="true">
                  {action.hint}
                </span>
              )}
            </button>
          </div>
        );
      })}
    </div>
  );
};
