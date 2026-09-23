import type { TranslationKey } from '@/i18n/types';
import * as React from 'react';
import * as DialogPrimitive from '@radix-ui/react-dialog';
import {
  CheckCircle2,
  CircleX,
  LoaderCircle,
  Save,
  TriangleAlert,
  X,
} from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { ApiKeyField, ApiKeySourceForm } from './ApiKeySourceForm';
import {
  apiKeySourceCreate,
  apiKeyWriteSettled,
  draftComplete,
  EMPTY_API_KEY_DRAFT,
  sourceClientNonce,
  type ApiKeySourceDraft,
} from './apiKeySourceDraft';
import { classifyModelHubFailure, type ModelHubFailureClass } from './asyncLifetime';
import type { CollectionReadAuthority } from './collectionReadAuthority';
import { GuardImpact } from './GuardImpact';
import { apiFailure, modelsApi, type SourceCreated } from './modelsApi';
import {
  createContinuationSettlement,
  createSourceCreatedDelivery,
  type ContinuationTicket,
  type SourceMutationLanding,
  type SourceMutationSettlement,
  type TrackSourceMutation,
} from './mutationSettlement';
import { reconcileUnknownWrite } from './reconcileUnknownWrite';
import { mayHaveWritten, REPAIR_LINE_KEY, wasBlocked } from './repair';
import { serverText } from './serverCopy';
import {
  type RouteHopRef,
  type Source,
  type SupplyGap,
} from './types';

type Phase =
  | { kind: 'form' }
  | { kind: 'working' }
  | { kind: 'persist_failure'; messageKey: string | null }
  | { kind: 'save_unconfirmed' };

const INITIAL_PHASE: Phase = { kind: 'form' };

type ReplaceOutcome =
  | { kind: 'repaired' }
  | { kind: 'impact'; hops: RouteHopRef[]; gaps: SupplyGap[] };

const replacementOutcomeFromEvidence = (
  hops: RouteHopRef[] = [],
  gaps: SupplyGap[] = [],
): ReplaceOutcome => {
  if (hops.length > 0 || gaps.length > 0) return { kind: 'impact', hops, gaps };
  return { kind: 'repaired' };
};

type ReplacePhase =
  | { kind: 'edit' }
  | { kind: 'submitting' }
  | { kind: 'guard'; hops: RouteHopRef[]; gaps: SupplyGap[] }
  | { kind: 'done'; outcome: ReplaceOutcome }
  | { kind: 'failure'; failureClass: ModelHubFailureClass };

type AddApiKeyDialogProps = {
  open: boolean;
  onClose: () => void;
} & (
  | {
      mode?: 'add';
      onAdded: (created: SourceCreated) => void;
      sourceReads: CollectionReadAuthority<Source[]>;
    }
  | {
      mode: 'replace';
      source: Source;
      trackMutation: TrackSourceMutation;
    }
);

const failureMessageKey = (failure: ReturnType<typeof apiFailure>): string | null =>
  failure?.detail ?? failure?.code ?? null;

const REPLACE_FAILURE_KEY: Record<ModelHubFailureClass, TranslationKey> = {
  'authoritative-terminal': 'settings.models.repair.replaceFailed',
  inconclusive: 'settings.models.repair.replaceFailed',
  'retryable-provider': 'settings.models.repair.replaceFailed',
};

