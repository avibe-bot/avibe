// Setup's second screen: where models come from, and the one picture that explains
// why that question has a single answer.
//
// The screen is a diagram, not a form. Providers at the top, the gateway in the
// middle, the assistants at the bottom, wired together — so "add a key once and every
// assistant can use it" is something you SEE before you read it. Everything on the
// stage is therefore derived from one scan and one source list; two reads of the same
// machine would let the cards, the sentence and the button disagree about it.
//
// What this file owns is composition and lifecycle. It owns no migration semantics
// (the shipped takeover does), no install-and-start sequence (`runtimeLifecycle` does),
// no adoption sequence (`gatewayAdoption` does), no credential write (the add dialog
// does), no derivation (`providerStage.ts` does) and no primary button (the shell does,
// through `onActionChange` and `activate`). Ordering those two runtime sequences is
// composition, and therefore is this file's: the engine has to be up whether or not
// there is an assistant to adopt.
import * as React from 'react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import {
  createAgentCollectionReadAuthority,
  createSourceCollectionReadAuthority,
} from '@/components/settings/models/collectionReadAuthority';
import { resumeGatewayAdoption, type GatewayAdoptionFailure } from '@/components/settings/models/gatewayAdoption';
import { MigrationDialog } from '@/components/settings/models/MigrationDialog';
import { isImportableKey } from '@/components/settings/models/migrationScan';
import { modelsApi, type SourceCreated } from '@/components/settings/models/modelsApi';
import {
  installAndStartStep,
  resumeInstallAndStartRuntime,
} from '@/components/settings/models/runtimeLifecycle';
import type { AgentBackend, RuntimeDependency, Source } from '@/components/settings/models/types';

import { ImportKeysNotice } from '../ImportKeysNotice';
import { useOnboardingMotion } from '../motion';
import { setupNavigationReady, type SetupScreenHandle, type SetupScreenProps } from '../setupFlow';
import { AddSourceDialog } from './AddSourceDialog';
import { DestinationRow } from './DestinationRow';
import { GatewayCard, type GatewayPhase } from './GatewayCard';
import { AddMoreCard, ProviderCard } from './ProviderCard';
import { SupplyWires } from './SupplyWires';
import {
  addedThroughMoreCount,
  adoptionBackend,
  defaultSelection,
  gatewayIntent,
  offeredImportKeys,
  pendingImportRows,
  providerAction,
  providerSetupAction,
  providerSlots,
  providerSummary,
  reconcileSelection,
  slotSelected,
  toggleSlotSelection,
  unlistedDetected,
  usableSource,
  type ProviderSlot,
} from './providerStage';

// The tier tokens this screen is measured in, and the shell classes it renders
// (`.onboarding-setup`, `.onboarding-stage`, `.onboarding-heading`), are declared
// there. Today the Wizard always mounts an earlier step that imports it, so the
// dependency is satisfied by accident; a screen reached first — a handoff, a
// deep link — would render untiered without this line.
import '../onboarding.css';
import '../onboarding-providers.css';

/** The wires' first arrival, staggered so the fan-in lands before the fan-out leaves.
 *  Two numbers rather than a timeline: each band's own 3-wire stagger is the CSS's. */
const INBOUND_DELAY_MS = 120;
const OUTBOUND_DELAY_MS = 570;

/** A resume attempt this screen started. `step` is the step currently being attempted:
 *  it opens on what the authoritative read called for and follows the lifecycle
 *  helper's own report across the install/start boundary. */
type GatewayRun =
  | { kind: 'idle' }
  | { kind: 'running'; step: 'install' | 'start' }
  | { kind: 'failed'; step: GatewayAdoptionFailure['step'] };

/** Locale-correct enumeration without inventing a separator string for each language.
 *  Falls back to the ASCII list on a runtime without `Intl.ListFormat`. */
const formatNames = (names: readonly string[], locale: string): string => {
  try {
    return new Intl.ListFormat(locale, { style: 'narrow', type: 'unit' }).format([...names]);
  } catch {
    return names.join(', ');
  }
};

