import { forwardRef, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactElement, type Ref } from 'react';
import { ArrowLeft, ArrowRight, RefreshCw } from 'lucide-react';
import { Button } from './ui/button';
import { AccessTiles } from './onboarding/AccessTiles';
import { RouteSurfaceActiveContext, useRouteSurfaceActive } from '@/lib/routeSurfaceActivity';
import { modelHubEnabledFromConfig } from './settings/models/featureFlags';
import { loadingRegion, readyRegion, beginRegionRead, failRegionRead } from './settings/models/regionRead';
import { modelsApi } from './settings/models/modelsApi';
import { apiFetch } from '@/lib/apiFetch';
import { INITIAL_SETUP_FLOW_STATE, setupBackTarget, setupCapability, setupNavigationReady, type SetupAction, type SetupCapability, type SetupScreenId, type SetupScreenHandle, type SetupScreenProps } from './onboarding/setupFlow';
import { captureSetupCards, mediaQuery, playSetupHandoff, setupHandoffAllowed, type SetupSnapshot } from './onboarding/setupHandoff';
import { fetchSetupConfig, type SetupConfigRead, type SetupConfigSnapshot } from './onboarding/setupConfig';
import { SETUP_REGISTERED_SCREENS } from './onboarding/setupScreenRegistry';
import './onboarding/onboarding.css';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { Welcome } from './steps/Welcome';
import { AgentDetection } from './steps/AgentDetection';
import { ProvidersScreen, type ProvidersScreenProps } from './onboarding/providers/ProvidersScreen';
import {
  GatewayBootstrapError,
  bootstrapGateway,
  readRuntimeObservation,
  type GatewayBootstrapDeps,
} from './onboarding/providers/gatewayBootstrap';
import logoImg from '@/assets/logo.png';
import { LanguageSwitcher } from './LanguageSwitcher';
import { useApi } from '../context/ApiContext';
import { useStatus } from '../context/StatusContext';
import { setConfigField } from '../lib/configMutations';
import { SetupPlatformRecovery, type SavedPlatformRecovery } from './onboarding/SetupPlatformRecovery';
import { getEnabledPlatforms, getPlatformCatalog, platformHasRunnableConfig } from '../lib/platforms';
import { ASSISTANT_ORDER } from './onboarding/collaborationTimeline';
import { createAgentCollectionReadAuthority } from './settings/models/collectionReadAuthority';
import { admitEntry, chooseEntryDefault, readEntryEvidence, type EntryGateDeps, type EntryRefusal } from './onboarding/entryGate';

/**
 * The setup's top bar, drawn as the design draws it at every size: the brand lockup —
 * the logo asset, which already carries its white tile, beside the two-line wordmark —
 * at the window's own gutter, and the shared language switcher wearing its round
 * trigger opposite it.
 */
function SetupHeader() {
  const { t } = useTranslation();
  return (
    <header>
      <div className="onboarding-brand">
        <span className="onboarding-brand-mark"><img src={logoImg} alt="" /></span>
        <span className="onboarding-brand-wordmark">
          <strong>{t('onboarding.brand.name')}</strong>
          <span>{t('onboarding.brand.tagline')}</span>
        </span>
      </div>
      <LanguageSwitcher variant="icon-round" />
    </header>
  );
}

/**
 * A read that failed, kept as two different kinds of string.
 *
 * `message` is the line a person reads, and it only ever comes from a bundle key — the
 * producer resolves it, so a Chinese setup is addressed in Chinese whatever the server
 * happened to say. `detail` is what the server or the transport said: English error
 * text, a backend code, a transport exception. That is diagnostics, not copy, and it is
 * shown as a disclosure beside the sentence rather than as the sentence.
 *
 * Because `message` is language-bound the moment it is produced, the read that produces
 * it re-runs when the language changes — which is also how a second config read can
 * legitimately start while an earlier one is still open.
 */
export type SetupReadFailure = {
  message: string;
  detail?: string;
};

/** Each C4 refusal keeps the sentence its own situation already had. */
const REFUSAL_MESSAGE = {
  entryFailed: 'onboarding.connection.entryFailed',
  applyPending: 'onboarding.connection.applyPending',
  modelUnavailable: 'onboarding.connection.modelUnavailable',
} as const satisfies Record<EntryRefusal, string>;

