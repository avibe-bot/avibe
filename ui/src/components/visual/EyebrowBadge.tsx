import * as React from 'react';
import { cn } from '@/lib/utils';

type Tone = 'cyan' | 'mint' | 'violet' | 'gold';

interface EyebrowBadgeProps extends React.HTMLAttributes<HTMLSpanElement> {
  tone?: Tone;
}

// Mirrors design.pen Badge/Eyebrow (mtcmf): JetBrains Mono, 11px, weight 700,
// letterSpacing 1.4, cyan glow shadow. Used for `01 — STEP` style labels.
const TONE_CLASSES: Record<Tone, string> = {
  cyan: 'border-cyan/50 bg-cyan/[0.16] text-cyan-ink shadow-glow-md-cyan',
  mint: 'border-mint/50 bg-mint/[0.16] text-mint-ink shadow-glow-md-mint',
  violet: 'border-violet/50 bg-violet/[0.16] text-violet-ink shadow-glow-md-violet',
  gold: 'border-gold/50 bg-gold/[0.16] text-gold-ink shadow-glow-md-gold',
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
