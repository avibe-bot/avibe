// The "what actually happened upstream" half of a backend-failure notice.
//
// The notice itself is one sentence of Avibe copy; this reads the Turn's Model
// Hub provenance record (the notice's `turn_id` is the record's key) and lays
// out every attempt the gateway made: which source, which model, the upstream
// HTTP status and machine error code, and why it moved on. That is what lets a
// user see the refusal came from the upstream API and not from the gateway.
//
// Raw upstream response prose is never retained — the record keeps closed
// machine codes and status only, so a credential echoed in an error body can
// never reach the transcript. The details say so rather than implying more.
import * as React from 'react';
import { ChevronDown, ChevronRight, LoaderCircle, RefreshCw } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import type { WorkbenchMessage } from '../../context/ApiContext';
import { isRetryableFailureNotice } from '../../lib/chatMessageTypes';
import { apiFailure, modelsApi } from '../settings/models/modelsApi';
import type { ChainUnavailableReason, Source, TurnProvenance } from '../settings/models/types';
import { Button } from '../ui/button';
import { CopyButton } from '../ui/copy-button';

type Row = {
  key: string;
  source: string;
  model: string | null;
  status: number | null;
  code: string | null;
  reason: string;
};

type Answer =
  | { kind: 'loading' }
  | { kind: 'ready'; record: TurnProvenance; names: Record<string, string> }
  | { kind: 'unavailable'; detail: 'direct_mode' | 'attribution_ambiguous' | 'not_found' }
  | { kind: 'failed' };

const sourceNames = (sources: Source[]): Record<string, string> =>
  Object.fromEntries(sources.map((source) => [source.id, source.display_name]));

