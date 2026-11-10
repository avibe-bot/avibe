import * as React from 'react';
import * as PopoverPrimitive from '@radix-ui/react-popover';

import { useRouteSurfaceActive } from '../../lib/routeSurfaceActivity';
import { useLatestRef } from '../../lib/useLatestRef';
import { cn } from '../../lib/utils';

const Popover = PopoverPrimitive.Root;
const PopoverTrigger = PopoverPrimitive.Trigger;
const PopoverAnchor = PopoverPrimitive.Anchor;

const PopoverContent = React.forwardRef<
  React.ElementRef<typeof PopoverPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof PopoverPrimitive.Content>
>(({ className, align = 'center', sideOffset = 4, onCloseAutoFocus, ...props }, ref) => {
  const active = useRouteSurfaceActive();
  const activeRef = useLatestRef(active);
  if (!active) return null;
  return (
    <PopoverPrimitive.Portal>
      <PopoverPrimitive.Content
        ref={ref}
        align={align}
        sideOffset={sideOffset}
        className={cn(
          // `motion-popover` (index.css): grows from its trigger on `fast` and fades out on `fast`
          // (docs/plans/web-ui-motion.md M6). A floating surface: the popover fill, no outline,
          // level 2 (ui/design: system/overlays/popover).
          'motion-popover z-50 w-72 rounded-md border border-edge bg-popover p-4 text-text shadow-elevation-2 outline-none',
          className,
        )}
        {...props}
        onCloseAutoFocus={(event) => {
          if (!activeRef.current) event.preventDefault();
          else onCloseAutoFocus?.(event);
        }}
      />
    </PopoverPrimitive.Portal>
  );
});
PopoverContent.displayName = PopoverPrimitive.Content.displayName;

export { Popover, PopoverTrigger, PopoverContent, PopoverAnchor };
