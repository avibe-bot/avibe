import { Bot } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { BackendRuntimeCard } from '../shared/BackendRuntimeCard';
import { BackendSupplyModeCard } from '../models/BackendSupplyModeCard';
import { useModelHubCapability } from '../models/useModelHubCapability';
import { useBackendRuntime } from '../shared/useBackendRuntime';
import { BackendTestPanel } from '../BackendTestPanel';
import { BackendConnectionForm } from './BackendConnectionForm';
import { Card, CardContent } from '@/components/ui/card';

export function CodexProviderConfig({ hideEnableToggle }: { hideEnableToggle?: boolean } = {}) {
  const { t } = useTranslation();
  const runtime = useBackendRuntime({ backend: 'codex', defaultCli: 'codex' });
  const modelHubEnabled = useModelHubCapability();
  if (!runtime.loaded) return <p>{t('common.loading')}</p>;
  return <div className="flex flex-col gap-4">
    <BackendRuntimeCard backend="codex" label="Codex" description={t('settings.backends.codexDescription')}
      Icon={Bot} iconTileClassName="bg-gold" iconClassName="text-gold-foreground" runtime={runtime} hideEnableToggle={hideEnableToggle} />
    {modelHubEnabled === true && <BackendSupplyModeCard backend="codex" />}
    <Card><CardContent className="p-6"><BackendConnectionForm backend="codex" connectionRevision={runtime.connectionRevision} /></CardContent></Card>
    <BackendTestPanel backend="codex" />
  </div>;
}