export const AddApiKeyDialog: React.FC<AddApiKeyDialogProps> = (props) => {
  const { open, onClose } = props;
  const replaceMode = props.mode === 'replace';
  const addSourceReads = replaceMode ? null : props.sourceReads;
  const addOnAdded = replaceMode ? null : props.onAdded;
  const replaceSourceId = replaceMode ? props.source.id : null;
  const { t } = useTranslation();
  const [draft, setDraft] = React.useState<ApiKeySourceDraft>(EMPTY_API_KEY_DRAFT);
  const apiKey = draft.apiKey;
  const [revealed, setRevealed] = React.useState(false);
  const [phase, setPhase] = React.useState<Phase>(INITIAL_PHASE);
  const [replacePhase, setReplacePhase] = React.useState<ReplacePhase>({ kind: 'edit' });
  const [continuation] = React.useState(createContinuationSettlement);
  const [createdDelivery] = React.useState(createSourceCreatedDelivery);
  const clientNonce = React.useRef(sourceClientNonce());
  const replaceCloseTimer = React.useRef<number | null>(null);
  React.useEffect(() => {
    if (addOnAdded) createdDelivery.update(addOnAdded, onClose);
  }, [addOnAdded, createdDelivery, onClose]);

  React.useEffect(() => {
    continuation.invalidate();
    if (replaceCloseTimer.current !== null) {
      window.clearTimeout(replaceCloseTimer.current);
      replaceCloseTimer.current = null;
    }
    if (open) {
      clientNonce.current = sourceClientNonce();
      setDraft(EMPTY_API_KEY_DRAFT);
      setRevealed(false);
      setPhase(INITIAL_PHASE);
      setReplacePhase({ kind: 'edit' });
    }
    return () => {
      continuation.invalidate();
      if (replaceCloseTimer.current !== null) {
        window.clearTimeout(replaceCloseTimer.current);
        replaceCloseTimer.current = null;
      }
    };
  }, [continuation, open, replaceSourceId]);

  const publishReplacementEvidence = React.useCallback((
    seq: ContinuationTicket,
    settlement: SourceMutationSettlement,
    source: Source,
    hops?: RouteHopRef[],
    gaps?: SupplyGap[],
  ) => {
    const outcome = replacementOutcomeFromEvidence(hops, gaps);
    const landed = continuation.settle(seq, () => setReplacePhase({ kind: 'done', outcome }));
    if (landed === 'landed' && outcome.kind === 'repaired') {
      replaceCloseTimer.current = window.setTimeout(onClose, 1400);
    }
    // Entity settlement applies synchronously; collection reconciliation is
    // trailing work and cannot gate an outcome already established by evidence.
    void settlement.source(source).catch(() => undefined);
  }, [continuation, onClose]);

  const publishReplacementFailure = React.useCallback((
    seq: ContinuationTicket,
    failureClass: ModelHubFailureClass,
    settle?: () => Promise<SourceMutationLanding>,
  ) => {
    continuation.settle(seq, () => setReplacePhase({ kind: 'failure', failureClass }));
    if (settle) void settle().catch(() => undefined);
  }, [continuation]);

  const persist = React.useCallback(async (seq: ContinuationTicket) => {
    if (continuation.settle(seq, () => setPhase({ kind: 'working' })) === 'stale') return;
    try {
      const created = await modelsApi.createApiKeySource(apiKeySourceCreate(draft, clientNonce.current));
      createdDelivery.settle(continuation, seq, created);
    } catch (error) {
      continuation.settle(seq, () => setPhase(apiKeyWriteSettled(error)
        ? { kind: 'persist_failure', messageKey: failureMessageKey(apiFailure(error)) }
        : { kind: 'save_unconfirmed' }));
    }
  }, [continuation, createdDelivery, draft]);

  const submitReplacement = React.useCallback(async (force: boolean) => {
    if (props.mode !== 'replace' || !apiKey.trim() || replacePhase.kind === 'submitting') return;
    const confirmation = force && replacePhase.kind === 'guard'
      ? {
          force: true as const,
          would_remove_hops: replacePhase.hops,
          would_interrupt: replacePhase.gaps,
        }
      : null;
    if (force && !confirmation) return;
    const key = apiKey.trim();
    const seq = continuation.begin();
    setReplacePhase({ kind: 'submitting' });
    await props.trackMutation(async (latest, settlement) => {
      try {
        const answer = await modelsApi.replaceCredential(
          latest.id,
          confirmation ? { key, ...confirmation } : { key },
        );
        publishReplacementEvidence(
          seq,
          settlement,
          answer.source,
          answer.removed_hops,
          answer.interrupted,
        );
      } catch (error) {
        const failure = apiFailure(error);
        if (failure && (failure.wouldRemoveHops.length > 0 || failure.wouldInterrupt.length > 0)) {
          settlement.release();
          continuation.settle(seq, () => setReplacePhase({
            kind: 'guard',
            hops: failure.wouldRemoveHops,
            gaps: failure.wouldInterrupt,
          }));
          return;
        }
        let failureClass = classifyModelHubFailure(failure);
        if (failure?.code === 'source_not_found') {
          publishReplacementFailure(
            seq,
            failureClass,
            () => settlement.gone(latest.id),
          );
          return;
        } else if (mayHaveWritten(failure)) {
          try {
            const inventory = await settlement.readInventory();
            const current = inventory.sources.find((source) => source.id === latest.id);
            if (!current) {
              failureClass = 'authoritative-terminal';
              publishReplacementFailure(
                seq,
                failureClass,
                () => settlement.gone(latest.id, inventory),
              );
              return;
            } else if (!wasBlocked(current.state)) {
              publishReplacementEvidence(
                seq,
                settlement,
                current,
                confirmation?.would_remove_hops,
                confirmation?.would_interrupt,
              );
              return;
            }
          } catch {
            publishReplacementFailure(seq, failureClass, settlement.unread);
            return;
          }
        } else settlement.release();
        publishReplacementFailure(seq, failureClass);
      }
    });
  }, [apiKey, continuation, props, publishReplacementEvidence, publishReplacementFailure, replacePhase]);

  const cancel = React.useCallback(() => {
    if (replaceMode) {
      if (replacePhase.kind === 'submitting') return;
      if (replacePhase.kind === 'guard') {
        setReplacePhase({ kind: 'edit' });
        return;
      }
      continuation.invalidate();
      onClose();
      return;
    }
    if (phase.kind === 'working') return;
    continuation.invalidate();
    createdDelivery.close();
  }, [continuation, createdDelivery, onClose, phase, replaceMode, replacePhase.kind]);

  const retry = async () => {
    if (phase.kind === 'save_unconfirmed') {
      if (!addSourceReads) return;
      const seq = continuation.begin();
      const reconciliation = await reconcileUnknownWrite(
        () => addSourceReads.readValue(),
        (sources) => sources.find((source) => source.client_nonce === clientNonce.current),
      );
      if (reconciliation.kind === 'committed') {
        createdDelivery.settle(continuation, seq, {
          source: reconciliation.value,
          added_to: [],
          adopted_by: reconciliation.value.adopted_by ?? [],
        });
        return;
      }
      if (reconciliation.kind === 'absent') {
        await persist(seq);
      }
      return;
    }
    if (phase.kind === 'persist_failure') await persist(continuation.begin());
  };

  const clearSaveFailure = () => {
    setPhase((current) => current.kind === 'persist_failure' ? INITIAL_PHASE : current);
  };

  const editKey = (value: string) => {
    setDraft((current) => ({ ...current, apiKey: value }));
    if (replaceMode && replacePhase.kind === 'failure') setReplacePhase({ kind: 'edit' });
    clearSaveFailure();
  };
  // A vendor change resets the phase outright rather than only clearing a save
  // failure: it restates what is being connected to, so an unsettled outcome
  // from the previous one no longer describes what the form holds.
  const editDraft = (next: ApiKeySourceDraft, field: keyof ApiKeySourceDraft) => {
    setDraft(next);
    if (field === 'vendor') setPhase(INITIAL_PHASE);
    else clearSaveFailure();
  };

  const isWorking = phase.kind === 'working';
  const formLocked = isWorking || phase.kind === 'save_unconfirmed';
  const canCancel = replaceMode ? replacePhase.kind !== 'submitting' : !isWorking;
  const canSubmit = draftComplete(draft) && !formLocked;
  const replaceTerminalFailure = replacePhase.kind === 'failure'
    && replacePhase.failureClass === 'authoritative-terminal';
  const replaceFieldLocked = replacePhase.kind === 'submitting'
    || replacePhase.kind === 'done'
    || replaceTerminalFailure;
  return (
    <DialogPrimitive.Root open={open} onOpenChange={(next) => !next && canCancel && cancel()}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="model-hub-add-key-overlay fixed inset-0 z-50" />
        <DialogPrimitive.Content
          className="model-hub-add-key-dialog fixed left-1/2 top-1/2 z-50 flex max-h-[calc(100dvh-2rem)] -translate-x-1/2 -translate-y-1/2 flex-col gap-0 overflow-hidden border border-border-strong bg-surface p-0 shadow-xl outline-none"
          onEscapeKeyDown={(event) => { if (!canCancel) event.preventDefault(); }}
          onPointerDownOutside={(event) => { if (!canCancel) event.preventDefault(); }}
        >
        <header className="model-hub-add-key-head flex flex-col border-b border-border px-5 py-4">
          <div className="flex items-center justify-between gap-3">
            <DialogPrimitive.Title className="model-hub-add-key-title font-bold text-foreground">
              {replaceMode
                ? replacePhase.kind === 'guard'
                  ? t('settings.models.guard.title.replaceKey', { source: props.source.display_name })
                  : t('settings.models.repair.replaceTitle', { name: props.source.display_name })
                : t('settings.models.addKey.title')}
            </DialogPrimitive.Title>
            <Button
              type="button"
              variant="ghost"
              size="icon"
              className="model-hub-ink-59 size-[27px]"
              aria-label={t(replacePhase.kind === 'guard' ? 'settings.models.guard.cancel' : 'settings.models.addKey.cancel')}
              disabled={!canCancel}
              onClick={cancel}
            >
              <X className="size-[15px]" />
            </Button>
          </div>
          <DialogPrimitive.Description className="model-hub-add-key-subtitle model-hub-ink-muted-b3 font-mono">
            {replaceMode
              ? replacePhase.kind === 'guard'
                ? t('settings.models.guard.subtitle.replaceKey')
                : t('settings.models.repair.replaceBody')
              : t('settings.models.addKey.saveFirstHint')}
          </DialogPrimitive.Description>
        </header>

        {replaceMode && replacePhase.kind === 'guard' && (
          <div className="model-hub-guard-body">
            <GuardImpact hops={replacePhase.hops} gaps={replacePhase.gaps} />
          </div>
        )}

        {replaceMode && replacePhase.kind !== 'guard' && (
          <div className="model-hub-add-key-body flex flex-col">
            <ApiKeyField
              value={apiKey}
              revealed={revealed}
              disabled={replaceFieldLocked}
              autoFocus
              label={t('settings.models.repair.replaceLabel')}
              onChange={editKey}
              onToggleReveal={() => setRevealed((value) => !value)}
              onEnter={replaceTerminalFailure ? undefined : () => void submitReplacement(false)}
            />
            {replacePhase.kind === 'submitting' && (
              <div className="model-hub-add-key-strip model-hub-add-key-strip--working">
                <LoaderCircle className="model-hub-ink-mint size-3.5 shrink-0 animate-spin" />
                <span className="model-hub-add-key-strip-title text-foreground">{t('settings.models.repair.replacing')}</span>
              </div>
            )}
            {replacePhase.kind === 'failure' && (
              <div
                className="model-hub-add-key-strip model-hub-add-key-strip--error"
                data-failure-class={replacePhase.failureClass}
              >
                <TriangleAlert className="model-hub-add-key-error-ink size-3.5 shrink-0" />
                <span className="model-hub-add-key-error-ink model-hub-add-key-strip-title">
                  {t(REPLACE_FAILURE_KEY[replacePhase.failureClass])}
                </span>
              </div>
            )}
            {replacePhase.kind === 'done' && replacePhase.outcome.kind === 'impact' && (
              <div className="flex flex-col gap-2 rounded-lg border border-gold/40 bg-gold/[0.08] px-3.5 py-3">
                <span className="model-hub-ink-gold text-[12.5px] font-semibold leading-relaxed">
                  {t('settings.models.repair.refreshed')}
                </span>
                <GuardImpact
                  hops={replacePhase.outcome.hops}
                  gaps={replacePhase.outcome.gaps}
                  committed
                />
              </div>
            )}
            {replacePhase.kind === 'done' && replacePhase.outcome.kind === 'repaired' && (
              <div className="model-hub-ink-mint flex items-center gap-2 rounded-lg border border-mint/30 bg-mint-soft/50 px-4 py-3 text-[13px] font-medium">
                <CheckCircle2 className="size-4 shrink-0" />
                {t(REPAIR_LINE_KEY.repaired)}
              </div>
            )}
          </div>
        )}

        {!replaceMode && (
          <div className="model-hub-add-key-body flex flex-col">
            <ApiKeySourceForm
              draft={draft}
              disabled={formLocked}
              revealed={revealed}
              onChange={editDraft}
              onToggleReveal={() => setRevealed((value) => !value)}
            />
            {isWorking && <div className="model-hub-add-key-strip model-hub-add-key-strip--working">
              <LoaderCircle className="model-hub-ink-mint size-3.5 animate-spin" />
              <span className="model-hub-add-key-strip-title">{t('settings.models.addKey.saving')}</span>
            </div>}
            {phase.kind === 'save_unconfirmed' && (
              <div className="model-hub-add-key-strip model-hub-add-key-strip--error">
                <CircleX className="model-hub-add-key-error-ink size-3.5 shrink-0" />
                <span className="model-hub-add-key-error-ink model-hub-add-key-strip-title">{t('settings.models.addKey.fail.save')}</span>
              </div>
            )}
            {phase.kind === 'persist_failure' && (
              <div className="model-hub-add-key-strip model-hub-add-key-strip--error">
                <CircleX className="model-hub-add-key-error-ink size-3.5 shrink-0" />
                <span className="model-hub-add-key-error-ink model-hub-add-key-strip-title">
                  {serverText(t, phase.messageKey, 'settings.models.addKey.fail.unclassified')
                    ?? t('settings.models.addKey.fail.unclassified')}
                </span>
              </div>
            )}
          </div>
        )}

        <footer className="model-hub-add-key-foot model-hub-fill-05 flex flex-row flex-wrap items-center justify-end border-t border-border">
          {replaceMode ? (
            <>
              <Button
                type="button"
                variant="outline"
                className="model-hub-add-key-action"
                disabled={!canCancel}
                onClick={cancel}
              >
                {t(replacePhase.kind === 'done'
                  ? 'common.close'
                  : replacePhase.kind === 'guard'
                    ? 'settings.models.guard.cancel'
                    : 'settings.models.addKey.cancel')}
              </Button>
              {replacePhase.kind !== 'done' && !replaceTerminalFailure && (
                <Button
                  type="button"
                  variant={replacePhase.kind === 'guard' ? 'destructive' : 'brand'}
                  className="model-hub-add-key-action"
                  disabled={replacePhase.kind === 'submitting' || !apiKey.trim()}
                  onClick={() => void submitReplacement(replacePhase.kind === 'guard')}
                >
                  {replacePhase.kind === 'submitting' && <LoaderCircle className="size-3 animate-spin" />}
                  {t(replacePhase.kind === 'submitting'
                    ? 'settings.models.repair.replacing'
                    : replacePhase.kind === 'guard'
                      ? 'settings.models.guard.confirm.replaceKey'
                      : replacePhase.kind === 'failure'
                        ? 'settings.models.addKey.retry'
                        : 'settings.models.repair.replaceSubmit')}
                </Button>
              )}
            </>
          ) : (
            <>
              <Button
                type="button"
                variant="outline"
                className="model-hub-add-key-action"
                disabled={!canCancel}
                onClick={cancel}
              >
                {t('settings.models.addKey.cancel')}
              </Button>
              <Button type="button" variant="brand" className="model-hub-add-key-action"
                disabled={isWorking || (phase.kind !== 'save_unconfirmed' && !canSubmit)}
                onClick={() => void (phase.kind === 'save_unconfirmed' || phase.kind === 'persist_failure'
                  ? retry() : persist(continuation.begin()))}>
                {isWorking ? <LoaderCircle className="size-3 animate-spin" /> : <Save className="size-3" />}
                {t(isWorking ? 'settings.models.addKey.saving'
                  : phase.kind === 'form' ? 'settings.models.addKey.confirm' : 'settings.models.addKey.retry')}
              </Button>
            </>
          )}
        </footer>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
};
