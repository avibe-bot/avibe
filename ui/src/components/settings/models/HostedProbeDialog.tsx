// 检测可用工具 — the confirm and result dialogs (design.pen frame vQPXo).
//
// A check sends real, cost-bearing requests, so it runs only after the user has
// seen what will be sent and what it is estimated to cost. Probes run one at a
// time; only verified and unsupported become evidence, so a probe that did not
// get an answer is reported as "not verified", never as "not supported".
import * as React from 'react';
import { Trans, useTranslation } from 'react-i18next';
import { AlertCircle, AtSign, Check, Globe, Image as ImageIcon, Loader2, RotateCw, ShieldCheck } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/components/ui/dialog';
import { cn } from '@/lib/utils';
import { probeKey, probeTotal, probeUnanswered, type ProbeTarget } from './hostedTools';
import { serverText } from './serverCopy';
import type { HostedToolName, HostedToolProbeResult, Source } from './types';
import { useEstimateLabel } from './useEstimateLabel';

const TOOL_ICON: Record<HostedToolName, React.ComponentType<{ className?: string }>> = {
  web_search: Globe,
  x_search: AtSign,
  image_generation: ImageIcon,
};

type Outcome = HostedToolProbeResult['call']['outcome'] | 'failed';

const outcomeOf = (result: HostedToolProbeResult | null): Outcome => result?.call.outcome ?? 'failed';

const TargetLine: React.FC<{ target: ProbeTarget; sources: ReadonlyMap<string, Source>; trailing: React.ReactNode }> = ({ target, sources, trailing }) => {
  const { t } = useTranslation();
  const Icon = TOOL_ICON[target.tool];
  return (
    <li className="model-hub-probe-row flex items-center gap-2.5 rounded-lg border border-border bg-background" data-probe={probeKey(target)}>
      <Icon className="model-hub-ink-mint size-[14px] shrink-0" aria-hidden />
      {/* The pair reads as the locale composes it; each part keeps its own style. */}
      <Trans
        i18nKey="settings.models.tools.dialog.pair"
        values={{
          tool: t(`settings.models.tools.tool.${target.tool}.name`),
          source: sources.get(target.candidate.source_id)?.display_name ?? target.candidate.source_id,
          model: target.candidate.model_id,
        }}
        components={{
          tool: <span className="shrink-0 font-semibold text-foreground" />,
          source: <span className="min-w-0 truncate text-foreground" />,
          model: <span className="min-w-0 truncate font-mono text-muted" />,
          muted: <span className="text-muted" />,
        }}
      />
      <span className="ml-auto shrink-0">{trailing}</span>
    </li>
  );
};

const OutcomeChip: React.FC<{ outcome: Outcome }> = ({ outcome }) => {
  const { t } = useTranslation();
  if (outcome === 'verified') {
    return <span className="model-hub-pill model-hub-status-mint gap-1 border"><Check className="size-3" aria-hidden />{t('settings.models.tools.result.outcome.verified')}</span>;
  }
  if (outcome === 'unsupported') {
    return <span className="model-hub-pill model-hub-fill-0a gap-1 border border-border text-muted">{t('settings.models.tools.result.outcome.unsupported')}</span>;
  }
  return <span className="model-hub-pill model-hub-status-gold gap-1 border"><AlertCircle className="size-3" aria-hidden />{t('settings.models.tools.result.outcome.unverified')}</span>;
};

/**
 * What a probe that became no evidence says where it was asked (a Tools row, a
 * Source chip): a request that failed says why, and one that completed without
 * the tool says that. Nothing for an answer that is evidence.
 */
