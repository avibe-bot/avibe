import { useRef, type KeyboardEvent, type MouseEvent, type PointerEvent } from 'react';
import { useHref } from 'react-router-dom';

import { openLinkInNewContext } from '../../lib/pwaNavigation';

// A card that opens what it shows when it is tapped or clicked (an Inbox conversation card, a row of the
// sidebar's Inbox peek). The card stays an ordinary region, so its text can be selected and its own
// controls stay real buttons; its title is the real link (focus, Enter, a screen reader's name for it,
// middle- and Cmd-click on desktop), and a press anywhere else on the card follows that link. The rule
// for what a press on the card does:
// - On one of the card's own controls (a button, a link, a field): that control's action, never the
//   card's. 「Mark read」 marks read and stays on the Inbox.
// - On anything else (the preview, whose links render as plain text; the project chip; a Bot avatar;
//   the time; empty space): opens the conversation.
// - Not when the press was a drag: a pointer that moved, or a drag that selected text, selects; only a
//   clean tap or click opens. A long press that selects text on a touch screen fires no click at all.
// - With Cmd, Ctrl or Shift, or the middle button, it opens in a new tab, as the link would (in the
//   installed app, wherever the app's own link policy sends a new-tab link: `openLinkInNewContext`).

/** Elements that do their own thing when pressed inside a card. */
const CONTROL = 'a[href], button, input, textarea, select, label, summary, [role="button"], [role="link"], [contenteditable="true"]';
/** How far a pointer may move between down and up and still count as a tap, in CSS px. */
const TAP_SLOP = 6;

const opensElsewhere = (event: MouseEvent) => event.metaKey || event.ctrlKey || event.shiftKey;

/** The route a conversation card opens. */
export const chatHref = (sessionId: string) => `/chat/${encodeURIComponent(sessionId)}`;

/** `to` is the route (`chatHref`); `open` is what a plain press does (the page's own navigation). */
export function useCardOpen(to: string, open: () => void) {
  // As the router writes it into the link (its basename, or a hash router's `#`), for a new tab.
  const href = useHref(to);
  const down = useRef<{ x: number; y: number } | null>(null);

  /** Whether this press belongs to the card, rather than to a control or a selection inside it. */
  const pressesCard = (event: MouseEvent<HTMLElement>) => {
    const control = (event.target as Element).closest(CONTROL);
    if (control && event.currentTarget.contains(control)) return false;
    const start = down.current;
    if (start && Math.hypot(event.clientX - start.x, event.clientY - start.y) > TAP_SLOP) return false;
    const selection = window.getSelection();
    if (selection && !selection.isCollapsed && selection.toString().trim() && event.currentTarget.contains(selection.anchorNode)) {
      return false;
    }
    return true;
  };

  return {
    /** Spread on the card. */
    card: {
      onPointerDown: (event: PointerEvent<HTMLElement>) => {
        down.current = { x: event.clientX, y: event.clientY };
      },
      onClick: (event: MouseEvent<HTMLElement>) => {
        if (event.defaultPrevented || !pressesCard(event)) return;
        if (opensElsewhere(event)) {
          openLinkInNewContext(href, 'noopener');
          return;
        }
        open();
      },
      onAuxClick: (event: MouseEvent<HTMLElement>) => {
        if (event.button !== 1 || event.defaultPrevented || !pressesCard(event)) return;
        event.preventDefault();
        openLinkInNewContext(href, 'noopener');
      },
    },
    /** Spread on the card's title link (`<Link to={href}>`): a plain press goes through `open`, so the
     *  page keeps what it does before leaving; a modified press is the browser's own. Space opens it too. */
    link: {
      onClick: (event: MouseEvent<HTMLAnchorElement>) => {
        if (event.button !== 0 || opensElsewhere(event)) return;
        event.preventDefault();
        open();
      },
      onKeyDown: (event: KeyboardEvent<HTMLAnchorElement>) => {
        if (event.key !== ' ') return;
        event.preventDefault();
        open();
      },
    },
  };
}
