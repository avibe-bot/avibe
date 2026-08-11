import * as React from 'react';
import { ChevronRight } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { cn } from '@/lib/utils';
import { sourceDetail } from './sourcePresentation';
import { sourceStatePresentation } from './sourceStatePresentation';
import { ACCENT_ICON, ACCENT_TILE, sourceVisual } from './vendorMeta';
import type { AdoptedBy, Source } from './types';

export const SourceRow: React.FC<{ source: Source; adoptedBy?: readonly AdoptedBy[]; onOpen: (source: Source) => void }> = ({ source, adoptedBy = [], onOpen }) => {
  const { t, i18n } = useTranslation();
  const [now] = React.useState(() => Date.now());
  const { Icon, accent } = sourceVisual(source);
  const detail = sourceDetail(source);
  const adoptedBackends = [...new Set(adoptedBy.map(({ backend }) => t(`settings.models.backends.${backend}`, { defaultValue: backend }) as string))];
  const state = sourceStatePresentation(source.state, 'card', i18n.language, now, {
    backends: adoptedBackends,
    native: source.supply_channel === 'native_cli',
  });
  const kindKey = source.supply_channel === 'native_cli'
    ? 'nativeCredential'
    : source.kind === 'subscription'
      ? 'subscription'
      : 'apiKey';
  const adopted = source.state.status === 'active' && adoptedBy.length > 0;
  return (
    <button
      type="button"
      data-source-id={source.id}
      onClick={() => onOpen(source)}
      className={cn(
        'flex h-20 w-full items-center gap-2.5 rounded-[10px] border border-border bg-background px-3 text-left transition-colors hover:border-border-strong focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
        adopted && source.supply_channel === 'native_cli' && 'border-cyan/30 bg-cyan/[0.04]',
        adopted && source.supply_channel === 'hub' && 'border-mint/30 bg-mint/[0.04]',
        source.state.status === 'cooldown' && 'border-gold/20 bg-gold/[0.04]',
      )}
    >
      <span className={cn('flex size-[34px] shrink-0 items-center justify-center rounded-[9px]', ACCENT_TILE[accent])}>
        <Icon className={cn('size-4', ACCENT_ICON[accent])} />
      </span>
      <span className="min-w-0 flex-1">
        <span className="flex min-w-0 items-center gap-2">
          <span className="truncate text-[12.5px] font-bold text-foreground" title={source.display_name}>{source.display_name}</span>
          <span className="shrink-0 rounded-full border border-border bg-foreground/[0.04] px-2 py-0.5 text-[10px] font-semibold text-muted">
            {t(`settings.models.upstream.kind.${kindKey}`)}
          </span>
        </span>
        {detail && <span className="mt-1 block truncate font-mono text-[10.5px] text-muted" title={detail}>{detail}</span>}
        {state.key && <span className={cn('mt-1 flex items-center gap-1.5 text-[10.5px] font-semibold', state.textClass)}>
          <span className={cn('size-[5px] rounded-full', state.dotClass)} />
          {t(state.key, state.values)}
        </span>}
      </span>
      <ChevronRight className="size-[15px] shrink-0 text-muted/50" />
    </button>
  );
};
