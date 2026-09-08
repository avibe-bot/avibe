import * as React from 'react';
import * as DialogPrimitive from '@radix-ui/react-dialog';
import {
  CheckCircle2,
  CircleX,
  Eye,
  EyeOff,
  LoaderCircle,
  Save,
  TriangleAlert,
  X,
} from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { Combobox, type ComboboxOption } from '@/components/ui/combobox';
import { Input } from '@/components/ui/input';
import { cn } from '@/lib/utils';
import { API_KEY_VENDOR_PRESETS, apiKeyVendorPreset, CUSTOM_VENDOR } from './apiKeyVendors';
import { classifyModelHubFailure, type ModelHubFailureClass } from './asyncLifetime';
import { Field } from './dialogFields';
import {
  PROTOCOL_COPY_KEYS,
} from './addApiKeyState';
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
  SOURCE_DISPLAY_NAME_MAX_LENGTH,
  SOURCE_PROTOCOLS,
  type ApiKeySourceCreate,
  type RouteHopRef,
  type Source,
  type SourceProtocol,
  type SupplyGap,
} from './types';
import { ProtocolGlyph } from './protocolGlyph';
import { optionalTrimmedTextWithin } from './validation';
import { VendorGlyph } from './vendorGlyph';

type Phase =
  | { kind: 'form' }
  | { kind: 'working' }
  | { kind: 'persist_failure'; messageKey: string | null }
  | { kind: 'save_unconfirmed' };

const INITIAL_PHASE: Phase = { kind: 'form' };
type ProtocolSelection = SourceProtocol;

const ProtocolSegments: React.FC<{
  id?: string;
  disabled: boolean;
  selection: ProtocolSelection;
  onSelect: (value: ProtocolSelection) => void;
}> = ({ id, disabled, selection, onSelect }) => {
  const { t } = useTranslation();
  return (
    <div
      id={id}
      role="group"
      aria-label={t('settings.models.addKey.field.protocol')}
      className="model-hub-add-key-segments flex max-w-full flex-wrap"
    >
      {SOURCE_PROTOCOLS.map((item) => (
        <button
          key={item}
          type="button"
          disabled={disabled}
          aria-pressed={selection === item}
          className={cn(
            'model-hub-add-key-segment',
            selection === item && 'is-selected',
          )}
          onClick={() => onSelect(item)}
        >
          <ProtocolGlyph protocol={item} />
          {t(PROTOCOL_COPY_KEYS[item])}
        </button>
      ))}
    </div>
  );
};

/**
 * The 服务商 field's rows: the shipped catalog in the order the file ships, then
 * the one entry that is not a vendor at all, each carrying its mark.
 *
 * File order because that order is a curated ranking, not an accident of how the
 * rows were appended: the vendors most users are here to add sit at the top, and
 * a name is only what you scan for once the list is long enough to have lost you.
 * Re-sorting here would put the ranking in a second place and make the catalog's
 * own order unobservable — so this reads the file verbatim, and moving a vendor
 * up the list is an edit to `vibe/data/api_key_vendors.json` and to nothing else.
 * 自定义 ranks nowhere: it is the absence of a vendor, so it sits after all of
 * them rather than inside them, while staying the value the field opens on.
 *
 * The mark is why the field is no longer a `<select>`. A vendor is recognised by
 * its logo long before its name is read, and an `<option>` holds text only — so
 * the closed control could only ever show what an option could hold, dropping
 * the mark exactly where the choice has already been made. What replaces it is
 * this app's one picker, `Combobox`, given a mark per row; a second local
 * implementation of the same trigger, panel, and keyboard would only be a place
 * for the two to diverge.
 */
const useVendorOptions = (): ComboboxOption[] => {
  const { t } = useTranslation();
  return React.useMemo(() => ([
    ...API_KEY_VENDOR_PRESETS.map((preset) => ({ value: preset.id, label: preset.label })),
    { value: CUSTOM_VENDOR, label: t('settings.models.addKey.field.vendor.custom') },
  ].map((option) => ({ ...option, icon: <VendorGlyph vendor={option.value} /> }))), [t]);
};

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

const sourceClientNonce = (): string => {
  const uuid = globalThis.crypto.randomUUID?.();
  if (uuid) return `scn_${uuid.replaceAll('-', '').toLowerCase()}`;
  const bytes = globalThis.crypto.getRandomValues(new Uint8Array(16));
  return `scn_${Array.from(bytes, (value) => value.toString(16).padStart(2, '0')).join('')}`;
};

