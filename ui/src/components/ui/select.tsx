import * as React from 'react';
import { ChevronDown } from 'lucide-react';

import { cn } from '@/lib/utils';
import { fieldBaseClass } from './field';

export type SelectProps = React.SelectHTMLAttributes<HTMLSelectElement> & {
  // Styles the positioning wrapper; `className` styles the <select> itself.
  wrapperClassName?: string;
};

// The non-search dropdown. A styled native <select> so it stays fully
// accessible, with the unified field surface + a custom chevron. When closed
// it is intentionally identical to Combobox's trigger, but for the chevron
// icon (ui/design: system/fields/all).
export const Select = React.forwardRef<HTMLSelectElement, SelectProps>(
  ({ className, wrapperClassName, children, disabled, ...props }, ref) => (
    <div className={cn('relative w-full', wrapperClassName)}>
      <select
        ref={ref}
        disabled={disabled}
        className={cn(fieldBaseClass, 'flex h-9 cursor-pointer appearance-none px-3 pr-9', className)}
        {...props}
      >
        {children}
      </select>
      <ChevronDown
        className={cn(
          'pointer-events-none absolute right-3 top-1/2 size-4 -translate-y-1/2 text-muted opacity-70',
          disabled && 'opacity-40'
        )}
        aria-hidden="true"
      />
    </div>
  )
);
Select.displayName = 'Select';
