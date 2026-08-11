import * as React from 'react';
import { LoaderCircle } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { AgentCard } from './AgentCard';
import { regionData, type RegionRead } from './regionRead';
import type { AgentSupply, RuntimeDependency } from './types';

type GatewayModuleProps = Omit<React.ComponentProps<typeof AgentCard>, 'agents'> & {
  supply: RegionRead<AgentSupply[]>;
  runtime: RuntimeDependency | null;
  onRetry: () => void;
};

export const GatewayModule: React.FC<GatewayModuleProps> = ({ runtime, supply, onRetry, ...props }) => {
  const { t } = useTranslation();
  const agents = regionData(supply);
  const listening = runtime?.status.listening;
  return (
    <section className="relative z-20 flex max-h-full min-h-[420px] flex-col self-start overflow-hidden rounded-[14px] border border-border bg-surface">
      <header className="flex h-14 shrink-0 items-center justify-between border-b border-border px-3.5">
        <h2 className="text-[16px] font-bold text-foreground">{t('settings.models.gateway.heading')}</h2>
        {listening && <span className="model-hub-fill-white-0a rounded-full border border-border px-2 py-[3px] text-[10.5px] font-semibold text-muted">{listening.host}:{listening.port}</span>}
      </header>
      <div className="min-h-0 flex-1 overflow-y-auto p-2">
        {supply.kind === 'loading' && agents === undefined
          ? <div className="flex h-full min-h-36 items-center justify-center"><LoaderCircle className="size-4 animate-spin text-muted" /></div>
          : supply.kind === 'unread'
            ? <div className="flex h-full min-h-36 flex-col items-center justify-center gap-3 px-4 text-center"><p className="text-[12px] text-muted">{t('settings.models.gateway.supply.unread')}</p><Button variant="outline" size="xs" onClick={onRetry}>{t('settings.models.gateway.retry')}</Button></div>
            : <AgentCard agents={agents ?? []} {...props} />}
      </div>
    </section>
  );
};
