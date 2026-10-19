import { useCallback, useEffect, useId, useRef, useState, type CSSProperties, type FocusEvent, type PointerEvent } from 'react';
import { createPortal } from 'react-dom';
import clsx from 'clsx';

const HOVER_DELAY_MS = 350;
const GAP_PX = 6;

/**
 * Where the tooltip sits relative to its trigger. `below-end` hangs under it,
 * right-aligned with it; `start` sits to its left, vertically centred.
 */
export type PetTooltipPlacement = 'below-end' | 'start';

type Tip = { host: Element; style: CSSProperties };

/**
 * A pet-styled tooltip for an icon button, in place of the native `title`
 * (which waits for the system delay and draws in system style). It shows after
 * a short hover, at once on keyboard focus, and goes on leave, blur, press or
 * Escape. It is portalled to the pet stage and placed in viewport coordinates,
 * so no scroll box, clip or edge mask around the trigger can cut it.
 *
 * Spread `triggerProps` and attach `ref` on the button, render `tooltip` next
 * to it, and give the button `title=""` so an ancestor's `title` is not
 * inherited as a second, native tooltip.
 *
 * Hover only reaches the page while the pet window receives hover; on macOS
 * that is once the window is key (spec §5.1).
 */
export function usePetTooltip(label: string, placement: PetTooltipPlacement) {
  const tooltipId = useId();
  const ref = useRef<HTMLButtonElement>(null);
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
    const button = ref.current;
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
    // The tooltip is placed once, so anything that could move the trigger away
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

  const triggerProps = {
    'aria-describedby': tip ? tooltipId : undefined,
    onPointerEnter: (event: PointerEvent<HTMLButtonElement>) => {
      if (event.pointerType === 'touch') return;
      cancel();
      timer.current = window.setTimeout(show, HOVER_DELAY_MS);
    },
    onPointerLeave: hide,
    onPointerDown: hide,
    onFocus: (event: FocusEvent<HTMLButtonElement>) => { if (event.currentTarget.matches(':focus-visible')) show(); },
    onBlur: hide,
  };
  const tooltip = tip && createPortal(
    <span id={tooltipId} role="tooltip" className={clsx('pet-mini-tooltip', `is-${placement}`)} style={tip.style}>{label}</span>,
    tip.host,
  );
  return { ref, triggerProps, tooltip };
}
