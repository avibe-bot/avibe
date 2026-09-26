import type { TranslationKey } from '@/i18n/types';
import * as React from 'react';
import * as DialogPrimitive from '@radix-ui/react-dialog';
import {
  CircleX,
  LoaderCircle,
  Save,
  TriangleAlert,
  X,
} from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { useToast } from '@/context/ToastContext';
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
import { GuardDialog } from './GuardDialog';
import { GuardImpact, type GuardPlan } from './GuardImpact';
import { confirmGuardPlan, guardedFailure, sendAgreed } from './guardedWrite';
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
import { mayHaveWritten, REPAIR_TOAST, wasBlocked } from './repair';
import { serverText } from './serverCopy';
import {
  type Source,
  type SupplyGap,
} from './types';

type Phase =
  | { kind: 'form' }
  | { kind: 'working' }
  | { kind: 'persist_failure'; messageKey: string | null }
  | { kind: 'save_unconfirmed' };

const INITIAL_PHASE: Phase = { kind: 'form' };

/**
 * A landed replacement is announced in one toast, in the repair verdict's own
 * words and order. Which hops it cost was the guard's question, answered before
 * the write, so the toast says only what is still wrong: an Agent the write
 * stranded, or a source that is still stopped.
 */
const replacementToast = (source: Source, gaps: SupplyGap[] = []) =>
  REPAIR_TOAST[gaps.length > 0 ? 'gaps' : wasBlocked(source.state) ? 'unresolved' : 'repaired'];