export const ProbeAnswerNote: React.FC<{ result: HostedToolProbeResult | null }> = ({ result }) => {
  const { t } = useTranslation();
  if (!probeUnanswered(result)) return null;
  const failure = serverText(t, result?.call.error);
  return (
    <span className="model-hub-tool-cost-note flex items-center gap-1 model-hub-ink-gold" role="status">
      <AlertCircle className="size-3 shrink-0" aria-hidden />
      {failure
        ? t('settings.models.tools.probe.outcome.transientReason', { reason: failure })
        : result?.call.outcome === 'inconclusive'
          ? t('settings.models.tools.probe.outcome.inconclusive')
          : t('settings.models.tools.probe.outcome.transient')}
    </span>
  );
};

/** Mounted for one check: the set it shows is the one it was opened with. */
export const HostedProbeDialog: React.FC<{
  targets: readonly ProbeTarget[];
  sources: ReadonlyMap<string, Source>;
  onRun: (target: ProbeTarget) => Promise<HostedToolProbeResult | null>;
  onClose: () => void;
}> = ({ targets, sources, onRun, onClose }) => {
  const { t } = useTranslation();
  const estimateLabel = useEstimateLabel();
  // The set is fixed when the dialog opens: the read refreshes under it as probes land.
  const [planned, setPlanned] = React.useState<readonly ProbeTarget[]>(targets);
  const [phase, setPhase] = React.useState<'confirm' | 'running' | 'result'>('confirm');
  const [results, setResults] = React.useState<ReadonlyMap<string, HostedToolProbeResult | null>>(() => new Map());

  const run = async (batch: readonly ProbeTarget[]) => {
    setPhase('running');
    for (const target of batch) {
      const result = await onRun(target);
      setResults((previous) => new Map(previous).set(probeKey(target), result));
    }
    setPhase('result');
  };

  const total = probeTotal(planned);
  const totalLabel = estimateLabel(total);
  const outcomes = planned.map((target) => outcomeOf(results.get(probeKey(target)) ?? null));
  const unverified = planned.filter((target) => results.has(probeKey(target)) && probeUnanswered(results.get(probeKey(target)) ?? null));
  const count = (wanted: (outcome: Outcome) => boolean) => outcomes.filter(wanted).length;
  // A retry is as paid as the first run, so it says its cost the same way.
  const retryLabel = estimateLabel(probeTotal(unverified));
  const done = results.size;

  return (
    <Dialog open onOpenChange={(next) => { if (!next && phase !== 'running') onClose(); }}>
      <DialogContent className="model-hub-probe-dialog flex max-h-[calc(100dvh-2rem)] flex-col gap-0 overflow-hidden p-0 sm:max-w-[560px]">
        <div className="model-hub-probe-head border-b border-border">
          <DialogTitle className="font-bold text-foreground">
            {t(phase === 'result' ? 'settings.models.tools.result.title' : 'settings.models.tools.dialog.title')}
          </DialogTitle>
          <DialogDescription className="text-muted">{t('settings.models.tools.dialog.subtitle')}</DialogDescription>
        </div>
        <div className="model-hub-probe-body flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto">
          {phase === 'confirm' && (
            <>
              <p className="text-foreground">{t('settings.models.tools.dialog.body', { count: planned.length })}</p>
              <p className="model-hub-probe-privacy flex items-start gap-2 rounded-lg">
                <ShieldCheck className="model-hub-ink-mint mt-px size-[14px] shrink-0" aria-hidden />
                <span>{t('settings.models.tools.dialog.privacy')}</span>
              </p>
              <span className="model-hub-probe-label">{t('settings.models.tools.dialog.listTitle')}</span>
              <ul className="flex flex-col gap-1.5">
                {planned.map((target) => (
                  <TargetLine key={probeKey(target)} target={target} sources={sources} trailing={<span className="font-semibold text-foreground">{estimateLabel(target.candidate.probe_estimate) ?? t('settings.models.tools.probe.unknown')}</span>} />
                ))}
              </ul>
              <div className="model-hub-probe-total flex items-center justify-between gap-3">
                <span className="flex flex-col">
                  <span className="font-semibold text-foreground">{t('settings.models.tools.dialog.total')}</span>
                  <span className="text-muted">{t('settings.models.tools.dialog.totalNote')}</span>
                </span>
                <span className="font-bold text-foreground">{totalLabel ?? t('settings.models.tools.probe.unknown')}</span>
              </div>
            </>
          )}
          {phase !== 'confirm' && (
            <>
              {phase === 'result' && (
                <span className="flex flex-wrap gap-1.5" role="status">
                  {count((outcome) => outcome === 'verified') > 0 && <span className="model-hub-pill model-hub-status-mint gap-1 border"><Check className="size-3" aria-hidden />{t('settings.models.tools.result.verified', { count: count((outcome) => outcome === 'verified') })}</span>}
                  {count((outcome) => outcome === 'unsupported') > 0 && <span className="model-hub-pill model-hub-fill-0a border border-border text-muted">{t('settings.models.tools.result.unsupported', { count: count((outcome) => outcome === 'unsupported') })}</span>}
                  {unverified.length > 0 && <span className="model-hub-pill model-hub-status-gold gap-1 border"><AlertCircle className="size-3" aria-hidden />{t('settings.models.tools.result.unverified', { count: unverified.length })}</span>}
                </span>
              )}
              {phase === 'running' && <p className="flex items-center gap-2 text-muted" role="status"><Loader2 className="size-3.5 animate-spin" aria-hidden />{t('settings.models.tools.dialog.running', { done, count: planned.length })}</p>}
              <ul className="flex flex-col gap-1.5">
                {planned.map((target, index) => {
                  const key = probeKey(target);
                  const result = results.get(key);
                  // A request that failed says why; one that completed without the tool says that.
                  const failure = serverText(t, result?.call.error);
                  const reason = outcomes[index] === 'verified' || outcomes[index] === 'unsupported'
                    ? null
                    : outcomes[index] === 'inconclusive' && !failure
                      ? t('settings.models.tools.result.reason.inconclusive')
                      : failure
                        ? t('settings.models.tools.result.reason.transientReason', { reason: failure })
                        : t('settings.models.tools.result.reason.transient');
                  return (
                    <React.Fragment key={key}>
                      <TargetLine
                        target={target}
                        sources={sources}
                        trailing={results.has(key)
                          ? <OutcomeChip outcome={outcomes[index]} />
                          : <Loader2 className="size-3.5 animate-spin text-muted" aria-label={t('settings.models.tools.probe.running') as string} />}
                      />
                      {results.has(key) && reason && <p className="model-hub-probe-reason">{reason}</p>}
                    </React.Fragment>
                  );
                })}
              </ul>
              {phase === 'result' && <p className="text-muted">{t('settings.models.tools.result.note')}</p>}
            </>
          )}
        </div>
        <div className={cn('model-hub-probe-foot flex items-center gap-2 border-t border-border', phase === 'result' && unverified.length > 0 ? 'justify-between' : 'justify-end')}>
          {phase === 'confirm' && (
            <>
              <Button variant="outline" size="sm" onClick={onClose}>{t('settings.models.tools.dialog.cancel')}</Button>
              <Button size="sm" disabled={planned.length === 0} onClick={() => void run(planned)}>
                {totalLabel ? t('settings.models.tools.dialog.startCost', { cost: totalLabel }) : t('settings.models.tools.dialog.start')}
              </Button>
            </>
          )}
          {phase === 'result' && (
            <>
              {unverified.length > 0 && (
                <Button variant="outline" size="sm" onClick={() => {
                  setPlanned(unverified);
                  setResults(new Map());
                  void run(unverified);
                }}>
                  <RotateCw className="size-3.5" aria-hidden />
                  {t('settings.models.tools.result.retryCost', { count: unverified.length, cost: retryLabel ?? t('settings.models.tools.probe.unknown') })}
                </Button>
              )}
              <Button size="sm" onClick={onClose}>{t('settings.models.tools.result.done')}</Button>
            </>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
};
