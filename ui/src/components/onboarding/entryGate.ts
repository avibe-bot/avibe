import type { BackendConnectionState, VibeAgentBrief, VibeAgentFull } from '../../context/ApiContext';
import type { CollectionReadAuthority } from '../settings/models/collectionReadAuthority';
import { runtimeIsRunning } from '../settings/models/runtimeLifecycle';
import type { AgentSupply, RuntimeDependency, SupplyStatus } from '../settings/models/types';
import { ASSISTANT_ORDER, type AssistantId } from './collaborationTimeline';
import { readSetupTargets } from './setupTargets';

/**
 * C4 — the gate setup's last click has to pass.
 *
 * The rule this file exists for: **one assistant must satisfy the whole gate.** Three
 * independent existential checks can each be met by a different object — an enabled
 * Codex with no route, a route persisted against an uninstalled Claude, a healthy
 * source neither of them uses — and setup would finish on a machine whose first
 * workspace turn cannot run. So every condition below is joined to the same candidate,
 * by backend AND by Agent name, and every one of them is read from the server.
 *
 * Kept stateless and separate from the Wizard for the same reason `gatewayBootstrap`
 * is: the interesting part is a decision over evidence, and a decision over evidence
 * can be stated as data and tested without a React tree.
 */

/** One named Agent that satisfies the gate, with the model the server resolved for it. */
export type EntryCandidate = {
  agent: VibeAgentBrief;
  backend: AssistantId;
  modelId: string;
};

export type EntryEvidence = {
  /** False when the Agent listing itself failed. An empty list and an unread list are
   *  different facts, and only one of them is something the person can act on. */
  agentsRead: boolean;
  agents: readonly VibeAgentBrief[];
  defaultAgentName: string | null;
  /** Designated builtins from uncached full-Agent metadata; empty when none can be proved. */
  targets: readonly VibeAgentBrief[];
  connections: readonly BackendConnectionState[];
  /** False when the shared collection refresh threw; empty supplies then mean unread, not none. */
  suppliesRead: boolean;
  supplies: readonly AgentSupply[];
  /** Null when the Hub runtime read threw or did not answer. Unread is not "running". */
  runtime: RuntimeDependency | null;
  /** Per-backend `detectCli(configured path)` result. Missing keys are not found. */
  cliFound: Readonly<Record<AssistantId, boolean>>;
};

/**
 * Why nothing was admitted, named as the sentence the person is owed.
 *
 * Each value is an existing `onboarding.connection.*` key, and each keeps the
 * situation it already described: nothing eligible at all, a machine that has not
 * applied its configuration yet, and — now also covering the route join — a machine
 * that is up but has no assistant that could actually run a turn.
 */
export type EntryRefusal = 'entryFailed' | 'applyPending' | 'modelUnavailable';

export type EntryAdmission =
  | { kind: 'admitted'; candidates: EntryCandidate[] }
  | { kind: 'refused'; reason: EntryRefusal; startable: boolean };

/** A configured route whose sources are all unusable is not a runnable route. */
const RUNNABLE_SUPPLY: ReadonlySet<SupplyStatus> = new Set<SupplyStatus>(['ok', 'degraded']);

const usableAgent = (agent: VibeAgentBrief): boolean => agent.enabled && !agent.archived;

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

/**
 * The configured CLI path the gate must detect, from the already-fresh prerequisite.
 *
 * A persisted `agents.<backend>.cli_path` wins. When none is configured, the existing
 * detect path falls back to the backend name — the same default AgentDetection uses.
 */
export function configuredCliPath(config: Record<string, unknown> | undefined, backend: AssistantId): string {
  const agents = config && isRecord(config.agents) ? config.agents : undefined;
  const row = agents && isRecord(agents[backend]) ? agents[backend] : undefined;
  if (row && typeof row.cli_path === 'string' && row.cli_path.trim()) return row.cli_path.trim();
  return backend;
}

/**
 * Whether this candidate's assistant is enabled, applied, in custody, and actually on disk.
 *
 * Installed is not `connection.installed`. C4 asks for `detectCli(configured path)`
 * and a current-generation `refresh()` whose `cli_present` agrees, plus
 * `getBackendConnection.enabled`. A failed detector, a not-found binary, or a stale
 * refresh cannot be replaced by the connection's own installed bit.
 *
 * `ready` carries permission and credential ownership; `applied` is the only
 * application state that means the configuration is live. A confirmed `stopped` is
 * something to recover from, never something to enter on.
 */
