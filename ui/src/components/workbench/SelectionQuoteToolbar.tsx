import React, { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useTranslation } from 'react-i18next';
import { Check, Copy, GitFork, TextQuote } from 'lucide-react';

import { Button } from '../ui/button';
import { copyTextToClipboard } from '../../lib/utils';

type SelectionState = { text: string; top: number; bottom: number; left: number };

const TOOLBAR_H = 36;
const GAP = 8;
const EDGE = 8;

// A floating toolbar that appears over a text selection inside the chat
// transcript. "Quote" appends the (quoted) selection to the current composer;
// "Ask in a new session" forks + prefills the fork's draft (only offered when
// the session is forkable); "Copy" (touch only) is a fallback for when the OS
// selection menu doesn't cooperate. It follows the selection through scrolling
// (hides while scrolling, re-shows at the new spot) and only disappears when
// the selection is cleared — so the user can scroll to dodge the OS menu.
export const SelectionQuoteToolbar: React.FC<{
  containerRef: React.RefObject<HTMLDivElement | null>;
  onQuote: (text: string) => void;
  // Omitted when the session can't be forked yet (no native id) — the action is
  // hidden rather than offered just to 409.
  onAskInNew?: (text: string) => void;
}> = ({ containerRef, onQuote, onAskInNew }) => {
  const { t } = useTranslation();
  const [sel, setSel] = useState<SelectionState | null>(null);
  const [copied, setCopied] = useState(false);
  const toolbarRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0);
  // Touch (coarse pointer — phones AND tablets/iPads) is where the OS selection
  // menu coexists; it drives the stagger-positioning + the touch-only Copy.
  const [isTouch] = useState(
    () => typeof window !== 'undefined' && !!window.matchMedia?.('(pointer: coarse)').matches,
  );

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    let timer = 0;
    const recompute = () => {
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
      const rect = range.getBoundingClientRect();
      if (!rect.width && !rect.height) {
        setSel(null);
        return;
      }
      setSel({ text, top: rect.top, bottom: rect.bottom, left: rect.left + rect.width / 2 });
    };
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
  }, [containerRef]);

  // Measure the rendered toolbar so we can clamp it on-screen by its real width
  // (label widths vary by locale + which actions are shown).
  useLayoutEffect(() => {
    if (sel && toolbarRef.current) setWidth(toolbarRef.current.offsetWidth);
  }, [sel, onAskInNew, isTouch]);

  if (!sel) return null;

  const dismiss = () => {
    window.getSelection()?.removeAllRanges();
    setSel(null);
    setCopied(false);
  };
  const runQuote = () => {
    onQuote(sel.text);
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
  // pointerdown preventDefault keeps the text selection alive (and on touch
  // cancels the synthetic click we don't use), so onClick is intentionally
  // avoided — it wouldn't fire on touch yet would double-fire on mouse.
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

  const roomAbove = sel.top > TOOLBAR_H + GAP + EDGE;
  const roomBelow = window.innerHeight - sel.bottom > TOOLBAR_H + GAP + EDGE;
  let below: boolean;
  if (isTouch) {
    // The OS selection menu can't be queried or suppressed, but it sits toward
    // screen center: BELOW a selection in the top half, ABOVE one in the bottom
    // half. Stagger ours toward the nearer edge (the opposite side) so the two
    // don't overlap. Fall back if that edge is clipped.
    below = (sel.top + sel.bottom) / 2 >= window.innerHeight / 2;
    if (below && !roomBelow) below = false;
    else if (!below && !roomAbove) below = true;
  } else {
    // Desktop has no OS selection menu — just prefer above, flip if no room.
    below = !roomAbove;
  }
  const rawTop = below ? sel.bottom + GAP : sel.top - TOOLBAR_H - GAP;
  // Keep the toolbar on-screen vertically too, so it stays visible (and the
  // user can still act) when the selection is scrolled near a viewport edge.
  const top = Math.min(Math.max(rawTop, EDGE), window.innerHeight - TOOLBAR_H - EDGE);
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
      <Button variant="ghost" className={itemClass} {...activate(runQuote)}>
        <TextQuote className="size-3.5 text-muted" />
        {t('chat.selection.quote')}
      </Button>
      {onAskInNew && (
        <>
          <span className="h-5 w-px bg-border" />
          <Button variant="ghost" className={itemClass} {...activate(runAsk)}>
            <GitFork className="size-3.5 text-muted" />
            {t('chat.selection.askInNew')}
          </Button>
        </>
      )}
      {isTouch && (
        <>
          <span className="h-5 w-px bg-border" />
          <Button variant="ghost" className={itemClass} {...activate(runCopy)}>
            {copied ? <Check className="size-3.5 text-mint" /> : <Copy className="size-3.5 text-muted" />}
            {t('chat.selection.copy')}
          </Button>
        </>
      )}
    </div>,
    document.body,
  );
};
