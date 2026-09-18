import { useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useApi } from '@/context/ApiContext';
import { useInstanceAuthorization } from '@/context/InstanceAuthorizationContext';
import { configChanges } from '@/lib/configMutations';
import { errorMessage } from '@/lib/errorMessage';
import { platformText, type PlatformDescriptor } from '@/lib/platforms';
import { PlatformConfigEmbed } from '../settings/PlatformConfigEmbed';
import { savePlatformSettings } from '../settings/shared/savePlatformSettings';
import { Button } from '../ui/button';

export type SavedPlatformRecovery = { config: Record<string, unknown>; descriptor: PlatformDescriptor };

export function SetupPlatformRecovery({ saved, onRepaired, onCancel }: {
  saved: SavedPlatformRecovery; onRepaired: () => Promise<void>; onCancel: () => void;
}) {
  const api = useApi(); const { t } = useTranslation();
  const { capabilities } = useInstanceAuthorization();
  const [editing, setEditing] = useState(false);
  const [error, setError] = useState('');
  const busy = useRef(false);
  const { config, descriptor } = saved;
  const key = descriptor.config_key || descriptor.id;
  const apply = async (next: Record<string, unknown>) => {
    if (busy.current) return;
    busy.current = true; setError('');
    try {
      const result = await api.mutateConfig(configChanges(config[key], next[key], [key]));
      const runtime = result?.platform_runtime;
      if (runtime?.hot_reconciled === false && !runtime.restart_scheduled) throw new Error(runtime.restart_error || runtime.error || t('platform.restartFailed'));
      await savePlatformSettings(api, descriptor.id, next, capabilities.can_manage_access_members);
      await onRepaired();
    } catch (cause) { setError(errorMessage(cause) || t('common.saveFailed')); }
    finally { busy.current = false; }
  };
  return <section className="onboarding-model-recovery" aria-label={t('onboarding.connection.platformRepair')}>
    <p role="alert">{t('onboarding.connection.platformRepairHint', { platform: platformText(t, descriptor.id, 'title', descriptor.title_key) })}</p>
    {error && <p role="alert" className="connection-error">{error}</p>}
    {editing ? <PlatformConfigEmbed platform={descriptor.id} config={config} onApply={apply} onCancel={onCancel} />
      : <div className="flex flex-wrap gap-2"><Button variant="secondary" onClick={() => setEditing(true)}>{t('onboarding.connection.platformRepair')}</Button><Button variant="ghost" onClick={onCancel}>{t('common.cancel')}</Button></div>}
  </section>;
}