const backendUsable = (
  connection: BackendConnectionState | undefined,
  supply: AgentSupply | undefined,
  cliFound: boolean,
): boolean =>
  connection !== undefined && connection.ok
  && connection.enabled
  && connection.ready && connection.application === 'applied'
  && supply !== undefined && supply.cli_present === true
  && cliFound;

/**
 * Whether the server can route THIS named Agent to a model right now.
 *
 * Deliberately not `selected_model_id`: that is the backend-level pick belonging to
 * `selected_by_agent`, so reading it would let one Agent's configuration speak for
 * another's. The runnable-hop owner is the server resolver, and its per-Agent answer
 * is the `named_agents` row — an absent row is a backend that never routed this name.
 */
const routeRunnable = (supply: AgentSupply | undefined, name: string): string | null => {
  if (!supply || supply.mode !== 'hub') return null;
  const row = supply.named_agents?.find((named) => named.name === name);
  if (!row || row.route_reason === 'route_unconfigured') return null;
  if (!row.effective_model_id || !row.supply_status || !RUNNABLE_SUPPLY.has(row.supply_status)) return null;
  return row.effective_model_id;
};

/** Every Agent that passes the whole gate, in C6's backend order and then by name. */
export function entryCandidates(evidence: EntryEvidence): EntryCandidate[] {
  // Mode alone is not runtime readiness: a backend can be in hub mode while the Hub
  // that would serve it is down, and then no route on this machine is runnable.
  // An unread runtime is the same: we did not observe it, so we may not admit on it.
  if (!evidence.runtime || !runtimeIsRunning(evidence.runtime) || !evidence.suppliesRead) return [];
  const connectionOf = new Map(evidence.connections.map((connection) => [connection.backend, connection]));
  const supplyOf = new Map(evidence.supplies.map((supply) => [supply.backend, supply]));
  return ASSISTANT_ORDER.flatMap((backend) => {
    if (!backendUsable(connectionOf.get(backend), supplyOf.get(backend), evidence.cliFound[backend] === true)) return [];
    return evidence.agents
      .filter((agent) => agent.backend === backend && usableAgent(agent))
      .flatMap((agent) => {
        const modelId = routeRunnable(supplyOf.get(backend), agent.name);
        return modelId ? [{ agent, backend, modelId }] : [];
      })
      .sort((left, right) => (left.agent.name < right.agent.name ? -1 : left.agent.name > right.agent.name ? 1 : 0));
  });
}

/**
 * Whether a refusal is one an explicit start may still resolve.
 *
 * Only a connection the server confirmed `stopped` counts. Pending, draining, failed
 * and unknown are not confirmations, and a browser may not turn "I could not tell"
 * into a request to start something. Startup itself has no assistant, source, or
 * `entry_eligible` prerequisite — those belong to the gate after recovery.
 */
const startable = (connections: readonly BackendConnectionState[]): boolean =>
  !connections.some((connection) => connection.ready)
  && connections.some((connection) => connection.ok && connection.application === 'stopped');

export function admitEntry(evidence: EntryEvidence): EntryAdmission {
  const candidates = entryCandidates(evidence);
  if (candidates.length) return { kind: 'admitted', candidates };
  const connections = evidence.connections.filter((connection) => connection.ok);
  const reason: EntryRefusal = !evidence.agentsRead || !connections.some((connection) => connection.entry_eligible)
    ? 'entryFailed'
    : !connections.some((connection) => connection.ready)
      ? 'applyPending'
      // The machine is up and in custody, so what is missing is the run itself: no
      // enabled Agent to run, or none the Hub can route to a model.
      : 'modelUnavailable';
  return { kind: 'refused', reason, startable: startable(connections) };
}

/**
 * The Agent setup should enter on, or `null` when the saved default already serves.
 *
 * A default someone chose is preserved whenever it still works, including a custom
 * Agent no card represents — setup is finishing a machine, not imposing its own
 * preference on one. Only when the saved name cannot run does setup pick, and then it
 * prefers the assistant's own Agent so the first workspace turn matches the card the
 * person was just looking at.
 */
