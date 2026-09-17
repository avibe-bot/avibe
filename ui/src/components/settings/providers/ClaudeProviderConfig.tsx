import { Sparkles } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { BackendRuntimeCard } from '../shared/BackendRuntimeCard';
import { BackendSupplyModeCard } from '../models/BackendSupplyModeCard';
import { useModelHubCapability } from '../models/useModelHubCapability';
import { useBackendRuntime } from '../shared/useBackendRuntime';
import { BackendTestPanel } from '../BackendTestPanel';
import { BackendConnectionForm } from './BackendConnectionForm';
import { Card, CardContent } from '@/components/ui/card';

export function ClaudeProviderConfig({ hideEnableToggle }: { hideEnableToggle?: boolean } = {}) {
  const { t } = useTranslation();
  const runtime = useBackendRuntime({ backend: 'claude', defaultCli: 'claude' });
  const modelHubEnabled = useModelHubCapability();
  if (!runtime.loaded) return <p>{t('common.loading')}</p>;
  return <div className="flex flex-col gap-4">
    <BackendRuntimeCard backend="claude" label="Claude Code" description={t('settings.backends.claudeDescription')}
      Icon={Sparkles} iconTileClassName="bg-cyan-soft" iconClassName="text-cyan-ink" runtime={runtime} hideEnableToggle={hideEnableToggle} />
    {modelHubEnabled === true && <BackendSupplyModeCard backend="claude" />}
    <Card><CardContent className="p-6"><BackendConnectionForm backend="claude" /></CardContent></Card>
    <BackendTestPanel backend="claude" />
  </div>;
}