export function FailureDetails({ message }: { message: WorkbenchMessage }) {
  const { t } = useTranslation();
  const [open, setOpen] = React.useState(false);
  const [answer, setAnswer] = React.useState<Answer>({ kind: 'loading' });
  const [attempt, setAttempt] = React.useState(0);
  const turnId = typeof message.metadata?.turn_id === 'string' ? message.metadata.turn_id : '';

  React.useEffect(() => {
    if (!open || !turnId) return;
    let active = true;
    setAnswer({ kind: 'loading' });
    void (async () => {
      try {
        const record = await modelsApi.getTurnProvenance(turnId);
        // Names are a courtesy: a role that cannot read Sources, or a source
        // deleted since, still gets the stable id.
        const names = await modelsApi.listSources().then(sourceNames, () => ({}));
        if (active) setAnswer({ kind: 'ready', record, names });
      } catch (error) {
        if (!active) return;
        const failure = apiFailure(error);
        if (failure?.code === 'turn_not_found') setAnswer({ kind: 'unavailable', detail: 'not_found' });
        else if (failure?.code === 'provenance_unavailable') {
          setAnswer({
            kind: 'unavailable',
            detail: failure.detail === 'models.provenance.direct_mode' ? 'direct_mode' : 'attribution_ambiguous',
          });
        } else setAnswer({ kind: 'failed' });
      }
    })();
    return () => { active = false; };
  }, [open, turnId, attempt]);

  if (!isRetryableFailureNotice(message) || !turnId) return null;

  const blockerLabel = (reason: ChainUnavailableReason): string => {
    if (reason === 'native_cli_unavailable') return t('models.probe.native_cli_unavailable');
    if (reason === 'source_missing') return t('chat.failureDetails.reason.source_missing');
    if (reason === 'model_unsupported') return t('chat.failureDetails.reason.model_unsupported');
    return t(reason);
  };

  const rows = (record: TurnProvenance, names: Record<string, string>): Row[] => {
    const name = (id: string | null) => (id ? names[id] ?? id : '—');
    const attempts: Row[] = record.failed_attempts.map((entry, index) => ({
      key: `attempt:${index}`,
      source: name(entry.source_id),
      model: entry.configured_model_id,
      status: entry.http_status ?? null,
      code: null,
      reason: t(`chat.failureDetails.reason.${entry.reason}`, { defaultValue: entry.reason }),
    }));
    const terminal = record.terminal_error;
    if (terminal) {
      attempts.push({
        key: 'terminal',
        source: name(terminal.source_id),
        model: terminal.configured_model_id,
        status: terminal.http_status ?? null,
        code: terminal.upstream_error_code ?? null,
        reason: t(`settings.models.routing.errorReason.${terminal.reason}`),
      });
    }
    return attempts;
  };

  const body = (() => {
    if (answer.kind === 'loading') {
      return <p className="flex items-center gap-1.5 text-muted"><LoaderCircle className="size-3 animate-spin" aria-hidden />{t('chat.failureDetails.loading')}</p>;
    }
    if (answer.kind === 'failed') {
      return (
        <p className="flex items-center gap-2 text-muted" role="alert">
          {t('chat.failureDetails.failed')}
          <Button type="button" variant="ghost" size="sm" onClick={() => setAttempt((value) => value + 1)}>
            <RefreshCw className="size-3" aria-hidden />{t('common.retry')}
          </Button>
        </p>
      );
    }
    if (answer.kind === 'unavailable') return <p className="text-muted">{t(`chat.failureDetails.unavailable.${answer.detail}`)}</p>;
    const { record, names } = answer;
    const attempts = rows(record, names);
    return (
      <div className="flex flex-col gap-2">
        <p className="text-foreground">
          {t(`chat.failureDetails.outcome.${record.outcome}`, { model: record.requested_model_id })}
        </p>
        {attempts.length > 0 && (
          <ol className="flex flex-col gap-1.5">
            {attempts.map((row) => (
              <li key={row.key} className="flex flex-col gap-0.5 rounded-md border border-border bg-background px-2.5 py-1.5">
                <span className="font-mono text-foreground">{row.source} · {row.model ?? '—'}</span>
                <span className="flex flex-wrap gap-x-3 gap-y-0.5 text-muted">
                  <span>{t('chat.failureDetails.httpStatus')}: <span className="font-mono text-foreground">{row.status ?? '—'}</span></span>
                  {row.code && <span>{t('chat.failureDetails.errorCode')}: <span className="font-mono text-foreground">{row.code}</span></span>}
                  <span>{t('chat.failureDetails.reasonLabel')}: <span className="text-foreground">{row.reason}</span></span>
                </span>
              </li>
            ))}
          </ol>
        )}
        {record.blockers.length > 0 && (
          <div className="flex flex-col gap-1">
            <span className="text-muted">{t('chat.failureDetails.blockers')}</span>
            <ul className="flex flex-col gap-0.5">
              {record.blockers.map((blocker, index) => (
                <li key={index} className="font-mono text-foreground">
                  {names[blocker.source_id] ?? blocker.source_id} · {blocker.model_id}
                  <span className="font-sans text-muted"> — {blockerLabel(blocker.reason)}</span>
                </li>
              ))}
            </ul>
          </div>
        )}
        <p className="text-muted">{t('chat.failureDetails.upstreamNote')}</p>
        <span className="flex items-center gap-2 text-muted">
          <span className="font-mono">{record.turn_id}</span>
          <CopyButton value={JSON.stringify(record, null, 2)} label={t('chat.failureDetails.copyRecord') as string} />
        </span>
      </div>
    );
  })();

  return (
    <div className="flex w-full max-w-full flex-col gap-1.5">
      <Button
        type="button"
        size="sm"
        variant="ghost"
        className="self-start"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        {open ? <ChevronDown className="size-3.5" aria-hidden /> : <ChevronRight className="size-3.5" aria-hidden />}
        {t(open ? 'chat.failureDetails.hide' : 'chat.failureDetails.show')}
      </Button>
      {open && <div className="rounded-lg border border-border bg-surface-2/60 px-3 py-2 text-[12px] leading-relaxed">{body}</div>}
    </div>
  );
}
