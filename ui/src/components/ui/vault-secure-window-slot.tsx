import { AppWindow } from 'lucide-react';

import { cn } from '@/lib/utils';

/** Where a Protected value would be typed: in the secure window, opened when the dialog saves.
 *  ui/design: vault/secret-dialog/protected-window-*. The page never holds a Protected value. */
export function SecureWindowSlot({ title, hint }: { title: string; hint: string }) {
  return (
    <div className="flex items-start gap-2.5 rounded-[10px] border border-dashed border-border-strong bg-surface-2 px-3 py-2.5">
      <AppWindow className="mt-0.5 size-4 shrink-0 text-muted" />
      <div className="flex min-w-0 flex-col gap-0.5">
        <span className="text-[13px] font-medium text-foreground">{title}</span>
        <span className="text-[11.5px] leading-snug text-muted">{hint}</span>
      </div>
    </div>
  );
}

/** A field row's value cell for a Protected value typed in the secure window. */
export function SecureWindowCell({ label, tone = 'muted' }: { label: string; tone?: 'muted' | 'mint' }) {
  return (
    <span className={cn(
      'flex h-8 min-w-0 flex-1 items-center gap-1.5 rounded-md border border-dashed px-2 text-[12px]',
      tone === 'mint' ? 'border-mint/50 bg-mint-soft/40 text-mint-ink' : 'border-border-strong text-muted',
    )}>
      <AppWindow className="size-3.5 shrink-0" />
      <span className="truncate">{label}</span>
    </span>
  );
}
