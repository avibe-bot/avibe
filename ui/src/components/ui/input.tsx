import * as React from 'react';

import { cn } from '@/lib/utils';
import { fieldBaseClass } from './field';

export type InputProps = React.InputHTMLAttributes<HTMLInputElement> & {
  // ``bare`` drops the inset-field chrome (border / fill / height) for inputs
  // that sit inside a container which already owns the framing — e.g. a search
  // field inside a bordered pill with its own glyph + clear/Esc affordances.
  // Callers add only layout + text size on top. Keeps such fields on the shared
  // primitive (focus behavior, future design-system fixes) instead of
  // re-rolling a native <input>.
  variant?: 'default' | 'bare';
};

export const Input = React.forwardRef<HTMLInputElement, InputProps>(
  ({ className, type, variant = 'default', ...props }, ref) => (
    <input
      ref={ref}
      type={type}
      className={cn(
        variant === 'bare'
          ? 'bg-transparent text-foreground outline-none placeholder:text-muted disabled:cursor-not-allowed disabled:opacity-50'
          : cn(fieldBaseClass, 'flex h-9 px-3 py-1'),
        className,
      )}
      {...props}
    />
  ),
);
Input.displayName = 'Input';
