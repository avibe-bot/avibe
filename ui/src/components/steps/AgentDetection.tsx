import React, { useCallback, useEffect, useImperativeHandle, useLayoutEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import {
  ArrowLeft,
  ArrowRight,
  ArrowUpToLine,
  ChevronDown,
  ChevronUp,
  Download,
  ExternalLink,
  RefreshCw,
  Sliders,
} from 'lucide-react';
import { Link, useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';
import { useApi } from '../../context/ApiContext';
import { useToast } from '../../context/ToastContext';
import { BackendIcon } from '../visual';
import { AssistantRow } from '../onboarding/AssistantRow';
import { ASSISTANT_ORDER } from '../onboarding/collaborationTimeline';
import '../onboarding/onboarding.css';
import type { BackendId } from '../visual';
import { BackendLifecycleChip, type BackendLifecycleVisual } from '../settings/BackendLifecycleChip';
import { ToggleSwitch } from '../settings/SettingsPrimitives';
import { BackendConnectionDialog } from '../onboarding/BackendConnectionDialog';
import type { BackendConnectionState } from '@/context/ApiContext';
import { setConfigField } from '@/lib/configMutations';
import { OpencodePermissionSetup } from '../settings/shared/OpencodePermissionSetup';
import { modelHubEnabledFromConfig } from '../settings/models/featureFlags';
import type { SetupAction, SetupFlowState, SetupScreenHandle, SetupScreenId } from '../onboarding/setupFlow';
import type { BackendId as RuntimeBackendId } from '../settings/shared/useBackendRuntime';
import { useOpencodePermission } from '../settings/shared/useOpencodePermission';
import { Button } from '../ui/button';
import { DEFAULT_AGENT_STATE, getBackendUiMeta } from '@/lib/agentBackends';
import { useRouteSurfaceActive } from '@/lib/routeSurfaceActivity';
import { MODEL_HUB_SETTINGS_PATH } from '../settings/models/modelHubRoutes';
import { RouteChainDialog, type RouteChainSelection } from '../settings/models/RouteChainDialog';
import type { CollectionReadAuthority } from '../settings/models/collectionReadAuthority';
import type { AgentChain, AgentSupply, Source } from '../settings/models/types';
import type { AssistantRouteView } from '../onboarding/AssistantRow';
import { isBuiltinAgent } from '../onboarding/setupTargets';
import { readyRegion } from '../settings/models/regionRead';
import { modelsApi } from '../settings/models/modelsApi';

interface AgentDetectionProps {
  active?: boolean;
  ref?: React.Ref<SetupScreenHandle>;
  onActionChange?: (action: SetupAction) => void;
  data: any;
  onNext: (data: any) => void | Promise<void>;
  onBack?: (data?: { agents: Record<string, AgentState> }) => void;
  isPage?: boolean;
  onSave?: (data: { agents: Record<string, AgentState> }) => Promise<void> | void;
  flowState?: SetupFlowState;
  setFlowState?: React.Dispatch<React.SetStateAction<SetupFlowState>>;
  onNavigate?: (screen: SetupScreenId) => void;
  agentReads?: CollectionReadAuthority<AgentSupply[]>;
}

type AgentState = {
  enabled: boolean;
  cli_path: string;
  status?: 'unknown' | 'ok' | 'missing';
};

/**
 * The verdict of the latest settled enable write, and the intent that earned it.
 *
 * A write can finish with nobody reading the screen, and two reads can start at once
 * when the screen comes back. The intent is what makes the verdict answerable: only a
 * read asking the same question may report it, so a stale read can neither spend it
 * nor throw it away.
 */
type EnableReceipt = { intent: number; message: string };

/**
 * Whether this read answers for the outstanding verdict, or only reports it.
 *
 * Only an operation that succeeded at the same thing the verdict is about may spend
 * it: the card's own Retry, a provider connection that went through, an install that
 * worked. A dialog mounting, closing or being cancelled, a write-state notification
 * saying nothing is pending, an install or a read that failed — those are the
 * lifecycle talking, not an answer, and an apply failure the person has not dealt
 * with yet must still be there afterwards. An ordinary activation, detect or chip
 * refresh reports the verdict rather than replacing it with silence.
 */
type ConnectionRefresh = { acknowledge?: boolean };

const DEFAULT_AGENTS = DEFAULT_AGENT_STATE as Record<string, AgentState>;

// Backends with a dedicated provider config body (rendered in the wizard
// modal / the settings route). Mirrors ``BackendProviderConfig``'s switch —
// anything outside this set has no provider UI to configure.
const PROVIDER_BACKENDS: ReadonlySet<string> = new Set(['claude', 'codex', 'opencode']);

const normalizeAgents = (source: any): Record<string, AgentState> => {
  const raw = source?.agents || {};
  return Object.fromEntries(
    Object.entries(DEFAULT_AGENTS).map(([name, fallback]) => {
      const next = raw?.[name] || {};
      return [
        name,
        {
          ...fallback,
          ...next,
          status: next.status || fallback.status,
        },
      ];
    })
  );
};

// Mirrors design.pen JHgjz (Backends wizard step) and qVHh4 (Settings → Backends).
// Each backend renders as a two-row card: a header row (icon, label, one-line
// description, status pill, enable switch) and an action row (configure
// provider / set up Allow / install). Detection runs automatically on mount —
// the user enables what they have and installs anything missing.
export const AgentDetection: React.FC<AgentDetectionProps> = ({ data, onNext, onBack, isPage = false, onSave, active = true, ref, onActionChange, onNavigate, agentReads }) => {
  const { t } = useTranslation();
  const api = useApi();
  const { showToast } = useToast();
  const navigate = useNavigate();
  const routeSurfaceActive = useRouteSurfaceActive();
  const modelHubEnabled = modelHubEnabledFromConfig(data);
  const [visited, setVisited] = useState(active);
  const activation = useRef(0);
  const activeRef = useRef(active);
  useLayoutEffect(() => { activeRef.current = active; activation.current += 1; }, [active]);
  useEffect(() => { if (active) setVisited(true); }, [active]);
  const [agents, setAgents] = useState<Record<string, AgentState>>(normalizeAgents(data));
  const permission = useOpencodePermission({ autoFetchStatus: active });
  const [installingAgents, setInstallingAgents] = useState<Record<string, boolean>>({});
  const [installResults, setInstallResults] = useState<
    Record<string, { ok: boolean; message: string; output?: string | null }>
  >({});
  const [expandedOutputs, setExpandedOutputs] = useState<Record<string, boolean>>({});
  // Which backend's "Configure provider" modal is open (wizard mode only).
  const [providerModal, setProviderModal] = useState<{ backend: RuntimeBackendId; method: 'oauth' | 'api_key' } | null>(null);
  const [routeSelection, setRouteSelection] = useState<RouteChainSelection | null>(null);
  const [routeReconciliation, setRouteReconciliation] = useState({ pending: false, failed: false });
  const canEditSetupRoute = Boolean(onNavigate && agentReads);
  const [routeRead, setRouteRead] = useState<{
    done: boolean;
    failed: boolean;
    confirmed: { epoch: number; token: number } | null;
    chains: Partial<Record<RuntimeBackendId, AgentChain | null>>;
    models: Partial<Record<RuntimeBackendId, string | null>>;
    sources: Source[];
    supplies: AgentSupply[];
  }>({ done: false, failed: false, confirmed: null, chains: {}, models: {}, sources: [], supplies: [] });
  const routeReadToken = useRef(0);
  const routeTargetReady = () => active && activeRef.current && routeRead.done && !routeRead.failed
    && routeRead.confirmed?.epoch === activation.current
    && routeRead.confirmed?.token === routeReadToken.current;
  const readCardRoutes = useCallback(async () => {
    if (!agentReads) return false;
    const token = ++routeReadToken.current;
    const epoch = activation.current;
    setRouteRead((current) => ({ ...current, done: false, failed: false, confirmed: null }));
    try {
      const [supplyRead, listed, vibeAgents] = await Promise.all([
        agentReads.read(),
        modelsApi.listSources(),
        api.listVibeAgents({ cache: false, includeDisabled: true }),
      ]);
      if (supplyRead.kind !== 'current' || !vibeAgents.ok) throw new Error('Route inventory unreadable');
      const supplies = supplyRead.value;
      const fullAgents = await Promise.all(vibeAgents.agents.filter((row) =>
        !row.archived && ASSISTANT_ORDER.includes(row.backend as RuntimeBackendId))
        .map(async (row) => (await api.getVibeAgent(row.name, { cache: false })).agent ?? null));
      const models: Partial<Record<RuntimeBackendId, string | null>> = {};
      const chains: Partial<Record<RuntimeBackendId, AgentChain | null>> = {};
      await Promise.all(ASSISTANT_ORDER.map(async (backend) => {
        const candidates = fullAgents.filter((row) => row?.backend === backend && isBuiltinAgent(row));
        const selected = candidates.find((row) => row?.name === backend)
          ?? candidates.find((row) => row?.name === 'default') ?? candidates[0];
        models[backend] = selected?.model ?? null;
        const supply = supplies.find((row) => row.backend === backend);
        if (supply?.mode === 'hub' && selected?.model) {
          const backendChains = await modelsApi.getAgentChains(backend);
          chains[backend] = backendChains.find((chain) => chain.model_id === selected.model) ?? null;
        }
      }));
      if (token !== routeReadToken.current || epoch !== activation.current) return false;
      setRouteRead({ done: true, failed: false, confirmed: { epoch, token }, chains, models, sources: listed, supplies });
      return true;
    } catch {
      if (token !== routeReadToken.current || epoch !== activation.current) return false;
      setRouteRead((current) => ({ ...current, done: true, failed: true, confirmed: null }));
      return false;
    }
  }, [agentReads, api]);
  useEffect(() => {
    if (!active || !modelHubEnabled || !canEditSetupRoute) return;
    void readCardRoutes();
  }, [active, modelHubEnabled, canEditSetupRoute, readCardRoutes]);
  const modelLabel = (hop: { source_id: string; model_id: string }): string => {
    const source = routeRead.sources.find((row) => row.id === hop.source_id);
    const model = source?.models?.find((row) => row.id === hop.model_id);
    const catalog = routeRead.supplies.flatMap((supply) => supply.catalog_models ?? [])
      .find((row) => row.id === hop.model_id);
    return model?.display_name?.trim() || catalog?.display_name?.trim() || hop.model_id;
  };
  const routeViewFor = (backend: RuntimeBackendId): AssistantRouteView => {
    const loading = Boolean(canEditSetupRoute && modelHubEnabled && !routeRead.failed && !routeTargetReady());
    const chain = routeRead.chains[backend];
    const mine = chain?.chain[0] ?? null;
    return {
      loading,
      model: mine ? modelLabel(mine) : null,
      backups: Math.max(0, (chain?.chain.length ?? 0) - 1),
      noModel: routeTargetReady() && !routeRead.models[backend],
    };
  };
  const [connections, setConnections] = useState<Partial<Record<RuntimeBackendId, BackendConnectionState>>>({});
  const [connectionConfirmed, setConnectionConfirmed] = useState<Partial<Record<RuntimeBackendId, { epoch: number; token: number }>>>({});
  const [connectionPending, setConnectionPending] = useState<Partial<Record<RuntimeBackendId, boolean>>>({});
  const [connectionErrors, setConnectionErrors] = useState<Partial<Record<RuntimeBackendId, string>>>({});
  const [pendingWrites, setPendingWrites] = useState<Partial<Record<RuntimeBackendId, boolean>>>({});
  const [entering, setEntering] = useState(false);
  const [entryError, setEntryError] = useState('');
  const connectionTokens = useRef<Partial<Record<RuntimeBackendId, number>>>({});
  const previousRouteSurfaceActive = useRef(routeSurfaceActive);
  const enableQueue = useRef(Promise.resolve());
  const enableIntent = useRef<Partial<Record<RuntimeBackendId, number>>>({});
  const pendingEnable = useRef<Partial<Record<RuntimeBackendId, number>>>({});
  // What the latest settled write still owes this screen, held until someone who can
  // answer for it reports it — see EnableReceipt.
  const enableReceipt = useRef<Partial<Record<RuntimeBackendId, EnableReceipt>>>({});
  const confirmedConnectionMode = (backend: RuntimeBackendId) => {
    const confirmed = connectionConfirmed[backend];
    if (!active || !activeRef.current || connections[backend]?.ok !== true || connectionPending[backend]
      || confirmed?.epoch !== activation.current || confirmed.token !== connectionTokens.current[backend]) return undefined;
    return connections[backend]?.supply_mode ?? (!modelHubEnabled ? 'direct' : undefined);
  };
  const confirmedSupplyMode = (backend: RuntimeBackendId) => routeTargetReady()
    ? routeRead.supplies.find((row) => row.backend === backend)?.mode
    : undefined;
  const installMode = (backend: RuntimeBackendId) => {
    const connection = confirmedConnectionMode(backend);
    const supply = confirmedSupplyMode(backend);
    return connection && supply && connection !== supply ? undefined : connection;
  };
  // One provider modal/reconciliation at a time; Configure and navigation stay
  // disabled until persisted fields and the subsequent detection reach agents.
  const [syncing, setSyncing] = useState(false);
  const [detectingAgents, setDetectingAgents] = useState<Record<string, boolean>>({});
  const [detectionErrors, setDetectionErrors] = useState<Record<string, string>>({});
  // The lifecycle the each card's chip derives, reported up so the card can draw
  // the action the pill offers — update or upgrade-in-flight — on its state row.
  const [visuals, setVisuals] = useState<Partial<Record<string, BackendLifecycleVisual>>>({});
  const [refreshingAgents, setRefreshingAgents] = useState<Record<string, boolean>>({});
  const [chipRefresh, setChipRefresh] = useState<Record<string, number>>({});
  // A successful upgrade leaves the chip's own runtime probe in flight while this
  // handler has already finished, so the pill still reads `update` for a moment and
  // would re-arm the card's upgrade button against a backend that was just upgraded.
  // The lock holds the button disabled until the chip's reported visual leaves
  // `update`, which is the probe confirming what the upgrade did.
  const [upgradeLocks, setUpgradeLocks] = useState<Record<string, boolean>>({});
  const pendingInstalls = useRef(new Set<string>());
  const detectionTokens = useRef<Record<string, number>>({});
  const isMissing = (agent: AgentState) => agent.status === 'missing';

  const refreshConnection = useCallback(async (name: RuntimeBackendId, { acknowledge = false }: ConnectionRefresh = {}) => {
    if (!activeRef.current || pendingEnable.current[name] !== undefined) return;
    const epoch = activation.current;
    const intent = enableIntent.current[name];
    const token = (connectionTokens.current[name] || 0) + 1;
    connectionTokens.current[name] = token;
    // Read the verdict, don't take it. Starting is not accepting: this read may turn
    // out to be stale, and it may be answering a different intent entirely — either
    // way the verdict has to still be there for the read that can report it.
    const receipt = enableReceipt.current[name];
    const owed = receipt && receipt.intent === intent ? receipt : undefined;
    const owns = () => epoch === activation.current && connectionTokens.current[name] === token && enableIntent.current[name] === intent;
    setConnectionPending((current) => ({ ...current, [name]: true }));
    // Nothing owed means nothing to say about the write, which is not the same as
    // saying it went fine: an ordinary refresh leaves a reported failure standing.
    if (owed) setConnectionErrors((current) => ({ ...current, [name]: owed.message }));
    try {
      const result = await api.getBackendConnection(name);
      if (!owns()) return;
      if (!result.ok) throw new Error(result.message || t('onboarding.connection.readFailed'));
      setConnections((current) => ({ ...current, [name]: result }));
      setConnectionConfirmed((current) => ({ ...current, [name]: { epoch, token } }));
      setAgents((current) => ({ ...current, [name]: { ...current[name], enabled: result.enabled } }));
      // Spending the verdict takes all three: this read still owns the screen, the
      // caller is an operation that actually answers for it, and the verdict in hand
      // is the one that was there when the read began. A newer write that landed
      // meanwhile is still owed to whoever reads next.
      const retire = acknowledge && owed && enableReceipt.current[name] === owed;
      if (retire) delete enableReceipt.current[name];
      setConnectionErrors((current) => ({
        ...current,
        [name]: retire ? '' : owed?.message || '',
      }));
    } catch (error) {
      if (!owns()) return;
      setConnections((current) => ({ ...current, [name]: undefined }));
      // A read that also failed does not get to bury what the write reported, and it
      // answers for nothing: the apply failure is the original evidence, it is the one
      // the person can act on, and it stays owed until something actually settles it.
      setConnectionErrors((current) => ({ ...current, [name]: owed?.message || String(error) }));
    } finally {
      if (connectionTokens.current[name] === token) setConnectionPending((current) => ({ ...current, [name]: false }));
    }
  }, [api, t]);
  // Being read again is one event with one owner, however it arrives: the shell
  // activates this screen, or the route surface it sits on comes back. In the shell both
  // happen in the same commit, so two effects would mean two refreshes — and neither of
  // them consumes the write verdict, so whichever one wins still reports it.
  useEffect(() => {
    const leftSurface = previousRouteSurfaceActive.current && !routeSurfaceActive;
    previousRouteSurfaceActive.current = routeSurfaceActive;
    // Returning also collects what a write settled while there was nobody to tell.
    if (!isPage && active && !leftSurface) for (const name of ASSISTANT_ORDER) {
      void refreshConnection(name);
    }
    // Leaving drops in-flight READS only. A queued enable intent is a write the person
    // already asked for; invalidating it here would strand `pendingEnable` behind a
    // completion whose intent check can never pass again.
    return () => { for (const name of ASSISTANT_ORDER) {
      connectionTokens.current[name] = (connectionTokens.current[name] || 0) + 1;
    } };
  }, [refreshConnection, isPage, active, routeSurfaceActive]);

  const isAnyInstalling = Object.values(installingAgents).some(Boolean);

  useEffect(() => {
    if (!active || (!onActionChange && !isPage && data.__onboardingDetected)) return;
    // The first showing adopts the parent snapshot, because Welcome may have finished
    // detection after this retained screen mounted. After that the screen owns its own
    // paths: the snapshot predates every install, provider write and probe made here, so
    // copying it back on re-entry would probe a binary nobody chose and report a backend
    // missing while the right path is the one persisted. `visited` is still false in the
    // render that activates the screen, which is exactly that first showing.
    const source = visited ? agents : normalizeAgents(data);
    if (!visited) {
      setAgents((previous) => Object.fromEntries(Object.entries(previous).map(([name, agent]) => [name, { ...agent, cli_path: source[name].cli_path }])));
    }
    void Promise.all(Object.entries(source).map(([name, agent]) => detect(name, agent.cli_path)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active]);

  const detect = async (name: string, binary?: string) => {
    if (!activeRef.current) return;
    const epoch = activation.current;
    const token = (detectionTokens.current[name] || 0) + 1;
    detectionTokens.current[name] = token;
    setDetectingAgents((prev) => ({ ...prev, [name]: true }));
    setDetectionErrors((prev) => ({ ...prev, [name]: '' }));
    try {
      const result = await api.detectCli(binary || name);
      if (epoch !== activation.current || detectionTokens.current[name] !== token) return;
      if (result.found) {
        setInstallResults((prev) => {
          if (!prev[name] || prev[name].ok) return prev;
          const next = { ...prev };
          delete next[name];
          return next;
        });
      }
      if (!isPage) void refreshConnection(name as RuntimeBackendId);
      setAgents((prev) => ({
        ...prev,
        [name]: {
          ...prev[name],
          cli_path: result.path || prev[name].cli_path,
          status: result.found ? 'ok' : 'missing',
        },
      }));
    } catch (error) {
      if (epoch !== activation.current || detectionTokens.current[name] !== token) return;
      setDetectionErrors((prev) => ({ ...prev, [name]: String(error) }));
      setAgents((prev) => ({ ...prev, [name]: { ...prev[name], status: 'unknown' } }));
    } finally {
      if (detectionTokens.current[name] === token) setDetectingAgents((prev) => ({ ...prev, [name]: false }));
    }
  };

  const detectAll = async () => {
    await Promise.all(Object.entries(agents).map(([name, agent]) => detect(name, agent.cli_path)));
  };

  // Re-read persisted config after the provider modal closes so runtime edits
  // made inside it (enabled / cli_path via useBackendRuntime) flow back into the
  // wizard's local ``agents`` state — otherwise handlePrimaryAction would save a
  // stale snapshot and clobber them. Then re-detect to refresh the status pill.
  const syncBackendFromConfig = async (name: string) => {
    setSyncing(true);
    let cliPath = agents[name]?.cli_path;
    try {
      const config = await api.getConfig();
      const saved = config?.agents?.[name];
      if (saved) {
        if (typeof saved.cli_path === 'string' && saved.cli_path) {
          cliPath = saved.cli_path;
        }
        // Spread the full persisted backend so provider-level fields the modal
        // may have changed (e.g. opencode default_provider) are
        // refreshed too — not just enabled / cli_path.
        const merged: AgentState = {
          ...agents[name],
          ...saved,
          cli_path: cliPath || agents[name].cli_path,
        };
        // ``enabled`` is owned by the live card toggle, never this async sync's
        // snapshot: closing the provider modal kicks off this sync, but the user
        // may flip the toggle before it resolves. Apply the *live* enable state
        // here so a just-flipped toggle is never reverted. Navigation consumes
        // the live row after detection, never this pre-detection snapshot.
        setAgents((prev) => ({
          ...prev,
          [name]: { ...prev[name], ...merged, enabled: prev[name].enabled },
        }));
      }
      await detect(name, cliPath);
    } catch {
      // Best-effort: fall back to whatever we already have.
    } finally {
      setSyncing(false);
    }
  };

  const toggle = (name: string, enabled: boolean) => {
    setAgents((prev) => ({ ...prev, [name]: { ...prev[name], enabled } }));
    if (isPage) return;
    const backend = name as RuntimeBackendId;
    const intent = (enableIntent.current[backend] || 0) + 1;
    enableIntent.current[backend] = intent;
    pendingEnable.current[backend] = intent;
    connectionTokens.current[backend] = (connectionTokens.current[backend] || 0) + 1;
    setConnectionPending((current) => ({ ...current, [backend]: true }));
    setConnections((current) => ({ ...current, [backend]: undefined }));
    // Persist each queued intent, but only the latest intent may publish state.
    enableQueue.current = enableQueue.current.then(async () => {
      let receiptError = '';
      try {
        const saved = await api.mutateConfig([setConfigField(['agents', backend, 'enabled'], enabled)]);
        const applied = saved?.agent_backend_runtime;
        if (applied && !applied.hot_reconciled && !applied.restart_scheduled && !applied.apply_on_next_start) {
          receiptError = applied.restart_error || applied.error || t('onboarding.connection.applyFailed');
        }
      } catch (error) { receiptError = String(error); }
      if (enableIntent.current[backend] !== intent) return;
      delete pendingEnable.current[backend];
      // Record the verdict before anyone is asked to read it, whether or not there is
      // anybody to tell. Two reads can start at once when the screen comes back, and
      // the one that loses the race used to take the message with it.
      enableReceipt.current[backend] = { intent, message: receiptError };
      // A screen the person has left may neither read nor publish; the verdict waits.
      if (!activeRef.current) return;
      // This uncached projection reads persisted enabled even after a rejected
      // write. Apply failure cannot roll back config that was already committed.
      await refreshConnection(backend);
      // The presence read is a follow-up to the write, not the write itself. It is also
      // the only thing in here that can reject, and this job is a link in the serial
      // queue: a rejection settles the queue rejected, so every toggle after it chains
      // onto a continuation that never runs and the person's next enable silently does
      // nothing. Its failure is reported where this screen already reports a read that
      // failed, and it does not bury what the write had to say.
      try {
        await agentReads?.refresh();
        if (modelHubEnabled && canEditSetupRoute) await readCardRoutes();
      } catch (error) {
        if (enableIntent.current[backend] !== intent) return;
        setConnectionErrors((current) => ({ ...current, [backend]: current[backend] || String(error) }));
      }
    });
  };

  // The setup card draws the update action on the state row, beside the pill the
  // chip renders, while the chip still owns the probe and the write. Bumping
  // `chipRefresh` is what lets the chip re-probe a runtime the card's own button
  // changed, without the card duplicating the chip's lifecycle knowledge.
  useEffect(() => {
    setUpgradeLocks((current) => {
      let changed = false;
      const next = { ...current };
      for (const [name, locked] of Object.entries(current)) {
        if (locked && visuals[name] !== 'update' && visuals[name] !== 'updating') {
          next[name] = false;
          changed = true;
        }
      }
      return changed ? next : current;
    });
  }, [visuals]);

  const releaseUpgradeLock = (name: string) =>
    setUpgradeLocks((current) => (current[name] ? { ...current, [name]: false } : current));

  const upgradeAgent = async (name: string) => {
    setRefreshingAgents((current) => ({ ...current, [name]: true }));
    setUpgradeLocks((current) => ({ ...current, [name]: true }));
    // The chip's own upgrade handler owns the toast contract for lifecycle
    // operations; the card's affordance is the same operation drawn on the state
    // row, so it settles failures the same way rather than swallowing them, and
    // `refreshingAgents` is handed to the chip as externally busy so its popover
    // cannot launch a second install against the same backend.
    try {
      const result = await api.installAgent(name);
      if (result.ok) {
        showToast(t('backendLifecycle.upgradeSuccess'), 'success');
        const installedPath = typeof result.path === 'string' && result.path ? result.path : null;
        if (installedPath) {
          setAgents((prev) => ({ ...prev, [name]: { ...prev[name], cli_path: installedPath } }));
        }
        setChipRefresh((current) => ({ ...current, [name]: (current[name] || 0) + 1 }));
        await detect(name, installedPath || agents[name]?.cli_path || name);
      } else {
        // A failed upgrade leaves the pill on `update` with no probe in flight, so
        // the visuals-driven release never fires; settle the lock here instead of
        // stranding the button disabled until a remount.
        releaseUpgradeLock(name);
        showToast(result.message || t('backendLifecycle.upgradeFailed'), 'error');
      }
    } catch (cause) {
      releaseUpgradeLock(name);
      showToast(String(cause), 'error');
    } finally {
      setRefreshingAgents((current) => ({ ...current, [name]: false }));
    }
  };

  const installAgent = async (name: string, promisedEnable = false) => {
    if (pendingInstalls.current.has(name) || (isPage && isAnyInstalling)) return;
    const backend = name as RuntimeBackendId;
    const installEnableIntent = enableIntent.current[backend];
    const hubAtClick = promisedEnable && installMode(backend) === 'hub';
    pendingInstalls.current.add(name);

    setInstallingAgents((prev) => ({ ...prev, [name]: true }));
    setInstallResults((prev) => ({ ...prev, [name]: { ok: false, message: '', output: null } }));
    setExpandedOutputs((prev) => ({ ...prev, [name]: false }));

    let installed = false;
    try {
      const result = await api.installAgent(name);
      installed = result.ok;
      const installedPath = typeof result.path === 'string' && result.path ? result.path : null;
      setInstallResults((prev) => ({
        ...prev,
        [name]: { ok: result.ok, message: result.message, output: result.output },
      }));
      if (result.ok) {
        if (installedPath) {
          setAgents((prev) => ({
            ...prev,
            [name]: { ...prev[name], cli_path: installedPath },
          }));
        }
        await detect(name, installedPath || agents[name]?.cli_path || name);
        if (!isPage && hubAtClick && !agents[name]?.enabled
          && enableIntent.current[backend] === installEnableIntent) {
          try {
            const current = await api.getBackendConnection(backend);
            if (current.ok && current.supply_mode === 'hub' && !current.enabled
              && enableIntent.current[backend] === installEnableIntent) toggle(name, true);
          } catch {
            // The connection refresh below reports an unreadable post-install state.
          }
        }
        await agentReads?.refresh();
      }
    } catch (e) {
      setInstallResults((prev) => ({
        ...prev,
        [name]: { ok: false, message: String(e), output: null },
      }));
    } finally {
      pendingInstalls.current.delete(name);
      setInstallingAgents((prev) => ({ ...prev, [name]: false }));
      // An install that failed reports its own failure and answers for nothing else;
      // only one that worked replaces what the enable write had to say.
      if (!isPage) void refreshConnection(name as RuntimeBackendId, { acknowledge: installed });
    }
  };

  const toggleOutput = (name: string) => {
    setExpandedOutputs((prev) => ({ ...prev, [name]: !prev[name] }));
  };

  const opencodeAgent = agents['opencode'];
  const readyBackends = ASSISTANT_ORDER.filter((name) => agents[name].enabled && agents[name].status === 'ok'
    && !installingAgents[name] && !detectingAgents[name] && !connectionPending[name]
    && !pendingWrites[name] && !refreshingAgents[name] && !connectionErrors[name] && connections[name]?.entry_eligible);
  const canContinue = isPage ? Object.values(agents).some((agent) => agent.enabled) : readyBackends.length > 0;
  const primaryPending = useRef(false);
  const handlePrimaryAction = async () => {
    if (!active || entering || primaryPending.current) return;
    primaryPending.current = true;
    if (isPage && onSave) { try { await onSave({ agents }); } finally { primaryPending.current = false; } return; }
    setEntering(true); setEntryError('');
    try {
      await enableQueue.current;
      await onNext({ agents, readyBackends });
    } catch (error) { setEntryError(String(error)); }
    finally { primaryPending.current = false; setEntering(false); }
  };


  const actionBusy = syncing || entering || isAnyInstalling || Object.values(pendingWrites).some(Boolean) || Object.values(refreshingAgents).some(Boolean);
  useImperativeHandle(ref, () => ({ activate: () => { if (canContinue && !actionBusy) void handlePrimaryAction(); } }));
  // A layout effect, so the shell's action label lands in the same commit as the state
  // it describes: a passive publish would leave one render where the screen already
  // shows a settled state while the shared button still carries the previous label.
  useLayoutEffect(() => {
    if (active) onActionChange?.({ labelKey: entering ? 'onboarding.connection.connecting' : 'onboarding.connection.enter',
      disabled: !canContinue || actionBusy, busy: actionBusy, icon: entering ? 'spinner' : 'arrow-right' });
  }, [active, onActionChange, entering, canContinue, actionBusy]);

  // What sits under the shared pair in the shell: the readiness caption and the
  // OpenCode permission callout. Both are ancillary to the action and both grow — a
  // permission error carries a full diagnostic. Kept inside the screen they push the
  // anchor the two steps share; lifted out of flow they cover the very buttons they
  // explain. So the shell reserves a slot after the pair and the active screen portals
  // them into it.
  const setupRoot = useRef<HTMLDivElement>(null);
  const [actionAside, setActionAside] = useState<HTMLElement | null>(null);
  useEffect(() => {
    setActionAside(onActionChange
      ? setupRoot.current?.closest('.onboarding-step')?.querySelector<HTMLElement>('[data-setup-action-aside]') ?? null
      : null);
  }, [onActionChange]);
  // Only while the action is held. Once it lights up, the cards already say which
  // assistants are connected and the button says the rest; a caption repeating it is a
  // line of type under a settled screen. Held, it is the only place that says WHY the
  // button is grey — and the rescan goes with it, because the reason to rescan is a
  // CLI installed outside this window that the screen has not noticed yet.
  /* The whole-screen rescan: what answers 「I installed it in another window」. In the
     sentence rather than beside the action, so it reads as part of the explanation
     instead of a second control competing with the button. */
  const rescan = (
          <Button type="button" variant="link" size="xs" className="h-auto p-0 align-baseline text-xs"
            onClick={() => void detectAll()} disabled={isAnyInstalling || Object.values(detectingAgents).some(Boolean)}>
            <RefreshCw size={12} />{t('agentDetection.rescan')}
          </Button>
  );
  const hintSentence = canContinue ? null : (
        /* The sentence is what the eye lines up with the action below it, so the
           sentence is what gets centred. The rescan rides in the flanking column
           beside it — inside one centred line it pulled the sentence off the
           action's axis by half its own width. */
        <p className="onboarding-setup-hint-line text-xs text-muted">
          <span>{t('onboarding.connection.entryHint')}</span>
          {rescan}
        </p>
  );
  const hintInner = (hintSentence || entryError) ? (<>
        {hintSentence}
        {entryError && <div role="alert" className="connection-error">{entryError} <Button variant="link" size="sm" disabled={entering} onClick={() => void handlePrimaryAction()}>{t('common.retry')}</Button></div>}
  </>) : null;
  // Hosted in one place so the order under the pair is the same every time, and so the
  // standalone host keeps the arrangement it already had.
  const permissionNode = <OpencodePermissionSetup cliReady={opencodeAgent?.status === 'ok'}
    permissionAllowed={permission.permissionAllowed} state={permission.state} message={permission.message}
    // Granting permission succeeds at permission. It says nothing about whether
    // enabling the backend was persisted, so it reads the connection without
    // spending a verdict that is about something else.
    onSetup={() => void permission.setupPermission().then(() => refreshConnection('opencode'))} className="w-full" />;
  // A portal leaves the screen root, and with it the `inert` the shell puts on a screen
  // nobody is reading, so what it carries has to answer to the same activity itself.
  const asideNode = onActionChange
    ? (active && routeSurfaceActive && actionAside ? createPortal(<>
        {hintInner && <div className="onboarding-setup-hint">{hintInner}</div>}
        {permissionNode}
      </>, actionAside) : null)
    : (
      <div className="onboarding-setup-footer">
        <Button type="button" variant="brand" className="group onboarding-action-w onboarding-primary-action" onClick={() => void handlePrimaryAction()}
          disabled={!canContinue || syncing || entering}>
          {t(entering ? 'onboarding.connection.connecting' : 'onboarding.connection.enter')}
          <ArrowRight size={16} className="motion-safe:transition-transform motion-safe:duration-180 motion-safe:group-hover:translate-x-1" />
        </Button>
        {/* The standalone host has no shell slot and no screen after it, so it keeps the
            rescan reachable even once the action has lit up. */}
        {hintInner ?? <p className="text-center text-xs text-muted">{rescan}</p>}
        {onBack && <Button type="button" variant="ghost" className="onboarding-action-w onboarding-back-action" disabled={syncing || entering} onClick={() => onBack({ agents })}><ArrowLeft size={14} />{t('common.back')}</Button>}
      </div>
    );
  // Page mode keeps the existing settings shell — render the inner content only
  const Inner = isPage ? (
    <>
      <div className="flex flex-col gap-3 rounded-xl border border-border bg-background px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="min-w-0">
          <span className="text-[11px] font-semibold uppercase tracking-[0.12em] text-muted">
            {t('agentDetection.backendsLabel')}
          </span>
          <p className="mt-0.5 text-[12px] leading-snug text-muted">{t('agentDetection.detectedHelper')}</p>
        </div>
        <Button type="button" variant="secondary" size="sm" onClick={detectAll} className="shrink-0">
          <RefreshCw className="size-3.5" /> {t('agentDetection.rescan')}
        </Button>
      </div>

      <div className="flex flex-col gap-3">
        {Object.entries(agents).map(([name, agent]) => {
          const meta = getBackendUiMeta(name);
          const ready = agent.status === 'ok';
          const description = t(`settings.backends.${name}Description`, { defaultValue: '' });
          // Supported backends can open the provider config regardless of
          // detection status — the modal's runtime card lets the user point at a
          // custom CLI path when auto-detection missed an installed binary.
          const canConfigure = PROVIDER_BACKENDS.has(name);
          return (
            <div
              key={name}
              className="flex flex-col gap-3.5 rounded-xl border border-border bg-background px-5 py-4 transition-colors hover:border-border-strong"
            >
              {/* Top row — identity + status + enable switch. */}
              <div className="flex items-center justify-between gap-3">
                <div className="flex min-w-0 flex-1 items-center gap-3">
                  <div className="flex size-9 shrink-0 items-center justify-center rounded-lg border border-border bg-surface-2">
                    <BackendIcon backend={name as BackendId} size={18} variant="glyph" />
                  </div>
                  <div className="min-w-0">
                    <h3 className="text-[13px] font-semibold text-foreground">{meta.label}</h3>
                    {description && (
                      <p className="mt-0.5 truncate text-[11px] leading-snug text-muted">{description}</p>
                    )}
                  </div>
                </div>
                <div className="flex shrink-0 items-center gap-3">
                  <BackendLifecycleChip
                    name={name}
                    enabled={agent.enabled}
                    cliStatus={active ? agent.status || 'unknown' : 'unknown'}
                    onChanged={async (info) => {
                      // After a successful (re)install the chip hands back the
                      // path the installer landed at — adopt it before
                      // detecting, otherwise a stale ``agent.cli_path`` from
                      // this render keeps the row in a false ``missing`` state.
                      const installedPath = info?.installedPath || null;
                      if (installedPath) {
                        setAgents((prev) => ({
                          ...prev,
                          [name]: { ...prev[name], cli_path: installedPath },
                        }));
                      }
                      await detect(name, installedPath || agent.cli_path);
                    }}
                  />
                  <ToggleSwitch enabled={agent.enabled} onClick={() => toggle(name, !agent.enabled)} />
                </div>
              </div>

              {/* Action row — configure / set up Allow / install. */}
              <div className="flex flex-col gap-2">
                <div className="flex flex-wrap items-center gap-3">
                  {canConfigure &&
                    (isPage ? (
                      meta.settingsRoute && (
                        <Button asChild variant="secondary" size="sm">
                          <Link to={meta.settingsRoute}>
                            <Sliders className="size-3.5" />
                            {t('agentDetection.configureProvider')}
                            <ExternalLink className="size-3.5" />
                          </Link>
                        </Button>
                      )
                    ) : (
                      <Button
                        type="button"
                        variant="secondary"
                        size="sm"
                        onClick={() => setProviderModal({ backend: name as RuntimeBackendId, method: 'oauth' })}
                        disabled={syncing}
                      >
                        <Sliders className="size-3.5" />
                        {t('agentDetection.configureProvider')}
                      </Button>
                    ))}

                  {name === 'opencode' && (
                    <OpencodePermissionSetup
                      cliReady={ready}
                      permissionAllowed={permission.permissionAllowed}
                      state={permission.state}
                      message={permission.message}
                      onSetup={() => void permission.setupPermission()}
                      className="w-full"
                    />
                  )}

                  {isMissing(agent) && (
                    <>
                      <Button
                        type="button"
                        variant="brand-cyan"
                        size="sm"
                        onClick={() => installAgent(name)}
                        disabled={isAnyInstalling}
                      >
                        {installingAgents[name] ? (
                          <RefreshCw className="size-3.5 animate-spin" />
                        ) : (
                          <Download className="size-3.5" />
                        )}
                        {installingAgents[name]
                          ? t('agentDetection.installing')
                          : t('agentDetection.installAgentNamed', { name: meta.label })}
                      </Button>
                      {installResults[name]?.message && (
                        <span
                          className={clsx('text-[11px]', installResults[name].ok ? 'text-mint-ink' : 'text-destructive-ink')}
                        >
                          {installResults[name].message}
                        </span>
                      )}
                    </>
                  )}
                </div>

                {isMissing(agent) && installResults[name]?.output && (
                  <div>
                    <button
                      onClick={() => toggleOutput(name)}
                      className="inline-flex items-center gap-1 text-[11px] text-cyan-ink transition hover:text-cyan-ink/80"
                    >
                      {expandedOutputs[name] ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
                      {t('agentDetection.showOutput')}
                    </button>
                    {expandedOutputs[name] && (
                      <pre className="mt-2 max-h-40 overflow-auto whitespace-pre-wrap rounded border border-border bg-background px-3 py-2 font-mono text-[11px] text-muted">
                        {installResults[name].output}
                      </pre>
                    )}
                  </div>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </>
  ) : null;

  const providerDialog = !isPage && providerModal && <BackendConnectionDialog
    key={`${providerModal.backend}:${providerModal.method}`} backend={providerModal.backend} method={providerModal.method}
    onClose={() => {
      const name = providerModal.backend;
      setProviderModal(null);
      void syncBackendFromConfig(name);
      // Closing is how the dialog leaves, not a result. Cancelled without writing
      // anything, it has answered for nothing the enable failure was about.
      void refreshConnection(name);
    }}
    onWriteState={(pending) => {
      const name = providerModal.backend;
      setPendingWrites((current) => ({ ...current, [name]: pending }));
      // "Nothing is pending" is the same notification whether a write succeeded,
      // failed or never happened; `onConnected` is the one that means it worked.
      if (!pending) void refreshConnection(name);
    }}
    onConnected={async () => { await refreshConnection(providerModal.backend, { acknowledge: true }); }} />;

  if (isPage) {
    return (
      <div className="flex flex-col gap-4">
        {Inner}
        <div className="flex justify-end">
          <Button variant="brand" size="default" onClick={() => void handlePrimaryAction()} disabled={!canContinue || syncing}>
            {t('common.save')}
          </Button>
        </div>
      </div>
    );
  }

  return (
    <div className="onboarding-setup" ref={setupRoot}>
      <header className="onboarding-heading">
        <h1 tabIndex={-1}>{t('onboarding.setup.title')}</h1>
        <p>{t('onboarding.setup.subtitle')}</p>
      </header>
      {/* The stage both steps share, so the action below lands on the same
          coordinates as the intro's — see `.onboarding-stage` in onboarding.css. */}
      <div className="onboarding-stage">
      <div className="onboarding-assistants">
        {/* No section bar above the cards: the page heading already names the three,
            and a second title here pushed them below the line the intro left them on.
            Rescanning moved to the footer hint, which is where a person looks once the
            cards have not told them what they expected. */}
        {/* The intro's three tracks, reused rather than restated — see
            `.onboarding-assistants-list` in onboarding.css. */}
        <div className="onboarding-assistants-list">
        {ASSISTANT_ORDER.map((name) => {
          const agent = agents[name];
          const result = installResults[name];
          const error = detectionErrors[name] ? { message: detectionErrors[name] }
            : result && !result.ok && result.message ? result : undefined;
          const connectionMode = confirmedConnectionMode(name);
          const supplyMode = confirmedSupplyMode(name);
          const mode = agent.status === 'missing' ? installMode(name)
            : connectionMode && supplyMode && connectionMode !== supplyMode ? undefined
              : connectionMode ?? supplyMode;
          const hubRoute = mode === 'hub';
          const openRoute = () => {
            if (mode !== 'hub' || !routeTargetReady()) return;
            const supply = routeRead.supplies.find((row) => row.backend === name);
            const modelId = routeRead.models[name];
            if (supply?.mode === 'hub' && modelId) {
              setRouteSelection({ agent: supply, modelId,
                read: routeRead.chains[name] ? readyRegion(routeRead.chains[name]) : undefined });
            }
          };
          return <AssistantRow key={name} backend={name} status={agent.status || 'unknown'}
            installing={!!installingAgents[name]} detecting={!!detectingAgents[name]} error={error}
            onInstall={() => void installAgent(name, mode === 'hub')} onDetect={() => void detect(name, agent.cli_path)}
            onConfigure={() => {
              if (!mode) return;
              if (hubRoute) {
                if (canEditSetupRoute) { openRoute(); return; }
                navigate(MODEL_HUB_SETTINGS_PATH);
                return;
              }
              setProviderModal({ backend: name, method: 'oauth' });
            }}
            onAddKey={() => {
              if (!mode) return;
              if (hubRoute) {
                if (canEditSetupRoute) { openRoute(); return; }
                navigate(MODEL_HUB_SETTINGS_PATH);
                return;
              }
              setProviderModal({ backend: name, method: 'api_key' });
            }}
            connection={hubRoute
              ? 'hub'
              : mode === 'direct' && !connectionErrors[name] && connections[name]?.ready
                ? (connections[name]?.auth === 'subscription' ? 'subscription' : 'api_key')
                : undefined}
            hubManaged={hubRoute}
            enabled={agent.enabled}
            route={routeViewFor(name)}
            connectionPending={connectionPending[name]}
            connectionError={connectionErrors[name] || connections[name]?.message}
            onRefreshConnection={() => void refreshConnection(name, { acknowledge: true })}
            routeError={canEditSetupRoute && modelHubEnabled && routeRead.failed
              ? t('settings.models.routeDialog.fail.reconcileRead') : undefined}
            onRetryRoute={() => void readCardRoutes()}
            configuringDisabled={!mode || syncing || pendingWrites[name] || !!refreshingAgents[name] || !!connectionPending[name]
              || (hubRoute && canEditSetupRoute && (!routeTargetReady() || supplyMode !== 'hub'))
              || (hubRoute
                ? agent.status !== 'ok'
                : !agent.enabled || agent.status !== 'ok')}
            enabledControl={<ToggleSwitch variant="onboarding" enabled={agent.enabled}
              label={t('onboarding.setup.enableNamed', { name: getBackendUiMeta(name).label })}
              onClick={() => toggle(name, !agent.enabled)} />}
            lifecycle={<BackendLifecycleChip name={name} enabled={agent.enabled} cliStatus={active ? agent.status || 'unknown' : 'unknown'}
              readyLabel={t('onboarding.setup.enabled')}
              disabledLabel={t('onboarding.setup.notEnabled')}
              refreshKey={chipRefresh[name]}
              externallyBusy={!!refreshingAgents[name]}
              onVisual={(visual) => setVisuals((current) => (current[name] === visual ? current : { ...current, [name]: visual }))}
              onOperationChange={(pending) => {
                setPendingWrites((current) => ({ ...current, [name]: pending }));
                if (!pending) void refreshConnection(name);
              }}
              onChanged={async (info) => {
                const installedPath = info?.installedPath || agent.cli_path;
                setAgents((previous) => ({ ...previous, [name]: { ...previous[name], cli_path: installedPath } }));
                await detect(name, installedPath);
              }} />}
            upgrade={agent.status === 'ok' && (visuals[name] === 'update' || visuals[name] === 'updating' || refreshingAgents[name]) ? (
              <Button type="button" variant="secondary" className="onboarding-life-action"
                onClick={() => void upgradeAgent(name)}
                disabled={refreshingAgents[name] || !!upgradeLocks[name] || visuals[name] === 'updating' || !!installingAgents[name]}>
                {refreshingAgents[name] || visuals[name] === 'updating'
                  ? <RefreshCw size={14} className="motion-safe:animate-spin" />
                  : <ArrowUpToLine size={14} />}
                {t(refreshingAgents[name] || visuals[name] === 'updating'
                  ? 'backendLifecycle.upgrading'
                  : 'backendLifecycle.upgradeNow')}
              </Button>
            ) : undefined}
          />;
        })}
        </div>
      </div>
      {!onActionChange && permissionNode}
      </div>
      {providerDialog}
      {canEditSetupRoute && agentReads && (
        <RouteChainDialog selection={routeSelection} sources={routeRead.sources}
          onClose={() => { setRouteSelection(null); void readCardRoutes(); }}
          onCommitted={() => {
            setRouteReconciliation({ pending: true, failed: false });
            void Promise.all([
              readCardRoutes(),
              routeSelection ? refreshConnection(routeSelection.agent.backend) : Promise.resolve(),
            ]).then(([ok]) => setRouteReconciliation({ pending: false, failed: !ok }));
          }}
          commitReconciliation={{ ...routeReconciliation, retry: () => {
            setRouteReconciliation({ pending: true, failed: false });
            void readCardRoutes().then((ok) => setRouteReconciliation({ pending: false, failed: !ok }));
          } }}
          readAgents={async () => {
            const value = await agentReads.readValue();
            return { value, install: () => setRouteRead((current) => ({ ...current, supplies: value })) };
          }}
          readSources={async () => {
            const value = await modelsApi.listSources();
            return { value, install: () => setRouteRead((current) => ({ ...current, sources: value })) };
          }}
        />
      )}
      {asideNode}
    </div>
  );
};
