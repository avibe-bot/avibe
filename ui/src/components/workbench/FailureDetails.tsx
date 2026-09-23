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
import { ChevronDown, ChevronRight } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import type { WorkbenchMessage } from '../../context/ApiContext';
import { isRetryableFailureNotice } from '../../lib/chatMessageTypes';
import { modelsApi } from '../settings/models/modelsApi';
import type { ChainUnavailableReason, Source, TurnProvenance } from '../settings/models/types';
import { CopyButton } from '../ui/copy-button';

type Row = {
  key: string;
  source: string;
  model: string | null;
  status: number | null;
  code: string | null;
  reason: string;
};

type Detail = { record: TurnProvenance; names: Record<string, string> };

const sourceNames = (sources: Source[]): Record<string, string> =>
  Object.fromEntries(sources.map((source) => [source.id, source.display_name]));

// Drawn as the notice bubble's second line. The record is read up front, and
// a turn with no readable record (direct mode, no gateway record, an ambiguous
// attribution, a failed read) offers no details at all rather than a toggle
// that opens onto an apology.
export function FailureDetails({ message }: { message: WorkbenchMessage }) {
  const { t } = useTranslation();
  const [open, setOpen] = React.useState(false);
  const [detail, setDetail] = React.useState<Detail | null>(null);
  const eligible = isRetryableFailureNotice(message);
  const turnId = typeof message.metadata?.turn_id === 'string' ? message.metadata.turn_id : '';

  React.useEffect(() => {
    if (!eligible || !turnId) return;
    let active = true;
    setDetail(null);
    void (async () => {
      try {
        const record = await modelsApi.getTurnProvenance(turnId);
        // Names are a courtesy: a role that cannot read Sources, or a source
        // deleted since, still gets the stable id.
        const names = await modelsApi.listSources().then(sourceNames, () => ({}));
        if (active) setDetail({ record, names });
      } catch {
        // Nothing to show is shown as nothing.
      }
    })();
    return () => { active = false; };
  }, [eligible, turnId]);

  if (!eligible || !turnId || !detail) return null;

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
    const { record, names } = detail;
    const attempts = rows(record, names);
    return (
      <div className="flex flex-col gap-2">
        <p className="text-gold-ink">
          {t(`chat.failureDetails.outcome.${record.outcome}`, { model: record.requested_model_id })}
        </p>
        {attempts.length > 0 && (
          <ol className="flex flex-col gap-1.5">
            {attempts.map((row) => (
              <li key={row.key} className="flex flex-col gap-0.5 rounded-md border border-gold/25 bg-gold/[0.06] px-2.5 py-1.5">
                <span className="font-mono text-gold-ink">{row.source} · {row.model ?? '—'}</span>
                <span className="flex flex-wrap gap-x-3 gap-y-0.5 text-gold-ink/70">
                  <span>{t('chat.failureDetails.httpStatus')}: <span className="font-mono text-gold-ink">{row.status ?? '—'}</span></span>
                  {row.code && <span>{t('chat.failureDetails.errorCode')}: <span className="font-mono text-gold-ink">{row.code}</span></span>}
                  <span>{t('chat.failureDetails.reasonLabel')}: <span className="text-gold-ink">{row.reason}</span></span>
                </span>
              </li>
            ))}
          </ol>
        )}
        {record.blockers.length > 0 && (
          <div className="flex flex-col gap-1">
            <span className="text-gold-ink/70">{t('chat.failureDetails.blockers')}</span>
            <ul className="flex flex-col gap-0.5">
              {record.blockers.map((blocker, index) => (
                <li key={index} className="font-mono text-gold-ink">
                  {names[blocker.source_id] ?? blocker.source_id} · {blocker.model_id}
                  <span className="font-sans text-gold-ink/70"> — {blockerLabel(blocker.reason)}</span>
                </li>
              ))}
            </ul>
          </div>
        )}
        <p className="text-gold-ink/70">{t('chat.failureDetails.upstreamNote')}</p>
        <span className="flex items-center gap-2 text-gold-ink/70">
          <span className="font-mono">{record.turn_id}</span>
          <CopyButton value={JSON.stringify(record, null, 2)} label={t('chat.failureDetails.copyRecord') as string} />
        </span>
      </div>
    );
  })();

  return (
    <>
      <button
        type="button"
        className="mt-0.5 inline-flex items-center gap-0.5 self-start rounded text-[12px] text-gold-ink/80 hover:text-gold-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        {open ? <ChevronDown className="size-3" aria-hidden /> : <ChevronRight className="size-3" aria-hidden />}
        {t(open ? 'chat.failureDetails.hide' : 'chat.failureDetails.show')}
      </button>
      {open && <div className="mt-1.5 text-[12px] leading-relaxed">{body}</div>}
    </>
  );
}
