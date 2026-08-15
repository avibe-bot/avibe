import * as React from 'react';
import { cn } from '@/lib/utils';
import logoImg from '@/assets/logo.png';

interface BrandLogoProps extends React.HTMLAttributes<HTMLSpanElement> {
  size?: number;
  withGlow?: boolean;
}

export const BrandLogo: React.FC<BrandLogoProps> = ({
  size = 36,
  withGlow = true,
  className,
  ...props
}) => (
  <span
    className={cn(
      'inline-flex shrink-0 items-center justify-center overflow-hidden rounded-xl border border-mint/30 bg-mint/[0.08]',
      withGlow && 'shadow-glow-md-mint',
      className
    )}
    style={{ width: size, height: size }}
    {...props}
  >
    <img src={logoImg} alt="avibe" className="h-full w-full object-cover" />
  </span>
);
