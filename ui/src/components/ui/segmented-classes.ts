import clsx from 'clsx';

/** Active-option color. Mint is the default; Show Pages tints by visibility. */
export type SegmentedTone = 'mint' | 'gold' | 'cyan' | 'muted';

const ACTIVE_TONES: Record<SegmentedTone, string> = {
  mint: 'border border-mint/30 bg-mint-soft font-bold text-mint-ink',
  gold: 'border border-gold/40 bg-gold/10 font-bold text-gold-ink',
  cyan: 'border border-cyan/40 bg-cyan-soft font-bold text-cyan-ink',
  muted: 'border border-edge bg-foreground/[0.06] font-bold text-foreground shadow-elevation-0',
};

/** The one segmented look (other fix #5, docs/plans/elevation-system.md): a filled track, no
 *  outline (ui/design: system/elevation/edges), and the active option in its tone. A control that
 *  is not a radio -- the Diagnostics section links -- draws the same track and items from these. */
export const segmentedTrackClass = 'flex h-9 items-stretch gap-0.5 rounded-md border border-edge bg-field p-0.5';

export const segmentedItemClass = (active: boolean, tone: SegmentedTone = 'mint') => clsx(
  'press inline-flex flex-1 items-center justify-center gap-1.5 whitespace-nowrap rounded-[4px] px-3 text-[12px]',
  active
    ? ACTIVE_TONES[tone]
    : 'font-medium text-muted hover:text-foreground active:bg-wash-press active:text-foreground',
);
