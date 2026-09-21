import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactElement, type Ref } from 'react';
import { ArrowLeft, ArrowRight, RefreshCw } from 'lucide-react';
import { Button } from './ui/button';
import { AccessTiles } from './onboarding/AccessTiles';
import { RouteSurfaceActiveContext, useRouteSurfaceActive } from '@/lib/routeSurfaceActivity';
import { modelHubEnabledFromConfig } from './settings/models/featureFlags';
import { loadingRegion, beginRegionRead, failRegionRead } from './settings/models/regionRead';
import { INITIAL_SETUP_FLOW_STATE, setupBackTarget, setupCapability, setupNavigationReady, type SetupAction, type SetupCapability, type SetupScreenId, type SetupScreenHandle, type SetupScreenProps } from './onboarding/setupFlow';
import { mediaQuery, playSetupHandoff, setupHandoffAllowed } from './onboarding/setupHandoff';
import { fetchSetupConfig, type SetupConfigRead, type SetupConfigSnapshot } from './onboarding/setupConfig';
import { SETUP_REGISTERED_SCREENS } from './onboarding/setupScreenRegistry';
import './onboarding/onboarding.css';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { Welcome } from './steps/Welcome';
import { AgentDetection } from './steps/AgentDetection';
import logoImg from '@/assets/logo.png';
import { LanguageSwitcher } from './LanguageSwitcher';
import { useApi, type VibeAgentBrief } from '../context/ApiContext';
import { useStatus } from '../context/StatusContext';
import { setConfigField } from '../lib/configMutations';
import { useInstanceAuthorization } from '../context/InstanceAuthorizationContext';
import { SetupPlatformRecovery, type SavedPlatformRecovery } from './onboarding/SetupPlatformRecovery';
import { getEnabledPlatforms, getPlatformCatalog, platformHasRunnableConfig } from '../lib/platforms';
import { SetupModelRecovery } from './onboarding/SetupModelRecovery';
import { readOpencodeSetupRoutes } from './onboarding/opencodeSetupRoutes';
import { ASSISTANT_ORDER } from './onboarding/collaborationTimeline';

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

