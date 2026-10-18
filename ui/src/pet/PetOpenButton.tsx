import { useCallback, useEffect, useId, useRef, useState, type CSSProperties } from 'react';
import { createPortal } from 'react-dom';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';
import { ExternalLink } from 'lucide-react';

import { petBridge } from './petBridge';

const HOVER_DELAY_MS = 350;
const GAP_PX = 6;

/**
 * Where the tooltip sits relative to its button. `below-end` hangs under the
 * button, right-aligned with it; `start` sits to its left, vertically centred.
 */
export type PetTooltipPlacement = 'below-end' | 'start';

type Tip = { host: Element; style: CSSProperties };

/**
 * The pet's Open in Avibe icon button, with a pet-styled tooltip in place of
 * the native `title` (which waits for the system delay and draws in system
 * style). The tooltip is portalled to the pet stage and placed in viewport
 * coordinates, so no scroll box, clip or edge mask around the button can cut it.
 *
 * Hover only reaches the page while the pet window receives hover; on macOS
 * that is once the window is key (spec §5.1), which opening the history or the
 * card list makes it.
 */
export function PetOpenButton({ link, placement, className }: { link: string; placement: PetTooltipPlacement; className?: string }) {
  const { t } = useTranslation();
  const label = t('pet.openInAvibe');
  const tooltipId = useId();
  const buttonRef = useRef<HTMLButtonElement>(null);
  const timer = useRef<number | null>(null);
  const [tip, setTip] = useState<Tip | null>(null);

  const cancel = useCallback(() => {
    if (timer.current !== null) window.clearTimeout(timer.current);
    timer.current = null;
  }, []);
  const hide = useCallback(() => {
    cancel();
    setTip(null);
  }, [cancel]);
  const show = useCallback(() => {
    cancel();
    const button = buttonRef.current;
    if (!button) return;
    const rect = button.getBoundingClientRect();
    const right = document.documentElement.clientWidth - (placement === 'start' ? rect.left - GAP_PX : rect.right);
    const style: CSSProperties = placement === 'start'
      ? { top: rect.top + rect.height / 2, right, transform: 'translateY(-50%)' }
      : { top: rect.bottom + GAP_PX, right };
    setTip({ host: button.closest('.pet-mini-stage') ?? document.body, style });
  }, [cancel, placement]);

  useEffect(() => cancel, [cancel]);
  useEffect(() => {
    if (!tip) return undefined;
    // The tooltip is placed once, so anything that could move the button away
    // from it dismisses it instead.
    // Escape closes the innermost layer first: the tooltip takes this one, so
    // the pet's own Escape (which collapses the history or the panel) does not
    // also run. Captured on window, ahead of that bubbling listener.
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' || event.isComposing) return;
      event.stopImmediatePropagation();
      hide();
    };
    window.addEventListener('keydown', onKey, true);
    window.addEventListener('scroll', hide, true);
    window.addEventListener('resize', hide);
    return () => {
      window.removeEventListener('keydown', onKey, true);
      window.removeEventListener('scroll', hide, true);
      window.removeEventListener('resize', hide);
    };
  }, [tip, hide]);

  return (
    <>
      <button
        ref={buttonRef}
        type="button"
        className={clsx('pet-mini-open', className)}
        aria-label={label}
        aria-describedby={tip ? tooltipId : undefined}
        onClick={() => void petBridge.open(link)}
        onPointerEnter={(event) => {
          if (event.pointerType === 'touch') return;
          cancel();
          timer.current = window.setTimeout(show, HOVER_DELAY_MS);
        }}
        onPointerLeave={hide}
        onPointerDown={hide}
        onFocus={(event) => { if (event.currentTarget.matches(':focus-visible')) show(); }}
        onBlur={hide}
      >
        <ExternalLink className="size-3.5" />
      </button>
      {tip && createPortal(
        <span id={tooltipId} role="tooltip" className={clsx('pet-mini-tooltip', `is-${placement}`)} style={tip.style}>{label}</span>,
        tip.host,
      )}
    </>
  );
}
