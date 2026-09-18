import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useApi, type VibeAgentBrief, type VibeAgentUpdatePayload } from '@/context/ApiContext';
import { fetchBackendModels, modelOptionLabel, type BackendModels } from '@/lib/backendModels';
import { resolveEffortOptions } from '@/lib/effortOptions';
import { errorMessage } from '@/lib/errorMessage';
import { Button } from '../ui/button';
import { Combobox } from '../ui/combobox';
import { readOpencodeSetupRoutes } from './opencodeSetupRoutes';

/** An explicit model repair, scoped to the named Agent and existing catalog. */
export function SetupModelRecovery({ agent, onComplete, onCancel }: {
  agent: VibeAgentBrief; onComplete: () => Promise<void>; onCancel: () => void;
}) {
  const api = useApi(); const { t } = useTranslation();
  const [catalog, setCatalog] = useState<BackendModels>({ models: [] });
  const [model, setModel] = useState('');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [attempt, setAttempt] = useState(0);
  const busy = useRef(false);
  useEffect(() => {
    let cancelled = false;
    setLoading(true); setError('');
    void Promise.all([readOpencodeSetupRoutes(api), fetchBackendModels(api, 'opencode')]).then(([routes, models]) => {
      if (cancelled) return;
      if (routes.mode !== 'direct') throw new Error(t('onboarding.connection.modelChanged'));
      setCatalog({ ...models, models: models.models.filter(routes.accepts) });
    }).catch((cause) => { if (!cancelled) setError(errorMessage(cause) || t('onboarding.connection.modelUnavailable')); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [api, attempt, t]);
  const apply = async () => {
    if (busy.current || !catalog.models.includes(model)) return;
    busy.current = true; setSaving(true); setError('');
    try {
      const [routes, fresh] = await Promise.all([readOpencodeSetupRoutes(api), api.getVibeAgent(agent.name, { cache: false })]);
      if (!fresh.ok || !fresh.agent.enabled || fresh.agent.archived || fresh.agent.backend !== 'opencode' || routes.mode !== 'direct' || !routes.accepts(model)) throw new Error(t('onboarding.connection.modelChanged'));
      if (fresh.agent.model !== agent.model && fresh.agent.model !== model) throw new Error(t('onboarding.connection.modelChanged'));
      const patch: VibeAgentUpdatePayload = { model };
      // Only an explicit catalog statement justifies changing existing effort.
      const effort = fresh.agent.reasoning_effort;
      if (effort && catalog.reasoningOptions && Object.prototype.hasOwnProperty.call(catalog.reasoningOptions, model)) {
        const options = resolveEffortOptions('opencode', model, catalog.reasoningOptions);
        if (!options.includes(effort)) patch.reasoning_effort = options.includes('medium') ? 'medium' : options[0] ?? null;
      }
      if (fresh.agent.model !== model) {
        const result = await api.updateVibeAgent(agent.name, patch);
        if (!result.ok) throw new Error(t('onboarding.connection.saveFailed'));
      }
      await onComplete();
    } catch (cause) { setError(errorMessage(cause) || t('onboarding.connection.modelUnavailable')); }
    finally { busy.current = false; setSaving(false); }
  };
  return <section className="onboarding-model-recovery" aria-label={t('onboarding.connection.modelTitle')}>
    <h3>{t('onboarding.connection.modelTitle')}</h3>
    <p>{t('onboarding.connection.modelHint', { agent: agent.display_name || agent.name, model: agent.model || '—' })}</p>
    {error && <p role="alert" className="connection-error">{error}</p>}
    {!loading && !catalog.models.length && <p role="status">{t('onboarding.connection.modelEmpty')}</p>}
    <Combobox options={catalog.models.map((value) => ({ value, label: modelOptionLabel(value, catalog.modelLabels) }))}
      value={model} onValueChange={setModel} disabled={loading || saving} allowCustomValue={false}
      searchPlaceholder={t('onboarding.connection.modelSelect')} emptyText={t('onboarding.connection.modelEmpty')}
      ariaLabel={t('onboarding.connection.modelSelect')} placeholder={t('onboarding.connection.modelSelect')} />
    <div className="flex flex-wrap justify-end gap-2">
      <Button variant="ghost" disabled={saving} onClick={onCancel}>{t('common.cancel')}</Button>
      <Button variant="secondary" disabled={loading || saving} onClick={() => setAttempt((value) => value + 1)}>{t('common.retry')}</Button>
      <Button variant="brand" disabled={loading || saving || !catalog.models.includes(model)} onClick={() => void apply()}>{t('onboarding.connection.modelApply')}</Button>
    </div>
  </section>;
}
