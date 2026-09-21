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
// (the shipped takeover does), no runtime sequence (`resumeGatewayAdoption` does), no
// credential write (the add dialog does), no derivation (`providerStage.ts` does) and
// no primary button (the shell does, through `onActionChange` and `activate`).
import * as React from 'react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import {
  createAgentCollectionReadAuthority,
  createSourceCollectionReadAuthority,
} from '@/components/settings/models/collectionReadAuthority';
import { resumeGatewayAdoption, type GatewayAdoptionFailure } from '@/components/settings/models/gatewayAdoption';
import { MigrationDialog } from '@/components/settings/models/MigrationDialog';
import { importableKeys, isImportableKey } from '@/components/settings/models/migrationScan';
import { modelsApi, type SourceCreated } from '@/components/settings/models/modelsApi';
import type { AgentBackend, Source } from '@/components/settings/models/types';

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
  gatewayIntent,
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

/** A resume attempt this screen started. `step` is the step the authoritative read
 *  called for when it began — the lifecycle helper reports no intermediate progress,
 *  so the card says what was needed rather than guessing what is happening now. */
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
    const [supplyFailed, setSupplyFailed] = React.useState(false);
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
    // The capsule counts the same filtered scan the import list is built from, so the
    // sentence and the dialog can never advertise different numbers of the same keys.
    const offered = React.useMemo(
      () => importableKeys(selection.scan?.items ?? []),
      [selection.scan],
    );

    // ── Supply ──────────────────────────────────────────────────────────────

    React.useEffect(() => {
      if (!active || !ready) return;
      let cancelled = false;
      void (async () => {
        try {
          const [read, scan] = await Promise.all([sourceReads.refresh(), modelsApi.scanMigration()]);
          if (cancelled) return;
          setSupplyFailed(false);
          if (read.kind === 'current') setSources(read.value);
          setFlowState((previous) => {
            const next = { scan, selectedBackends: previous.providerSelection.selectedBackends };
            // A fresh scan can retire a consent: imported, newly blocked, or simply
            // gone. Carrying the old name forward would keep the CTA offering a batch
            // the dialog would refuse to build.
            return { ...previous, providerSelection: { scan, selectedBackends: reconcileSelection(next) } };
          });
        } catch {
          if (!cancelled) setSupplyFailed(true);
        }
      })();
      return () => { cancelled = true; };
    }, [active, ready, supplyToken, setFlowState, sourceReads]);

    // ── Gateway ─────────────────────────────────────────────────────────────

    // One attempt per activation, per explicit retry. Not per intent change: the
    // intent is recomputed from a read the shell refreshes, and re-running on every
    // recomputation would reinstall in a loop while an install was still settling.
    const attemptedRef = React.useRef<number | null>(null);
    React.useEffect(() => {
      if (!active) attemptedRef.current = null;
    }, [active]);

    const resumeStep = intent.kind === 'resume' ? intent.step : null;
    React.useEffect(() => {
      if (!active || resumeStep === null || attemptedRef.current === gatewayToken) return;
      attemptedRef.current = gatewayToken;
      let cancelled = false;
      setGatewayRun({ kind: 'running', step: resumeStep });
      void (async () => {
        try {
          const agents = await agentReads.refresh();
          if (cancelled) return;
          const backend = agents.kind === 'current' ? adoptionBackend(agents.value) : null;
          // No CLI on this machine is not a gateway failure — there is simply nothing
          // to adopt, and the engine's state stays the shell's read to report.
          if (backend === null) { setGatewayRun({ kind: 'idle' }); return; }
          const outcome = await resumeGatewayAdoption(modelsApi, agentReads, backend);
          if (cancelled) return;
          setGatewayRun(outcome.ok ? { kind: 'idle' } : { kind: 'failed', step: outcome.failure.step });
          // An engine that just came up can answer reads that failed before it did.
          if (outcome.ok) setSupplyToken((token) => token + 1);
        } catch {
          if (!cancelled) setGatewayRun({ kind: 'failed', step: 'read' });
        }
      })();
      return () => { cancelled = true; };
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
            : 'idle';

    // ── The action the shell renders ────────────────────────────────────────

    const action = providerAction({
      pendingCount: pending.length,
      importFailed,
      hasSource: sources.some(usableSource),
      gatewayBusy,
      verifying,
    });

    // Held in a ref so a shell that re-renders on every publication cannot turn this
    // into a loop: what the effect depends on is the action's VALUE.
    const publishRef = React.useRef(onActionChange);
    publishRef.current = onActionChange;
    React.useEffect(() => {
      if (!active) return;
      publishRef.current(providerSetupAction({ kind: action.kind, count: action.count }));
    }, [active, action.kind, action.count]);

    React.useImperativeHandle(ref, () => ({
      activate: () => {
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
          default:
            // Busy. The shell already renders the action disabled; ignoring the call
            // rather than trusting that is what makes it unrepresentable.
        }
      },
    }), [action.kind, onNavigate]);

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
        const read = await sourceReads.refresh();
        if (read.kind === 'current') setSources(read.value);
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
      slots,
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
            value={selection}
            onChange={(next) => changeSelection(next.selectedBackends)}
            onApplied={(applied) => {
              // `onApplied(0)` is a refresh trigger, not a receipt: the takeover reports
              // a rejected batch that way, and the only observable difference between a
              // batch that landed and one that did not is this number.
              if (applied === 0) { setImportFailed(true); return; }
              setImportFailed(false);
              setFlowState((previous) => ({ ...previous, importedCount: previous.importedCount + applied }));
              setSupplyToken((token) => token + 1);
            }}
            onClose={() => setImportOpen(false)}
          />
        )}
      </div>
    );
  },
);
