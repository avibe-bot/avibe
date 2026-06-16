import * as React from 'react';
import { Slot } from '@radix-ui/react-slot';
import { cva, type VariantProps } from 'class-variance-authority';

import { cn } from '@/lib/utils';

const buttonVariants = cva(
  'inline-flex items-center justify-center whitespace-nowrap rounded-lg font-medium transition disabled:cursor-not-allowed disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background [&_svg]:shrink-0',
  {
    variants: {
      variant: {
        // Mint primary — flat, no glow shadow (design.pen Button/Default).
        default: 'gap-1.5 bg-primary text-primary-foreground hover:brightness-110',
        // Brand CTA — bright bg + brand-color glow shadow + bold text + brighten on hover.
        // Foreground tokens flip in light mode (--primary/--accent/--gold-foreground).
        brand:
          'gap-2 bg-mint font-bold text-primary-foreground shadow-[0_0_24px_-4px_rgba(91,255,160,0.6)] hover:brightness-105 disabled:shadow-none',
        'brand-cyan':
          'gap-2 bg-cyan font-bold text-accent-foreground shadow-[0_0_24px_-4px_rgba(63,224,229,0.6)] hover:brightness-105 disabled:shadow-none',
        'brand-gold':
          'gap-2 bg-gold font-bold text-gold-foreground shadow-[0_0_24px_-4px_rgba(255,200,87,0.55)] hover:brightness-105 disabled:shadow-none',
        'brand-violet':
          'gap-2 bg-violet font-bold text-white shadow-[0_0_24px_-4px_rgba(124,91,255,0.55)] hover:brightness-105 disabled:shadow-none',
        secondary: 'gap-1.5 border border-border bg-secondary text-secondary-foreground hover:border-border-strong',
        // Outline — bg matches page surface so it sits cleanly on glow gradients.
        outline:
          'gap-1.5 border border-border bg-background text-foreground shadow-[0_1px_2px_rgba(0,0,0,0.05)] hover:bg-surface-2',
        // Cyan outline — for "Read Vibe Remote" / docs style CTAs.
        'outline-cyan':
          'gap-1.5 border border-cyan/40 bg-cyan/[0.06] text-cyan hover:bg-cyan/[0.10]',
        ghost: 'gap-1.5 text-foreground hover:bg-surface-2',
        destructive: 'gap-1.5 bg-destructive text-destructive-foreground hover:opacity-90',
        // Pink-soft destructive — design.pen T09T8Z. Pink fill + pink border
        // + pink text/icon, used for in-panel delete CTAs where a full
        // destructive shouts too loud. Drives the --pink / --pink-soft tokens
        // (see index.css); the old bg-[#FF5B8A14] one-off plus an unresolved
        // `pink` token left the border falling back to currentColor (black in
        // light, white in dark) and the fill near-invisible.
        // Fill is pink/15 (not the 10% --pink-soft token) so it carries the
        // same visual weight as the mint-soft "Run" button it usually sits next
        // to — a balanced soft pair rather than a near-invisible wash.
        'destructive-soft':
          'gap-1.5 border border-pink/45 bg-pink/15 text-pink hover:border-pink/60 hover:bg-pink/[0.22]',
        link: 'text-primary underline-offset-4 hover:underline',
        accent: 'gap-1.5 border border-cyan/40 bg-cyan-soft text-cyan hover:bg-cyan/15',
      },
      size: {
        // h-8 toolbar buttons (LogsPanel/DoctorPanel/SettingsServicePage/AgentDetection toolbar).
        xs: 'h-8 px-3 text-[12px] [&_svg]:size-3.5',
        // h-9 config-inline CTAs (Slack/Discord/Telegram/...).
        sm: 'h-9 px-4 text-[13px] [&_svg]:size-3.5',
        // h-10 wizard "下一步" — most common.
        default: 'h-10 px-5 text-[13px] [&_svg]:size-3.5',
        // h-12 prominent CTAs (Summary main step CTA).
        lg: 'h-12 px-7 text-[14px] [&_svg]:size-4',
        // Welcome-only hero CTA.
        hero: 'h-[52px] rounded-xl px-8 text-[15px] [&_svg]:size-4',
        icon: 'h-9 w-9',
      },
    },
    defaultVariants: {
      variant: 'default',
      size: 'default',
    },
  }
);

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

export { buttonVariants };
