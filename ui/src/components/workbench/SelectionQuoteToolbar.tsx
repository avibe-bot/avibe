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
// the session is forkable); "Copy" replaces the native callout that the
// transcript suppresses on touch devices.
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
  // Touch devices get a Copy action because the transcript suppresses the native
  // selection callout there (a coarse pointer covers phones AND tablets/iPads,
  // unlike a width breakpoint).
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
    // A scrolled transcript makes the cached rect stale — hide immediately.
    const onScroll = () => {
      window.clearTimeout(timer);
      setSel(null);
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
  // (the label widths vary by locale + how many actions are shown).
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

  const above = sel.top > TOOLBAR_H + GAP + EDGE;
  const top = above ? sel.top - TOOLBAR_H - GAP : sel.bottom + GAP;
  const half = width / 2;
  const left = Math.min(Math.max(sel.left, EDGE + half), window.innerWidth - EDGE - half);

  const itemClass = 'h-9 gap-1.5 rounded-none px-3 text-[13px] font-medium';

  return createPortal(
    <div
      ref={toolbarRef}
      role="toolbar"
      // Keep the selection alive when the toolbar is pressed (desktop). On touch
      // the handlers use the captured `sel.text`, so a collapse is harmless too.
      onMouseDown={(e) => e.preventDefault()}
      style={{ position: 'fixed', top, left, transform: 'translateX(-50%)', zIndex: 60 }}
      className="flex items-center overflow-hidden rounded-lg border border-border-strong bg-surface-2 shadow-[0_12px_30px_-8px_rgba(0,0,0,0.7)]"
    >
      <Button variant="ghost" className={itemClass} onClick={runQuote}>
        <TextQuote className="size-3.5 text-muted" />
        {t('chat.selection.quote')}
      </Button>
      {onAskInNew && (
        <>
          <span className="h-5 w-px bg-border" />
          <Button variant="ghost" className={itemClass} onClick={runAsk}>
            <GitFork className="size-3.5 text-muted" />
            {t('chat.selection.askInNew')}
          </Button>
        </>
      )}
      {isTouch && (
        <>
          <span className="h-5 w-px bg-border" />
          <Button variant="ghost" className={itemClass} onClick={runCopy}>
            {copied ? <Check className="size-3.5 text-mint" /> : <Copy className="size-3.5 text-muted" />}
            {t('chat.selection.copy')}
          </Button>
        </>
      )}
    </div>,
    document.body,
  );
};
