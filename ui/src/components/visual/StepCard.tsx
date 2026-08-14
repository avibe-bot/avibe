import * as React from 'react';
import { cn } from '@/lib/utils';

interface StepCardProps extends Omit<React.HTMLAttributes<HTMLDivElement>, 'title'> {
  eyebrow: React.ReactNode;
  title: React.ReactNode;
  description?: React.ReactNode;
  icon?: React.ReactNode;
}

// Mirrors design.pen Card/Step (CdFKF):
//  rounded-12, fill --surface-2, padding 32, gap 16
//  shadow blur 48 / color #5BFFA014 / offset y24 / spread -12
//  stroke #5BFFA033 (mint @ 0.20) 1px
//  Step icon block: 48x48 mint-tinted square with mint icon (sparkles, etc).
export const StepCard: React.FC<StepCardProps> = ({
  eyebrow,
  title,
  description,
  icon,
  className,
  children,
  ...props
}) => (
  <div
    className={cn(
      'flex flex-col gap-4 rounded-xl border border-mint/[0.20] bg-surface-2 p-8',
      'shadow-[0_24px_48px_-12px_rgba(91,255,160,0.078)]',
      className
    )}
    {...props}
  >
    {icon ? (
      <span className="inline-flex size-12 items-center justify-center rounded-[10px] border border-mint/[0.33] bg-mint/[0.12] text-mint-ink [&>svg]:size-[22px]">
        {icon}
      </span>
    ) : null}
    <span className="font-mono text-[12px] font-semibold uppercase tracking-[0.16em] text-mint-ink">
      {eyebrow}
    </span>
    <h3 className="text-[22px] font-bold leading-tight tracking-[-0.4px] text-foreground">{title}</h3>
    {description ? (
      <p className="text-[14px] leading-[1.55] text-muted">{description}</p>
    ) : null}
    {children}
  </div>
);
