import { ArrowRight } from 'lucide-react';
import { type ReactNode, useContext } from 'react';

import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';

import { SignInChromeContext, type SignInFrameProps } from './chromeContext';

/** Draws one step in the shell's frame, or a plain column where no shell supplied one. */
export function SignInFrame({ children, back }: SignInFrameProps) {
  const chrome = useContext(SignInChromeContext);
  if (chrome) return <chrome.Frame back={back}>{children}</chrome.Frame>;
  return <main className="mx-auto flex w-full max-w-md flex-col">{children}</main>;
}

export function Primary({
  children,
  disabled,
  onClick,
  type = 'button',
}: {
  children: ReactNode;
  disabled?: boolean;
  onClick?: () => void;
  type?: 'button' | 'submit';
}) {
  return (
    <Button
      type={type}
      variant="brand"
      size="lg"
      disabled={disabled}
      onClick={onClick}
      className="w-full gap-3 rounded-full"
    >
      {children}
      <ArrowRight className="size-4" />
    </Button>
  );
}

export const raised = 'rounded-3xl border border-edge bg-surface/80 p-5 shadow-elevation-0';
/** A secondary way through a step, below its primary action. */
export const alternative = 'h-auto max-w-full whitespace-normal px-0 text-sm text-foreground underline';

/** The raised tile that heads a sign-in step. */
export function PageMark({ children, warn = false }: { children: ReactNode; warn?: boolean }) {
  return (
    <span
      className={cn(
        'mb-5 grid size-16 place-items-center rounded-3xl border bg-surface/80 shadow-elevation-0',
        warn ? 'border-gold/40' : 'border-edge',
      )}
    >
      {children}
    </span>
  );
}
