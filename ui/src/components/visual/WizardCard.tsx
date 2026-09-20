import * as React from 'react';
import { cn } from '@/lib/utils';

interface WizardCardProps extends React.HTMLAttributes<HTMLDivElement> {
  /** Outer max-width. Design.pen uses 840 (Platforms) or 920 (rest). */
  width?: number;
  /** Use the larger Welcome / Summary padding (64) instead of default (40, 48). */
  size?: 'default' | 'hero';
  /** Extra mint accent border for the Summary "all done" card. */
  accent?: boolean;
}

// Mirrors design.pen wCard / welCard / sumCard:
//  cornerRadius 20, fill --surface-2, stroke --border 1px, padding [40, 48]
//  shadow blur 64 / color #5BFFA014 / offset y32 / spread -12
export const WizardCard: React.FC<WizardCardProps> = ({
  width = 920,
  size = 'default',
  accent = false,
  className,
  children,
  ...props
}) => (
  <div
    className={cn(
      'mx-auto flex w-full flex-col',
      // Card chrome (radius / border / surface / shadow) and the large inner
      // padding kick in from sm up. On phones the wizard goes full-bleed on the
      // page background with no card — matches the mobile wizard frames in
      // design.pen (Tbqur / byTzd); the page gutter supplies the side spacing.
      'sm:rounded-2xl sm:border sm:bg-surface-2 sm:shadow-mint-card-lg',
      accent ? 'sm:border-mint/35' : 'sm:border-border',
      size === 'hero' ? 'sm:p-12 md:p-16' : 'sm:px-6 sm:py-8 md:px-12 md:py-10',
      className
    )}
    style={{ maxWidth: width }}
    {...props}
  >
    {children}
  </div>
);
