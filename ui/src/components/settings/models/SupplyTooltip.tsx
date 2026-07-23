// Dark hover/focus tooltip listing the models a source supplies (frame 01r:
// "supply list appears ONLY as hover tooltip on icon/title"). CSS group-hover
// keeps it dependency-free; the trigger must not sit inside an overflow-hidden
// ancestor (the 来源 card container is deliberately not clipped).
//
// bg-foreground/text-background is token-driven: dark tooltip in Light mode
// (matches the V4 mock) and auto-inverts in Dark (refine when Dark mocks land).
import * as React from 'react';
import { Cpu } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { cn } from '@/lib/utils';
import type { SuppliedModel } from './types';

const MAX_SHOWN = 6;

export const SupplyTooltip: React.FC<{
  models: SuppliedModel[];
  children: React.ReactNode;
  /** Layout classes for the wrapper (e.g. flex-1). Defaults to inline. */
  className?: string;
}> = ({ models, children, className }) => {
  const { t } = useTranslation();
  const names = models.map((m) => m.display_name || m.id);
  const shown = names.slice(0, MAX_SHOWN);
  const more = names.length - shown.length;

  return (
    <span className={cn('group/supply relative', className ?? 'inline-flex items-center')}>
      {children}
      {names.length > 0 && (
        <span
          role="tooltip"
          className="pointer-events-none absolute left-0 top-full z-30 mt-2 hidden w-max max-w-sm group-hover/supply:block group-focus-within/supply:block"
        >
          <span className="flex items-center gap-2 rounded-lg bg-foreground px-3 py-2 text-[12px] leading-snug text-background shadow-lg">
            <Cpu className="size-3.5 shrink-0 text-mint" />
            <span>
              <span className="text-background/70">{t('settings.models.supplyTooltip.label')}</span>
              {shown.join(' · ')}
              {more > 0 ? ` +${more}` : ''}
            </span>
          </span>
        </span>
      )}
    </span>
  );
};