type FlowShellProps = {
  sequence: readonly SetupScreenId[];
  capability: SetupCapability;
  gatewayEnabled: boolean | null;
  onRetrySetup: () => void;
  loading?: boolean;
  error?: SetupReadFailure | null;
  paused?: boolean;
  /**
   * Something the parent is showing inside the active screen owns the flow until it
   * is finished or cancelled, so the shell may not move away from it.
   *
   * Private between this shell and its parent — deliberately NOT part of C2. A screen
   * publishes `busy` to say "work is running, show the spinner"; a recovery is usually
   * idle, waiting for someone to type. Those are different facts and the pair draws
   * them differently: this one holds the controls without claiming anything is running.
   */
  navigationLocked?: boolean;
  runtimeRead: SetupScreenProps['runtimeRead'];
  renderScreen: (id: SetupScreenId, props: SetupScreenProps, ref: Ref<SetupScreenHandle>) => ReactElement<{ ref?: Ref<SetupScreenHandle> }>;
};

function SetupScreenContent({ id, screenProps, renderScreen, ref }: {
  id: SetupScreenId; screenProps: SetupScreenProps; renderScreen: FlowShellProps['renderScreen']; ref: Ref<SetupScreenHandle>;
}) {
  return renderScreen(id, screenProps, ref);
}

