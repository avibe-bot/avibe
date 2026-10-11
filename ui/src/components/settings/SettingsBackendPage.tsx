import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { ArrowLeft, ArrowRight } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { useApi } from '@/context/ApiContext';
import { getBackendUiMeta, type AgentBackendId } from '@/lib/agentBackends';
import { SettingsPageShell } from './SettingsPageShell';
import { BackendRuntimeCard } from './shared/BackendRuntimeCard';
import { useBackendRuntime } from './shared/useBackendRuntime';
import { BackendSupplyModeCard } from './models/BackendSupplyModeCard';
import { hubSupplyBlocks, type HubSupplyBlock } from './models/featureFlags';
import { useModelHubCapability } from './models/useModelHubCapability';

/** Shared settings body for backends whose runtime is owned by Avibe. */
export function SettingsBackendPage({ backend }: { backend: AgentBackendId }) {
  const { t } = useTranslation();
  const meta = getBackendUiMeta(backend);
  const runtime = useBackendRuntime({ backend });
  const modelHubEnabled = useModelHubCapability();
  const { getConfig } = useApi();
  const [supplyBlock, setSupplyBlock] = useState<HubSupplyBlock | undefined>();

  useEffect(() => {
    let cancelled = false;
    getConfig()
      .then((config) => {
        if (!cancelled) setSupplyBlock(hubSupplyBlocks(config).get(backend));
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [getConfig, backend]);

  return (
    <SettingsPageShell
      activeTab="backends"
      title={meta.label}
      subtitle={t(meta.descriptionKey)}
      breadcrumb={
        <Link to="/settings/backends" className="inline-flex items-center gap-1.5 hover:text-foreground">
          <ArrowLeft className="size-3" />
          {t('settings.backends.backToBackends')}
        </Link>
      }
    >
      {!runtime.loaded ? <p>{t('common.loading')}</p> : (
        <div className="flex flex-col gap-4">
          <BackendRuntimeCard
            backend={backend}
            label={meta.label}
            description={t(meta.descriptionKey)}
            Icon={meta.Icon}
            iconTileClassName={meta.tileCls}
            iconClassName={meta.iconCls}
            runtime={runtime}
            extraSlot={supplyBlock && (
              <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-gold/40 bg-gold/10 px-3 py-2.5">
                <p className="text-[12px] text-gold-ink">
                  {supplyBlock === 'hub_disabled'
                    ? t('settings.backends.hubDisabledNotice', { name: meta.label })
                    : t('settings.backends.gatewayOff', { name: meta.label })}
                </p>
                {supplyBlock === 'gateway_off' && <Link
                  to="/settings/models"
                  className="model-hub-action-mint inline-flex shrink-0 items-center gap-1 text-[13px] font-medium transition-colors"
                >
                  {t('settings.models.supplyMode.hub.openModels')}
                  <ArrowRight className="size-3.5" />
                </Link>}
              </div>
            )}
          />
          {modelHubEnabled === true && <BackendSupplyModeCard backend={backend} />}
        </div>
      )}
    </SettingsPageShell>
  );
}
