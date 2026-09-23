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
import { createPortal } from 'react-dom';
import { useTranslation } from 'react-i18next';

import { useRouteSurfaceActive } from '@/lib/routeSurfaceActivity';
import { clearMigrationDismissed, isMigrationDismissed, writeMigrationDismissed } from '@/lib/modelHubMigrationDismiss';
import { Button } from '@/components/ui/button';
import {
  createAgentCollectionReadAuthority,
  createSourceCollectionReadAuthority,
  type CollectionReadAuthority,
} from '@/components/settings/models/collectionReadAuthority';
import { resumeGatewayAdoption, type GatewayAdoptionFailure } from '@/components/settings/models/gatewayAdoption';
import { MigrationDialog } from '@/components/settings/models/MigrationDialog';
import { isImportableKey } from '@/components/settings/models/migrationScan';
import { modelsApi, type SourceCreated } from '@/components/settings/models/modelsApi';
import { foldRegionRead } from '@/components/settings/models/regionRead';
import {
  installAndStartStep,
  resumeInstallAndStartRuntime,
} from '@/components/settings/models/runtimeLifecycle';
import type { AgentBackend, AgentSupply, RuntimeDependency, Source } from '@/components/settings/models/types';

import { useOnboardingMotion } from '../motion';
import {
  setupCanAttemptInstall,
  setupNavigationReady,
  type SetupScreenHandle,
  type SetupScreenProps,
} from '../setupFlow';
import { AddSourceDialog } from './AddSourceDialog';
import { DestinationRow } from './DestinationRow';
import { GatewayCard, type GatewayPhase } from './GatewayCard';
import { AddMoreCard, ProviderCard } from './ProviderCard';
import { SupplyWires } from './SupplyWires';
import {
  addedThroughMoreCount,
  adoptionBackend,
  defaultSelection,
  offeredImportKeys,
  gatewayEvidenceSettled,
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

/** A resume attempt this screen started. `step` is the step currently being attempted:
 *  it opens on what the authoritative read called for and follows the lifecycle
 *  helper's own report across the install/start boundary. */
type GatewayRun =
  /** Nothing outstanding: either nothing has been attempted for the current
   *  authorization, or one is about to be armed for it. */
  | { kind: 'idle' }
  | { kind: 'running'; step: 'install' | 'start' }
  /**
   * Every step reported success, and the read this screen asked for on the strength
   * of that is outstanding. Kept apart from `idle` because success is not a claim
   * that the engine is ready — only the shell's read says that — and `against` is
   * the evidence the request was made on, so the answer can be told apart from the
   * stale read the attempt itself ran against. It is a waiting room: the answer
   * turns it into one of the states above or below.
   */
  | { kind: 'settled'; against: GatewayEvidence }
  /** A dead end with a Retry beside it. `step` names the step that failed where one
   *  did, and is `null` where none did — every step reported success and the machine
   *  the shell then read still asks to be resumed. There is nothing to name there,
   *  and naming one would be a report of a failure that did not happen. */
  | { kind: 'failed'; step: GatewayAdoptionFailure['step'] | null };

/** Everything the shell hands down that decides whether an attempt is authorized at
 *  all: the runtime read and the two configuration facts beside it. A retry holds the
 *  set it was pressed against, and is answered when any part of it is replaced. */
type GatewayEvidence = Pick<SetupScreenProps, 'runtimeRead' | 'gatewayEnabled' | 'capability'>;

/**
 * Whether the shell has ANSWERED a read this screen asked for.
 *
 * Two things have to be true, and they are different things. The evidence must have
 * moved off what the request was made against — the only read a request can see is
 * the one it was made from, so that read is never its answer. And what replaced it
 * must be an answer at all: the shell reports a read STARTING before it reports what
 * it found — `refreshing` over the old value, a configuration back to 「pending」 —
 * and treating one of those as the reply loses the request the person is waiting on.
 */
const gatewayReadAnswered = (against: GatewayEvidence | null, now: GatewayEvidence): boolean =>
  (against === null
    || against.runtimeRead !== now.runtimeRead
    || against.gatewayEnabled !== now.gatewayEnabled
    || against.capability !== now.capability)
  && gatewayEvidenceSettled(now);

/**
 * An observation as a VALUE, rather than as the object that carried it.
 *
 * What a supply read is taken against is the machine that will answer it, and these
 * are the facts that decide whether it can: whether it is up, whether it is the
 * runtime it claims to be, which build is installed, and whether one may exist on
 * this host at all. A shell that hands down an equal read it rebuilt — a refresh
 * that confirmed nothing changed, a parent that re-created its props — has observed
 * nothing new, so taking the carrier's identity for the observation would restart
 * the read on every render that produced one.
 */
const observationOf = (runtime: RuntimeDependency | null): string => (runtime === null
  ? 'none'
  : [
    runtime.status.health,
    runtime.status.verified,
    runtime.status.installed_version ?? '',
    runtime.manifest.resolution,
  ].join(':'));

/** Enumeration joined with the separator the active language punctuates lists with
 *  (`, ` in English, `、` in Chinese), which `Intl.ListFormat`'s unit style drops
 *  entirely for zh. */
const formatNames = (names: readonly string[], separator: string): string =>
  names.join(separator);

/**
 * C4 names one owner for every setup supply read, so the shell may hand this screen
 * the authority it shares with the completion gate. Optional because a screen mounted
 * on its own is still a screen: it then owns one for its own life, which is what the
 * shipped behaviour already was.
 */
export type ProvidersScreenProps = SetupScreenProps & {
  agentReads?: CollectionReadAuthority<AgentSupply[]>;
};

export const ProvidersScreen = React.forwardRef<SetupScreenHandle, ProvidersScreenProps>(
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
    agentReads: sharedAgentReads,
  }, ref) {
    const { t } = useTranslation();
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
    const [ownAgentReads] = React.useState(createAgentCollectionReadAuthority);
    const agentReads = sharedAgentReads ?? ownAgentReads;

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
    // Against the Hub's own inventory: a key it is already supplying is not a batch
    // this screen should ask for, and the action continues instead of opening a review
    // of work that is done.
    const pending = React.useMemo(() => pendingImportRows(selection), [selection]);
    const importDeclined = selection.selectedBackends.length === 0
      && isMigrationDismissed(offeredImportKeys(selection));
    // ── Supply ──────────────────────────────────────────────────────────────

    // Whether the server's own row defaults have been honoured yet. A scan is nulled
    // again once a batch lands, so 「no scan held」 alone cannot answer this: it is
    // also true for the rescan that follows an import, whose consent has just been
    // spent on purpose and must not come back ticked.
    const seededSelectionRef = React.useRef(false);
    /**
     * Which request the answer on screen belongs to, or `null` while none does.
     *
     * A read with no answer yet, and a retry someone asked for, both mean the screen
     * is back to not knowing — that is what `reading` says and both have to say it. A
     * refresh a NEW OBSERVATION started is neither: the inventory it describes has not
     * been contradicted, so the answer in hand is still the best thing known about it,
     * and retracting it would take the action away for exactly as long as that refresh
     * runs. The same reason the shell holds a stale runtime region through a refresh
     * rather than falling back to `loading`.
     *
     * The window that costs is not a matter of patience. The observation that starts
     * the refresh is the same one that makes the action pressable — `hubAdmitted` needs
     * the runtime read this screen is re-reading against — so the two land one commit
     * apart: Continue turns pressable, and the effect that runs straight after takes it
     * back. A press that arrives in between reaches `activate` with the screen already
     * 「checking」 and is dropped, and nothing re-issues it. Holding the answer through
     * the refresh is what stops that commit from existing, rather than narrowing it.
     */
    const answeredRef = React.useRef<number | null>(null);
    /**
     * The runtime observation this screen's supply read is taken against.
     *
     * A supply read is answered by the controller, and D11 is what makes the controller
     * answerable. On a machine whose controller was merely stopped, the read taken on
     * arrival can fail for that reason alone — and nothing would ever come back for it,
     * because the sequence that fixes it publishes into the shell's runtime region, not
     * into this screen. The observation is therefore part of what the read is taken
     * against: one current read per observation. It adds no owner, no timer and no
     * second bootstrap, and a read that fails AFTER establishment is still the explicit
     * Retry it always was, because nothing new has been observed since.
     *
     * Held as the observation's own value — see `observationOf` — so it changes when a
     * genuinely new one lands and not when the shell merely re-reports the one already
     * standing. That is also how coming back to this screen brings current server facts
     * rather than the ones it left behind.
     */
    const observation = observationOf(foldRegionRead(runtimeRead, {
      loading: () => null,
      ready: (runtime) => runtime,
      unread: () => null,
      degraded: (stale) => stale,
    }));
    React.useEffect(() => {
      if (!active || !ready) return;
      let cancelled = false;
      // A read with no answer behind it is a read: while it is in flight the screen is
      // back to not knowing, which is what a retry means and what the action should
      // say. A refresh of an answer this screen already holds is not — see
      // `answeredRef`.
      if (answeredRef.current !== supplyToken) setSourceRead('reading');
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
        if (read.status === 'rejected') {
          // Nothing is held now, so the next refresh is a read with no answer behind
          // it again and says so.
          answeredRef.current = null;
          setSourceRead('unreadable');
        } else if (read.value.kind === 'current') {
          setSources(read.value.value);
          setSourceRead('read');
          answeredRef.current = supplyToken;
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
          const offer = offeredImportKeys({ scan: scanned, selectedBackends: [] });
          const priorDismissed = isMigrationDismissed(offeredImportKeys(previous.providerSelection));
          const dismissed = isMigrationDismissed(offer);
          const selectedBackends = first && previous.providerSelection.scan === null
            ? (dismissed ? [] : defaultSelection(scanned))
            : priorDismissed && !dismissed && previous.providerSelection.selectedBackends.length === 0
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
    }, [active, ready, observation, supplyToken, setFlowState, sourceReads]);

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
    // What the shell is handing down right now, for the attempt to record its own
    // refresh request against when it completes. An attempt spans awaits, so the
    // props its closure captured are a memory of the evidence rather than the
    // evidence — and the whole point of the record is which read came after it.
    const evidenceRef = React.useRef<GatewayEvidence>({ runtimeRead, gatewayEnabled, capability });
    evidenceRef.current = { runtimeRead, gatewayEnabled, capability };

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
          setGatewayRun(outcome.ok
            // Recorded against the evidence standing NOW, in the same update that
            // ends the attempt: the refresh below is this screen asking a question,
            // and the read it is asking about is by definition the next one.
            ? { kind: 'settled', against: evidenceRef.current }
            : { kind: 'failed', step: outcome.failure.step });
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

    // A retry that is waiting for the shell to answer. The press asks for a fresh read;
    // the ANSWER re-arms the attempt, never the press — the only read a press can see is
    // the one its failure was read from, so arming on it starts the same attempt against
    // the same snapshot, ahead of the shell and on evidence already known to be stale.
    const [resumeRequested, setResumeRequested] = React.useState(false);
    const requestedAgainstRef = React.useRef<GatewayEvidence | null>(null);

    const retryGateway = React.useCallback(() => {
      // One owner at a time: an attempt that has not reported still owns the engine, and
      // a second press over it is a second install or start on the same machine.
      if (gatewayRun.kind === 'running') return;
      requestedAgainstRef.current = { runtimeRead, gatewayEnabled, capability };
      setResumeRequested(true);
      // The shell owns the authoritative read. Asking for it is all this does.
      onRetrySetup();
    }, [gatewayRun.kind, runtimeRead, gatewayEnabled, capability, onRetrySetup]);

    React.useEffect(() => {
      if (!active || !resumeRequested) return;
      // Nothing to act on until the shell has answered the read this press asked for:
      // the same read and the same configuration it was made against is not new
      // information, and a different one that has not landed yet is not an answer
      // either. Spending the request on one loses it — the person pressed Retry, the
      // read they asked for arrives a moment later saying the engine is stopped, and
      // nothing starts it.
      if (!gatewayReadAnswered(requestedAgainstRef.current, { runtimeRead, gatewayEnabled, capability })) return;
      requestedAgainstRef.current = null;
      setResumeRequested(false);
      // Only an answer that still calls for a resume re-arms one. A configuration that
      // came back disabled ends the request instead of being overridden by it, and an
      // engine that is simply running now has nothing left to resume. Either way the
      // card goes on describing the fresh read rather than this screen's last attempt.
      if (resumeStep === null) return;
      setGatewayRun({ kind: 'idle' });
      setGatewayToken((token) => token + 1);
    }, [active, resumeRequested, runtimeRead, gatewayEnabled, capability, resumeStep]);

    // The other half of the same rule, for the refresh an ATTEMPT asks for when it
    // finishes. The answer to that one may not re-arm anything — a machine the engine
    // keeps dying on would reinstall forever without a person ever asking — so it is
    // recorded instead: a demand still standing is a dead end, and a dead end on this
    // screen is the card with the Retry on it. Recorded rather than re-derived each
    // render, because the press that follows starts another read, and a verdict
    // recomputed from a read in flight would take its own card away mid-press.
    React.useEffect(() => {
      if (gatewayRun.kind !== 'settled') return;
      if (!gatewayReadAnswered(gatewayRun.against, { runtimeRead, gatewayEnabled, capability })) return;
      setGatewayRun(resumeStep === null ? { kind: 'idle' } : { kind: 'failed', step: null });
    }, [gatewayRun, runtimeRead, gatewayEnabled, capability, resumeStep]);

    // Supply recovers on its own, because its failures are reads: a source list or a
    // scan that could not be fetched is what breaks the sentence, and the gateway
    // neither caused that nor can fix it. Rearming an install from it would be a server
    // mutation nobody asked for.
    const retrySupply = React.useCallback(() => setSupplyToken((token) => token + 1), []);

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

    // ── Write admission ─────────────────────────────────────────────────────

    // One rule for every control that can start a write, wherever it is drawn: the
    // cards, the footer, the capsule and the dialogs all admit the same thing, so a
    // control that is drawn somewhere else cannot admit what the footer refuses.
    //
    // Four separate facts, and a write needs all of them. What the machine's HEALTH is
    // (`intent.kind === 'running'`, the authoritative read rather than this screen's
    // attempt). Whether this flow is admitted to use the Hub at all (`ready`, the same
    // prerequisite C4 gates the next screen on). Whether this screen is the one the
    // person is on. And whether a write this screen already issued is still out.
    //
    // Health is not permission, and the card is right to say so: `gatewayIntent`
    // reports a running engine even while the configuration that admits the flow is
    // pending or off, on purpose — a truthful engine state is what the person needs to
    // read. It is not authorization to write to it, and reusing it as one is how a
    // Model Hub someone turned off still gets a source written into it.
    //
    // What this deliberately keeps is the case that looks like an exception and is
    // not: a healthy engine on a host that cannot INSTALL one is still a healthy
    // engine, and with the configuration enabled, writing to it is fine. That
    // boundary is about installing, not about writing.
    const hubAdmitted = ready && intent.kind === 'running';
    const writeAdmitted = active && hubAdmitted && !gatewayBusy && !verifying;

    // The one write on this screen that installs. `apply_native_migration` ensures the
    // runtime dependency before it touches a credential, and it does so unconditionally
    // — on a host whose runtime manifest resolves to unsupported that ensure fails ahead
    // of the branch that would have reused the engine already running, so the whole
    // batch comes back 422 no matter how healthy the engine is. Offering it there is
    // offering something the server has already decided to refuse.
    //
    // It narrows nothing else. A source, a key or an OAuth authorization is a write to
    // an engine that is up, and the paragraph above is why that stays available: the
    // boundary is about installing one, and this is the only button behind which an
    // install is still waiting to happen.
    const migrationAdmitted = writeAdmitted
      && setupCanAttemptInstall(capability, gatewayEnabled, runtimeRead);

    const openAdd = React.useCallback((more: boolean, vendor: string | null) => {
      if (!writeAdmitted || sourceRead !== 'read') return;
      setAddDialog({ more, vendor });
    }, [writeAdmitted, sourceRead]);

    // The takeover reads the machine for itself, and can take over a credential an
    // unreadable inventory knows nothing about; an unread source list is no reason to
    // withhold it. An engine that cannot be written to is.
    const openImport = React.useCallback(() => {
      if (!writeAdmitted) return;
      setImportOpen(true);
    }, [writeAdmitted]);

    // ── The action the shell renders ────────────────────────────────────────

    // What the screen knows, not what the list happens to hold: an inventory it
    // could not read is not an empty one, and 「添加」 offered against it is how a
    // credential that already exists gets written a second time.
    const hasSource = sourceRead === 'read' && sources.some(usableSource);

    const action = providerAction({
      pendingCount: pending.length,
      importFailed,
      supply: sourceRead === 'read'
        ? { kind: 'read', hasSource: hasSource || importDeclined }
        : { kind: sourceRead },
      gatewayBusy,
      // The same admission the dialogs are opened and submitted against, so the footer
      // cannot reach a write the rest of the screen refuses. `gatewayBusy` and
      // `verifying` are answered earlier in that ordering and stay separate facts.
      hubAdmitted,
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
            openImport();
            return;
          case 'continue':
            onNavigate('assistants');
            return;
          case 'add':
            openAdd(true, null);
            return;
          case 'retrySupply':
            // Asking again is the whole action. The effect flips back to 「reading」 on
            // its way in, so the button reports the retry it just started. It is the
            // supply read that failed, so the supply read is what it asks for again.
            retrySupply();
            return;
          default:
            // Busy. The shell already renders the action disabled; ignoring the call
            // rather than trusting that is what makes it unrepresentable.
        }
      },
    }), [action.kind, action.blocked, onNavigate, openAdd, openImport, retrySupply]);

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
      if (selectedBackends.length > 0) clearMigrationDismissed();
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
      setFlowState((previous) => {
        const selectedBackends = toggleSlotSelection(previous.providerSelection, slot);
        if (selectedBackends.length > 0) clearMigrationDismissed();
        return { ...previous, providerSelection: { ...previous.providerSelection, selectedBackends } };
      });
    }, [setFlowState]);

    const declineImport = React.useCallback(() => {
      writeMigrationDismissed(offeredImportKeys(selection));
      changeSelection([]);
    }, [selection, changeSelection]);

    // ── Sentence ────────────────────────────────────────────────────────────

    const summary = providerSummary({
      scan: selection.scan,
      sources,
      selected: selection.selectedBackends,
      failed: supplyFailed,
      reading: sourceRead === 'reading',
    });
    const summaryText = summary.kind === 'pending'
      ? ''
      : summary.kind === 'none'
      ? t('onboarding.providers.summaryNone')
      : summary.kind === 'error'
        ? t('onboarding.providers.summaryError')
        : [
          t(
            summary.kind === 'added'
              ? 'onboarding.providers.summaryAdded'
              : 'onboarding.providers.summarySelected',
            { count: summary.count, names: formatNames(summary.names, t('onboarding.providers.summaryNameSeparator')) },
          ),
        ].join(' · ');

    // ── The way on when nothing is connected ────────────────────────────────

    // Connecting a provider is what this screen is for, and it is still the only
    // thing the stage and the footer offer. But an inventory that answered「none」is
    // an answer: whoever meant to connect later — or declined the takeover on offer —
    // has nothing here to press, and a step whose every control stays put is a dead
    // end. So the way on is stated where the shell keeps what is ancillary to the
    // pair, as a sentence rather than a second button competing with the one above.
    //
    // It navigates and does nothing else. Adding a source, taking over a credential
    // and installing the engine each keep the control that already owns them, and
    // none of them happens on the way to the next screen. The engine's admission is
    // not asked about either: it gates writes, and this is not one — gating the way
    // out on it is how the dead end got here.
    //
    // The slot is the shell's, reached the same way the connection step reaches it,
    // and only while this screen is the one being read: a portal leaves the screen
    // root and with it the `inert` the shell puts on the others, so the sentence has
    // to answer to that activity itself.
    const routeSurfaceActive = useRouteSurfaceActive();
    const setupRoot = React.useRef<HTMLDivElement>(null);
    const [actionAside, setActionAside] = React.useState<HTMLElement | null>(null);
    React.useEffect(() => {
      setActionAside(setupRoot.current?.closest('.onboarding-step')
        ?.querySelector<HTMLElement>('[data-setup-action-aside]') ?? null);
    }, [onActionChange]);
    const onwardNode = sourceRead === 'read' && !hasSource && !importDeclined ? (
      <div className="onboarding-setup-hint">
        <p className="text-center text-xs text-muted">
          {t('onboarding.providers.continueHint')}{' '}
          <Button type="button" variant="link" size="xs" className="h-auto p-0 align-baseline text-xs"
            onClick={() => onNavigate('assistants')}>
            {t('onboarding.providers.actionContinue')}
          </Button>
        </p>
      </div>
    ) : null;

    return (
      <div className="onboarding-setup" ref={setupRoot}>
        <header className="onboarding-heading">
          {/* `h1` with a programmatic tab stop, like every other screen's heading: the
              shell moves focus here on activation, and a heading it cannot find or
              cannot focus leaves a keyboard journey standing on the footer button. */}
          <h1 tabIndex={-1}>{t('onboarding.providers.title')}</h1>
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
                  // The brand, not the slot's identity: the dialog opens on a catalog
                  // vendor, and a card standing for a credential the server named no
                  // provider for has none to open on.
                  onAdd={() => openAdd(false, slot.brand)}
                />
              ))}
              <AddMoreCard
                count={addedThroughMoreCount(flowState.addedThroughMore, sources)}
                onAdd={() => openAdd(true, null)}
              />
            </div>

            <SupplyWires
              direction="inbound"
              stage={stage}
              endpointSelector=".setup-provider-card"
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
                <Button type="button" variant="outline" size="sm" className="setup-summary-retry" onClick={retrySupply}>
                  {t('common.retry')}
                </Button>
              )}
            </p>
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
            // Admission is not only a door: it can be withdrawn while this is open, and
            // the write is the thing that must not happen then. Closing the dialog
            // instead would take down a flow that is mid-authorization and lose the
            // report of what it landed, which is worse than the press it prevents.
            writable={writeAdmitted}
            isSelected={(slot) => slotSelected(slot, selection.selectedBackends)}
            onToggleDetected={toggleSlot}
            onReviewDetected={() => { setAddDialog(null); openImport(); }}
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
            // The add dialog's admission plus the one thing only this batch needs: an
            // engine that stopped while the review was open has nothing to migrate keys
            // into, and a host that cannot install one has a server-side refusal waiting
            // behind the button. Either way the review stays readable and cancellable and
            // only the batch is refused — and Settings passes nothing and keeps writing,
            // as it always has.
            writable={migrationAdmitted}
            onChange={(next) => changeSelection(next.selectedBackends)}
            onApplied={(applied) => {
              // `onApplied(0)` is a refresh trigger, not a receipt: the takeover reports
              // a rejected batch that way, and the only observable difference between a
              // batch that landed and one that did not is this number. The count moves
              // only when something actually landed.
              //
              // The snapshot is spent on BOTH paths, in this tick. Success would
              // otherwise keep advertising rows that are now imported until the rescan
              // answers. A terminal rejection — `migration_credentials_invalid` closes
              // the dialog behind it — would otherwise leave the held scan and consent
              // looking like a batch still authorized to submit; a failed rescan must
              // not be able to revive those ids, and retrying them returns the same
              // error forever. Re-reading is what surfaces the reauthentication.
              setImportFailed(applied === 0);
              setFlowState((previous) => ({
                ...previous,
                importedCount: applied > 0 ? previous.importedCount + applied : previous.importedCount,
                providerSelection: { scan: null, selectedBackends: [] },
              }));
              setSupplyToken((token) => token + 1);
            }}
            onClose={() => setImportOpen(false)}
            onDecline={declineImport}
          />
        )}

        {onwardNode && active && routeSurfaceActive && actionAside
          ? createPortal(onwardNode, actionAside)
          : null}
      </div>
    );
  },
);
