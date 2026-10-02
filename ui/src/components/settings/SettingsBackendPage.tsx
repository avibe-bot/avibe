import { Link } from 'react-router-dom';
import { ArrowLeft } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { getBackendUiMeta, type AgentBackendId } from '@/lib/agentBackends';
import { SettingsPageShell } from './SettingsPageShell';
import { BackendRuntimeCard } from './shared/BackendRuntimeCard';
import { useBackendRuntime } from './shared/useBackendRuntime';
import { BackendSupplyModeCard } from './models/BackendSupplyModeCard';
import { useModelHubCapability } from './models/useModelHubCapability';

/** Shared settings body for backends whose runtime is owned by Avibe. */
export function SettingsBackendPage({ backend }: { backend: AgentBackendId }) {
  const { t } = useTranslation();
  const meta = getBackendUiMeta(backend);
  const runtime = useBackendRuntime({ backend });
  const modelHubEnabled = useModelHubCapability();

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
          />
          {modelHubEnabled === true && <BackendSupplyModeCard backend={backend} />}
        </div>
      )}
    </SettingsPageShell>
  );
}
