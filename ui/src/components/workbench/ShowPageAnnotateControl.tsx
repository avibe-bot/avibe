import React, { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';
import { Camera, Check, type LucideIcon, MessageSquarePlus, MousePointerClick, X } from 'lucide-react';

import { Button } from '../ui/button';
import { Popover, PopoverContent, PopoverTrigger } from '../ui/popover';
import type { AnnotationMode, AnnotationState } from './useShowPageAnnotation';

// Show Page annotation control rendered in the chat header (only in Show Page
// mode, immediately left of the back-to-chat button). It is purely
// presentational: state comes from the `useShowPageAnnotation` bridge and every
// action is a callback into it.
//
// - Desktop ≥ md: a collapsed icon button that expands, once enabled, to a mint
//   pill (toggle square + Smart/截图 segments).
// - Mobile < md: a single button that both enables (remembered mode) and opens
//   a mode-picker popover.
//
// The Smart/截图 segments use the design system's mint segmented tone (see
// ui/segmented.tsx) but are composed inline: the header needs a compact h-7,
// per-segment icons, and the segments embedded in a single mint-glow pill
// alongside a separate toggle square — a genuinely different visual unit than
// the standalone SegmentedRadio primitive.

interface ModeDef {
  id: AnnotationMode;
  icon: LucideIcon;
  labelKey: string;
  descKey: string;
}

const MODES: readonly ModeDef[] = [
  {
    id: 'smart',
    // mouse-pointer-click (matches design.pen + Lane R overlay). Sparkles is
    // reserved for AI-generate affordances elsewhere in the workbench.
    icon: MousePointerClick,
    labelKey: 'chat.showPage.annotate.smart',
    descKey: 'chat.showPage.annotate.smartDesc',
  },
  {
    id: 'screenshot',
    icon: Camera,
    labelKey: 'chat.showPage.annotate.screenshot',
    descKey: 'chat.showPage.annotate.screenshotDesc',
  },
] as const;

interface ShowPageAnnotateControlProps {
  /** Overlay state from the bridge; null until the first state message. */
  state: AnnotationState | null;
  /** Enable with the remembered mode (no explicit mode). */
  onEnable: () => void;
  onDisable: () => void;
  onSetMode: (mode: AnnotationMode) => void;
  /**
   * Mobile popover open/close. The popover floats over the Show Page iframe, so
   * ChatPage makes the iframe inert while it is open (an outside tap inside the
   * iframe never reaches us) — same pattern as the share control.
   */
  onPopoverOpenChange?: (open: boolean) => void;
}

export const ShowPageAnnotateControl: React.FC<ShowPageAnnotateControlProps> = ({
  state,
  onEnable,
  onDisable,
  onSetMode,
  onPopoverOpenChange,
}) => {
  const { t } = useTranslation();
  const ready = state !== null;
  const available = state?.available === true;
  const enabled = state?.enabled === true;
  const mode: AnnotationMode = state?.mode ?? 'smart';
  // Until the overlay reports, or when writes aren't possible, the control is a
  // disabled collapsed button with an explanatory tooltip.
  const locked = !ready || !available;

  const [popoverOpen, setPopoverOpen] = useState(false);
  // If we unmount while open (leaving Show Page mode), Radix won't fire
  // onOpenChange(false), so report closed here — otherwise ChatPage keeps the
  // next Show Page's iframe inert.
  useEffect(() => () => onPopoverOpenChange?.(false), [onPopoverOpenChange]);

  // A state reset or `available=false` transition (iframe re-point, overlay
  // reporting writes unavailable) swaps the picker subtree for the locked
  // button below WITHOUT a Radix-driven close, so Radix never fires
  // onOpenChange(false) and the unmount-only cleanup above doesn't run. Force
  // the picker shut so ChatPage clears the iframe-inert flag and the Show Page
  // stays clickable: reset our own flag during render (React's adjust-on-input
  // pattern) and notify the parent from an effect (can't setState the parent
  // during our render).
  if (locked && popoverOpen) setPopoverOpen(false);
  useEffect(() => {
    if (locked) onPopoverOpenChange?.(false);
  }, [locked, onPopoverOpenChange]);

  const handlePopoverOpenChange = (next: boolean) => {
    setPopoverOpen(next);
    onPopoverOpenChange?.(next);
    // Opening while off enables with the remembered mode — matches the design:
    // "tapping enables (last mode) and pops up the picker". Re-opening while on
    // never re-enables (or disables); "关闭标注" is the only off switch.
    if (next && !enabled) onEnable();
  };

  const closeFromPopover = () => {
    onDisable();
    setPopoverOpen(false);
    onPopoverOpenChange?.(false);
  };

  const toggleLabel = t('chat.showPage.annotate.toggle');
  // The enabled desktop toggle square turns annotation OFF — label it as such
  // (icon-only, so the tooltip/screen-reader name must describe the action).
  const offLabel = t('chat.showPage.annotate.closeAnnotation');

  if (locked) {
    return (
      <Button
        type="button"
        variant="outline"
        size="icon"
        className="size-7 shrink-0"
        disabled
        aria-label={t('chat.showPage.annotate.unavailable')}
        title={t('chat.showPage.annotate.unavailable')}
      >
        <MessageSquarePlus className="size-3.5" />
      </Button>
    );
  }

  return (
    <>
      {/* Desktop ≥ md: collapsed icon button ⇄ mint pill (toggle + segments). */}
      <div className="hidden items-center md:flex">
        {enabled ? (
          <div className="flex h-7 items-center gap-0.5 rounded-lg border border-mint/40 bg-mint/[0.06] p-0.5 shadow-[0_0_16px_-6px_rgba(91,255,160,0.5)]">
            <button
              type="button"
              onClick={onDisable}
              aria-label={offLabel}
              title={offLabel}
              aria-pressed
              className="grid size-6 shrink-0 place-items-center rounded-[5px] bg-mint text-primary-foreground transition hover:brightness-110"
            >
              <MessageSquarePlus className="size-3.5" />
            </button>
            <div
              role="radiogroup"
              aria-label={t('chat.showPage.annotate.modeTitle')}
              className="flex items-center gap-0.5"
            >
              {MODES.map(({ id, icon: Icon, labelKey }) => {
                const active = mode === id;
                return (
                  <button
                    key={id}
                    type="button"
                    role="radio"
                    aria-checked={active}
                    onClick={() => onSetMode(id)}
                    className={clsx(
                      'flex h-6 items-center gap-1 rounded-[5px] px-2 text-[12px] transition-colors',
                      active ? 'bg-mint-soft font-bold text-mint' : 'font-medium text-muted hover:text-foreground',
                    )}
                  >
                    <Icon className="size-3.5" />
                    {t(labelKey)}
                  </button>
                );
              })}
            </div>
          </div>
        ) : (
          <Button
            type="button"
            variant="outline"
            size="icon"
            className="size-7 shrink-0"
            onClick={() => onEnable()}
            aria-label={toggleLabel}
            title={toggleLabel}
          >
            <MessageSquarePlus className="size-3.5" />
          </Button>
        )}
      </div>

      {/* Mobile < md: single button that enables + opens a mode-picker popover. */}
      <div className="md:hidden">
        <Popover open={popoverOpen} onOpenChange={handlePopoverOpenChange}>
          <PopoverTrigger asChild>
            <Button
              type="button"
              variant={enabled ? 'default' : 'outline'}
              size="icon"
              className="size-7 shrink-0"
              aria-label={toggleLabel}
              title={toggleLabel}
            >
              <MessageSquarePlus className="size-3.5" />
            </Button>
          </PopoverTrigger>
          <PopoverContent align="end" className="w-64 p-1.5">
            <div className="flex items-center justify-between px-2 pb-1.5 pt-1">
              <span className="text-[13px] font-medium text-foreground">{t('chat.showPage.annotate.modeTitle')}</span>
              <span className="text-[11px] text-muted">{t('chat.showPage.annotate.rememberHint')}</span>
            </div>
            <div role="radiogroup" aria-label={t('chat.showPage.annotate.modeTitle')}>
              {MODES.map(({ id, icon: Icon, labelKey, descKey }) => {
                const active = enabled && mode === id;
                return (
                  <button
                    key={id}
                    type="button"
                    role="radio"
                    aria-checked={active}
                    onClick={() => onSetMode(id)}
                    className="flex w-full items-center gap-2.5 rounded-md px-2 py-2 text-left transition-colors hover:bg-surface-2"
                  >
                    <span
                      className={clsx(
                        'grid size-8 shrink-0 place-items-center rounded-md',
                        active ? 'bg-mint-soft text-mint' : 'bg-foreground/[0.04] text-muted',
                      )}
                    >
                      <Icon className="size-4" />
                    </span>
                    <span className="min-w-0 flex-1">
                      <span
                        className={clsx(
                          'block text-[13px]',
                          active ? 'font-bold text-mint' : 'font-medium text-foreground',
                        )}
                      >
                        {t(labelKey)}
                      </span>
                      <span className="block text-[11px] text-muted">{t(descKey)}</span>
                    </span>
                    {active && <Check className="size-4 shrink-0 text-mint" />}
                  </button>
                );
              })}
            </div>
            <div className="my-1 border-t border-border" />
            <button
              type="button"
              onClick={closeFromPopover}
              className="flex w-full items-center gap-2.5 rounded-md px-2 py-2 text-left text-[13px] text-muted transition-colors hover:bg-surface-2 hover:text-foreground"
            >
              <span className="grid size-8 shrink-0 place-items-center">
                <X className="size-4" />
              </span>
              {t('chat.showPage.annotate.closeAnnotation')}
            </button>
          </PopoverContent>
        </Popover>
      </div>
    </>
  );
};
