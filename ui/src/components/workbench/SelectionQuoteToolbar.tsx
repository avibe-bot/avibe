import React, { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useTranslation } from 'react-i18next';
import { Check, Copy, GitFork, TextQuote } from 'lucide-react';

import { Button } from '../ui/button';
import { copyTextToClipboard } from '../../lib/utils';

type SelectionRect = {
  top: number;
  bottom: number;
  left: number;
  right: number;
  width: number;
  height: number;
};

type SelectionState = {
  text: string;
  top: number;
  bottom: number;
  left: number;
  first: SelectionRect;
  last: SelectionRect;
};

const TOOLBAR_H = 36;
const GAP = 8;
const EDGE = 8;
// iOS keeps touch sequences that start within this padding around a selection
// handle in its native selection gesture recognizer. Keep the web toolbar
// outside that region so pointerup/touchend remains deliverable to the button.
const SELECTION_HANDLE_PADDING = 44;
const SELECTION_HANDLE_GAP = SELECTION_HANDLE_PADDING + GAP;

const toSelectionRect = (rect: DOMRect | DOMRectReadOnly): SelectionRect => ({
  top: rect.top,
  bottom: rect.bottom,
  left: rect.left,
  right: rect.right,
  width: rect.width,
  height: rect.height,
});

// A floating toolbar that appears over a text selection inside the chat
// transcript. "Quote" appends the (quoted) selection to the current composer
// (only offered when the composer can accept it); "Ask in a new session" forks +
// prefills the fork's draft (only offered when the session is forkable); "Copy"
// (touch only) is a fallback for when the OS selection menu doesn't cooperate. It
// follows the selection through scrolling (hides while scrolling, re-shows at the
// new spot) and only disappears when the selection is cleared — so the user can
// scroll to dodge the OS menu.
export const SelectionQuoteToolbar: React.FC<{
  containerRef: React.RefObject<HTMLDivElement | null>;
  // Both write actions are optional and hidden rather than offered just to fail.
  // ``onQuote`` is omitted for a read-only (archived) transcript, whose composer
  // is disabled — inserting there would only leave an unsendable draft.
  onQuote?: (text: string) => void;
  // Omitted when the session can't be forked (no native id yet, or archived —
  // archive is terminal, so the fork endpoint refuses it).
  onAskInNew?: (text: string) => void;
}> = ({ containerRef, onQuote, onAskInNew }) => {
  const { t } = useTranslation();
  const [sel, setSel] = useState<SelectionState | null>(null);
  const [copied, setCopied] = useState(false);
  const toolbarRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0);
  const [height, setHeight] = useState(TOOLBAR_H);
  // Touch (coarse pointer — phones AND tablets/iPads) is where the OS selection
  // menu coexists; it drives the stagger-positioning + the touch-only Copy.
  const [isTouch] = useState(
    () => typeof window !== 'undefined' && !!window.matchMedia?.('(pointer: coarse)').matches,
  );

  const recompute = useCallback(() => {
    const container = containerRef.current;
    if (!container) return;
    const selection = window.getSelection();
    if (!selection || selection.isCollapsed || selection.rangeCount === 0) {
      setSel(null);
      return;
    }
    const text = selection.toString().trim();
    const range = selection.getRangeAt(0);
    if (!text || !container.contains(range.commonAncestorContainer)) {
      setSel(null);
      return;
    }
    const rect = toSelectionRect(range.getBoundingClientRect());
    const lineRects = Array.from(range.getClientRects())
      .filter((lineRect) => lineRect.width || lineRect.height)
      .map(toSelectionRect);
    if (!rect.width && !rect.height && lineRects.length === 0) {
      setSel(null);
      return;
    }
    setSel({
      text,
      top: rect.top,
      bottom: rect.bottom,
      left: rect.left + rect.width / 2,
      first: lineRects[0] ?? rect,
      last: lineRects[lineRects.length - 1] ?? rect,
    });
  }, [containerRef]);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    let timer = 0;
    // Debounce so the toolbar appears when the selection settles, not on every
    // intermediate range while dragging the selection / handles.
    const onSelectionChange = () => {
      window.clearTimeout(timer);
      timer = window.setTimeout(recompute, 150);
    };
    // Follow the selection through scrolling: hide while scrolling, then re-show
    // at the new position once it settles. The toolbar persists as long as the
    // selection exists, so the user can scroll to move it clear of the OS menu.
    const onScroll = () => {
      setSel(null);
      window.clearTimeout(timer);
      timer = window.setTimeout(recompute, 150);
    };
    document.addEventListener('selectionchange', onSelectionChange);
    container.addEventListener('scroll', onScroll, { passive: true });
    return () => {
      window.clearTimeout(timer);
      document.removeEventListener('selectionchange', onSelectionChange);
      container.removeEventListener('scroll', onScroll);
    };
  }, [containerRef, recompute]);

  // Measure the rendered toolbar so we can clamp it on-screen by its real width
  // and height (label widths vary by locale + which actions are shown).
  useLayoutEffect(() => {
    if (sel && toolbarRef.current) {
      setWidth(toolbarRef.current.offsetWidth);
      setHeight(toolbarRef.current.offsetHeight || TOOLBAR_H);
    }
  }, [sel, onQuote, onAskInNew, isTouch]);

  if (!sel) return null;
  // Nothing left to offer (read-only transcript on a pointer device, where the
  // OS already provides copy) — render no chrome rather than an empty bar.
  if (!onQuote && !onAskInNew && !isTouch) return null;

  const dismiss = () => {
    window.getSelection()?.removeAllRanges();
    setSel(null);
    setCopied(false);
  };
  const runQuote = () => {
    onQuote?.(sel.text);
    dismiss();
  };
  const runAsk = () => {
    onAskInNew?.(sel.text);
    dismiss();
  };
  const runCopy = () => {
    const text = sel.text;
    void copyTextToClipboard(text).then((ok) => {
      if (ok) {
        setCopied(true);
        window.setTimeout(dismiss, 800);
      }
    });
  };

  // Activate on pointerup (mouse + touch) and Enter/Space (keyboard). The
  // pointerdown preventDefault keeps the text selection alive. We deliberately
  // do not retain pointer ownership here: iOS can deliver pointerdown for a
  // touch near a selection handle without ever delivering pointerup, so any
  // state that waits for an end event can permanently freeze this toolbar.
  const activate = (run: () => void) => ({
    onPointerDown: (e: React.PointerEvent) => e.preventDefault(),
    onPointerUp: () => run(),
    onKeyDown: (e: React.KeyboardEvent) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        run();
      }
    },
  });

  const toolbarHeight = height || TOOLBAR_H;
  const roomAbove = sel.first.top >= toolbarHeight + SELECTION_HANDLE_GAP + EDGE;
  const roomBelow = window.innerHeight - sel.last.bottom >= toolbarHeight + SELECTION_HANDLE_GAP + EDGE;
  const preferBelow = (sel.top + sel.bottom) / 2 >= window.innerHeight / 2;
  let below: boolean;
  if (!isTouch) {
    // Desktop has no OS selection menu — just prefer above, flip if no room.
    below = !roomAbove;
  } else if (preferBelow && roomBelow) {
    // Keep the web toolbar toward the nearer viewport edge, away from the
    // native selection menu around the screen center.
    below = true;
  } else if (!preferBelow && roomAbove) {
    below = false;
  } else if (roomBelow) {
    below = true;
  } else if (roomAbove) {
    below = false;
  } else {
    // A very tall selection can leave neither side with a full safe slot.
    // Dock to whichever viewport edge is farther from its corresponding
    // selection handle; some visible action is better than hiding the bar.
    const topEdge = EDGE;
    const bottomEdge = Math.max(EDGE, window.innerHeight - toolbarHeight - EDGE);
    const topDistance = sel.first.top - SELECTION_HANDLE_PADDING - (topEdge + toolbarHeight);
    const bottomDistance = bottomEdge - (sel.last.bottom + SELECTION_HANDLE_PADDING);
    below = bottomDistance >= topDistance;
  }
  const topEdge = EDGE;
  const bottomEdge = Math.max(EDGE, window.innerHeight - toolbarHeight - EDGE);
  const placementGap = isTouch ? SELECTION_HANDLE_GAP : GAP;
  const rawTop = below
    ? sel.last.bottom + placementGap
    : sel.first.top - toolbarHeight - placementGap;
  // Keep the toolbar on-screen vertically too, so it stays visible (and the
  // user can still act) when the selection is scrolled near a viewport edge.
  const top = !isTouch
    ? Math.min(Math.max(rawTop, topEdge), bottomEdge)
    : roomAbove || roomBelow
      ? rawTop
      : below
        ? bottomEdge
        : topEdge;
  // Clamp by the on-screen (capped) width so a toolbar wider than the viewport
  // centers + scrolls internally instead of pushing an edge off-screen.
  const half = Math.min(width, window.innerWidth - 2 * EDGE) / 2;
  const left = Math.min(Math.max(sel.left, EDGE + half), window.innerWidth - EDGE - half);

  const itemClass = 'h-9 gap-1.5 rounded-none px-3 text-[13px] font-medium';

  return createPortal(
    <div
      ref={toolbarRef}
      role="toolbar"
      style={{ position: 'fixed', top, left, maxWidth: 'calc(100vw - 16px)', transform: 'translateX(-50%)', zIndex: 60 }}
      className="flex items-center overflow-x-auto rounded-lg border border-border-strong bg-surface-2 shadow-[0_12px_30px_-8px_rgba(0,0,0,0.7)]"
    >
      {/* Separators sit BEFORE each item after the first, so a hidden action
          never leaves a dangling divider at the edge of the bar. */}
      {onQuote && (
        <Button variant="ghost" className={itemClass} {...activate(runQuote)}>
          <TextQuote className="size-3.5 text-muted" />
          {t('chat.selection.quote')}
        </Button>
      )}
      {onAskInNew && (
        <>
          {onQuote && <span className="h-5 w-px bg-border" />}
          <Button variant="ghost" className={itemClass} {...activate(runAsk)}>
            <GitFork className="size-3.5 text-muted" />
            {t('chat.selection.askInNew')}
          </Button>
        </>
      )}
      {isTouch && (
        <>
          {(onQuote || onAskInNew) && <span className="h-5 w-px bg-border" />}
          <Button variant="ghost" className={itemClass} {...activate(runCopy)}>
            {copied ? <Check className="size-3.5 text-mint-ink" /> : <Copy className="size-3.5 text-muted" />}
            {t('chat.selection.copy')}
          </Button>
        </>
      )}
    </div>,
    document.body,
  );
};
