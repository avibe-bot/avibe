import { useTranslation } from 'react-i18next';
import { BackendRuntimeCard } from '../shared/BackendRuntimeCard';
import { BackendSupplyModeCard } from '../models/BackendSupplyModeCard';
import { useModelHubCapability } from '../models/useModelHubCapability';
import { useBackendRuntime } from '../shared/useBackendRuntime';
import { BackendTestPanel } from '../BackendTestPanel';
import { BackendConnectionForm } from './BackendConnectionForm';
import { Card, CardContent } from '@/components/ui/card';
import { getBackendUiMeta } from '@/lib/agentBackends';

export function AntigravityProviderConfig({ hideEnableToggle }: { hideEnableToggle?: boolean } = {}) {
  const { t } = useTranslation();
  const meta = getBackendUiMeta('antigravity');
  const runtime = useBackendRuntime({ backend: 'antigravity', defaultCli: meta.defaultCli ?? 'agy' });
  const modelHubEnabled = useModelHubCapability();
  if (!runtime.loaded) return <p>{t('common.loading')}</p>;
  return (
    <div className="flex flex-col gap-4">
      <BackendRuntimeCard
        backend="antigravity"
        label={meta.label}
        description={t('settings.backends.antigravityDescription')}
        Icon={meta.Icon}
        iconTileClassName={meta.tileCls}
        iconClassName={meta.iconCls}
        runtime={runtime}
        hideEnableToggle={hideEnableToggle}
      />
      {modelHubEnabled === true && <BackendSupplyModeCard backend="antigravity" />}
      <Card>
        <CardContent className="p-6">
          <BackendConnectionForm backend="antigravity" connectionRevision={runtime.connectionRevision} />
        </CardContent>
      </Card>
      <BackendTestPanel backend="antigravity" />
    </div>
  );
}
