import * as React from 'react';
import { FlaskConical, LoaderCircle } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { Combobox } from '@/components/ui/combobox';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogTitle } from '@/components/ui/dialog';
import { Field } from './dialogFields';
import { apiFailure, modelsApi } from './modelsApi';
import { createContinuationSettlement, type TrackSourceMutation } from './mutationSettlement';
import { foldRegionRead } from './regionRead';
import { serverText } from './serverCopy';
import { selectTestModel } from './testModelPreference';
import type { Source, SourceProbeResult } from './types';

type TestStatus = { kind: 'result'; result: SourceProbeResult } | { kind: 'unconfirmed' } | null;

export const SourceTestDialog: React.FC<{
  source: Source;
  onClose: () => void;
  trackMutation: TrackSourceMutation;
}> = ({ source, onClose, trackMutation }) => {
  const { t } = useTranslation();
  const [selection, setSelection] = React.useState(() => selectTestModel(source.models));
  const [busy, setBusy] = React.useState(false);
  const [status, setStatus] = React.useState<TestStatus>(null);
  const [continuation] = React.useState(createContinuationSettlement);
  const running = React.useRef(false);
  const identity = `${source.id}/${source.credential_ref}/${source.base_url}/${source.protocol}`;
  const selected = selectTestModel(source.models, selection);
  React.useEffect(() => {
    continuation.invalidate();
    setStatus(null);
    return () => { continuation.invalidate(); };
  }, [continuation, identity, selected]);
  React.useEffect(() => {
    if (source.verification_pending) {
      continuation.invalidate();
      setStatus(null);
    }
  }, [continuation, source.verification_pending]);
  const options = source.models.filter((model) => !model.retired).map((model) => ({
    value: model.id, label: model.id,
  }));
  const run = async () => {
    if (!selected || running.current) return;
    running.current = true;
    setBusy(true);
    setStatus(null);
    const ticket = continuation.begin();
    try {
      await trackMutation(async (latest, settlement) => {
        let answer: SourceProbeResult;
        try {
          answer = await modelsApi.probeSource(latest.id, selected);
        } catch (error) {
          if (apiFailure(error)?.code === 'source_not_found') await settlement.gone(latest.id);
          else await settlement.unread();
          continuation.settle(ticket, () => setStatus({ kind: 'unconfirmed' }));
          return;
        }
        // Reconciliation, not an old dialog snapshot, owns the current identity.
        // Its failure must neither publish a verdict nor retry the same read.
        const landing = await settlement.unread();
        const current = landing.verdict === 'landed' ? foldRegionRead<Source[], Source | undefined>(landing.reads.sources, {
          loading: () => undefined,
          ready: (sources) => sources.find((item) => item.id === latest.id),
          unread: () => undefined,
          degraded: () => undefined,
        }) : undefined;
        const matches = current && current.credential_ref === latest.credential_ref
          && current.base_url === latest.base_url && current.protocol === latest.protocol
          && (!current.verification_pending || current.verification_pending === latest.verification_pending)
          && current.models.some((model) => model.id === selected && !model.retired);
        continuation.settle(ticket, () => setStatus(matches
          ? { kind: 'result', result: answer } : { kind: 'unconfirmed' }));
      });
    } catch {
      continuation.settle(ticket, () => setStatus({ kind: 'unconfirmed' }));
    } finally {
      running.current = false;
      setBusy(false);
    }
  };
  const result = status?.kind === 'result' ? status.result : null;
  return (
    <Dialog open onOpenChange={(open) => { if (!open) onClose(); }}>
      <DialogContent closeLabel={t('common.close')}>
        <DialogTitle className="min-w-0 break-words pr-6">{t('settings.models.sourceTest.title', { source: source.display_name })}</DialogTitle>
        <DialogDescription>{t('settings.models.sourceTest.hint')}</DialogDescription>
        <Field label={t('settings.models.sourceTest.model')}>
          {(id) => <Combobox id={id} ariaLabel={t('settings.models.sourceTest.model')} options={options}
            value={selected} disabled={busy || options.length === 0} allowCustomValue={false}
            onValueChange={(value) => { setSelection(value); setStatus(null); }} />}
        </Field>
        {options.length === 0 && <p className="text-sm text-muted">{t('settings.models.sourceTest.empty')}</p>}
        {result && <p role="status" className="min-w-0 break-words text-sm">
          {result.reachable
            ? t('settings.models.sourceTest.success', { model: result.model_id, ms: result.latency_ms })
            : t('settings.models.sourceTest.failure', {
              model: result.model_id,
              reason: serverText(t, result.error, 'settings.models.sourceTest.unknown'),
            })}
        </p>}
        {status?.kind === 'unconfirmed' && <p role="alert" className="text-sm text-destructive-ink">{t('settings.models.sourceTest.requestFailed')}</p>}
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>{t('common.close')}</Button>
          <Button disabled={busy || !selected} onClick={() => void run()}>
            {busy ? <LoaderCircle className="size-4 animate-spin" /> : <FlaskConical className="size-4" />}
            {t(busy ? 'settings.models.sourceTest.running' : 'settings.models.sourceTest.run')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
};
