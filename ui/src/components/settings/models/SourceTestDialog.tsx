import * as React from 'react';
import { FlaskConical, LoaderCircle } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { Combobox } from '@/components/ui/combobox';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogTitle } from '@/components/ui/dialog';
import { Field } from './dialogFields';
import { apiFailure, modelsApi } from './modelsApi';
import { createContinuationSettlement, type TrackSourceMutation } from './mutationSettlement';
import { serverText } from './serverCopy';
import { selectTestModel } from './testModelPreference';
import type { Source, SourceProbeResult } from './types';

export const SourceTestDialog: React.FC<{
  source: Source;
  onClose: () => void;
  trackMutation: TrackSourceMutation;
}> = ({ source, onClose, trackMutation }) => {
  const { t } = useTranslation();
  const [selection, setSelection] = React.useState(() => selectTestModel(source.models));
  const [busy, setBusy] = React.useState(false);
  const [result, setResult] = React.useState<SourceProbeResult | null>(null);
  const [failed, setFailed] = React.useState(false);
  const [continuation] = React.useState(createContinuationSettlement);
  const running = React.useRef(false);
  const identity = `${source.id}/${source.credential_ref}/${source.base_url}/${source.protocol}`;
  const selected = selectTestModel(source.models, selection);
  React.useEffect(() => {
    continuation.invalidate();
    setResult(null);
    setFailed(false);
    return () => { continuation.invalidate(); };
  }, [continuation, identity, selected]);
  React.useEffect(() => {
    if (source.verification_pending) {
      continuation.invalidate();
      setResult(null);
    }
  }, [continuation, source.verification_pending]);
  const options = source.models.filter((model) => !model.retired).map((model) => ({
    value: model.id, label: model.id,
  }));
  const run = async () => {
    if (!selected || running.current) return;
    running.current = true;
    setBusy(true);
    setResult(null);
    setFailed(false);
    const ticket = continuation.begin();
    try {
      await trackMutation(async (latest, settlement) => {
        try {
          const answer = await modelsApi.probeSource(latest.id, selected);
          continuation.settle(ticket, () => setResult(answer));
          // The successful call may have cleared the credential marker. Re-read
          // through the existing mutation owner; never manufacture a Source echo.
          await settlement.unread();
        } catch (error) {
          continuation.settle(ticket, () => setFailed(true));
          if (apiFailure(error)?.code === 'source_not_found') await settlement.gone(latest.id);
          else await settlement.unread();
        }
      });
    } catch {
      continuation.settle(ticket, () => setFailed(true));
    } finally {
      running.current = false;
      setBusy(false);
    }
  };
  return (
    <Dialog open onOpenChange={(open) => { if (!open) onClose(); }}>
      <DialogContent closeLabel={t('common.close')}>
        <DialogTitle className="min-w-0 break-words pr-6">{t('settings.models.sourceTest.title', { source: source.display_name })}</DialogTitle>
        <DialogDescription>{t('settings.models.sourceTest.hint')}</DialogDescription>
        <Field label={t('settings.models.sourceTest.model')}>
          {(id) => <Combobox id={id} ariaLabel={t('settings.models.sourceTest.model')} options={options}
            value={selected} disabled={busy || options.length === 0} allowCustomValue={false}
            onValueChange={(value) => { setSelection(value); setResult(null); setFailed(false); }} />}
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
        {failed && <p role="alert" className="text-sm text-destructive-ink">{t('settings.models.sourceTest.requestFailed')}</p>}
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
