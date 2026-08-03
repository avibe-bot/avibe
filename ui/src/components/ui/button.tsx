import * as React from 'react';
import { Slot } from '@radix-ui/react-slot';
import { type VariantProps } from 'class-variance-authority';

import { cn } from '@/lib/utils';
import { buttonVariants } from './button-variants';

export type ButtonProps = React.ButtonHTMLAttributes<HTMLButtonElement> &
  VariantProps<typeof buttonVariants> & { asChild?: boolean };

export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, asChild = false, title, ...props }, ref) => {
    const Comp = asChild ? Slot : 'button';
    // Icon-only buttons carry their label in `aria-label` (there's no visible
    // text). Mirror it into `title` so hovering shows that label as a native
    // tooltip — unless the caller passed an explicit title. Gives every
    // icon-sized button a hover hint for free.
    const ariaLabel = props['aria-label'];
    const resolvedTitle = title ?? (size === 'icon' && typeof ariaLabel === 'string' ? ariaLabel : undefined);
    return (
      <Comp ref={ref} title={resolvedTitle} className={cn(buttonVariants({ variant, size }), className)} {...props} />
    );
  }
);
Button.displayName = 'Button';