/** One mounted screen collection and one action pair. Activation epochs reject late work. */
export function SetupFlowShell({ sequence, capability, gatewayEnabled, onRetrySetup, loading = false, error = null, paused = false, navigationLocked = false, runtimeRead, renderScreen }: FlowShellProps) {
  const { t } = useTranslation();
  const routeActive = useRouteSurfaceActive();
  const [activation, setActivation] = useState({ id: sequence[0], epoch: 0 });
  const [handoff, setHandoff] = useState<SetupScreenProps['handoff']>(false);
  const [action, setAction] = useState<SetupAction | null>(null);
  const [flowState, setFlowState] = useState(INITIAL_SETUP_FLOW_STATE);
  const host = useRef<HTMLDivElement>(null);
  const roots = useRef<Partial<Record<SetupScreenId, HTMLDivElement | null>>>({});
  const handles = useRef<Partial<Record<SetupScreenId, SetupScreenHandle | null>>>({});
  const current = useRef(activation);
  const transition = useRef<(() => void) | null>(null);
  const transitioning = useRef(false);
  /** A flight waiting for the screen it lands on to be on screen. */
  const flight = useRef<{ snapshot: SetupSnapshot; to: SetupScreenId } | null>(null);
  const ready = setupNavigationReady(capability, gatewayEnabled);
  const policy = useRef({ ready, sequence, locked: navigationLocked, routeActive });
  useLayoutEffect(() => { current.current = activation; policy.current = { ready, sequence, locked: navigationLocked, routeActive }; });
  useLayoutEffect(() => {
    roots.current[activation.id]?.querySelector<HTMLElement>('h1')?.focus({ preventScroll: true });
  }, [activation]);
  // The screens swap first and the identities fly afterwards, onto the screen that is
  // now really there. Landing into a live diagram is what the reference does, and it
  // is what keeps the wires, the summary and the action from appearing in one jump
  // once the flight is already over.
  useLayoutEffect(() => {
    const pending = flight.current;
    const incoming = roots.current[activation.id];
    if (!pending || pending.to !== activation.id || !host.current || !incoming) return;
    flight.current = null;
    const finish = () => {
      transition.current = null;
      transitioning.current = false;
      setHandoff(false);
    };
    transition.current = playSetupHandoff(host.current, pending.snapshot, incoming, pending.to, finish);
  }, [activation]);
  useEffect(() => () => transition.current?.(), []);

  const navigate = useCallback((target: SetupScreenId) => {
    // The guard is here rather than on the Back button, because a screen's own
    // `onNavigate` reaches the same journey without touching that button at all —
    // and an asynchronous continuation that settles after `/setup` was retained
    // behind another surface reaches it without anyone touching anything.
    //
    // Three facts decide whether this journey may move, and this is the one place
    // that answers for all three: the recovery hold, the activation that issued the
    // request, and whether the route surface holding this shell is the one being
    // read. A screen may re-check its own activity, but it cannot be the owner:
    // every screen would have to remember to, and a future one would not.
    if (policy.current.locked || !policy.current.routeActive) return;
    if (transitioning.current || !policy.current.sequence.includes(target) || target === current.current.id) return;
    const previous = current.current;
    const backwards = policy.current.sequence.indexOf(target) < policy.current.sequence.indexOf(previous.id);
    if (!backwards && !policy.current.ready) return;
    const next = { id: target, epoch: previous.epoch + 1 };
    if (mediaQuery('(max-width: 759px)')) {
      host.current?.closest('.onboarding-shell')?.scrollTo({ top: 0, behavior: 'instant' });
      window.scrollTo({ top: 0, behavior: 'instant' });
    }
    const animated = !backwards && setupHandoffAllowed(paused) && target !== 'intro'
      && !!host.current && !!roots.current[previous.id] && !!roots.current[target];
    if (animated) {
      transitioning.current = true;
      flight.current = { snapshot: captureSetupCards(roots.current[previous.id]!, previous.id), to: target };
    }
    current.current = next;
    setAction(null);
    setHandoff(animated ? target : false);
    setActivation(next);
  }, [paused]);
  // A callback belongs to this activation, even if an asynchronous consumer saves it.
  const feeds = useMemo(() => Object.fromEntries(sequence.map((id) => [id, {
    onActionChange: (next: SetupAction) => {
      if (current.current.id === id && current.current.epoch === activation.epoch) setAction(next);
    },
    onNavigate: (target: SetupScreenId) => {
      if (current.current.id === id && current.current.epoch === activation.epoch) navigate(target);
    },
  }])) as Record<SetupScreenId, Pick<SetupScreenProps, 'onActionChange' | 'onNavigate'>>, [activation.epoch, navigate, sequence]);
  const back = setupBackTarget(sequence, activation.id);
  const authoritativeBlock = capability === 'disabled' || gatewayEnabled === false;
  const retry = !loading && (authoritativeBlock || !!error);
  return <div className="onboarding-step" data-setup-sequence={sequence.join(' ')} data-setup-screen={activation.id} data-handoff={handoff || undefined}>
    <div className="onboarding-screens" ref={host}>
      {sequence.map((id) => {
        const active = routeActive && id === activation.id;
        return <div key={id} ref={(node) => { roots.current[id] = node; }} data-setup-screen-root={id}
          hidden={id !== activation.id} inert={!active || !!handoff}>
          <RouteSurfaceActiveContext.Provider value={active}>
            <SetupScreenContent id={id} screenProps={{ active, handoff, capability, gatewayEnabled, runtimeRead,
              onRetrySetup, flowState, setFlowState, ...feeds[id] }} renderScreen={renderScreen}
              ref={(handle) => { handles.current[id] = handle; }} />
          </RouteSurfaceActiveContext.Provider>
        </div>;
      })}
    </div>
    <div className="onboarding-setup-footer">
      <Button type="button" variant="brand" className="group onboarding-action-w onboarding-primary-action"
        disabled={loading || !!handoff || navigationLocked || (!retry && (!action || action.disabled || action.busy || !ready))}
        onClick={() => { if (transitioning.current || policy.current.locked) return; if (retry) onRetrySetup(); else if (ready && action && !action.disabled && !action.busy) handles.current[current.current.id]?.activate(); }}>
        {t(loading ? 'common.loading' : retry ? 'common.retry' : action?.labelKey ?? 'common.loading', action?.labelArgs)}
        {(loading || action?.busy || action?.icon === 'spinner') ? <RefreshCw size={16} className="motion-safe:animate-spin" /> : action?.icon === 'arrow-right' && <ArrowRight size={16} />}
      </Button>
      <Button type="button" variant="ghost" className="onboarding-action-w onboarding-back-action" style={{ visibility: back ? 'visible' : 'hidden' }}
        disabled={!back || !!handoff || !!action?.busy || navigationLocked} onClick={() => { if (back) navigate(back); }}>
        <ArrowLeft size={14} />{t(back === 'providers' ? 'onboarding.flow.backToProviders' : 'onboarding.flow.backToIntro')}
      </Button>
    </div>
    {/* Where a screen puts what is ancillary to the pair above: below it, in normal flow,
        so a caption that grows can neither move the anchor nor cover it. */}
    <div className="onboarding-action-aside" data-setup-action-aside="" />
    {(authoritativeBlock || (!!error && !loading)) && <div className="onboarding-flow-error" role="alert">
      <p>{authoritativeBlock ? t('onboarding.flow.gatewayRequired') : error?.message ?? t('onboarding.connection.readFailed')}</p>
      {/* Same shape the detection failure already uses one screen over: the sentence
          addresses the reader, the disclosure keeps what the server actually said. */}
      {!authoritativeBlock && error?.detail && <details className="mt-2 max-w-xl break-words">
        <summary>{t('onboarding.details')}</summary>{error.detail}</details>}
    </div>}
    {/* The owner handoff, the design boards and the prototype all collapse the six entry
        tiles for the whole setup journey; the block stays mounted hidden and inert so its
        motion lifecycle survives, and the reserved stage keeps the anchor where it was. */}
    <AccessTiles active={false} />
  </div>;
}

