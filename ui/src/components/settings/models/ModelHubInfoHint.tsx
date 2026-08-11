import * as React from 'react';
import { Info } from 'lucide-react';

import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import { cn } from '@/lib/utils';

export const ModelHubInfoHint: React.FC<{
  label: string;
  content: React.ReactNode;
  className?: string;
  align?: 'start' | 'center' | 'end';
}> = ({ label, content, className, align = 'start' }) => {
  const [open, setOpen] = React.useState(false);
  return (
    <Popover open={open} onOpenChange={setOpen} modal>
      <PopoverTrigger asChild>
        <button
          type="button"
          aria-label={label}
          onPointerEnter={(event) => { if (event.pointerType === 'mouse') setOpen(true); }}
          onPointerLeave={(event) => { if (event.pointerType === 'mouse') setOpen(false); }}
          className={cn('inline-flex size-4 shrink-0 items-center justify-center rounded-full text-muted outline-none transition hover:text-foreground focus-visible:text-foreground', className)}
        >
          <Info className="size-3.5" />
        </button>
      </PopoverTrigger>
      <PopoverContent
        align={align}
        sideOffset={6}
        onOpenAutoFocus={(event) => event.preventDefault()}
        className="w-64 p-3 text-[12px] font-normal leading-relaxed text-muted"
      >
        {content}
      </PopoverContent>
    </Popover>
  );
};