const failureMessageKey = (failure: ReturnType<typeof apiFailure>): string | null =>
  failure?.detail ?? failure?.code ?? null;

const REPLACE_FAILURE_KEY: Record<ModelHubFailureClass, string> = {
  'authoritative-terminal': 'settings.models.repair.replaceFailed',
  inconclusive: 'settings.models.repair.replaceFailed',
  'retryable-provider': 'settings.models.repair.replaceFailed',
};

const ApiKeyField: React.FC<{
  value: string;
  revealed: boolean;
  disabled: boolean;
  label: React.ReactNode;
  autoFocus?: boolean;
  onChange: (value: string) => void;
  onToggleReveal: () => void;
  onEnter?: () => void;
}> = ({ value, revealed, disabled, label, autoFocus, onChange, onToggleReveal, onEnter }) => {
  const { t } = useTranslation();
  return (
    <Field className="model-hub-add-key-field" labelClassName="model-hub-add-key-label" label={label}>
      {(id) => (
        <span className="model-hub-add-key-secret relative flex items-center">
          <Input
            id={id}
            value={value}
            type={revealed ? 'text' : 'password'}
            disabled={disabled}
            autoFocus={autoFocus}
            autoComplete="off"
            spellCheck={false}
            onChange={(event) => onChange(event.target.value)}
            onKeyDown={(event) => {
              if (event.key !== 'Enter' || disabled || !onEnter) return;
              event.preventDefault();
              onEnter();
            }}
            className="model-hub-add-key-input w-full pr-10 font-mono"
          />
          <Button
            type="button"
            variant="ghost"
            size="icon"
            className="model-hub-ink-59 absolute right-1 size-7"
            aria-label={t(`settings.models.addKey.field.apiKey.${revealed ? 'conceal' : 'reveal'}`)}
            disabled={disabled}
            onClick={onToggleReveal}
          >
            {revealed ? <EyeOff className="size-3.5" /> : <Eye className="size-3.5" />}
          </Button>
        </span>
      )}
    </Field>
  );
};