/**
 * The providers screen, plus the one fact about it the shell's props cannot state:
 * that this journey has ARRIVED here.
 *
 * D11 is an entry event, not a condition. Every screen is mounted from the first
 * render — that is how drafts survive leaving one — so a mount says nothing about
 * where the person is, and `active` turning true again on the way back from the
 * assistants must not seed a config or start a controller a second time. So the edge
 * is reported once, from inside the tree that knows it, and what to do with it stays
 * with the shell's single runtime owner.
 */
const ProvidersEntry = forwardRef<SetupScreenHandle, ProvidersScreenProps & { onEnter: () => void }>(
  function ProvidersEntry({ onEnter, ...props }, ref) {
    const { active } = props;
    useEffect(() => { if (active) onEnter(); }, [active, onEnter]);
    return <ProvidersScreen ref={ref} {...props} />;
  },
);

export function Wizard() {
  const api = useApi(); const { t } = useTranslation(); const navigate = useNavigate();
  const { control } = useStatus();
  const [platformRecovery, setPlatformRecovery] = useState<SavedPlatformRecovery | null>(null);
  const [data, setData] = useState<Record<string, any> | null>(null);
  const [error, setError] = useState<SetupReadFailure | null>(null);
  const [loading, setLoading] = useState(true);
  const [capability, setCapability] = useState<SetupCapability>('pending');
  const [gatewayEnabled, setGatewayEnabled] = useState<boolean | null>(null);
  const configGeneration = useRef(0);
  // The shell owns the region and its request generation; the stateless D11 sequence
  // below is what fills it, once the journey actually reaches the provider screen.
  // A config read on its own authorizes no runtime read and no bootstrap.
  const [runtimeRead, setRuntimeRead] = useState<SetupScreenProps['runtimeRead']>(() => loadingRegion());
  const completing = useRef(false);
  /**
   * The single place a config read becomes shell state.
   *
   * Unknown is not an opt-out. An unread answer — transport, HTTP, JSON or shape —
   * leaves the prerequisite pending/null and says so, which is what turns the shared
   * primary into an explicit Retry. It never decays into `false`, which would claim
   * the gateway is off, nor into `true`, which would let setup finish against a
   * prerequisite nobody could confirm.
   */
  const applyRead = useCallback((result: SetupConfigRead): SetupConfigSnapshot | null => {
    if (result.state === 'unread') {
      setCapability('pending'); setGatewayEnabled(null);
      // The sentence is copy, so both bundles own it, and an HTTP status is a number
      // the sentence can carry. What the server or the transport said is not copy — it
      // is written in whatever language that producer speaks — so it travels beside
      // the sentence as a disclosure and never becomes the sentence itself.
      setError({
        message: result.status !== undefined
          ? t('onboarding.connection.readFailedStatus', { status: result.status })
          : t('onboarding.connection.readFailed'),
        detail: result.detail,
      });
      setRuntimeRead((previous) => failRegionRead(previous));
      return null;
    }
    const snapshot = result.config;
    setData(snapshot.raw); setError(null);
    setCapability(setupCapability(modelHubEnabledFromConfig(snapshot.raw)));
    setGatewayEnabled(snapshot.savedIntentEnabled);
    return snapshot;
  }, [t]);
  const load = useCallback(async () => {
    const generation = ++configGeneration.current;
    setLoading(true); setError(null); setCapability('pending'); setGatewayEnabled(null);
    const result = await fetchSetupConfig();
    // A newer read — another Retry, or a completion boundary — already owns the state.
    if (generation !== configGeneration.current) return;
    applyRead(result);
    setLoading(false);
  }, [applyRead]);
  useEffect(() => { void load(); return () => { configGeneration.current += 1; }; }, [load]);
  // ── The runtime region's one owner ────────────────────────────────────────
  //
  // `load` deliberately does not touch the region. It re-runs for reasons that have
  // nothing to do with the machine — a language change re-resolves the copy a failed
  // read is reported in — and marking a ready region as refreshing there would strand
  // it: nothing would re-observe the runtime, because nothing asked anything to.
  // Reporting a read in flight belongs to whoever is actually reading, which is here.
  //
  // One ticket covers both reasons the runtime is read: arriving at the provider
  // screen, and an explicit retry. They are the same request — "observe the machine
  // now" — and keeping them as one number is also what keeps them countable: an
  // effect that re-runs for a config read landing, a capability arriving or a
  // re-render cannot turn one request into several, because only a new ticket is a
  // new request. Ticket 0 is "the journey has not been here yet".
  const [entryTicket, setEntryTicket] = useState(0);
  const enterProviders = useCallback(() => setEntryTicket((previous) => previous + 1), []);
  const attemptedTicket = useRef(-1);
  // Whether the sequence has already established what it establishes. Afterwards a
  // refresh is the observation alone: the config exists and the controller is up, so
  // re-seeding or re-starting would act on state that is already proven.
  //
  // Establishing once is not observing once. Coming back to the provider screen must
  // show the machine as it is now — something may have changed it while the journey
  // was on the assistants — so every arrival reads the runtime again, and only the
  // writes are what happen a single time.
  const bootstrapped = useRef(false);
  const runtimeGeneration = useRef(0);
  const bootstrapDeps = useMemo<GatewayBootstrapDeps>(() => ({
    // Wrapped rather than passed by reference: the sequence is a dependency injection
    // seam, and a test that spies on a module after this tree rendered must still be
    // the thing that runs.
    fetch: (input, init) => apiFetch(input, init),
    getBackendConnection: (backend) => api.getBackendConnection(backend),
    control: (action) => control(action),
    getRuntimeStatus: () => modelsApi.getRuntimeStatus(),
  }), [api, control]);
  // C4 names one owner for every setup supply read, and this is it: the provider
  // screen's adoption reads and the completion gate's corroboration share a single
  // generation, so a read one of them superseded cannot come back as evidence for
  // the other.
  const [setupAgentReads] = useState(createAgentCollectionReadAuthority);
  const entryDeps = useMemo<EntryGateDeps>(() => ({
    listAgents: () => api.listVibeAgents({ cache: false }),
    getBackendConnection: (backend) => api.getBackendConnection(backend),
    agentReads: setupAgentReads,
    getRuntimeStatus: () => modelsApi.getRuntimeStatus(),
    getVibeAgent: (name, params) => api.getVibeAgent(name, { ...params, cache: false }),
    detectCli: (binary) => api.detectCli(binary),
  }), [api, setupAgentReads]);
  const retrySetup = useCallback(() => {
    // Order matters only in what it means: re-read the configuration, and let the
    // runtime owner resume from whatever that read says — which, when it says the
    // gateway is off, is nothing at all.
    setEntryTicket((previous) => previous + 1);
    void load();
  }, [load]);
  useEffect(() => {
    if (entryTicket === 0 || loading) return;
    // C2's boundary, read from the shell's own config state rather than assumed: a
    // disabled deployment or a gateway somebody turned off authorizes no attempt.
    if (!setupNavigationReady(capability, gatewayEnabled)) return;
    if (attemptedTicket.current === entryTicket) return;
    attemptedTicket.current = entryTicket;
    const generation = ++runtimeGeneration.current;
    setRuntimeRead((previous) => beginRegionRead(previous));
    void (async () => {
      try {
        const runtime = bootstrapped.current
          ? await readRuntimeObservation(bootstrapDeps)
          : (await bootstrapGateway(bootstrapDeps, ASSISTANT_ORDER[0])).runtime;
        if (generation !== runtimeGeneration.current) return;
        bootstrapped.current = true;
        setRuntimeRead(readyRegion(runtime));
      } catch (cause) {
        if (generation !== runtimeGeneration.current) return;
        if (cause instanceof GatewayBootstrapError && cause.reason === 'disabled') {
          // The sequence read a configuration this shell does not have yet, and the
          // answer a person is owed is the configuration path rather than a retry.
          // Re-reading is how that arrives: the config owner is the only thing that
          // may set capability and saved intent, so the alert and the disabled cards
          // come from the same authoritative read as everything else, and nothing
          // here invents a flag or turns one back on.
          void load();
          return;
        }
        // Everything else is the region's own failure, and the provider screen's
        // gateway card owns what to offer for it. Raising a second flow-level error
        // beside that card would put two Retrys on screen for one machine.
        setRuntimeRead((previous) => failRegionRead(previous));
      }
    })();
  }, [loading, capability, gatewayEnabled, entryTicket, bootstrapDeps, load]);
  /**
   * Read the prerequisite as it is now, and answer whether setup may still act on it.
   *
   * `null` means stop, for the three reasons a caller must treat identically: the read
   * failed, the gateway is persisted off, or a newer read took ownership while this one
   * was in flight — a config Retry racing the completion owner. None of them may write.
   *
   * A successful read hands back the generation it claimed. That is the lease the
   * caller carries through its awaited work: an admission is only good for as long as
   * it is still the current answer. Passing a lease back in asks to renew it, which an
   * owner that has already lost it may not do — re-reading is how a live completion
   * continues, not how an obsolete one comes back to life.
   */
  const readPrerequisite = useCallback(async (held?: number): Promise<{ config: SetupConfigSnapshot; lease: number } | null> => {
    if (held !== undefined && held !== configGeneration.current) return null;
    const generation = ++configGeneration.current;
    const result = await fetchSetupConfig();
    if (generation !== configGeneration.current) return null;
    const snapshot = applyRead(result);
    if (!snapshot) return null;
    if (!setupNavigationReady(setupCapability(modelHubEnabledFromConfig(snapshot.raw)), snapshot.savedIntentEnabled)) return null;
    return { config: snapshot, lease: generation };
  }, [applyRead]);
  const complete = async () => {
    if (completing.current) return;
    completing.current = true;
    try {
      // The prerequisite is persisted state another browser, operator or Settings page
      // can switch off while this flow is open, so completion reads it instead of
      // trusting the initial load. A refusal here writes nothing and leaves the shell
      // showing why, with Retry.
      const entry = await readPrerequisite();
      if (!entry) return;
      // Everything below is awaited, and an answer read before a wait is not an answer
      // that survives it. The lease is what the rest of this operation acts under: each
      // step asks whether the admission it started from is still the current one, and a
      // newer load — a Retry, a language change, this shell unmounting — ends it here.
      // Writes already issued cannot be recalled; the lease is what stops the next one.
      let lease = entry.lease;
      const holds = () => lease === configGeneration.current;
      const enabledPlatforms = getEnabledPlatforms(entry.config.raw);
      const missing = getPlatformCatalog(entry.config.raw).find((platform) => enabledPlatforms.includes(platform.id) && !platformHasRunnableConfig(entry.config.raw, platform.id));
      if (missing) {
        setPlatformRecovery({ config: entry.config.raw, descriptor: missing });
        return;
      }
      // C4. The gate is correlated over ONE assistant, so the reads that feed it are
      // taken together and judged together; what the browser must not do is assemble a
      // verdict out of three facts about three different machines.
      let evidenceOptions = { config: entry.config.raw };
      let evidence = await readEntryEvidence(entryDeps, evidenceOptions);
      if (!holds()) return;
      // A superseded supply read is neither an answer nor a failure — a newer
      // generation of the shared authority is already reading, and that read is what
      // will settle this. Nothing is written and nothing is claimed.
      if (!evidence) return;
      let admission = admitEntry(evidence);
      if (admission.kind === 'refused' && admission.startable) {
        // Connection and Agent reads can outlast the saved-intent read that authorised
        // this click. A generation lease only notices a newer read in THIS shell; another
        // browser can turn the gateway off while those waits are open. Recheck the
        // persisted prerequisite immediately before the start, refuse unread/disabled/
        // superseded, and use the refreshed config for the post-recovery CLI evidence.
        const stillAuthorised = await readPrerequisite(lease);
        if (!stillAuthorised) return;
        lease = stillAuthorised.lease;
        evidenceOptions = { config: stillAuthorised.config.raw };
        // Only a connection the server confirmed `stopped` reaches here, and only
        // once: the re-read below is what admits, not this call's own answer, so a
        // machine that recovered on its own is never sent a second start. Startup
        // itself has no assistant or source prerequisite.
        const started = await control('start');
        if (!holds()) return;
        if (started?.ok === false) throw new Error(started.message || t('onboarding.connection.entryFailed'));
        const recovered = await readEntryEvidence(entryDeps, evidenceOptions);
        if (!holds()) return;
        if (!recovered) return;
        evidence = recovered;
        admission = admitEntry(evidence);
      }
      if (admission.kind === 'refused') {
        // No detour. Setup's own contract is that the Hub serves the assistants, so a
        // machine with no routable assistant is repaired where routes are configured —
        // the route editor on this screen, or the providers screen behind it — not by a
        // completion-time form that could only reach a backend the gate cannot admit.
        throw new Error(t(REFUSAL_MESSAGE[admission.reason]));
      }
      // Everything above was awaited — a start, several connection reads, an Agent
      // listing — so the prerequisite is read once more before the writes that end
      // setup. A recovery's completion callback re-enters here and passes the same
      // boundary. This is not a cross-process transaction; it is the last moment the
      // browser can still see a gateway that was turned off underneath it.
      const settled = await readPrerequisite(lease);
      if (!settled) return;
      lease = settled.lease;
      const choice = chooseEntryDefault(admission.candidates, evidence);
      if (choice) {
        const selected = await api.setDefaultVibeAgent(choice.agent.name);
        // The default write is itself a wait, and the answer that authorised it can
        // stop being the current one inside that wait. What was written stands; what
        // it was written for — finishing setup — does not follow from it.
        if (!holds()) return;
        if (!selected.ok) throw new Error(t('onboarding.connection.entryFailed'));
        // Confirmed by a fresh read rather than by the write's own answer: the next
        // screen runs on whatever the server thinks the default is.
        const confirmed = await api.listVibeAgents({ cache: false });
        if (!holds()) return;
        if (!confirmed.ok || confirmed.default_agent_name !== choice.agent.name) {
          throw new Error(t('onboarding.connection.entryFailed'));
        }
      }
      // Persist completion last: failed start/readiness leaves AuthGuard's
      // existing setup gate intact. The locked config API validates existing IM.
      //
      // Issued once, and settled by a read. A write that lost its reply on the way
      // back is indistinguishable here from one that never arrived, so it is neither
      // believed nor sent again — the config itself is the only thing that can say
      // which happened, and it is read uncached because a cached answer from before
      // the write cannot settle anything about it.
      try {
        await api.mutateConfig([setConfigField(['setup_completed'], true)]);
      } catch {
        // Unknown, not failed. The readback below is what decides.
      }
      if (!holds()) return;
      const persisted = await fetchSetupConfig();
      if (persisted.state !== 'read' || !persisted.config.setup_completed) {
        throw new Error(t('onboarding.flow.completionUnconfirmed'));
      }
      // Confirmed persisted, so the journey is over whatever else changed meanwhile:
      // the lease exists to stop the next write, not to take back a finished setup.
      navigate('/', { state: { onboardingCompleted: true } });
    } finally { completing.current = false; }
  };
  return <div className="onboarding-shell">
    <SetupHeader />
    <main className="onboarding-shell-content">
      <SetupFlowShell sequence={SETUP_REGISTERED_SCREENS} capability={capability} gatewayEnabled={gatewayEnabled}
        runtimeRead={runtimeRead} loading={loading} error={error} onRetrySetup={retrySetup}
        navigationLocked={Boolean(platformRecovery)} renderScreen={(id, props, ref) => id === 'intro'
          ? <Welcome ref={ref} data={data ?? undefined} active={props.active} onActionChange={props.onActionChange}
              onNext={(next) => { setData((previous) => ({ ...previous, ...Object(next) })); props.onNavigate(SETUP_REGISTERED_SCREENS[1]); }} />
          : id === 'providers'
          ? <ProvidersEntry ref={ref} {...props} agentReads={setupAgentReads} onEnter={enterProviders} />
          : <AgentDetection ref={ref} data={data ?? {}} active={props.active} onActionChange={props.onActionChange}
              flowState={props.flowState} setFlowState={props.setFlowState} onNavigate={props.onNavigate} agentReads={setupAgentReads}
              completionRecovery={platformRecovery ? <SetupPlatformRecovery key={platformRecovery.descriptor.id} saved={platformRecovery} onRepaired={complete} onCancel={() => setPlatformRecovery(null)} /> : undefined}
              onNext={complete} />}
      />
    </main>
  </div>;
}
