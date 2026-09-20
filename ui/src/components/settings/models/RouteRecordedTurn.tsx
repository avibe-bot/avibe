import * as React from 'react';
import { FileText, RefreshCw } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/components/ui/dialog';
import { modelsApi } from './modelsApi';
import type { AgentBackend, TurnProvenance } from './types';

export function RouteRecordedTurn({ backend, modelId }: { backend: AgentBackend; modelId: string }) {
  const { t, i18n } = useTranslation();
  const [record, setRecord] = React.useState<TurnProvenance | null>(null);
  const [failed, setFailed] = React.useState(false);
  const [attempt, setAttempt] = React.useState(0);
  const [details, setDetails] = React.useState(false);
  React.useEffect(() => {
    let active = true;
    setRecord(null);
    setFailed(false);
    setDetails(false);
    void modelsApi.getAgentProvenance(backend, modelId).then((value) => {
      if (active) setRecord(value);
    }, () => { if (active) setFailed(true); });
    return () => { active = false; };
  }, [backend, modelId, attempt]);
  if (failed) return <div className="model-hub-recorded-turn" role="alert">
    <span>{t('settings.models.routing.historyFailed')}</span>
    <Button variant="ghost" size="sm" onClick={() => setAttempt((value) => value + 1)}><RefreshCw aria-hidden />{t('settings.models.order.retry')}</Button>
  </div>;
  if (!record?.terminal_error || record.agent !== backend || record.requested_model_id !== modelId) return null;
  const error = record.terminal_error;
  const reason = error.upstream_error_code === 'model_not_found'
    ? t('settings.models.routing.modelNotFound')
    : t(`settings.models.routing.errorReason.${error.reason}`);
  return <section className="model-hub-recorded-turn">
    <strong>{t('settings.models.routing.latestRecorded')} · {reason}</strong>
    <time dateTime={record.ts}>{new Date(record.ts).toLocaleString(i18n.language)}</time>
    <span className="font-mono">{error.source_id ?? '—'} · {error.configured_model_id ?? '—'}</span>
    {error.upstream_error_code && <span className="font-mono">{error.upstream_error_code}</span>}
    <Button variant="ghost" size="sm" className="self-start text-cyan-ink" onClick={() => setDetails(true)}><FileText aria-hidden />{t('settings.models.routing.errorDetails')}</Button>
    <Dialog open={details} onOpenChange={setDetails}>
      <DialogContent className="max-h-[85dvh] overflow-y-auto">
        <DialogTitle>{t('settings.models.routing.latestRecorded')}</DialogTitle>
        <DialogDescription>{record.turn_id}</DialogDescription>
        <pre className="whitespace-pre-wrap break-words text-xs">{JSON.stringify(record, null, 2)}</pre>
      </DialogContent>
    </Dialog>
  </section>;
}