type ReplacePhase =
  | { kind: 'edit' }
  | { kind: 'submitting' }
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
  const { showToast } = useToast();
  const [draft, setDraft] = React.useState<ApiKeySourceDraft>(EMPTY_API_KEY_DRAFT);
  const apiKey = draft.apiKey;
  const [revealed, setRevealed] = React.useState(false);
  const [phase, setPhase] = React.useState<Phase>(INITIAL_PHASE);
  const [replacePhase, setReplacePhase] = React.useState<ReplacePhase>({ kind: 'edit' });
  const [guard, setGuard] = React.useState<GuardPlan | null>(null);
  const [continuation] = React.useState(createContinuationSettlement);
  const [createdDelivery] = React.useState(createSourceCreatedDelivery);
  const clientNonce = React.useRef(sourceClientNonce());
  React.useEffect(() => {
    if (addOnAdded) createdDelivery.update(addOnAdded, onClose);
  }, [addOnAdded, createdDelivery, onClose]);

  React.useEffect(() => {
    continuation.invalidate();
    if (open) {
      clientNonce.current = sourceClientNonce();
      setDraft(EMPTY_API_KEY_DRAFT);
      setRevealed(false);
      setPhase(INITIAL_PHASE);
      setReplacePhase({ kind: 'edit' });
      setGuard(null);
    }
    return () => continuation.invalidate();
  }, [continuation, open, replaceSourceId]);

  const publishReplacementEvidence = React.useCallback((
    seq: ContinuationTicket,
    settlement: SourceMutationSettlement,
    source: Source,
    gaps?: SupplyGap[],
  ) => {
    continuation.settle(seq, () => {
      const toast = replacementToast(source, gaps);
      showToast(t(toast.key) as string, toast.tone);
      onClose();
    });
    // Entity settlement applies synchronously; collection reconciliation is
    // trailing work and cannot gate an outcome already established by evidence.
    void settlement.source(source).catch(() => undefined);
  }, [continuation, onClose, showToast, t]);

  const publishReplacementFailure = React.useCallback((
    seq: ContinuationTicket,
    failureClass: ModelHubFailureClass,
    settle?: () => Promise<SourceMutationLanding>,
  ) => {
    continuation.settle(seq, () => {
      setGuard(null);
      setReplacePhase({ kind: 'failure', failureClass });
    });
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

  // `plan` is the guard's question already answered: the write echoes it, and a
  // plan that moved before the forced write lands is resent, not asked again.
  const submitReplacement = React.useCallback(async (plan: GuardPlan | null) => {
    if (props.mode !== 'replace' || !apiKey.trim() || replacePhase.kind === 'submitting') return;
    const key = apiKey.trim();
    const seq = continuation.begin();
    setReplacePhase({ kind: 'submitting' });
    await props.trackMutation(async (latest, settlement) => {
      let sent = plan;
      try {
        const answer = await sendAgreed(plan !== null, plan, (next) => {
          sent = next;
          return modelsApi.replaceCredential(latest.id, next ? { key, ...confirmGuardPlan(next) } : { key });
        });
        publishReplacementEvidence(
          seq,
          settlement,
          answer.source,
          answer.interrupted,
        );
      } catch (error) {
        const failure = apiFailure(error);
        // Only an unconfirmed attempt asks: a confirmed one already resent the
        // moving plan within its bound, and ends as the failure below.
        const refusal = plan === null ? guardedFailure(error) : null;
        if (refusal) {
          settlement.release();
          continuation.settle(seq, () => {
            setGuard(refusal);
            setReplacePhase({ kind: 'edit' });
          });
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
                sent?.gaps,
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
  }, [apiKey, continuation, props, publishReplacementEvidence, publishReplacementFailure, replacePhase.kind]);

  const cancel = React.useCallback(() => {
    if (replaceMode) {
      if (replacePhase.kind === 'submitting') return;
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
  const replaceFieldLocked = replacePhase.kind === 'submitting' || replaceTerminalFailure;
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
                ? t('settings.models.repair.replaceTitle', { name: props.source.display_name })
                : t('settings.models.addKey.title')}
            </DialogPrimitive.Title>
            <Button
              type="button"
              variant="ghost"
              size="icon"
              className="model-hub-ink-59 size-[27px]"
              aria-label={t('settings.models.addKey.cancel')}
              disabled={!canCancel}
              onClick={cancel}
            >
              <X className="size-[15px]" />
            </Button>
          </div>
          <DialogPrimitive.Description className="model-hub-add-key-subtitle model-hub-ink-muted-b3 font-mono">
            {replaceMode
              ? t('settings.models.repair.replaceBody')
              : t('settings.models.addKey.saveFirstHint')}
          </DialogPrimitive.Description>
        </header>

        {replaceMode && (
          <div className="model-hub-add-key-body flex flex-col">
            <ApiKeyField
              value={apiKey}
              revealed={revealed}
              disabled={replaceFieldLocked}
              autoFocus
              label={t('settings.models.repair.replaceLabel')}
              onChange={editKey}
              onToggleReveal={() => setRevealed((value) => !value)}
              onEnter={replaceTerminalFailure ? undefined : () => void submitReplacement(null)}
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
                {t('settings.models.addKey.cancel')}
              </Button>
              {!replaceTerminalFailure && (
                <Button
                  type="button"
                  variant="brand"
                  className="model-hub-add-key-action"
                  disabled={replacePhase.kind === 'submitting' || !apiKey.trim()}
                  onClick={() => void submitReplacement(null)}
                >
                  {replacePhase.kind === 'submitting' && <LoaderCircle className="size-3 animate-spin" />}
                  {t(replacePhase.kind === 'submitting'
                    ? 'settings.models.repair.replacing'
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
        {replaceMode && (
          <GuardDialog
            open={guard !== null}
            title={t('settings.models.guard.title.replaceKey', { source: props.source.display_name })}
            subtitle={t('settings.models.guard.subtitle.replaceKey')}
            confirmLabel={t('settings.models.guard.confirm.replaceKey')}
            busy={replacePhase.kind === 'submitting'}
            onCancel={() => setGuard(null)}
            onConfirm={() => { if (guard) void submitReplacement(guard); }}
          >
            {guard && <GuardImpact hops={guard.hops} gaps={guard.gaps} />}
          </GuardDialog>
        )}
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
};
