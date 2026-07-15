import * as React from 'react';
import { cn } from '@/lib/utils';

type Tone = 'running' | 'stopped' | 'warning' | 'idle';

interface StatusPillProps extends React.HTMLAttributes<HTMLSpanElement> {
  tone?: Tone;
  label: React.ReactNode;
  indicator?: React.ReactNode;
}

const TONE_CLASSES: Record<Tone, { wrapper: string; dot: string }> = {
  running: { wrapper: 'border-mint/40 bg-mint/[0.12] text-foreground', dot: 'bg-mint shadow-[0_0_8px_rgba(91,255,160,0.6)]' },
  stopped: { wrapper: 'border-border-strong bg-surface-2 text-muted', dot: 'bg-muted' },
  warning: { wrapper: 'border-gold/40 bg-gold/[0.12] text-gold', dot: 'bg-gold' },
  idle: { wrapper: 'border-border bg-surface text-muted', dot: 'bg-muted' },
};

export const StatusPill: React.FC<StatusPillProps> = ({ tone = 'idle', label, indicator, className, ...props }) => {
  const tones = TONE_CLASSES[tone];
  return (
    <span
      className={cn(
        'inline-flex items-center gap-2 rounded-full border px-3 py-1 text-sm font-medium',
        tones.wrapper,
        className
      )}
      {...props}
    >
      {indicator ?? <span className={cn('h-2 w-2 rounded-full', tones.dot)} />}
      {label}
    </span>
  );
};
