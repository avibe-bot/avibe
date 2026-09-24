import * as React from 'react';
import { ChevronRight } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { cn } from '@/lib/utils';
import { PROTOCOL_COPY_KEYS } from './addApiKeyState';
import { sourceDetail } from './sourcePresentation';
import { SourceAccountLabel } from './SourceAccountLabel';
import { activeSourceAdoption, sourceStatePresentation } from './sourceStatePresentation';
import { useDeadlineClock } from './useDeadlineClock';
import { ACCENT_ICON, ACCENT_PILL, ACCENT_TILE, sourceVisual } from './vendorMeta';
import type { AgentBackend, Source } from './types';

export const SourceRow: React.FC<{
  source: Source;
  onOpen: (source: Source, opener: HTMLButtonElement) => void;
  activeBackends?: ReadonlySet<AgentBackend>;
}> = ({ source, onOpen, activeBackends }) => {
  const { t, i18n } = useTranslation();
  const now = useDeadlineClock(source.state.status === 'cooldown' ? source.state.retry_at : null);
  const { Icon, accent } = sourceVisual(source);
  const detail = sourceDetail(source);
  const interfaceLabel = t(PROTOCOL_COPY_KEYS[source.protocol]);
  const adoptedBy = activeSourceAdoption(source.adopted_by, activeBackends);
  const adoptedBackends = [...new Set((adoptedBy ?? []).map(({ backend }) => t(`settings.models.backends.${backend}`, { defaultValue: backend }) as string))];
  const state = sourceStatePresentation(source.state, 'card', i18n.language, now, {
    known: adoptedBy !== undefined,
    backends: adoptedBackends,
    native: source.supply_channel === 'native_cli',
    verificationPending: Boolean(source.verification_pending),
  });
  const kindKey = source.supply_channel === 'native_cli'
    ? 'nativeCredential'
    : source.kind === 'subscription'
      ? 'subscription'
      : 'apiKey';
  const adopted = !source.verification_pending && (source.state.status === 'active' || source.state.status === 'standby') && (adoptedBy?.length ?? 0) > 0;
  return (
    <div
      data-source-id={source.id}
      className={cn(
        'relative flex h-auto min-h-[96px] w-full items-center gap-2.5 rounded-[10px] border border-border bg-background px-3 py-2 text-left transition-colors hover:border-border-strong',
        adopted && source.supply_channel === 'native_cli' && 'border-cyan/30 bg-cyan/[0.04]',
        adopted && source.supply_channel === 'hub' && 'border-mint/30 bg-mint/[0.04]',
        source.state.status === 'cooldown' && 'border-gold/20 bg-gold/[0.04]',
      )}
    >
      <span className={cn('pointer-events-none flex size-[34px] shrink-0 items-center justify-center rounded-[9px]', ACCENT_TILE[accent])}>
        <Icon className={cn('size-[17px]', ACCENT_ICON[accent])} />
      </span>
      <span className="pointer-events-none min-w-0 flex-1">
        <button
          type="button"
          aria-label={source.display_name}
          onClick={(event) => onOpen(source, event.currentTarget)}
          className="pointer-events-auto block w-full text-left before:absolute before:inset-0 before:rounded-[10px] focus-visible:outline-none focus-visible:before:ring-2 focus-visible:before:ring-ring"
        >
          <span className="block min-w-0 truncate text-[12.5px] font-bold leading-[18px] text-foreground" title={source.display_name}>{source.display_name}</span>
          <span className="mt-1 flex min-w-0 flex-wrap items-center gap-1.5">
            <span className={cn('model-hub-pill border', ACCENT_PILL[source.kind === 'api_key' ? 'cyan' : accent])}>
              {t(`settings.models.upstream.kind.${kindKey}`)}
            </span>
            <span
              className="model-hub-pill model-hub-source-interface-pill border"
              title={interfaceLabel}
            >
              <span className="truncate">{interfaceLabel}</span>
            </span>
          </span>
        </button>
        {source.account_label && <SourceAccountLabel label={source.account_label} className="model-hub-upstream-detail mt-1 text-[10.5px] leading-[14px]" />}
        {detail && <span className="model-hub-upstream-detail mt-1 block truncate font-mono text-[10.5px] leading-[14px]" title={detail}>{detail}</span>}
        {state.key && <span className={cn('mt-1 flex items-center gap-[5px] text-[10.5px] font-semibold', state.textClass)}>
          <span className={cn('size-[5px] rounded-full', state.dotClass)} />
          {t(state.key, state.values)}
        </span>}
      </span>
      <ChevronRight className="model-hub-overview-chevron pointer-events-none size-[15px] shrink-0" />
    </div>
  );
};