type FlowShellProps = {
  sequence: readonly SetupScreenId[];
  capability: SetupCapability;
  gatewayEnabled: boolean | null;
  onRetrySetup: () => void;
  loading?: boolean;
  error?: string;
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
export function SetupFlowShell({ sequence, capability, gatewayEnabled, onRetrySetup, loading = false, error = '', paused = false, navigationLocked = false, runtimeRead, renderScreen }: FlowShellProps) {
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
  const ready = setupNavigationReady(capability, gatewayEnabled);
  const policy = useRef({ ready, sequence, locked: navigationLocked });
  useLayoutEffect(() => { current.current = activation; policy.current = { ready, sequence, locked: navigationLocked }; });
  useLayoutEffect(() => {
    roots.current[activation.id]?.querySelector<HTMLElement>('h1')?.focus({ preventScroll: true });
  }, [activation]);
  useEffect(() => () => transition.current?.(), []);

  const navigate = useCallback((target: SetupScreenId) => {
    // The guard is here rather than on the Back button, because a screen's own
    // `onNavigate` reaches the same journey without touching that button at all.
    if (policy.current.locked) return;
    if (transitioning.current || !policy.current.sequence.includes(target) || target === current.current.id) return;
    const previous = current.current;
    const backwards = policy.current.sequence.indexOf(target) < policy.current.sequence.indexOf(previous.id);
    if (!backwards && !policy.current.ready) return;
    const next = { id: target, epoch: previous.epoch + 1 };
    const finish = () => {
      transition.current = null;
      transitioning.current = false;
      current.current = next;
      setAction(null);
      setHandoff(false);
      setActivation(next);
    };
    if (mediaQuery('(max-width: 759px)')) {
      host.current?.closest('.onboarding-shell')?.scrollTo({ top: 0, behavior: 'instant' });
      window.scrollTo({ top: 0, behavior: 'instant' });
    }
    if (backwards || !setupHandoffAllowed(paused) || !host.current || !roots.current[previous.id] || !roots.current[target]) {
      finish(); return;
    }
    transitioning.current = true;
    setHandoff(target === 'intro' ? false : target);
    transition.current = playSetupHandoff(host.current, roots.current[previous.id]!, roots.current[target]!, previous.id, target, finish);
  }, [paused]);
  // A callback belongs to this activation, even if an asynchronous consumer saves it.
  const feeds = useMemo(() => Object.fromEntries(sequence.map((id) => [id, {
    onActionChange: (next: SetupAction) => {
      if (current.current.id === id && current.current.epoch === activation.epoch && !transitioning.current) setAction(next);
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
          <RouteSurfaceActiveContext.Provider value={active && !handoff}>
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
      <p>{capability === 'disabled' || gatewayEnabled === false ? t('onboarding.flow.gatewayRequired') : error || t('onboarding.connection.readFailed')}</p>
    </div>}
    {/* The owner handoff, the design boards and the prototype all collapse the six entry
        tiles for the whole setup journey; the block stays mounted hidden and inert so its
        motion lifecycle survives, and the reserved stage keeps the anchor where it was. */}
    <AccessTiles active={false} />
  </div>;
}

export function Wizard() {
  const api = useApi(); const { t } = useTranslation(); const navigate = useNavigate();
  const { control } = useStatus();
  const { capabilities } = useInstanceAuthorization();
  const [platformRecovery, setPlatformRecovery] = useState<SavedPlatformRecovery | null>(null);
  const [recovery, setRecovery] = useState<VibeAgentBrief | null>(null);
  const [data, setData] = useState<Record<string, any> | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [capability, setCapability] = useState<SetupCapability>('pending');
  const [gatewayEnabled, setGatewayEnabled] = useState<boolean | null>(null);
  const configGeneration = useRef(0);
  // The shell owns the region and its request generation. L2 integrates a stateless
  // D11 loader here on active provider entry; it returns a validated RuntimeDependency.
  // Until that screen is registered, config reads authorize no runtime read/bootstrap.
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
      setError(result.detail || (result.status !== undefined ? `HTTP ${result.status}` : '') || t('onboarding.connection.readFailed'));
      setRuntimeRead((previous) => failRegionRead(previous));
      return null;
    }
    const snapshot = result.config;
    setData(snapshot.raw); setError('');
    setCapability(setupCapability(modelHubEnabledFromConfig(snapshot.raw)));
    setGatewayEnabled(snapshot.savedIntentEnabled);
    return snapshot;
  }, [t]);
  const load = useCallback(async () => {
    const generation = ++configGeneration.current;
    setRuntimeRead((previous) => beginRegionRead(previous));
    setLoading(true); setError(''); setCapability('pending'); setGatewayEnabled(null);
    const result = await fetchSetupConfig();
    // A newer read — another Retry, or a completion boundary — already owns the state.
    if (generation !== configGeneration.current) return;
    applyRead(result);
    setLoading(false);
  }, [applyRead]);
  useEffect(() => { void load(); return () => { configGeneration.current += 1; }; }, [load]);
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
      const readCandidates = async () => {
        const results = await Promise.allSettled(ASSISTANT_ORDER.map((name) => api.getBackendConnection(name)));
        return results.flatMap((result) => result.status === 'fulfilled' && result.value.ok ? [result.value] : []);
      };
      let connections = await readCandidates();
      if (!holds()) return;
      if (!connections.some((connection) => connection.entry_eligible)) throw new Error(t('onboarding.connection.entryFailed'));
      if (!connections.some((connection) => connection.ready)) {
        // Only confirmed stopped state allows this explicit start. Pending or
        // unknown IPC never becomes a restart request from the browser.
        if (!connections.some((connection) => connection.entry_eligible && connection.application === 'stopped')) throw new Error(t('onboarding.connection.applyPending'));
        const started = await control('start');
        if (!holds()) return;
        if (started?.ok === false) throw new Error(started.message || t('onboarding.connection.entryFailed'));
        connections = await readCandidates();
        if (!holds()) return;
      }
      const ready = new Set(connections.filter((connection) => connection.ready).map((connection) => connection.backend));
      if (!ready.size) throw new Error(t('onboarding.connection.entryFailed'));
      const agents = await api.listVibeAgents({ cache: false });
      if (!holds()) return;
      const candidates = agents.agents.filter((agent) => agent.enabled && !agent.archived && ready.has(agent.backend as typeof ASSISTANT_ORDER[number]));
      if (!agents.ok || !candidates.length) throw new Error(t('onboarding.connection.entryFailed'));
      let available = candidates.filter((agent) => agent.backend !== 'opencode');
      const opencode = candidates.filter((agent) => agent.backend === 'opencode');
      if (opencode.length) {
        try {
          const routes = await readOpencodeSetupRoutes(api);
          if (!holds()) return;
          available = [...available, ...opencode.filter((agent) => routes.accepts(agent.model))];
          if (!available.length && routes.mode === 'direct' && capabilities.can_manage_agents) {
            setPlatformRecovery(null);
            setRecovery(opencode.find((agent) => agent.name === agents.default_agent_name) || opencode[0]);
            return;
          }
        } catch (cause) {
          // An unused OpenCode failure cannot block another usable backend.
          if (!available.length) throw cause;
        }
      }
      if (!available.length) throw new Error(t('onboarding.connection.modelUnavailable'));
      // Everything above was awaited — a start, several connection reads, an Agent
      // listing — so the prerequisite is read once more before the writes that end
      // setup. A recovery's completion callback re-enters here and passes the same
      // boundary. This is not a cross-process transaction; it is the last moment the
      // browser can still see a gateway that was turned off underneath it.
      const settled = await readPrerequisite(lease);
      if (!settled) return;
      lease = settled.lease;
      // Preserve a usable selected Agent. First setup may have auto-seeded a
      // default for a missing backend; choose a real enabled Agent in that case.
      if (!available.some((agent) => agent.name === agents.default_agent_name)) {
        const selected = await api.setDefaultVibeAgent(available[0].name);
        // The default write is itself a wait, and the answer that authorised it can
        // stop being the current one inside that wait. What was written stands; what
        // it was written for — finishing setup — does not follow from it.
        if (!holds()) return;
        if (!selected.ok) throw new Error(t('onboarding.connection.entryFailed'));
      }
      // Persist completion last: failed start/readiness leaves AuthGuard's
      // existing setup gate intact. The locked config API validates existing IM.
      await api.mutateConfig([setConfigField(['setup_completed'], true)]);
      if (!holds()) return;
      navigate('/', { state: { onboardingCompleted: true } });
    } finally { completing.current = false; }
  };
  return <div className="onboarding-shell">
    <SetupHeader />
    <main className="onboarding-shell-content">
      <SetupFlowShell sequence={SETUP_REGISTERED_SCREENS} capability={capability} gatewayEnabled={gatewayEnabled}
        runtimeRead={runtimeRead} loading={loading} error={error} onRetrySetup={() => void load()}
        navigationLocked={Boolean(platformRecovery || recovery)} renderScreen={(id, props, ref) => id === 'intro'
          ? <Welcome ref={ref} data={data ?? undefined} active={props.active} onActionChange={props.onActionChange}
              onNext={(next) => { setData((previous) => ({ ...previous, ...Object(next) })); props.onNavigate(SETUP_REGISTERED_SCREENS[1]); }} />
          : <AgentDetection ref={ref} data={data ?? {}} active={props.active} onActionChange={props.onActionChange}
              completionRecovery={platformRecovery ? <SetupPlatformRecovery key={platformRecovery.descriptor.id} saved={platformRecovery} onRepaired={complete} onCancel={() => setPlatformRecovery(null)} /> : recovery ? <SetupModelRecovery key={recovery.id} agent={recovery} onComplete={complete} onCancel={() => setRecovery(null)} /> : undefined}
              onNext={complete} />}
      />
    </main>
  </div>;
}
