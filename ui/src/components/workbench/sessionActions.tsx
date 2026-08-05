import * as React from 'react';
import { useRef, useState } from 'react';
import { Ellipsis, Loader2 } from 'lucide-react';
import clsx from 'clsx';
import type { LucideIcon } from 'lucide-react';

import { Button } from '../ui/button';
import { Popover, PopoverContent, PopoverTrigger } from '../ui/popover';
import { mobileChatSessionActions } from './chatSessionActions';

// Presentation half of the shared session action menu: the ⋯ trigger and the menu
// body, used by the desktop sidebar row, the mobile projects row and the chat
// header. The actions themselves (labels, writes, pending state) come from
// useSessionActions.tsx; surfaces opt into only the capabilities they can render.

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
  /** Why this action is unavailable. Rendered as VISIBLE text under the label (a
   *  touch user never sees a ``title`` tooltip) as well as the native tooltip. */
  title?: string;
  disabled?: boolean;
  /** A write is in flight: the icon becomes a spinner and the row stops accepting clicks. */
  pending?: boolean;
  danger?: boolean;
  onSelect: () => void;
}

// The ⋯ trigger. Rows reveal it on hover / keyboard focus and keep it visible on
// coarse pointers (touch has no hover) and while its menu is open; a bar trigger
// stays visible whenever its responsive wrapper is mounted. forwardRef + prop
// spread lets <PopoverTrigger asChild> attach its own ref and handlers.
//
// It deliberately writes NO aria-haspopup / aria-expanded of its own: <PopoverTrigger
// asChild> injects both (as `dialog`, which is what a Popover actually opens), and
// because those injected props are spread last they would override ours anyway —
// a hand-written `aria-haspopup="menu"` was a lie that only the direct-render test
// could see (Codex). `open` still drives the reveal styling.
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
//
// A labelled GROUP of buttons, not role="menu": the surrounding Radix Popover is a
// dialog, and a menu role inside it promises a keyboard contract (single tab stop,
// type-ahead, aria-activedescendant) that a popover does not implement — the two
// together announced a menu whose items tabbed like dialog buttons (Codex). Tabbing
// through the buttons inside the popover's focus scope is now the honest model;
// arrow keys / Home / End are kept as a convenience on top.
//
// Unavailable rows use `aria-disabled` instead of `disabled` so they stay focusable
// and their reason is reachable by keyboard and screen reader; the click handler is
// what actually inhibits them.
export const SessionActionMenu: React.FC<{
  actions: SessionActionDescriptor[];
  /** Close the surface's popover — called after any row fires. */
  onAction?: (id: SessionActionId) => void;
  label?: string;
}> = ({ actions, onAction, label }) => {
  const itemsRef = useRef<Array<HTMLButtonElement | null>>([]);

  const focusItem = (index: number) => itemsRef.current[index]?.focus();
  const moveFocus = (from: number, delta: number) => {
    if (actions.length === 0) return;
    focusItem((from + delta + actions.length) % actions.length);
  };

  return (
    <div role="group" aria-label={label} className="flex flex-col">
      {actions.map((action, index) => {
        const Icon = action.icon;
        const previous = actions[index - 1];
        const newGroup =
          previous != null && GROUP_ORDER.indexOf(previous.group) !== GROUP_ORDER.indexOf(action.group);
        const reason = action.disabled ? action.title : undefined;
        return (
          <div key={action.id} className={clsx(newGroup && 'mt-1 border-t border-border pt-1')}>
            <button
              ref={(node) => {
                itemsRef.current[index] = node;
              }}
              type="button"
              aria-disabled={action.disabled || undefined}
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
                  focusItem(0);
                } else if (event.key === 'End') {
                  event.preventDefault();
                  focusItem(actions.length - 1);
                }
              }}
              className={clsx(
                'flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-[12px] transition',
                'focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-cyan/60',
                action.disabled
                  ? 'cursor-not-allowed text-muted hover:bg-transparent'
                  : action.danger
                    ? 'text-pink hover:bg-pink/[0.08]'
                    : 'text-foreground hover:bg-foreground/[0.04]',
              )}
            >
              {action.pending ? (
                <Loader2 className="size-3 shrink-0 animate-spin text-muted" aria-hidden="true" />
              ) : (
                <Icon
                  className={clsx('size-3 shrink-0', action.danger && !action.disabled ? '' : 'text-muted')}
                  aria-hidden="true"
                />
              )}
              <span className="flex flex-1 flex-col gap-0.5 overflow-hidden">
                <span className="truncate">{action.label}</span>
                {reason && <span className="text-[10.5px] leading-tight text-muted">{reason}</span>}
              </span>
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

// Actions that hand focus to an editor elsewhere (an inline rename <Input>, the
// chat header title, another chat's composer). Radix restores focus to the trigger
// on close — asynchronously, so it lands AFTER the editor focused itself and the
// caret (and, on touch, the on-screen keyboard) is lost (Codex).
const FOCUS_TRANSFER_ACTIONS: readonly SessionActionId[] = ['rename', 'reference'];

// One popover body for every surface: same width, padding, close-on-select and
// close-autofocus policy, so the three trigger sites can't drift on any of them.
export const SessionActionMenuContent: React.FC<{
  actions: SessionActionDescriptor[];
  label: string;
  align?: 'start' | 'center' | 'end';
  className?: string;
  /** Close the surface's popover (it owns the open state). */
  onClose: () => void;
}> = ({ actions, label, align = 'start', className, onClose }) => {
  const transferredFocus = useRef(false);

  return (
    <PopoverContent
      align={align}
      className={clsx('w-[196px] p-1', className)}
      onCloseAutoFocus={(event) => {
        if (!transferredFocus.current) return;
        transferredFocus.current = false;
        event.preventDefault(); // leave focus where the action put it
      }}
    >
      <SessionActionMenu
        actions={actions}
        label={label}
        onAction={(id) => {
          transferredFocus.current = FOCUS_TRANSFER_ACTIONS.includes(id);
          onClose();
        }}
      />
    </PopoverContent>
  );
};

// Chat keeps this compact action entry point on mobile only. Owning the responsive
// wrapper here makes the breakpoint part of the tested component contract instead
// of an incidental class buried in the large ChatPage render tree. Rename is also
// removed at this presentation boundary so future callers cannot reintroduce it.
export const MobileChatSessionActionMenu: React.FC<{
  actions: SessionActionDescriptor[];
  label: string;
}> = ({ actions, label }) => {
  const [open, setOpen] = useState(false);
  const visibleActions = mobileChatSessionActions(actions);
  if (visibleActions.length === 0) return null;

  return (
    <div className="md:hidden">
      <Popover open={open} onOpenChange={setOpen}>
        <PopoverTrigger asChild>
          <SessionActionsTrigger label={label} open={open} variant="bar" />
        </PopoverTrigger>
        <SessionActionMenuContent
          actions={visibleActions}
          label={label}
          align="end"
          onClose={() => setOpen(false)}
        />
      </Popover>
    </div>
  );
};
