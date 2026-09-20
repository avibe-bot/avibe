import * as React from 'react';
import { cn } from '@/lib/utils';

interface ProgressBarProps extends React.HTMLAttributes<HTMLDivElement> {
  segments: number;
  current: number;
  width?: number | string;
}

// Segmented mint-glow progress bar (design.pen wProgress / wbProg).
// Active + completed segments fill mint; future segments are border-tinted.
export const ProgressBar: React.FC<ProgressBarProps> = ({
  segments,
  current,
  width = 600,
  className,
  ...props
}) => {
  const items = Array.from({ length: Math.max(segments, 1) });
  return (
    <div
      className={cn('mx-auto flex items-center gap-1.5', className)}
      style={{ maxWidth: typeof width === 'number' ? `${width}px` : width, width: '100%' }}
      {...props}
    >
      {items.map((_, i) => {
        const filled = i <= current;
        return (
          <div
            key={i}
            className={cn(
              'h-1 flex-1 rounded-full transition-all duration-300',
              filled
                ? 'bg-mint shadow-glow-dot-mint'
                : 'bg-foreground/[0.06]'
            )}
          />
        );
      })}
    </div>
  );
};
