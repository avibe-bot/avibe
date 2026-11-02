import * as React from 'react';

import { cn } from '@/lib/utils';
import { fieldBaseClass } from './field';

export type TextareaProps = React.TextareaHTMLAttributes<HTMLTextAreaElement>;

// Multi-line field. Shares the unified field surface (ui/design:
// system/fields/all); only the height + vertical padding differ from Input.
export const Textarea = React.forwardRef<HTMLTextAreaElement, TextareaProps>(({ className, ...props }, ref) => (
  <textarea
    ref={ref}
    className={cn(fieldBaseClass, 'flex min-h-[72px] px-3 py-2', className)}
    {...props}
  />
));
Textarea.displayName = 'Textarea';