export const ProvidersScreen = React.forwardRef<SetupScreenHandle, SetupScreenProps>(
  function ProvidersScreen({
    active,
    capability,
    gatewayEnabled,
    runtimeRead,
    onRetrySetup,
    flowState,
    setFlowState,
    onActionChange,
    onNavigate,
  }, ref) {
    const { t, i18n } = useTranslation();
    const motion = useOnboardingMotion();
    const attachMotion = motion.ref;

    // The stage is held as an ELEMENT, not a ref: `SupplyWires` measures inside it and
    // a ref object would still be empty when the children first lay out.
    const [stage, setStage] = React.useState<HTMLElement | null>(null);
    const attachStage = React.useCallback((element: HTMLDivElement | null) => {
      setStage(element);
      attachMotion(element);
    }, [attachMotion]);

    // One owner per collection, for the whole life of the screen: the add dialog
    // settles an unknown write against the same generation this screen reads.
    const [sourceReads] = React.useState(createSourceCollectionReadAuthority);
    const [agentReads] = React.useState(createAgentCollectionReadAuthority);

    const [sources, setSources] = React.useState<Source[]>([]);
    // Whether the list above is an answer. It starts as neither empty nor absent but
    // unknown, because that is what it is before the first read lands. It tracks the
    // SOURCE read alone: a scan that failed says nothing about whether a credential
    // exists, and only this read can answer that.
    const [sourceRead, setSourceRead] = React.useState<'reading' | 'unreadable' | 'read'>('reading');
    const [scanFailed, setScanFailed] = React.useState(false);
    // The sentence is about the picture, which either read can leave incomplete.
    const supplyFailed = sourceRead === 'unreadable' || scanFailed;
    const [supplyToken, setSupplyToken] = React.useState(0);
    const [gatewayRun, setGatewayRun] = React.useState<GatewayRun>({ kind: 'idle' });
    const [gatewayToken, setGatewayToken] = React.useState(0);
    const [addDialog, setAddDialog] = React.useState<{ more: boolean; vendor: string | null } | null>(null);
    const [importOpen, setImportOpen] = React.useState(false);
    const [importFailed, setImportFailed] = React.useState(false);
    const [verifying, setVerifying] = React.useState(false);
    const [pulse, setPulse] = React.useState({ inbound: false, outbound: false });

    const selection = flowState.providerSelection;
    const ready = setupNavigationReady(capability, gatewayEnabled);
    const intent = gatewayIntent({ capability, gatewayEnabled, runtimeRead });

    const slots = React.useMemo(
      () => providerSlots({ sources, scan: selection.scan }),
      [sources, selection.scan],
    );
    const detected = React.useMemo(
      () => unlistedDetected({ sources, scan: selection.scan }),
      [sources, selection.scan],
    );
    const pending = React.useMemo(() => pendingImportRows(selection), [selection]);
    // The capsule counts through the same consent grouping the cards and the dialog
    // use, so a key it advertises is always one the review can actually act on.
    const offered = React.useMemo(() => offeredImportKeys(selection), [selection]);

    // ── Supply ──────────────────────────────────────────────────────────────

    // Whether the server's own row defaults have been honoured yet. A scan is nulled
    // again once a batch lands, so 「no scan held」 alone cannot answer this: it is
    // also true for the rescan that follows an import, whose consent has just been
    // spent on purpose and must not come back ticked.
    const seededSelectionRef = React.useRef(false);
    React.useEffect(() => {
      if (!active || !ready) return;
      let cancelled = false;
      // A re-read is a read: while it is in flight the screen is back to not knowing,
      // which is what a retry means and what the action should say.
      setSourceRead('reading');
      void (async () => {
        // Settled independently, because they answer different questions. A scan that
        // fails says nothing about the sources, and discarding a source list that did
        // arrive would report providers that exist as providers that do not — leaving
        // the action offering 「添加」 on a machine that already has credentials.
        const [read, scan] = await Promise.allSettled([
          sourceReads.refresh(),
          modelsApi.scanMigration(),
        ]);
        if (cancelled) return;
        // A stale source read is neither an answer nor a failure: a newer generation
        // superseded it, and that newer one is what will settle this.
        if (read.status === 'rejected') setSourceRead('unreadable');
        else if (read.value.kind === 'current') {
          setSources(read.value.value);
          setSourceRead('read');
        }
        // Either failure is still a failure for the sentence: what the screen cannot
        // report is exactly what it and its retry exist to say.
        setScanFailed(scan.status === 'rejected');
        if (scan.status === 'rejected') return;
        const scanned = scan.value;
        // Read outside the updater and written after it: an updater has to be pure,
        // and one that flips this on its first call answers its own question
        // differently on the second.
        const first = !seededSelectionRef.current;
        setFlowState((previous) => {
          // The first scan has no prior consent to carry, so it opens on the
          // server's own defaults — the same rows the shipped dialog opens ticked.
          // Afterwards the selection is the person's, and survives only where this
          // scan asks the same question the last one did.
          const selectedBackends = first && previous.providerSelection.scan === null
            ? defaultSelection(scanned)
            : reconcileSelection(
              { scan: scanned, selectedBackends: previous.providerSelection.selectedBackends },
              previous.providerSelection.scan,
            );
          return { ...previous, providerSelection: { scan: scanned, selectedBackends } };
        });
        seededSelectionRef.current = true;
      })();
      return () => { cancelled = true; };
    }, [active, ready, supplyToken, setFlowState, sourceReads]);

    // ── Gateway ─────────────────────────────────────────────────────────────

    // Which attempt the gateway effect has already made, named by the token that
    // armed it — so it is both the guard against making it twice and the identity of
    // the live one. `gatewayToken` only ever increases, so an attempt is superseded
    // exactly when this no longer holds its token.
    //
    // One attempt per explicit retry. Not per intent change: the intent is recomputed
    // from a read the shell refreshes, and re-running on every recomputation would
    // reinstall in a loop while an install was still settling. Not per activation
    // either — an attempt outlives the screen being hidden, so re-arming on re-entry
    // could launch a second install over one still running. Coming back to a failed
    // attempt finds it on the card, with its retry.
    //
    // And nothing else supersedes one. A closure flag would be flipped by the effect's
    // cleanup, which runs when the screen is merely hidden and again on every
    // StrictMode replay — abandoning a mutation the server is still performing,
    // leaving the card stuck on 「正在安装」 and the shell's runtime read describing a
    // machine that has since changed. What supersedes an attempt is another attempt.
    const attemptedRef = React.useRef<number | null>(null);

    // Held in a ref for the same reason the publication is: a shell whose callback is
    // re-created each render would otherwise re-run the effect below, re-arming an
    // attempt that is still in flight.
    const refreshRuntimeRef = React.useRef(onRetrySetup);
    refreshRuntimeRef.current = onRetrySetup;

    const resumeStep = intent.kind === 'resume' ? intent.step : null;
    // The snapshot the step was read from, held for the same reason the callbacks are:
    // it is a new object on every read, and depending on it would re-arm an attempt
    // that is still in flight. Captured synchronously below, before any await.
    const resumeRuntimeRef = React.useRef<RuntimeDependency | null>(null);
    resumeRuntimeRef.current = intent.kind === 'resume' ? intent.runtime : null;

    React.useEffect(() => {
      if (!active || resumeStep === null || attemptedRef.current === gatewayToken) return;
      const runtime = resumeRuntimeRef.current;
      if (runtime === null) return;
      attemptedRef.current = gatewayToken;
      const superseded = () => attemptedRef.current !== gatewayToken;
      setGatewayRun({ kind: 'running', step: resumeStep });
      void (async () => {
        try {
          // The engine first, and on its own. `resumeGatewayAdoption` is named for
          // adoption: it returns success without touching the runtime when its backend
          // is already in hub mode, and on a machine with no assistant CLI there is no
          // backend to name at all. Either exit used to leave a missing or stopped
          // engine exactly as it was — card idle, no retry beside it, Continue blocked
          // on a read that never moves, and the screen that installs assistants on the
          // far side of it.
          const started = await resumeInstallAndStartRuntime(modelsApi, runtime, (value) => {
            // Install and start are one press and two waits; the helper reports the
            // crossing, so the card can say which one it is in.
            const step = installAndStartStep(value);
            if (!superseded() && step !== 'complete') setGatewayRun({ kind: 'running', step });
          });
          if (superseded()) return;
          if (started.failedStep !== null) {
            setGatewayRun({ kind: 'failed', step: started.failedStep });
            // A failed install or start changed supervisor health; the shell's read
            // still describes the health it had before the attempt.
            refreshRuntimeRef.current();
            return;
          }
          // Then what there is to adopt, which may be nothing: no CLI on this machine
          // is not a gateway failure, and the engine is up either way.
          const agents = await agentReads.refresh();
          if (superseded()) return;
          const backend = agents.kind === 'current' ? adoptionBackend(agents.value) : null;
          const outcome = backend === null
            ? { ok: true as const }
            : await resumeGatewayAdoption(modelsApi, agentReads, backend);
          if (superseded()) return;
          setGatewayRun(outcome.ok ? { kind: 'idle' } : { kind: 'failed', step: outcome.failure.step });
          // The engine moved; the shell's read still describes where it was. Asking
          // for that read again is what turns the card from 「正在启动」 to 「运行中」
          // and unblocks Continue — without it the attempt succeeds and the screen
          // falls back to the stale intent, which is still 'resume'.
          refreshRuntimeRef.current();
          // And an engine that just came up can answer reads that failed before it did.
          if (outcome.ok) setSupplyToken((token) => token + 1);
        } catch {
          if (!superseded()) setGatewayRun({ kind: 'failed', step: 'read' });
        }
      })();
    }, [active, resumeStep, gatewayToken, agentReads]);

    const retryGateway = React.useCallback(() => {
      // The shell owns the authoritative read; this screen owns the attempt. Both are
      // re-armed, because either one could be what is stale.
      onRetrySetup();
      setGatewayRun({ kind: 'idle' });
      setGatewayToken((token) => token + 1);
      setSupplyToken((token) => token + 1);
    }, [onRetrySetup]);

    const gatewayBusy = gatewayRun.kind === 'running';
    const gatewayPhase: GatewayPhase = gatewayBusy
      ? (gatewayRun.step === 'install' ? 'installing' : 'starting')
      : intent.kind === 'running'
        ? 'running'
        : gatewayRun.kind === 'failed'
          ? 'failed'
          : intent.kind === 'unsupported'
            ? 'unsupported'
            // A read this screen could not make wears the same not-ready line and the
            // same retry as an attempt that failed. The contract asks for exactly
            // that — the runtime-unread copy with retry, never the unsupported
            // notice — and the alternative is the idle card, which says the engine is
            // fine while Continue stays disabled for a reason nothing on screen gives.
            : intent.kind === 'unreadable'
              ? 'failed'
              : 'idle';

    // ── The action the shell renders ────────────────────────────────────────

    const action = providerAction({
      pendingCount: pending.length,
      importFailed,
      // What the screen knows, not what the list happens to hold: an inventory it
      // could not read is not an empty one, and 「添加」 offered against it is how a
      // credential that already exists gets written a second time.
      supply: sourceRead === 'read'
        ? { kind: 'read', hasSource: sources.some(usableSource) }
        : { kind: sourceRead },
      gatewayBusy,
      // The authoritative read, not this screen's attempt: an attempt that reported
      // success is not the engine answering, and C4 gates the next screen on the read.
      gatewayRunning: intent.kind === 'running',
      verifying,
    });

    // Held in a ref so a shell that re-renders on every publication cannot turn this
    // into a loop: what the effect depends on is the action's VALUE.
    const publishRef = React.useRef(onActionChange);
    publishRef.current = onActionChange;
    React.useEffect(() => {
      if (!active) return;
      publishRef.current(providerSetupAction({
        kind: action.kind,
        count: action.count,
        ...(action.blocked ? { blocked: true } : {}),
      }));
    }, [active, action.kind, action.count, action.blocked]);

    React.useImperativeHandle(ref, () => ({
      activate: () => {
        // A blocked state is the right one to show and the wrong one to press. The
        // shell already renders it disabled; refusing it here too is what makes a
        // keyboard activation on a stale render unrepresentable rather than merely
        // unlikely.
        if (action.blocked) return;
        switch (action.kind) {
          case 'import':
          case 'retryImport':
            // Consent is the takeover's, always. Continuing is what the next press
            // does, once the batch has landed and the rescan has emptied the pending
            // set — an automatic navigation here would hide the report of what landed.
            setImportOpen(true);
            return;
          case 'continue':
            onNavigate('assistants');
            return;
          case 'add':
            setAddDialog({ more: true, vendor: null });
            return;
          case 'retrySupply':
            // Asking again is the whole action. The effect flips back to 「reading」 on
            // its way in, so the button reports the retry it just started.
            setSupplyToken((token) => token + 1);
            return;
          default:
            // Busy. The shell already renders the action disabled; ignoring the call
            // rather than trusting that is what makes it unrepresentable.
        }
      },
    }), [action.kind, action.blocked, onNavigate]);

    // ── First-entry sequence ────────────────────────────────────────────────

    React.useEffect(() => {
      if (!active) {
        // Reset so a re-entry replays: the pulse is a `<g>` that has to remount.
        setPulse({ inbound: false, outbound: false });
        return;
      }
      // Paused mid-sequence keeps the frame it is on rather than snapping back —
      // `data-motion` holds the CSS side, and clearing the timers holds this one.
      if (!motion.running) return;
      const timers = [
        window.setTimeout(() => setPulse((state) => ({ ...state, inbound: true })), INBOUND_DELAY_MS),
        window.setTimeout(() => setPulse((state) => ({ ...state, outbound: true })), OUTBOUND_DELAY_MS),
      ];
      return () => timers.forEach(window.clearTimeout);
    }, [active, motion.running]);

    // ── Writes landing ──────────────────────────────────────────────────────

    const landSource = React.useCallback(async (created: SourceCreated | null, viaMore: boolean) => {
      setVerifying(true);
      try {
        try {
          const read = await sourceReads.refresh();
          if (read.kind === 'current') {
            setSources(read.value);
            setSourceRead('read');
          }
        } catch {
          // The write may well have landed; what failed is the read that would show
          // it. The dialog swallows a rejection here to stay closable, so saying so
          // is this screen's job — keeping the old list silently would report a
          // provider that exists as one that does not.
          setSourceRead('unreadable');
        }
        const id = created?.source.id;
        if (!viaMore || !id) return;
        setFlowState((previous) => (previous.addedThroughMore.includes(id)
          ? previous
          : { ...previous, addedThroughMore: [...previous.addedThroughMore, id] }));
      } finally {
        setVerifying(false);
      }
    }, [setFlowState, sourceReads]);

    const changeSelection = React.useCallback((selectedBackends: AgentBackend[]) => {
      // Editing the selection retires the verdict the server gave about the batch it
      // no longer describes.
      setImportFailed(false);
      setFlowState((previous) => ({
        ...previous,
        providerSelection: { ...previous.providerSelection, selectedBackends },
      }));
    }, [setFlowState]);

    const toggleSlot = React.useCallback((slot: ProviderSlot) => {
      setImportFailed(false);
      setFlowState((previous) => ({
        ...previous,
        providerSelection: {
          ...previous.providerSelection,
          selectedBackends: toggleSlotSelection(previous.providerSelection, slot),
        },
      }));
    }, [setFlowState]);

    // ── Sentence ────────────────────────────────────────────────────────────

    const summary = providerSummary({
      scan: selection.scan,
      sources,
      selected: selection.selectedBackends,
      failed: supplyFailed,
    });
    const summaryText = summary.kind === 'none'
      ? t('onboarding.providers.summaryNone')
      : summary.kind === 'error'
        ? t('onboarding.providers.summaryError')
        : t(
          summary.kind === 'added'
            ? 'onboarding.providers.summaryAdded'
            : 'onboarding.providers.summarySelected',
          { count: summary.count, names: formatNames(summary.names, i18n.language) },
        );

    return (
      <div className="onboarding-setup">
        <header className="onboarding-heading">
          <h2>{t('onboarding.providers.title')}</h2>
          <p>{t('onboarding.providers.subtitle')}</p>
        </header>

        <div className="onboarding-stage">
          <div
            ref={attachStage}
            className="setup-provider-stage"
            data-motion={motion.running ? 'running' : 'paused'}
            // The diagram's meaning is its arrangement, which a screen reader cannot
            // see. One group, named by what the arrangement says.
            role="group"
            aria-label={t('onboarding.providers.stageLabel')}
          >
            <div className="setup-provider-grid">
              {slots.map((slot) => (
                <ProviderCard
                  key={slot.vendor}
                  slot={slot}
                  selected={slotSelected(slot, selection.selectedBackends)}
                  onToggle={() => toggleSlot(slot)}
                  onAdd={() => setAddDialog({ more: false, vendor: slot.vendor })}
                />
              ))}
              <AddMoreCard
                count={addedThroughMoreCount(flowState.addedThroughMore, sources)}
                onAdd={() => setAddDialog({ more: true, vendor: null })}
              />
            </div>

            <SupplyWires
              direction="inbound"
              stage={stage}
              endpointSelector=".setup-provider-card"
              pulse={pulse.inbound}
            />

            <GatewayCard
              phase={gatewayPhase}
              failedStep={gatewayRun.kind === 'failed' ? gatewayRun.step : null}
              onRetry={gatewayPhase === 'failed' || gatewayPhase === 'unsupported' ? retryGateway : null}
            />

            <SupplyWires
              direction="outbound"
              stage={stage}
              endpointSelector=".setup-destination"
              pulse={pulse.outbound}
            />

            <DestinationRow />

            <p
              className="setup-provider-summary"
              {...(summary.kind === 'error' ? { 'data-tone': 'error' } : {})}
              role="status"
              aria-live="polite"
            >
              {summaryText}
              {summary.kind === 'error' && (
                <Button type="button" variant="outline" size="sm" className="setup-summary-retry" onClick={retryGateway}>
                  {t('common.retry')}
                </Button>
              )}
            </p>

            {/* The slot is always here; only the capsule inside it comes and goes. That
                is what keeps the footer action still when someone dismisses the offer. */}
            <div className="setup-provider-offer">
              {active && (
                <ImportKeysNotice
                  candidates={offered}
                  imported={flowState.importedCount}
                  onReview={() => setImportOpen(true)}
                />
              )}
            </div>
          </div>
        </div>

        {addDialog && (
          <AddSourceDialog
            more={addDialog.more}
            vendor={addDialog.vendor}
            detected={detected}
            pendingCount={pending.length}
            sources={sources}
            sourceReads={sourceReads}
            isSelected={(slot) => slotSelected(slot, selection.selectedBackends)}
            onToggleDetected={toggleSlot}
            onReviewDetected={() => { setAddDialog(null); setImportOpen(true); }}
            onAdded={(created) => landSource(created, addDialog.more)}
            onClose={() => setAddDialog(null)}
          />
        )}

        {importOpen && (
          <MigrationDialog
            open
            scope="setup"
            eligible={isImportableKey}
            takeable={isImportableKey}
            value={selection}
            onChange={(next) => changeSelection(next.selectedBackends)}
            onApplied={(applied) => {
              // `onApplied(0)` is a refresh trigger, not a receipt: the takeover reports
              // a rejected batch that way, and the only observable difference between a
              // batch that landed and one that did not is this number.
              //
              // It is a trigger on BOTH paths. A rejection the server terminalised —
              // `migration_credentials_invalid` closes the dialog behind it — leaves the
              // held scan describing rows the server has just disagreed about, and
              // retrying that same batch returns the same error forever. Re-reading is
              // what surfaces the reauthentication; only the count is not touched,
              // because nothing landed.
              if (applied === 0) {
                setImportFailed(true);
                setSupplyToken((token) => token + 1);
                return;
              }
              setImportFailed(false);
              setFlowState((previous) => ({
                ...previous,
                importedCount: previous.importedCount + applied,
                // The batch is spent in the same tick it landed. The rescan is a round
                // trip away, and until it answers the old scan still names rows that
                // are now imported — which would keep the capsule advertising them,
                // the cards offering them and the action saying 「导入」 for a batch the
                // dialog would refuse to build.
                providerSelection: { scan: null, selectedBackends: [] },
              }));
              setSupplyToken((token) => token + 1);
            }}
            onClose={() => setImportOpen(false)}
          />
        )}
      </div>
    );
  },
);