export const AddApiKeyDialog: React.FC<AddApiKeyDialogProps> = (props) => {
  const { open, onClose } = props;
  const replaceMode = props.mode === 'replace';
  const addSourceReads = replaceMode ? null : props.sourceReads;
  const addOnAdded = replaceMode ? null : props.onAdded;
  const replaceSourceId = replaceMode ? props.source.id : null;
  const { t } = useTranslation();
  const vendorOptions = useVendorOptions();
  const [vendor, setVendor] = React.useState<string>(CUSTOM_VENDOR);
  const [displayName, setDisplayName] = React.useState('');
  const [baseUrl, setBaseUrl] = React.useState('');
  const [apiKey, setApiKey] = React.useState('');
  const [protocolSelection, setProtocolSelection] = React.useState<ProtocolSelection>('openai_chat');
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
      setVendor(CUSTOM_VENDOR);
      setDisplayName('');
      setBaseUrl('');
      setApiKey('');
      setProtocolSelection('openai_chat');
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

  const draft = React.useCallback((): ApiKeySourceCreate => ({
    kind: 'api_key', vendor,
    ...(displayName.trim() ? { display_name: displayName.trim() } : {}),
    base_url: baseUrl.trim(), key: apiKey.trim(),
    protocol: apiKeyVendorPreset(vendor)?.protocol ?? protocolSelection,
    client_nonce: clientNonce.current, save_unverified: true,
  }), [apiKey, baseUrl, displayName, protocolSelection, vendor]);

  const persist = React.useCallback(async (seq: ContinuationTicket) => {
    if (continuation.settle(seq, () => setPhase({ kind: 'working' })) === 'stale') return;
    try {
      const created = await modelsApi.createApiKeySource(draft());
      createdDelivery.settle(continuation, seq, created);
    } catch (error) {
      const failure = apiFailure(error);
      const definitive = failure?.serverNamed && failure.responseStatus !== undefined
        && failure.responseStatus >= 400 && failure.responseStatus < 500 && failure.responseStatus !== 409;
      continuation.settle(seq, () => setPhase(definitive
        ? { kind: 'persist_failure', messageKey: failureMessageKey(failure) }
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

  const vendorPreset = apiKeyVendorPreset(vendor);
  const constrainedProtocol = vendorPreset?.protocol ?? protocolSelection;
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

  const editEndpoint = (value: string) => {
    setBaseUrl(value);
    clearSaveFailure();
  };
  const editKey = (value: string) => {
    setApiKey(value);
    if (replaceMode && replacePhase.kind === 'failure') setReplacePhase({ kind: 'edit' });
    clearSaveFailure();
  };
  const editDisplayName = (value: string) => {
    setDisplayName(value);
    if (phase.kind === 'persist_failure') setPhase(INITIAL_PHASE);
  };
  const editProtocol = (value: ProtocolSelection) => {
    setProtocolSelection(value);
    clearSaveFailure();
  };
  // Re-selecting the current vendor must preserve a hand-edited endpoint and
  // protocol. A different vendor starts from that vendor's defaults.
  const editVendor = (value: string) => {
    if (value === vendor) return;
    setVendor(value);
    setBaseUrl(apiKeyVendorPreset(value)?.official_base_url ?? '');
    setProtocolSelection('openai_chat');
    setPhase(INITIAL_PHASE);
  };

  const isWorking = phase.kind === 'working';
  const formLocked = isWorking || phase.kind === 'save_unconfirmed';
  const canCancel = replaceMode ? replacePhase.kind !== 'submitting' : !isWorking;
  const displayNameValid = optionalTrimmedTextWithin(displayName, SOURCE_DISPLAY_NAME_MAX_LENGTH);
  const canSubmit = Boolean(baseUrl.trim() && apiKey.trim()) && displayNameValid && !formLocked;
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
            {/* First, because it is the field the rest are conditioned on: it
                decides what the Base URL starts as and whether the interface is
                still a question. */}
            <Field
              className="model-hub-add-key-field"
              labelClassName="model-hub-add-key-label"
              hintClassName="model-hub-add-key-hint"
              label={t('settings.models.addKey.field.vendor')}
              hint={t('settings.models.addKey.field.vendor.hint')}
            >
              {(id) => (
                <Combobox
                  id={id}
                  // The label points here, but a `for` association contributes
                  // nothing to a button's accessible name, so the field has to
                  // name itself. The primitive appends the selection to what is
                  // passed here: the label alone would replace the trigger's
                  // contents, and those contents are the chosen vendor — the one
                  // thing a picker exists to report.
                  ariaLabel={t('settings.models.addKey.field.vendor')}
                  className="model-hub-add-key-input"
                  options={vendorOptions}
                  value={vendor}
                  onValueChange={editVendor}
                  // A vendor is a catalog row, and the request sends its id: there
                  // is no typed value this field could accept.
                  allowCustomValue={false}
                  disabled={formLocked}
                  searchPlaceholder={t('settings.models.addKey.field.vendor.search')}
                  emptyText={t('settings.models.addKey.field.vendor.empty')}
                />
              )}
            </Field>
            <Field className="model-hub-add-key-field" labelClassName="model-hub-add-key-label" label={t('settings.models.addKey.field.name')}>
              {(id) => <Input id={id} value={displayName} disabled={formLocked} aria-invalid={!displayNameValid} onChange={(event) => editDisplayName(event.target.value)} className="model-hub-add-key-input" />}
            </Field>
            <Field className="model-hub-add-key-field" labelClassName="model-hub-add-key-label" hintClassName="model-hub-add-key-hint" label={t('settings.models.addKey.field.baseUrl')} hint={t('settings.models.addKey.field.baseUrl.hint')}>
              {(id) => <Input id={id} value={baseUrl} disabled={formLocked} autoComplete="url" spellCheck={false} onChange={(event) => editEndpoint(event.target.value)} className="model-hub-add-key-input font-mono" />}
            </Field>
            <ApiKeyField
              value={apiKey}
              revealed={revealed}
              disabled={formLocked}
              label={t('settings.models.addKey.field.apiKey')}
              onChange={editKey}
              onToggleReveal={() => setRevealed((value) => !value)}
            />

            <div className="model-hub-add-key-protocol-area">
              <span className="model-hub-add-key-label">{t('settings.models.addKey.field.protocol')}</span>
              {vendorPreset ? (
                <div className="model-hub-add-key-protocol-idle-row">
                  <span className="model-hub-add-key-protocol-active">
                    <ProtocolGlyph protocol={constrainedProtocol} />{t(PROTOCOL_COPY_KEYS[constrainedProtocol])}
                  </span>
                </div>
              ) : <ProtocolSegments disabled={formLocked} selection={protocolSelection} onSelect={editProtocol} />}
              <p className="model-hub-add-key-hint">{t('settings.models.addKey.protocol.saveFirstHint')}</p>
            </div>
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