export function chooseEntryDefault(
  candidates: readonly EntryCandidate[],
  evidence: EntryEvidence,
): EntryCandidate | null {
  if (candidates.some((candidate) => candidate.agent.name === evidence.defaultAgentName)) return null;
  const targets = evidence.targets.map((target) => target.name);
  return candidates.find((candidate) => targets.includes(candidate.agent.name)) ?? candidates[0] ?? null;
}

export type EntryGateDeps = {
  listAgents: () => Promise<{ ok: boolean; agents: VibeAgentBrief[]; default_agent_name: string | null }>;
  getBackendConnection: (backend: AssistantId) => Promise<BackendConnectionState>;
  agentReads: CollectionReadAuthority<AgentSupply[]>;
  getRuntimeStatus: () => Promise<RuntimeDependency>;
  getVibeAgent: (
    name: string,
    params?: { cache?: boolean },
  ) => Promise<{ ok: boolean; agent: VibeAgentFull; default_agent_name: string | null }>;
  detectCli: (binary: string) => Promise<{ found?: boolean; path?: string }>;
};

export type EntryReadOptions = {
  /** Already-fresh prerequisite body, used only for configured CLI paths. */
  config?: Record<string, unknown>;
};

const asSettled = <T>(promise: Promise<T>): Promise<PromiseSettledResult<T>> =>
  promise.then(
    (value): PromiseSettledResult<T> => ({ status: 'fulfilled', value }),
    (reason): PromiseSettledResult<T> => ({ status: 'rejected', reason }),
  );

const noneFound = (): Record<AssistantId, boolean> =>
  Object.fromEntries(ASSISTANT_ORDER.map((backend) => [backend, false])) as Record<AssistantId, boolean>;

/**
 * One round of the fresh reads the gate judges, or `null` when it cannot judge.
 *
 * `null` is the superseded supply read: a newer generation of the shared authority is
 * already in flight, so the list this round would have used is not the current answer
 * and holding is the only honest thing to do. It is not a failure — nothing is written
 * and nothing is claimed — and the next click reads again.
 *
 * Hub IPC (`refresh`, `getRuntimeStatus`) is settled independently of application-state
 * evidence. A confirmed `stopped` connection must still reach recovery when those reads
 * throw `engine_down`. Unread supply or runtime is not an admitted machine: the
 * correlated gate still has to pass on a later current read.
 *
 * A connection that throws is dropped rather than fatal: one unreachable backend is a
 * backend that cannot be entered on, not a verdict about the other two.
 */
export async function readEntryEvidence(
  deps: EntryGateDeps,
  options: EntryReadOptions = {},
): Promise<EntryEvidence | null> {
  const [supplyOutcome, runtimeOutcome, connectionResults, agentsOutcome, cliResults] = await Promise.all([
    asSettled(deps.agentReads.refresh()),
    asSettled(deps.getRuntimeStatus()),
    Promise.allSettled(ASSISTANT_ORDER.map((backend) => deps.getBackendConnection(backend))),
    asSettled(deps.listAgents()),
    Promise.allSettled(ASSISTANT_ORDER.map(async (backend) => {
      const path = configuredCliPath(options.config, backend);
      const detected = await deps.detectCli(path);
      return { backend, found: detected?.found === true };
    })),
  ]);
  if (supplyOutcome.status === 'fulfilled' && supplyOutcome.value.kind === 'stale') return null;
  const supplies = supplyOutcome.status === 'fulfilled' && supplyOutcome.value.kind === 'current'
    ? supplyOutcome.value.value
    : null;
  const agents = agentsOutcome.status === 'fulfilled'
    ? agentsOutcome.value
    : { ok: false, agents: [] as VibeAgentBrief[], default_agent_name: null };
  const listed = agents.ok ? agents.agents : [];
  const targets = agents.ok ? await readSetupTargets(listed, deps) : [];
  const cliFound = noneFound();
  for (const result of cliResults) {
    if (result.status === 'fulfilled') cliFound[result.value.backend] = result.value.found;
  }
  return {
    agentsRead: agents.ok,
    agents: listed,
    defaultAgentName: agents.default_agent_name,
    targets,
    connections: connectionResults.flatMap((result) =>
      result.status === 'fulfilled' && result.value.ok ? [result.value] : []),
    suppliesRead: supplies !== null,
    supplies: supplies ?? [],
    runtime: runtimeOutcome.status === 'fulfilled' ? runtimeOutcome.value : null,
    cliFound,
  };
}
