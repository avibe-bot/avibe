import * as React from 'react';
import * as DialogPrimitive from '@radix-ui/react-dialog';
import {
  CheckCircle2,
  ChevronDown,
  CircleX,
  Eye,
  EyeOff,
  Info,
  LoaderCircle,
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
  classifyObservation,
  isAbortError,
  PROTOCOL_COPY_KEYS,
  type AddApiKeyFailure,
  type AddApiKeyOrigin,
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
  type SourceObservation,
  type SourceProtocol,
  type SupplyGap,
} from './types';
import { ProtocolGlyph } from './protocolGlyph';
import { optionalTrimmedTextWithin } from './validation';
import { VendorGlyph } from './vendorGlyph';

type Phase =
  | { kind: 'form'; report: SourceObservation | null }
  | { kind: 'working'; origin: AddApiKeyOrigin; stage: 'observe' | 'persist' }
  | { kind: 'failure'; origin: AddApiKeyOrigin; cause: AddApiKeyFailure }
  | { kind: 'undetermined'; origin: AddApiKeyOrigin; observation: SourceObservation }
  | { kind: 'inventory'; origin: AddApiKeyOrigin; observation: SourceObservation }
  | {
      kind: 'persist_failure';
      messageKey: string | null;
      protocol: SourceProtocol | undefined;
      acceptUnavailableInventory: boolean;
    }
  | {
      kind: 'save_unconfirmed';
      protocol: SourceProtocol | undefined;
      acceptUnavailableInventory: boolean;
    };

const INITIAL_PHASE: Phase = { kind: 'form', report: null };
type ProtocolSelection = 'auto' | SourceProtocol;

