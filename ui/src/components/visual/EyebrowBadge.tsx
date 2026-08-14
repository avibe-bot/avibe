import * as React from 'react';
import { cn } from '@/lib/utils';

type Tone = 'cyan' | 'mint' | 'violet' | 'gold';

interface EyebrowBadgeProps extends React.HTMLAttributes<HTMLSpanElement> {
  tone?: Tone;
}

// Mirrors design.pen Badge/Eyebrow (mtcmf): JetBrains Mono, 11px, weight 700,
// letterSpacing 1.4, cyan glow shadow. Used for `01 — STEP` style labels.
const TONE_CLASSES: Record<Tone, string> = {
  cyan: 'border-cyan/50 bg-cyan/[0.16] text-cyan-ink shadow-[0_0_24px_-4px_rgba(63,224,229,0.33)]',
  mint: 'border-mint/50 bg-mint/[0.16] text-mint-ink shadow-[0_0_24px_-4px_rgba(91,255,160,0.33)]',
  violet: 'border-violet/50 bg-violet/[0.16] text-violet-ink shadow-[0_0_24px_-4px_rgba(124,91,255,0.33)]',
  gold: 'border-gold/50 bg-gold/[0.16] text-gold-ink shadow-[0_0_24px_-4px_rgba(255,200,87,0.33)]',
};

export const EyebrowBadge = React.forwardRef<HTMLSpanElement, EyebrowBadgeProps>(
  ({ tone = 'cyan', className, children, ...props }, ref) => (
    <span
      ref={ref}
      className={cn(
        'inline-flex items-center gap-1.5 rounded-full border px-3 py-1 font-mono text-[11px] font-bold uppercase tracking-[0.14em]',
        TONE_CLASSES[tone],
        className
      )}
      {...props}
    >
      {children}
    </span>
  )
);
EyebrowBadge.displayName = 'EyebrowBadge';
