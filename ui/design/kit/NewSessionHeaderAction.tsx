// Variant A: with the center taken by the main Bot, "new session" becomes the ＋ at
// the right of the mobile brand header, on every tabbed page. It opens the shipped
// new-session sheet. (Drawn over the shipped header's empty right side.)
import { Plus } from 'lucide-react';
import clsx from 'clsx';

import { Button } from '@/components/ui/button';

import { useDraft } from './draft';

export function NewSessionHeaderAction({ pressed = false }: { pressed?: boolean }) {
  const draft = useDraft();
  return (
    <div className="fixed right-4 top-0 z-40 flex h-16 items-center md:hidden">
      <Button
        type="button"
        variant="outline"
        size="sm"
        aria-label={draft('新会话', 'New session')}
        className={clsx(
          'h-8 gap-1 rounded-full border-mint/35 bg-mint/8 px-3 text-xs font-semibold text-mint-ink hover:bg-mint/14',
          pressed && 'bg-mint/18 ring-2 ring-mint/40',
        )}
      >
        <Plus className="size-3.5" />
        {draft('新会话', 'New')}
      </Button>
    </div>
  );
}