// A phase whose primary commits WITHOUT observing again is carrying evidence
// that only this endpoint, this credential and this probe constraint produced:
// ①″'s report, ⑤'s waived inventory, and the protocol a refused or unanswered
// create already proved. That is the property, not a list of the states that
// happen to be reachable today — a phase added later inherits the rule from how
// its own exit behaves. Phases whose primary re-observes (③ / ④ / ⑤'s 重试, ⑥)
// are deliberately absent: their retry reads the fields as they now stand, and
// §0.8 keeps the form intact across it.
const persistsWithoutObserving = (phase: Phase): boolean =>
  (phase.kind === 'form' && phase.report !== null)
  || phase.kind === 'inventory'
  || phase.kind === 'persist_failure'
  || phase.kind === 'save_unconfirmed';

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
      {(['auto', ...SOURCE_PROTOCOLS] as const).map((item) => (
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
          {item !== 'auto' && <ProtocolGlyph protocol={item} />}
          {t(item === 'auto'
            ? 'settings.models.addKey.protocol.auto'
            : PROTOCOL_COPY_KEYS[item])}
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

const failureCopy = (cause: AddApiKeyFailure): string => {
  switch (cause) {
    case 'auth': return 'settings.models.addKey.fail.auth';
    case 'network': return 'settings.models.addKey.fail.network';
    case 'interface': return 'settings.models.addKey.fail.undetermined';
    case 'engineDown': return 'settings.models.addKey.fail.engineDown';
    case 'unclassified': return 'settings.models.addKey.fail.unclassified';
  }
};

const observationFailureCopy = (observation: SourceObservation | undefined): string | null => {
  if (!observation) return null;
  if (observation.outcome === 'authentication_failed') return 'settings.models.addKey.fail.auth';
  if (observation.outcome === 'unreachable' || observation.outcome === 'timeout') return 'settings.models.addKey.fail.network';
  if (observation.outcome === 'ambiguous') return 'settings.models.addKey.fail.undetermined';
  if (observation.outcome === 'observed' && observation.discovery === 'failed') return 'settings.models.addKey.fail.inventory';
  return 'settings.models.addKey.fail.unclassified';
};

const failureMessageKey = (failure: ReturnType<typeof apiFailure>): string | null => {
  if (!failure) return null;
  const observationKey = observationFailureCopy(failure.observation);
  if (observationKey) return observationKey;
  if (failure.detail?.startsWith('modelHub.errors.')) return failure.detail;
  return failure.code || null;
};

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
  const [protocolSelection, setProtocolSelection] = React.useState<ProtocolSelection>('auto');
  const [revealed, setRevealed] = React.useState(false);
  const [manualOpen, setManualOpen] = React.useState(false);
  const [phase, setPhase] = React.useState<Phase>(INITIAL_PHASE);
  const [replacePhase, setReplacePhase] = React.useState<ReplacePhase>({ kind: 'edit' });
  const [continuation] = React.useState(createContinuationSettlement);
  const [createdDelivery] = React.useState(createSourceCreatedDelivery);
  const clientNonce = React.useRef(sourceClientNonce());
  const observationAbort = React.useRef<AbortController | null>(null);
  const replaceCloseTimer = React.useRef<number | null>(null);
  React.useEffect(() => {
    if (addOnAdded) createdDelivery.update(addOnAdded, onClose);
  }, [addOnAdded, createdDelivery, onClose]);

  React.useEffect(() => {
    continuation.invalidate();
    observationAbort.current?.abort();
    observationAbort.current = null;
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
      setProtocolSelection('auto');
      setRevealed(false);
      setManualOpen(false);
      setPhase(INITIAL_PHASE);
      setReplacePhase({ kind: 'edit' });
    }
    return () => {
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

  const draft = React.useCallback((
    protocol?: SourceProtocol,
    acceptUnavailableInventory = false,
  ): ApiKeySourceCreate => ({
    kind: 'api_key',
    vendor,
    // The name is left out when empty rather than filled in here: the server
    // already names a catalog source after its catalog label, and a second
    // owner of that default is a second thing to keep in step with the catalog.
    ...(displayName.trim() ? { display_name: displayName.trim() } : {}),
    base_url: baseUrl.trim(),
    key: apiKey.trim(),
    client_nonce: clientNonce.current,
    ...(protocol ? { protocol } : {}),
    ...(acceptUnavailableInventory ? { accept_unavailable_inventory: true } : {}),
  }), [apiKey, baseUrl, displayName, vendor]);

  const persist = React.useCallback(async (
    seq: ContinuationTicket,
    protocol?: SourceProtocol,
    acceptUnavailableInventory = false,
  ) => {
    if (continuation.settle(seq, () => setPhase({ kind: 'working', origin: 'add', stage: 'persist' })) === 'stale') return;
    try {
      const created = await modelsApi.createApiKeySource(draft(protocol, acceptUnavailableInventory));
      createdDelivery.settle(continuation, seq, created);
    } catch (error) {
      const failure = apiFailure(error);
      const definitiveClientFailure = failure?.serverNamed
        && failure.responseStatus !== undefined
        && failure.responseStatus >= 400
        && failure.responseStatus < 500
        && failure.responseStatus !== 409;
      const verdict = failure?.observation ? classifyObservation(failure.observation) : null;
      continuation.settle(seq, () => {
        if (definitiveClientFailure && verdict && verdict.kind !== 'ready') {
          if (verdict.kind === 'undetermined') {
            setPhase({ kind: 'undetermined', origin: 'add', observation: verdict.observation });
          } else if (verdict.kind === 'inventory') {
            setPhase({ kind: 'inventory', origin: 'add', observation: verdict.observation });
          } else {
            setPhase({ kind: 'failure', origin: 'add', cause: verdict.cause });
          }
          return;
        }
        setPhase(definitiveClientFailure
          ? {
              kind: 'persist_failure',
              messageKey: failureMessageKey(failure),
              protocol,
              acceptUnavailableInventory,
            }
          : { kind: 'save_unconfirmed', protocol, acceptUnavailableInventory });
      });
    }
  }, [continuation, createdDelivery, draft]);

  const observe = React.useCallback(async (
    origin: AddApiKeyOrigin,
    protocol?: SourceProtocol,
  ) => {
    const seq = continuation.begin();
    observationAbort.current?.abort();
    const controller = new AbortController();
    observationAbort.current = controller;
    setPhase({ kind: 'working', origin, stage: 'observe' });
    try {
      const observation = await modelsApi.observeApiKeySource({
        vendor,
        base_url: baseUrl.trim(),
        key: apiKey.trim(),
        ...(protocol ? { protocol } : {}),
      }, controller.signal);
      const verdict = classifyObservation(observation);
      continuation.settle(seq, () => {
        observationAbort.current = null;
        if (verdict.kind === 'ready') {
          setPhase({ kind: 'form', report: observation });
        } else if (verdict.kind === 'undetermined') {
          setPhase({ kind: 'undetermined', origin, observation });
        } else if (verdict.kind === 'inventory') {
          setPhase({ kind: 'inventory', origin, observation });
        } else {
          setPhase({ kind: 'failure', origin, cause: verdict.cause });
        }
      });
    } catch (error) {
      if (isAbortError(error)) return;
      continuation.settle(seq, () => {
        observationAbort.current = null;
        setPhase({
          kind: 'failure',
          origin,
          cause: apiFailure(error)?.code === 'engine_down' ? 'engineDown' : 'unclassified',
        });
      });
    }
  }, [apiKey, baseUrl, continuation, vendor]);

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
    if (phase.kind === 'working' && phase.stage === 'persist') return;
    continuation.invalidate();
    observationAbort.current?.abort();
    observationAbort.current = null;
    if (phase.kind === 'working' && phase.stage === 'observe') {
      setPhase(INITIAL_PHASE);
      return;
    }
    createdDelivery.close();
  }, [continuation, createdDelivery, onClose, phase, replaceMode, replacePhase.kind]);

  // What the next observation will be made under. A catalog vendor pins the
  // interface (ladder rung 1: the catalog row is the proof, so the response only
  // has to authenticate); 自定义 leaves it to the disclosure — a concrete choice
  // is a declaration (rung 3), and Auto sends nothing so the shape must prove it
  // (rung 2). Read by the handlers below and by the render, because "what 检测
  // will send" and "what the row says 检测 will send" must be one value.
  const vendorPreset = apiKeyVendorPreset(vendor);
  const selectedProtocol = protocolSelection === 'auto' ? undefined : protocolSelection;
  const constrainedProtocol = vendorPreset?.protocol ?? selectedProtocol;

  const retry = async () => {
    if (phase.kind === 'undetermined') {
      if (!constrainedProtocol) return;
      await observe(phase.origin, constrainedProtocol);
      return;
    }
    if (phase.kind === 'inventory') {
      // 2026-08-11 ruling: retry repeats the complete observation. There is no
      // inventory-only credential lifetime or server capability.
      await observe(phase.origin, phase.observation.protocol ?? undefined);
      return;
    }
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
        await persist(seq, phase.protocol, phase.acceptUnavailableInventory);
      }
      return;
    }
    if (phase.kind === 'persist_failure') {
      await persist(
        continuation.begin(),
        phase.protocol,
        phase.acceptUnavailableInventory,
      );
      return;
    }
    if (phase.kind === 'failure') {
      await observe(phase.origin, constrainedProtocol);
    }
  };

  const addAnyway = async () => {
    if (phase.kind !== 'inventory' || !phase.observation.protocol) return;
    const seq = continuation.begin();
    await persist(seq, phase.observation.protocol, true);
  };

  // §0.8 ①″: editing Base URL, the API key, or the protocol selection returns
  // the dialog to ① Ready with the report dropped. The same three inputs are
  // what every persisting exit's evidence was proved against, so one retirement
  // covers the whole class instead of each handler picking its own phase.
  const retireProvedEvidence = () => {
    setPhase((current) => (persistsWithoutObserving(current) ? INITIAL_PHASE : current));
  };

  const editEndpoint = (value: string) => {
    setBaseUrl(value);
    retireProvedEvidence();
  };
  const editKey = (value: string) => {
    setApiKey(value);
    if (replaceMode && replacePhase.kind === 'failure') setReplacePhase({ kind: 'edit' });
    retireProvedEvidence();
  };
  // The display name is in no observation, so it proves and unproves nothing —
  // ①″ keeps its report across a rename. It IS in the create request, so a
  // server-named refusal of that request stops describing the request the user
  // now holds.
  const editDisplayName = (value: string) => {
    setDisplayName(value);
    if (phase.kind === 'persist_failure') setPhase(INITIAL_PHASE);
  };
  const editProtocol = (value: ProtocolSelection) => {
    setProtocolSelection(value);
    retireProvedEvidence();
  };
  // A vendor is not one more field of the same request. It changes the endpoint
  // AND the interface the request is made under, so anything observed before the
  // switch was observed against a different pair — including a report that
  // `retireProvedEvidence` would have kept, and an ④/failure the new vendor may
  // simply not have. §1.5: switching resets the URL to the new vendor's official
  // one, drops the interface selection, and retires the observation outright.
  // Picking the vendor that is already picked is not a switch, and everything
  // below is destructive: it would throw away a hand-edited address, a declared
  // interface, and a completed detection to arrive back where it started. The
  // guard is here rather than in the picker because this function is what
  // destroys that state — a caller that re-announces the same choice is not
  // wrong, acting on it is.
  const editVendor = (value: string) => {
    if (value === vendor) return;
    setVendor(value);
    setBaseUrl(apiKeyVendorPreset(value)?.official_base_url ?? '');
    setProtocolSelection('auto');
    setManualOpen(false);
    setPhase(INITIAL_PHASE);
  };

  const isWorking = phase.kind === 'working';
  const formLocked = isWorking || phase.kind === 'save_unconfirmed';
  const canCancel = replaceMode
    ? replacePhase.kind !== 'submitting'
    : !(phase.kind === 'working' && phase.stage === 'persist');
  const displayNameValid = optionalTrimmedTextWithin(displayName, SOURCE_DISPLAY_NAME_MAX_LENGTH);
  const canObserve = Boolean(baseUrl.trim() && apiKey.trim()) && !formLocked;
  const canSubmit = canObserve && displayNameValid;
  const protocolIdle = !baseUrl.trim() || !apiKey.trim();
  const detecting = isWorking && phase.kind === 'working' && phase.stage === 'observe';
  const identified = phase.kind === 'form' && phase.report !== null && Boolean(phase.report.protocol);
  const identifiedProtocol = identified && phase.kind === 'form' ? phase.report?.protocol ?? null : null;
  // The segments and the summary row are two renderings of one selection, so
  // only one of them is on screen at a time: expanded, the pressed segment IS
  // the statement; collapsed, the row carries it. Under a catalog pin neither
  // the disclosure nor ④'s forced selector may open one: the interface is not
  // this dialog's to choose, so offering the choice would be offering a control
  // whose value the request then ignores.
  const segmentsOpen = !vendorPreset && (phase.kind === 'undetermined' || (manualOpen && !isWorking));
  // Why the interface is what it is, on every strip that states it. A catalog
  // pin and a user declaration are the two answers that are not "the response
  // proved it" — and Auto, which is that third answer, carries no badge.
  const protocolBadge = vendorPreset
    ? <span className="model-hub-add-key-protocol-badge">{t('settings.models.addKey.protocol.catalogPinned')}</span>
    : selectedProtocol
      ? <span className="model-hub-add-key-protocol-badge">{t('settings.models.addKey.protocol.declared')}</span>
      : null;
  const showForm = phase.kind !== 'inventory';
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
          className="model-hub-add-key-dialog fixed left-1/2 top-1/2 z-50 flex max-h-[calc(100dvh-2rem)] -translate-x-1/2 -translate-y-1/2 flex-col gap-0 overflow-y-auto border border-border-strong bg-surface p-0 shadow-xl outline-none"
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
              : t('settings.models.addKey.subtitle')}
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

        {!replaceMode && showForm && (
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

            <div className={cn(
              'model-hub-add-key-protocol-area',
              protocolIdle && !isWorking && phase.kind !== 'undetermined' && 'is-idle',
            )}>
              <span className="model-hub-add-key-label">{t('settings.models.addKey.field.protocol')}</span>
              {phase.kind === 'undetermined' && (
                <div className="model-hub-add-key-strip model-hub-add-key-strip--advisory">
                  <Info className="model-hub-ink-gold size-3.5 shrink-0" />
                  <div className="flex min-w-0 flex-col gap-[3px]">
                    <span className="model-hub-add-key-strip-title model-hub-ink-gold">{t('settings.models.addKey.undetermined.title')}</span>
                    <span className="model-hub-add-key-strip-detail">{t('settings.models.addKey.undetermined.detail')}</span>
                  </div>
                  {protocolBadge}
                </div>
              )}
              {detecting && (
                <div className="model-hub-add-key-protocol-detecting">
                  <LoaderCircle className="model-hub-ink-mint size-3.5 shrink-0 animate-spin" />
                  <span>{t('settings.models.addKey.protocol.detecting')}</span>
                </div>
              )}
              {identified && identifiedProtocol && (
                <div className="model-hub-add-key-strip model-hub-add-key-strip--success">
                  <CheckCircle2 className="model-hub-ink-mint size-3.5 shrink-0" />
                  <ProtocolGlyph protocol={identifiedProtocol} />
                  <span>
                    {t(PROTOCOL_COPY_KEYS[identifiedProtocol])}
                    {' · '}
                    {phase.kind === 'form' && phase.report && phase.report.models.length === 0
                      ? t('settings.models.addKey.pull.empty')
                      : t('settings.models.addKey.pull.result', {
                          count: phase.kind === 'form' && phase.report ? phase.report.models.length : 0,
                        })}
                  </span>
                  {protocolBadge}
                </div>
              )}
              {phase.kind === 'form' && !phase.report && !segmentsOpen && (
                <div className="model-hub-add-key-protocol-idle-row">
                  {/* The statement and its badge are one line; the sentence that
                      explains them is the next. Beside the glyph the hint read as
                      a second statement competing with the first, and it is the
                      longer of the two — it wrapped around the name it belongs
                      to. Underneath, at its own smaller scale, it is plainly
                      subordinate to the line above. */}
                  <div className="model-hub-add-key-protocol-idle-line">
                    {/* Whatever 检测 is about to send, and nothing else: a catalog
                        pin and a concrete choice are each the active constraint
                        even while the disclosure that could have made one is shut
                        — or, under a pin, is not offered at all. */}
                    <span className="model-hub-add-key-protocol-active">
                      {constrainedProtocol && <ProtocolGlyph protocol={constrainedProtocol} />}
                      {t(constrainedProtocol
                        ? PROTOCOL_COPY_KEYS[constrainedProtocol]
                        : 'settings.models.addKey.protocol.auto')}
                    </span>
                    {/* The pin is the reason this row has no control beside it, so
                        it says so here rather than only on the strip 检测 returns. */}
                    {vendorPreset && protocolBadge}
                  </div>
                  {/* The idle hint promises automatic identification, which is
                      only what Auto does. A named interface has already answered
                      it; a pin answers a different question — what 检测 still has
                      to establish once the interface is no longer in doubt. */}
                  {vendorPreset ? (
                    <span className="model-hub-add-key-hint">{t('settings.models.addKey.protocol.catalogPinned.hint')}</span>
                  ) : !selectedProtocol && (
                    <span className="model-hub-add-key-hint">{t('settings.models.addKey.protocol.idleHint')}</span>
                  )}
                </div>
              )}
              {segmentsOpen && (
                <div className="model-hub-add-key-protocol-manual">
                  <ProtocolSegments
                    disabled={formLocked}
                    selection={protocolSelection}
                    onSelect={editProtocol}
                  />
                  <p className="model-hub-add-key-hint">{t('settings.models.addKey.field.protocol.hint')}</p>
                </div>
              )}
              {!vendorPreset && !isWorking && phase.kind !== 'undetermined' && (
                <button
                  type="button"
                  className="model-hub-add-key-protocol-disclosure"
                  aria-expanded={manualOpen}
                  onClick={() => setManualOpen((open) => !open)}
                >
                  <ChevronDown className={cn('size-3.5 shrink-0', manualOpen && 'rotate-180')} />
                  {t('settings.models.addKey.protocol.manual')}
                </button>
              )}
            </div>

            {phase.kind === 'working' && phase.stage === 'persist' && (
              <div className="model-hub-add-key-strip model-hub-add-key-strip--working">
                <LoaderCircle className="model-hub-ink-mint size-3.5 shrink-0 animate-spin" />
                <div className="flex min-w-0 flex-col gap-[3px]">
                  <span className="model-hub-add-key-strip-title text-foreground">{t('settings.models.addKey.saving')}</span>
                  <span className="model-hub-add-key-strip-detail">{t('settings.models.addKey.saving.detail')}</span>
                </div>
              </div>
            )}
            {phase.kind === 'failure' && (
              <div className="model-hub-add-key-strip model-hub-add-key-strip--error">
                <CircleX className="model-hub-add-key-error-ink size-3.5 shrink-0" />
                <div className="flex min-w-0 flex-col gap-[3px]">
                  <span className="model-hub-add-key-error-ink model-hub-add-key-strip-title">{t(failureCopy(phase.cause))}</span>
                  {phase.cause === 'auth' && <span className="model-hub-add-key-strip-detail">{t('settings.models.addKey.fail.auth.detail')}</span>}
                  {phase.cause !== 'engineDown' && <span className="model-hub-add-key-strip-detail">{t('settings.models.addKey.fail.subtitle')}</span>}
                </div>
              </div>
            )}
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

        {!replaceMode && phase.kind === 'inventory' && (
          <div className="model-hub-add-key-outcome-wrap">
            <div className="model-hub-add-key-strip model-hub-add-key-strip--advisory model-hub-add-key-strip--inventory">
              <TriangleAlert className="model-hub-ink-gold size-3.5 shrink-0" />
              <span className="model-hub-add-key-strip-title model-hub-ink-gold">{t('settings.models.addKey.inventory.title')}</span>
            </div>
          </div>
        )}

        <footer className="model-hub-add-key-foot model-hub-fill-05 flex flex-row items-center justify-end border-t border-border">
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
              {phase.kind === 'inventory' && (
                <Button type="button" variant="outline" className="model-hub-add-key-action" onClick={() => void addAnyway()}>
                  {t('settings.models.addKey.addAnyway')}
                </Button>
              )}
              {phase.kind === 'form' && !phase.report && (
                <Button
                  type="button"
                  variant="brand"
                  className="model-hub-add-key-action"
                  disabled={!canSubmit}
                  onClick={() => void observe('add', constrainedProtocol)}
                >
                  {t('settings.models.addKey.detect')}
                </Button>
              )}
              {phase.kind === 'form' && phase.report?.protocol && (
                <Button
                  type="button"
                  variant="brand"
                  className="model-hub-add-key-action"
                  disabled={!canSubmit}
                  onClick={() => void persist(continuation.begin(), phase.report?.protocol ?? undefined)}
                >
                  {t('settings.models.addKey.confirm')}
                </Button>
              )}
              {phase.kind === 'working' && (
                <Button type="button" variant="brand" className="model-hub-add-key-action" disabled>
                  <LoaderCircle className="size-3 animate-spin" />
                  {t(phase.stage === 'persist'
                    ? 'settings.models.addKey.saving'
                    : 'settings.models.addKey.protocol.detecting')}
                </Button>
              )}
              {(phase.kind === 'failure' || phase.kind === 'persist_failure' || phase.kind === 'save_unconfirmed' || phase.kind === 'inventory' || phase.kind === 'undetermined') && (
                <Button
                  type="button"
                  variant="brand"
                  className={cn(
                    'model-hub-add-key-action',
                    (phase.kind === 'inventory' || phase.kind === 'save_unconfirmed' || (phase.kind === 'undetermined' && !constrainedProtocol))
                      && 'model-hub-add-key-action--dim',
                  )}
                  disabled={phase.kind === 'undetermined' && !constrainedProtocol}
                  onClick={() => void retry()}
                >
                  {t('settings.models.addKey.retry')}
                </Button>
              )}
            </>
          )}
        </footer>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
};
