// Model Hub API client. Presents ONE typed surface to the UI; internally it
// either serves in-memory fixtures for hermetic visual tests or calls the frozen
// `/api/models/*` REST endpoints (live mode). Components never
// branch on the mode — flip `MODELS_API_MODE` in featureFlags.ts to switch.
//
// Methods unwrap the frozen envelope ({ok:true, …} | {ok:false, error}) and
// throw an Error carrying the machine code on failure, so callers work with
// plain domain objects.
import { apiFetch } from '@/lib/apiFetch';
import { MODELS_API_MODE } from './featureFlags';
import {
  buildMockAgents,
  buildMockEvents,
  buildMockMigration,
  buildMockRuntime,
  buildMockSources,
  mockDiscoveredCount,
  mockEligibility,
  mockRecommendedOrder,
} from './mockData';
import { canReauth, canReplaceKey, wasBlocked } from './repair';
import { validateRouteDraft } from './routeChainDraft';
import { isUnhealthy } from './supply';
import type {
  AdoptedBy,
  AddedTo,
  AgentBackend,
  AgentChain,
  AgentChainLink,
  AgentChainMutation,
  AgentChainPut,
  AgentMenu,
  AgentMode,
  AgentSourcesPut,
  AgentSupply,
  ApiKeySourceCreate,
  ApiKeySourceObservation,
  CredentialReplace,
  CustomModelCreate,
  MigrationApplyResult,
  MigrationScan,
  OAuthFlow,
  ProbeResult,
  ResolutionEvent,
  RouteHopRef,
  RuntimeDependency,
  Source,
  SourceObservation,
  SourcePatch,
  SourceRepaired,
  SupplyChannel,
  SupplyGap,
} from './types';
import { equalHopIdentity } from './hopIdentity';
import { AGENT_CHAIN_CONTRACT_VERSION, CONTRACT_VERSION, PROBE_RESULT_CONTRACT_VERSION } from './types';

/** Add-time Route placement returned by both source-creation paths. */
export type Adoption = { added_to: AddedTo[]; adopted_by: AdoptedBy[] };
export type SourceCreated = { source: Source } & Adoption;
export type SourceRefresh = { source: Source; discovered: number };
export type CredentialReplacement = {
  source: Source;
  removed_hops: RouteHopRef[];
  interrupted: SupplyGap[];
};
export type GuardConfirmation = {
  force: true;
  would_remove_hops: RouteHopRef[];
  would_interrupt: SupplyGap[];
};

/**
 * The response of BOTH oauth status and submit (api.md → OAuth completion): the
 * flow, plus — once a `create` flow reaches success — the source the server
 * materialized while answering THAT request, and who took it in.
 *
 * `created` is the half a client must not throw away. The server creates the
 * source inside the very call that first reports success and consumes the flow
 * binding doing it, so a client that keeps only `flow` and then posts to
 * `/sources` to "finalize" gets `flow_not_found` on a connection that in fact
 * succeeded. `null` means this response did not report a creation (still
 * pending, failed, cancelled, or a `reauth` flow, which reports recovery
 * instead) — NOT that nothing adopted the source. That distinction is why this
 * is nullable rather than `[]`.
 *
 * `repaired` is the other terminal arm, keyed on the payload's own `intent`: a
 * `reauth` flow succeeds into `{source, recovered, interrupted_pairs}` (api.md,
 * "recovery symmetry") rather than an adoption list. At most one arm is ever
 * non-null, because a flow has exactly one intent.
 */
export type OAuthResult = {
  flow: OAuthFlow;
  created: SourceCreated | null;
  repaired: SourceRepaired | null;
};

export type ModelsApi = {
  listSources(): Promise<Source[]>;
  /** Unsaved observation; its transient credential is revoked before settling. */
  observeApiKeySource(draft: ApiKeySourceObservation, signal?: AbortSignal): Promise<SourceObservation>;
  createApiKeySource(draft: ApiKeySourceCreate): Promise<SourceCreated>;
  /** Rename / re-point a source (display_name, base_url). */
  patchSource(id: string, patch: SourcePatch): Promise<Source>;
  /** Re-run discovery on a hub source; resolves with the updated source and count.
   *  Contractually ALSO the recovery test: run on a needs_action / error source
   *  it clears the blocker and returns the source to standby. v3 adds no second
   *  「recover」 endpoint, so this is the whole retry affordance. */
  refreshSource(id: string, confirmation?: GuardConfirmation): Promise<SourceRefresh>;
  /** Delete a source. `force` overrides the only-supplier guard. */
  deleteSource(id: string, force?: boolean): Promise<void>;
  /** Replace the credential of a hub-channel api_key source. The normal guarded
   *  mutation tail reports every route hop removed and model interrupted. */
  replaceCredential(id: string, body: CredentialReplace): Promise<CredentialReplacement>;
  /** Start re-authorization for a subscription source. Irreversible once it
   *  begins, which is why the acknowledgement is sent unconditionally — the
   *  server rejects a native source without it (`reauth_confirmation_required`).
   *  Resolves with the flow to drive; the repair tail arrives on its terminal
   *  status/submit response as `OAuthResult.repaired`. */
  reauthSource(id: string): Promise<OAuthFlow>;
  listAgents(): Promise<AgentSupply[]>;
  /** Per-backend enabled subset + order (the 来源顺序 drawer's read). */
  getAgentSources(backend: AgentBackend): Promise<AgentSupply>;
  /** Total write of the exact stored source order. */
  putAgentSources(backend: AgentBackend, body: AgentSourcesPut): Promise<AgentSupply>;
  /** Resolution chain for one model. Hub mode only — direct answers `direct_mode`. */
  getAgentChain(backend: AgentBackend, model: string): Promise<AgentChain>;
  /** Total replacement of the exact stored chain. */
  putAgentChain(backend: AgentBackend, model: string, body: AgentChainPut): Promise<AgentChainMutation>;
  /** One real request through the chain. Hub mode only, same reason. */
  probeAgent(backend: AgentBackend, model?: string): Promise<ProbeResult>;
  setAgentMode(backend: AgentBackend, mode: AgentMode): Promise<AgentSupply>;
  putMenu(menu: AgentMenu): Promise<AgentSupply>;
  addCustomModel(sourceId: string, draft: CustomModelCreate): Promise<Source>;
  updateModelReasoningEfforts(sourceId: string, modelId: string, reasoningEfforts: string[]): Promise<Source>;
  deleteCustomModel(sourceId: string, modelId: string, confirmation?: GuardConfirmation): Promise<Source>;
  scanMigration(): Promise<MigrationScan>;
  applyMigration(itemIds: string[]): Promise<MigrationApplyResult>;
  /** `before` is an event id cursor (「查看全部」 pagination). */
  listEvents(limit?: number, before?: string): Promise<ResolutionEvent[]>;
  getRuntimeStatus(): Promise<RuntimeDependency>;
  /** Start the contract-owned client installation transaction. */
  installRuntime(): Promise<RuntimeDependency>;
  startRuntime(): Promise<RuntimeDependency>;
  startOAuth(vendor: string, channel: SupplyChannel, clientNonce?: string): Promise<OAuthFlow>;
  getOAuthStatus(flowId: string): Promise<OAuthResult>;
  submitOAuth(flowId: string, value: string): Promise<OAuthResult>;
  cancelOAuth(flowId: string): Promise<void>;
};

const isLive = () => MODELS_API_MODE === 'live';

// ── Live client ─────────────────────────────────────────────────────────
export class ApiCallError extends Error {
  code: string;
  detail?: string;
  /**
   * The `source_last_supplier` payload half. Carried on the error rather than
   * looked up afterwards for the same reason `adopted_by` travels with a
   * creation: it is the server's evaluation of the write it just refused, and
   * no later read of `/agents` reproduces it — that read describes today's
   * supply, not the supply the refused write would have left behind.
   *
   * `[]` on every other code, so a call site can render the gap report without
   * first proving which error it has.
   */
  wouldInterrupt: SupplyGap[];
  wouldRemoveHops: RouteHopRef[];
  /**
   * The OTHER half, and deliberately not the same field: `would_interrupt` names
   * what a REFUSED write would have stranded (nothing changed, the copy is future
   * tense), while `interrupted_pairs` on an error names what a write that already
   * COMMITTED did strand. A native reauth is where that difference is not
   * academic — `_materialize_reauth` clears the source, marks it unavailable, and
   * only then answers `discovery_failed` with the pairs beside it, so these gaps
   * are a report about the past, not a confirm about the future. Collapsed into
   * one field, the caller would have no way to pick the right tense.
   */
  interrupted: SupplyGap[];
  /**
   * Whether the ROUTE named this failure, i.e. whether `code` came off the wire.
   *
   * Two of the codes here are this client's own summary of a response that never
   * said what happened: `bad_response` when the body would not parse, `http_<n>`
   * when it parsed and carried no `error`. They are not outcomes — a route that
   * names a failure has already decided what it did, while these say only that
   * the answer did not arrive intact, which the server having written first is
   * entirely consistent with. Anything downstream that skips a corrective re-read
   * because 「the route said so」 has to ask this rather than 「is it one of ours?」.
   *
   * `true` by default because every other throw in this module IS an outcome: the
   * local mock raises the same codes the routes do. Only `call` invents any, and
   * it says so at both sites.
   */
  serverNamed: boolean;
  /** HTTP status observed by this client. Absent when no response arrived. */
  responseStatus?: number;
  /** Safe, structured add-time observation returned with a rejected create. */
  observation?: SourceObservation;
  constructor(
    code: string,
    detail?: string,
    serverNamed = true,
    wouldInterrupt: SupplyGap[] = [],
    interrupted: SupplyGap[] = [],
    wouldRemoveHops: RouteHopRef[] = [],
    responseStatus?: number,
    observation?: SourceObservation,
  ) {
    super(detail || code);
    this.name = 'ApiCallError';
    this.code = code;
    this.detail = detail;
    this.serverNamed = serverNamed;
    this.wouldInterrupt = wouldInterrupt;
    this.interrupted = interrupted;
    this.wouldRemoveHops = wouldRemoveHops;
    this.responseStatus = responseStatus;
    this.observation = observation;
  }
}

/** Normalize to the FULL contract shape: a gap without `agents` is a gap whose
 *  confirm copy has nothing to name, and `[]` renders as 「无」 rather than
 *  crashing the dialog that was opened to explain the refusal. */
const supplyGaps = (raw: unknown): SupplyGap[] =>
  Array.isArray(raw)
    ? raw
        .filter((g): g is Record<string, unknown> => Boolean(g) && typeof g === 'object')
        .map((g) => ({
          backend: g.backend as SupplyGap['backend'],
          model_id: String(g.model_id ?? ''),
          agents: Array.isArray(g.agents) ? g.agents.map(String) : [],
        }))
    : [];

const routeHopRefs = (raw: unknown): RouteHopRef[] =>
  Array.isArray(raw)
    ? raw.filter((hop): hop is RouteHopRef => Boolean(hop) && typeof hop === 'object')
    : [];

/**
 * The one reader every call site uses instead of casting a caught `unknown`.
 *
 * Both clients throw `ApiCallError`, but a caught value is still `unknown` to
 * TypeScript, and `(err as {code?: string}).code` spread across call sites is
 * how a renamed field goes silently unread. Returns null for anything that is
 * not one of ours — a TypeError from our own render code must not be reported
 * to the user as a supply refusal.
 */
export const apiFailure = (
  err: unknown,
): {
  code: string;
  detail?: string;
  serverNamed: boolean;
  wouldInterrupt: SupplyGap[];
  interrupted: SupplyGap[];
  wouldRemoveHops: RouteHopRef[];
  responseStatus?: number;
  observation?: SourceObservation;
} | null =>
  err instanceof ApiCallError
    ? {
        code: err.code,
        detail: err.detail,
        serverNamed: err.serverNamed,
        wouldInterrupt: err.wouldInterrupt,
        interrupted: err.interrupted,
        wouldRemoveHops: err.wouldRemoveHops,
        responseStatus: err.responseStatus,
        observation: err.observation,
      }
    : null;

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await apiFetch(path, init);
  let payload: unknown = null;
  try {
    payload = await res.json();
  } catch {
    // Invented here, not read off the wire: the request may well have been
    // carried out and its answer lost coming back.
    throw new ApiCallError('bad_response', `Non-JSON response from ${path}`, false, [], [], [], res.status);
  }
  const envelope = payload && typeof payload === 'object' ? payload as Record<string, unknown> : {};
  if (!res.ok || envelope.ok === false) {
    const error = typeof envelope.error === 'string' ? envelope.error : null;
    throw new ApiCallError(
      error ?? `http_${res.status}`,
      typeof envelope.detail === 'string' ? envelope.detail : undefined,
      // `payload.error` is the only thing that carries a route's own verdict, so
      // its presence IS the answer. `http_502` is this client summarizing a
      // response that never gave one.
      error !== null,
      supplyGaps(envelope.would_interrupt),
      supplyGaps(envelope.interrupted_pairs),
      routeHopRefs(envelope.would_remove_hops),
      res.status,
      typeof envelope.observation === 'object' && envelope.observation !== null
        ? envelope.observation as SourceObservation
        : undefined,
    );
  }
  return payload as T;
}

const jsonInit = (method: string, body?: unknown): RequestInit => ({
  method,
  headers: { 'Content-Type': 'application/json' },
  body: body === undefined ? undefined : JSON.stringify(body),
});

/** The creation response's add-time placement projection. */
type AdoptionTail = { added_to?: AddedTo[]; adopted_by?: AdoptedBy[] };
const adoption = (r: AdoptionTail): Adoption => ({
  added_to: r.added_to ?? [],
  adopted_by: r.adopted_by ?? [],
});

/** api.md pins the shape to `{source, added_to, adopted_by}` with no extra
 *  nesting; the bare-`Source` arm is the same tolerance every other write here
 *  keeps. */
type SourceCreatedResponse = { source?: Source } & AdoptionTail & Source;
const created = (r: SourceCreatedResponse): SourceCreated => ({
  source: (r.source ?? r) as Source,
  ...adoption(r),
});

type CredentialReplacementResponse = {
  source?: Source;
  removed_hops?: RouteHopRef[];
  interrupted?: SupplyGap[];
} & Source;

const credentialReplacement = (r: CredentialReplacementResponse): CredentialReplacement => ({
  source: (r.source ?? r) as Source,
  removed_hops: routeHopRefs(r.removed_hops),
  interrupted: supplyGaps(r.interrupted),
});

/** The oauth terminal envelope, unwrapped without discarding either tail. */
export type OAuthResultResponse = { flow?: OAuthFlow } & OAuthFlow &
  AdoptionTail & {
    source?: Source;
    recovered?: boolean;
    interrupted_pairs?: SupplyGap[];
  };
/** Exported for its own test: this is where the create half was being dropped. */
export const oauthResult = (r: OAuthResultResponse): OAuthResult => {
  const flow = (r.flow ?? r) as OAuthFlow;
  // Discriminate on the payload's own `intent`, not on which dialog is open:
  // `reauth` also terminates with a `source` but carries recovery counts instead
  // of adoption, and reading those as an adoption list would report the wrong
  // thing about the wrong flow. Absent `intent` is a `create` flow (the field
  // postdates the first shipped payloads, and create is the flow this UI starts
  // from the vendor buttons).
  const isCreate = flow.intent !== 'reauth';
  return {
    flow,
    // No bare-`Source` tolerance here, unlike `created()`: this envelope always
    // nests the source under `source`, beside the flow it accompanies. The tail
    // itself goes through the same `adoption` reader, so the two routes cannot
    // default one of these arrays differently from the other.
    created: isCreate && r.source ? { source: r.source, ...adoption(r) } : null,
    // The repair tail. `recovered` is defaulted false rather than optional: it is
    // the server's own 「this source had been blocked」 judgement, and absent must
    // read as 「did not say so」 — never as a client guess that it was.
    repaired:
      !isCreate && r.source
        ? {
            source: r.source,
            recovered: r.recovered === true,
            interrupted_pairs: supplyGaps(r.interrupted_pairs),
          }
        : null,
  };
};

const liveApi: ModelsApi = {
  listSources: () => call<{ sources: Source[] }>('/api/models/sources').then((r) => r.sources),
  observeApiKeySource: (draft, signal) =>
    call<{ observation: SourceObservation }>('/api/models/sources/observe', {
      ...jsonInit('POST', draft),
      signal,
    }).then((r) => r.observation),
  // Both keep `adopted_by`. The old unwrap-to-`source` dropped it on the floor,
  // and no later read can put it back: `/agents` shows today's orders, not which
  // of them this commit changed.
  createApiKeySource: (draft) => call<SourceCreatedResponse>('/api/models/sources', jsonInit('POST', draft)).then(created),
  patchSource: (id, patch) => call<{ source?: Source } & Source>(`/api/models/sources/${encodeURIComponent(id)}`, jsonInit('PATCH', patch)).then((r) => (r.source ?? r) as Source),
  refreshSource: (id, confirmation) => call<SourceRefresh>(`/api/models/sources/${encodeURIComponent(id)}/refresh`, jsonInit('POST', confirmation ?? {})),
  deleteSource: (id, force) => call(`/api/models/sources/${encodeURIComponent(id)}${force ? '?force=true' : ''}`, jsonInit('DELETE')).then(() => undefined),
  // Both repair routes reject unknown body keys outright (`discovery_failed` /
  // `reauth_confirmation_required`), so these bodies are exactly the contract's
  // and carry no `contract_version` — the same closed-body rule as putAgentSources.
  replaceCredential: (id, body) => call<CredentialReplacementResponse>(`/api/models/sources/${encodeURIComponent(id)}/credential`, jsonInit('PUT', body)).then(credentialReplacement),
  // The acknowledgement is unconditional by design: the server enforces it for
  // native sources pre-login, and api.md's per-channel truth makes a hub grant
  // replacement equally irreversible once new material is written. One confirm,
  // no channel branch.
  reauthSource: (id) => call<{ flow?: OAuthFlow } & OAuthFlow>(`/api/models/sources/${encodeURIComponent(id)}/reauth`, jsonInit('POST', { acknowledge_irreversible: true })).then((r) => (r.flow ?? r) as OAuthFlow),
  listAgents: () => call<{ agents: AgentSupply[] }>('/api/models/agents').then((r) => r.agents),
  getAgentSources: (backend) => call<{ agent: AgentSupply }>(`/api/models/agents/${backend}/sources`).then((r) => r.agent),
  // The body is TOTAL and closed: the route rejects unknown keys, so
  // `contract_version` is deliberately absent (unlike every other write here).
  putAgentSources: (backend, body) => call<{ agent: AgentSupply }>(`/api/models/agents/${backend}/sources`, jsonInit('PUT', body)).then((r) => r.agent),
  getAgentChain: (backend, model) => call<{ chain: AgentChain }>(`/api/models/agents/${backend}/chain?model=${encodeURIComponent(model)}`).then((r) => r.chain),
  putAgentChain: (backend, model, body) => call<AgentChainMutation>(`/api/models/agents/${backend}/chain?model=${encodeURIComponent(model)}`, jsonInit('PUT', body)),
  probeAgent: (backend, model) => call<{ probe: ProbeResult }>(`/api/models/agents/${backend}/probe`, jsonInit('POST', model ? { model } : {})).then((r) => r.probe),
  setAgentMode: (backend, mode) => call<{ agent?: AgentSupply } & AgentSupply>(`/api/models/agents/${backend}/mode`, jsonInit('PATCH', { mode })).then((r) => (r.agent ?? r) as AgentSupply),
  putMenu: (menu) => call<{ agent?: AgentSupply } & AgentSupply>('/api/models/agents/opencode/menu', jsonInit('PUT', { menu })).then((r) => (r.agent ?? r) as AgentSupply),
  addCustomModel: (sourceId, draft) => call<{ source?: Source } & Source>(`/api/models/sources/${encodeURIComponent(sourceId)}/models`, jsonInit('POST', draft)).then((r) => (r.source ?? r) as Source),
  updateModelReasoningEfforts: (sourceId, modelId, reasoningEfforts) => call<{ source?: Source } & Source>(`/api/models/sources/${encodeURIComponent(sourceId)}/models/${encodeURIComponent(modelId)}`, jsonInit('PATCH', { reasoning_efforts: reasoningEfforts })).then((r) => (r.source ?? r) as Source),
  deleteCustomModel: (sourceId, modelId, confirmation) => call<{ source?: Source } & Source>(`/api/models/sources/${encodeURIComponent(sourceId)}/models/${encodeURIComponent(modelId)}`, jsonInit('DELETE', confirmation ?? {})).then((r) => (r.source ?? r) as Source),
  scanMigration: () => call<{ scan?: MigrationScan } & MigrationScan>('/api/models/migration/scan', jsonInit('POST')).then((r) => (r.scan ?? r) as MigrationScan),
  applyMigration: (itemIds) => call<MigrationApplyResult>('/api/models/migration/apply', jsonInit('POST', { item_ids: itemIds })),
  listEvents: (limit = 20, before) =>
    call<{ events: ResolutionEvent[] }>(
      `/api/models/events?limit=${limit}${before ? `&before=${encodeURIComponent(before)}` : ''}`,
    ).then((r) => r.events),
  getRuntimeStatus: () => call<{ runtime?: RuntimeDependency } & RuntimeDependency>('/api/models/runtime/status').then((r) => (r.runtime ?? r) as RuntimeDependency),
  installRuntime: () => call<{ runtime?: RuntimeDependency } & RuntimeDependency>('/api/models/runtime/install', jsonInit('POST')).then((r) => (r.runtime ?? r) as RuntimeDependency),
  startRuntime: () => call<{ runtime?: RuntimeDependency } & RuntimeDependency>('/api/models/runtime/start', jsonInit('POST')).then((r) => (r.runtime ?? r) as RuntimeDependency),
  startOAuth: (vendor, channel, clientNonce) =>
    call<{ flow?: OAuthFlow } & OAuthFlow>(
      '/api/models/oauth/start',
      jsonInit('POST', { vendor, channel, ...(clientNonce ? { client_nonce: clientNonce } : {}) }),
    ).then((r) => (r.flow ?? r) as OAuthFlow),
  getOAuthStatus: (flowId) => call<OAuthResultResponse>(`/api/models/oauth/status/${encodeURIComponent(flowId)}`).then(oauthResult),
  submitOAuth: (flowId, value) => call<OAuthResultResponse>('/api/models/oauth/submit', jsonInit('POST', { flow_id: flowId, value })).then(oauthResult),
  cancelOAuth: (flowId) => call('/api/models/oauth/cancel', jsonInit('POST', { flow_id: flowId })).then(() => undefined),
};

// ── Mock client ─────────────────────────────────────────────────────────
// A single mutable store so reorder / add / mode-switch stick across calls
// within a session, giving a realistic demo without a backend.
/** `recovered` is captured when a reauth flow STARTS, like the server's own
 *  `recovered = source.state.status in {needs_action, error}` — read before the
 *  native irreversible step rewrites that very status. */
type MockFlow = {
  flow: OAuthFlow;
  polls: number;
  submitted: boolean;
  recovered?: boolean;
  placement?: Adoption;
};

const rid = (prefix: string) => `${prefix}_${Math.random().toString(36).slice(2, 10)}`;
const delay = <T>(value: T, ms = 260, signal?: AbortSignal): Promise<T> =>
  new Promise((resolve, reject) => {
    const timer = globalThis.setTimeout(() => resolve(value), ms);
    signal?.addEventListener('abort', () => {
      globalThis.clearTimeout(timer);
      reject(new DOMException('The operation was aborted.', 'AbortError'));
    }, { once: true });
  });
const listedModelIdsForPlacement = (agent: AgentSupply): string[] => {
  const values = [
    ...(agent.builtin_models ?? []),
    ...(agent.menu?.checked ?? []),
    ...Object.keys(agent.routes ?? {}),
  ];
  return [...new Set(values)];
};

class MockStore {
  sources = buildMockSources();
  agents = buildMockAgents(this.sources);
  events = buildMockEvents();
  runtime = buildMockRuntime();
  flows = new Map<string, MockFlow>();

  // ── Fake server-side recomputation ───────────────────────────────────
  // Reads refresh server-authoritative eligibility while preserving the stored
  // order. Matching and Route construction happen only at add time.
  private syncAgents() {
    for (const a of this.agents) {
      if (a.mode === 'direct') {
        a.sources = null;
      } else {
        const eligibility = mockEligibility(this.sources, a.backend);
        const eligible = new Set(eligibility.filter((e) => e.eligible).map((e) => e.source_id));
        const order = (a.sources?.order ?? []).filter((id) => eligible.has(id));
        a.sources = { order, eligibility };
      }
      this.deriveSupply(a);
    }
  }

  /** §4.3 + §4.5 in miniature: capability (supplies the configured id) split from
   *  runnability (not blocked), then the rollup over the resulting chain. */
  private deriveSupply(a: AgentSupply) {
    if (a.mode === 'direct') {
      a.selected_model_id = null;
      a.selected_model_explicit = false;
      a.selected_by_agent = null;
      a.supply_status = null;
      a.model_supply = null;
      a.named_agents = (a.named_agents ?? []).map((n) => ({ ...n, effective_model_id: null, supply_status: null }));
      return;
    }
    const routeFor = (model: string) => a.routes?.[model]?.hops ?? [];
    const chainFor = (model: string) => this.chainFor(a, model).chain;
    if (a.builtin_models) {
      a.model_supply = a.builtin_models.map((m) => ({
        model_id: m,
        chain_length: routeFor(m).length,
        has_runnable_hop: chainFor(m).some((link) => link.runnable),
      }));
    }
    const selected = a.selected_model_id ?? null;
    if (!selected) {
      a.supply_status = null;
    } else {
      const chain = chainFor(selected);
      const head = chain.find((link) => link.runnable) ?? null;
      const blocked = chain.filter((link) => !link.runnable);
      if (!head) {
        a.supply_status =
          chain.length > 0 && blocked.every((link) => link.health === 'cooldown' && link.reason === null) ? 'waiting' : 'interrupted';
      } else {
        a.supply_status = head === chain[0] && blocked.length === 0 ? 'ok' : 'degraded';
      }
    }
    const rollup = a.supply_status ?? null;
    a.named_agents = (a.named_agents ?? []).map((n) =>
      n.effective_model_id ? { ...n, supply_status: rollup } : n,
    );
  }

  listSources() {
    // api.md: the inventory is explicitly UNORDERED — order is per-backend.
    return delay(structuredClone(this.sources));
  }

  /** Materialize placement once, in the same transaction that creates a Source. */
  private placeNewSource(source: Source): Adoption {
    const added_to: AddedTo[] = [];
    const adopted_by: AdoptedBy[] = [];
    for (const agent of this.agents) {
      if (agent.mode !== 'hub') continue;
      const eligible = mockEligibility(this.sources, agent.backend).some(
        (entry) => entry.source_id === source.id && entry.eligible,
      );
      if (!eligible) continue;
      const order = agent.sources?.order ?? [];
      if (!order.includes(source.id)) order.push(source.id);
      agent.sources = { order, eligibility: agent.sources?.eligibility ?? null };
      for (const menuModel of listedModelIdsForPlacement(agent)) {
        const supplied = source.models.find((model) => model.id === menuModel);
        if (!supplied) continue;
        const hops = agent.routes?.[menuModel]?.hops ?? [];
        if (hops.some((hop) => hop.source_id === source.id)) continue;
        const next = [...hops, { source_id: source.id, model_id: supplied.id }];
        agent.routes = { ...(agent.routes ?? {}), [menuModel]: { hops: next } };
        added_to.push({
          backend: agent.backend,
          menu_model: menuModel,
          source_id: source.id,
          model_id: supplied.id,
          position: next.length,
        });
        adopted_by.push({ backend: agent.backend, menu_model: menuModel });
      }
    }
    this.syncAgents();
    return { added_to, adopted_by };
  }

  createApiKeySource(draft: ApiKeySourceCreate) {
    const existing = draft.client_nonce
      ? this.sources.find((source) => source.client_nonce === draft.client_nonce)
      : undefined;
    if (existing) {
      return delay({
        source: structuredClone(existing),
        added_to: [],
        adopted_by: structuredClone(existing.adopted_by ?? []),
      });
    }
    const count = mockDiscoveredCount(draft.vendor);
    const source: Source = {
      id: rid('src'),
      client_nonce: draft.client_nonce ?? null,
      created_at: new Date().toISOString(),
      last_discovered_at: new Date().toISOString(),
      kind: 'api_key',
      vendor: draft.vendor,
      display_name: draft.display_name || (draft.vendor === 'custom' ? hostLabel(draft.base_url) : vendorLabel(draft.vendor)),
      protocol: draft.protocol_order?.[0] ?? (draft.vendor === 'anthropic' ? 'anthropic' : 'openai_chat'),
      base_url: draft.base_url ?? null,
      supply_channel: 'hub',
      billing: 'metered',
      state: { status: 'standby', retry_at: null, detail_key: null },
      usage: { cycle_used_pct: null, month_spend_cents: 0, currency: 'USD' },
      account_label: null,
      // Simulates L2 computing the display mask once at provisioning.
      masked_credential: maskKey(draft.key),
      models: Array.from({ length: count }, (_, i) => ({
        id: `${draft.vendor}-model-${i + 1}`,
        display_name: null,
        origin: 'discovered' as const,
        reasoning_efforts: [],
        discovered_at: new Date().toISOString(),
      })),
      credential_ref: rid('cred'),
    };
    this.sources.push(source);
    const placement = this.placeNewSource(source);
    source.adopted_by = placement.adopted_by;
    // simulate probe latency
    return delay({ source: structuredClone(source), ...placement }, 900);
  }

  observeApiKeySource(draft: ApiKeySourceObservation, signal?: AbortSignal) {
    const marker = `${draft.base_url ?? ''} ${draft.key}`.toLowerCase();
    const base = {
      contract_version: CONTRACT_VERSION,
      models: [] as string[],
    };
    let observation: SourceObservation;
    if (marker.includes('timeout')) {
      observation = { ...base, outcome: 'timeout', reachable: null, authenticated: 'unknown', protocol: null, discovery: 'not_attempted' };
    } else if (marker.includes('auth')) {
      observation = { ...base, outcome: 'authentication_failed', reachable: true, authenticated: 'rejected', protocol: null, discovery: 'not_attempted' };
    } else if (marker.includes('ambiguous')) {
      observation = { ...base, outcome: 'ambiguous', reachable: true, authenticated: 'authenticated', protocol: null, discovery: 'not_attempted' };
    } else if (marker.includes('inventory')) {
      observation = { ...base, outcome: 'observed', reachable: true, authenticated: 'authenticated', protocol: draft.protocol_order?.[0] ?? 'openai_chat', discovery: 'failed' };
    } else if (marker.includes('adapter')) {
      observation = { ...base, outcome: 'adapter_error', reachable: null, authenticated: 'unknown', protocol: null, discovery: 'not_attempted' };
    } else if (marker.includes('unreachable')) {
      observation = { ...base, outcome: 'unreachable', reachable: false, authenticated: 'unknown', protocol: null, discovery: 'not_attempted' };
    } else {
      observation = {
        ...base,
        outcome: 'observed',
        reachable: true,
        authenticated: 'authenticated',
        protocol: draft.protocol_order?.[0] ?? 'openai_chat',
        discovery: 'succeeded',
        models: ['model-1', 'model-2', 'model-3'],
      };
    }
    return delay(observation, 900, signal);
  }

  /**
   * The supply guard, evaluated the way the server evaluates it: against the
   * CANDIDATE config the write would produce, not against a live binding.
   *
   * The old version asked 「is this source some agent's `current`?」, which is a
   * different and weaker question: deleting the second source of a two-source
   * chain strands nothing (the head still serves), while deleting the only
   * supplier of a *cooling* head strands a model that has no `current` at all.
   * Reporting per (backend, model) with the Agents that run it is also what makes
   * the confirm copy nameable — 「删除后 pm 将没有可用来源」.
   */
  private wouldInterrupt(candidate: Source[]): SupplyGap[] {
    const byId = new Map(candidate.map((s) => [s.id, s]));
    const gaps: SupplyGap[] = [];
    for (const a of this.agents) {
      if (a.mode !== 'hub') continue;
      const model = a.selected_model_id;
      if (!model) continue;
      const hops = a.routes?.[model]?.hops ?? [];
      const survives = hops
        .map((hop) => byId.get(hop.source_id))
        .some((s) => s !== undefined && isRunnable(s));
      if (survives) continue;
      gaps.push({
        backend: a.backend,
        model_id: model,
        agents: (a.named_agents ?? []).filter((n) => n.effective_model_id === model).map((n) => n.name),
      });
    }
    return gaps;
  }

  deleteSource(id: string, force = false) {
    this.syncAgents();
    const remaining = this.sources.filter((s) => s.id !== id);
    // `source_last_supplier` — the code the contract actually sends here.
    // `mode_switch_blocked` belongs to the mode route, and a client written
    // against it retried nothing on a real refusal.
    const gaps = this.wouldInterrupt(remaining);
    if (gaps.length > 0 && !force)
      throw new ApiCallError('source_last_supplier', undefined, true, gaps);
    this.sources = remaining;
    // Orders and the rollup are recomputed on the next read (syncAgents).
    return delay(undefined);
  }

  replaceCredential(id: string, body: CredentialReplace) {
    this.syncAgents();
    const source = this.sources.find((s) => s.id === id);
    if (!source) throw new ApiCallError('source_not_found');
    // The server's preconditions, restated rather than assumed: the menu offers
    // this only where they hold, but a mock that failed open would hide a wiring
    // mistake until it reached a real backend.
    if (!canReplaceKey(source) || !body.key.trim()) throw new ApiCallError('discovery_failed');
    const recovered = wasBlocked(source.state);
    const previousMask = source.masked_credential;
    const previousState = { ...source.state };
    // Atomic commit, standby-clearing semantics shared with refreshSource: a
    // replacement re-discovers and lands on standby, never straight to active.
    source.masked_credential = maskKey(body.key);
    source.credential_ref = rid('cred');
    source.state = { status: 'standby', retry_at: null, detail_key: null };
    const interrupted = this.wouldInterrupt(this.sources);
    // A RECOVERING write is exempt from the guard and merely reports what is
    // still stranded; only an elective one can be refused.
    if (interrupted.length > 0 && !recovered && !body.force) {
      source.masked_credential = previousMask;
      source.state = previousState;
      throw new ApiCallError('source_last_supplier', undefined, true, interrupted);
    }
    this.syncAgents();
    return delay({ source: structuredClone(source), removed_hops: [], interrupted }, 700);
  }

  reauthSource(id: string) {
    const source = this.sources.find((s) => s.id === id);
    if (!source) throw new ApiCallError('source_not_found');
    if (!canReauth(source)) throw new ApiCallError('discovery_failed');
    // Read BEFORE the irreversible step below, which sets needs_action itself and
    // would otherwise make every native re-auth report a recovery.
    const recovered = wasBlocked(source.state);
    // Native re-auth is irreversible from the first step: the server clears the
    // discovered models and drops the account label BEFORE any login happens, so
    // the row goes to 需处理 even if the user abandons the browser tab. Mirrored
    // here, because a mock that kept the old models would make the confirm dialog
    // look like a formality.
    if (source.supply_channel === 'native_cli') {
      source.models = [];
      source.account_label = null;
      source.state = { status: 'needs_action', retry_at: null, detail_key: 'models.source.needs_action.oauth_expired' };
    }
    const isDevice = source.vendor === 'openai';
    const flow: OAuthFlow = {
      flow_id: rid('oaf'),
      intent: 'reauth',
      // A reauth flow binds to the EXISTING source, which is what makes its
      // terminal response a repair rather than a creation.
      source_id: source.id,
      vendor: source.vendor,
      channel: source.supply_channel,
      state: 'awaiting_action',
      presentation: isDevice
        ? { auth_url: 'https://chatgpt.com/device', device_code: 'RFTQ-MPZK', expects: 'none', instructions_key: 'settings.models.oauth.deviceCode.hint' }
        : { auth_url: 'https://claude.ai/oauth/authorize?code=true&client_id=avibe&scope=org%3Acreate_api_key', device_code: null, expects: 'paste_code', instructions_key: 'settings.models.oauth.pasteCode.hint' },
      error_key: null,
      expires_at: new Date(Date.now() + 15 * 60_000).toISOString(),
    };
    this.flows.set(flow.flow_id, { flow, polls: 0, submitted: false, recovered });
    this.syncAgents();
    return delay(structuredClone(flow), 400);
  }

  listAgents() {
    this.syncAgents();
    return delay(structuredClone(this.agents));
  }

  private agentOr404(backend: AgentBackend): AgentSupply {
    const agent = this.agents.find((a) => a.backend === backend);
    if (!agent) throw new ApiCallError('source_not_found');
    return agent;
  }

  getAgentSources(backend: AgentBackend) {
    this.syncAgents();
    return delay(structuredClone(this.agentOr404(backend)));
  }

  putAgentSources(backend: AgentBackend, body: AgentSourcesPut) {
    const agent = this.agentOr404(backend);
    if (agent.mode === 'direct') throw new ApiCallError('direct_mode');
    const eligible = new Set(
      mockEligibility(this.sources, backend).filter((e) => e.eligible).map((e) => e.source_id),
    );
    const seen = new Set<string>();
    for (const id of body.order) {
      if (!eligible.has(id) || seen.has(id)) throw new ApiCallError('invalid_source_order', id);
      seen.add(id);
    }
    agent.sources = { order: [...body.order], eligibility: null };
    this.syncAgents();
    return delay(structuredClone(agent), 380);
  }

  /**
   * The chain both hub-only routes answer from, shared the way the server shares
   * it: `probe_agent` calls `_agent_chain` and takes its first runnable member.
   * Deriving the head a second time is how the mock's probe and chain would
   * disagree about the same supply — and `supply_state` is a rollup the v4 schema
   * now pins to this very array, so it cannot be restated per route either.
   */
  private chainFor(agent: AgentSupply, model: string) {
    const byId = new Map(this.sources.map((s) => [s.id, s]));
    const hops = agent.routes?.[model]?.hops ?? [];
    const chain: AgentChainLink[] = hops
      .map((hop) => ({ hop, source: byId.get(hop.source_id) }))
      .map(({ hop, source }) => {
        const modelEntry = source?.models.find((entry) => entry.id === hop.model_id);
        const callable = modelEntry !== undefined && modelEntry.retired !== true;
        const reason = !source
          ? 'source_missing' as const
          : callable ? null : 'model_unsupported' as const;
        return {
          source_id: hop.source_id,
          model_id: hop.model_id,
          channel: source?.supply_channel ?? 'hub',
          health: source && callable ? chainHealth(source) : 'error',
          runnable: source !== undefined && callable && isRunnable(source),
          // v4: process availability is a fact about the serving process — which
          // native CLI it can launch under its own login — and a browser mock has
          // no way to observe it. So it stands in for a runtime where every
          // configured CLI is launchable, rather than inventing an outage. The
          // unavailable branch is asserted in the unit tests, which can state the
          // fact instead of guessing it.
          reason,
          retry_at: source && callable && source.state.status === 'cooldown' ? source.state.retry_at ?? null : null,
        };
      });
    const supply_state: AgentChain['supply_state'] = chain.some((l) => l.runnable)
      ? 'ok'
      : chain.length > 0 && chain.every((l) => l.health === 'cooldown' && l.reason === null)
        ? 'waiting'
        : 'interrupted';
    const current = chain.find((link) => link.runnable);
    return { chain, current: current ? { source_id: current.source_id, model_id: current.model_id } : null, supply_state };
  }

  getAgentChain(backend: AgentBackend, model: string) {
    this.syncAgents();
    const agent = this.agentOr404(backend);
    // AC-7: direct mode has no src_* identity to report, so the route refuses
    // rather than answering with an empty (falsely alarming) chain.
    if (agent.mode === 'direct') throw new ApiCallError('direct_mode');
    const { chain, current, supply_state } = this.chainFor(agent, model);
    return delay({
      contract_version: AGENT_CHAIN_CONTRACT_VERSION,
      backend,
      model_id: model,
      current,
      chain,
      supply_state,
    });
  }

  putAgentChain(backend: AgentBackend, model: string, body: AgentChainPut) {
    const agent = this.agentOr404(backend);
    if (agent.mode === 'direct') throw new ApiCallError('direct_mode');
    const byId = new Map(this.sources.map((source) => [source.id, source]));
    const previous = agent.routes?.[model]?.hops ?? [];
    if (!validateRouteDraft(agent, this.sources, previous, body.hops).valid) {
      throw new ApiCallError('model_unsupported');
    }
    const removed_hops: RouteHopRef[] = previous.flatMap((hop, index) => body.hops.some((next) => equalHopIdentity(next, hop))
      ? []
      : [{ backend, menu_model: model, ...hop, position: index + 1 }]);
    const impactedAgents = (agent.named_agents ?? [])
      .filter((entry) => entry.effective_model_id === model)
      .map((entry) => entry.name);
    const hasRunnableHop = body.hops.some((hop) => {
      const source = byId.get(hop.source_id);
      return source?.models.some((entry) => entry.id === hop.model_id && entry.retired !== true) === true
        && isRunnable(source);
    });
    const gaps: SupplyGap[] = impactedAgents.length > 0 && !hasRunnableHop
      ? [{ backend, model_id: model, agents: impactedAgents }]
      : [];
    const confirmed = body.force === true
      && JSON.stringify(body.would_interrupt ?? []) === JSON.stringify(gaps)
      && JSON.stringify(body.would_remove_hops ?? []) === JSON.stringify(removed_hops);
    if (gaps.length > 0 && !confirmed) throw new ApiCallError('source_last_supplier', undefined, true, gaps, [], removed_hops);
    agent.routes = { ...(agent.routes ?? {}), [model]: { hops: body.hops.map((hop) => ({ ...hop })) } };
    this.syncAgents();
    const settled = this.chainFor(agent, model);
    return delay({
      chain: {
        contract_version: AGENT_CHAIN_CONTRACT_VERSION,
        backend,
        model_id: model,
        current: settled.current,
        chain: settled.chain,
        supply_state: settled.supply_state,
      },
      removed_hops,
      interrupted: gaps,
    });
  }

  probeAgent(backend: AgentBackend, model?: string) {
    this.syncAgents();
    const agent = this.agentOr404(backend);
    if (agent.mode === 'direct') throw new ApiCallError('direct_mode');
    const modelId = model ?? agent.selected_model_id;
    if (!modelId) throw new ApiCallError('model_unsupported');
    const { chain, supply_state } = this.chainFor(agent, modelId);
    const head = chain.find((l) => l.runnable);
    // `probe_no_candidate` is the contract's code for this; `no_runnable_source`
    // was invented here and is in no error vocabulary, so the L5 dry-run button
    // would have been written against a code the server never sends. The server
    // also names WHICH blocked family it is in the 409's detail, and the drawer
    // renders that key — a detail-less throw would leave it unreachable here.
    // `ok` cannot occur on this branch: it is the rollup's word for "some member
    // is runnable", which is exactly what failing to find a head rules out.
    if (!head) throw new ApiCallError('probe_no_candidate', `models.probe.no_candidate.${supply_state}`);
    // The native_cli half is a readiness answer, not a request: nothing upstream
    // is attempted, so there is no latency to report and a measured local number
    // would impersonate completion evidence. Readiness itself follows `reason`
    // above — every configured CLI is launchable in the mock.
    const native = head.channel === 'native_cli';
    const probe: ProbeResult = {
      contract_version: PROBE_RESULT_CONTRACT_VERSION,
      backend,
      channel: head.channel,
      reachable: true,
      source_id: head.source_id,
      model_id: head.model_id,
      latency_ms: native ? null : 180 + Math.floor(Math.random() * 420),
      error: null,
    };
    // A real upstream request takes a real moment; a local readiness check does not.
    return delay(probe, native ? 400 : 1200);
  }

  setAgentMode(backend: AgentBackend, mode: AgentMode) {
    const agent = this.agentOr404(backend);
    agent.mode = mode;
    if (mode === 'hub') {
      // Rejoining the gateway materializes one exact Route per matching menu model.
      const order = mockRecommendedOrder(this.sources, backend);
      agent.sources = { order, eligibility: null };
      agent.routes = Object.fromEntries(
        (agent.builtin_models ?? []).map((modelId) => [modelId, { hops: order
          .map((sourceId) => this.sources.find((source) => source.id === sourceId))
          .filter((source): source is Source => Boolean(source && source.models.some((model) => model.id === modelId)))
          .map((source) => ({ source_id: source.id, model_id: modelId })) }]),
      );
      agent.selected_model_id = agent.builtin_models?.[0] ?? this.sources[0]?.models[0]?.id ?? null;
      // The server's default comes from the STORED per-backend request, so a
      // non-null id here is explicit; nothing selected is the false case.
      agent.selected_model_explicit = agent.selected_model_id !== null;
      agent.named_agents = (agent.named_agents ?? []).map((n) => ({
        ...n,
        effective_model_id: agent.selected_model_id ?? null,
      }));
    }
    this.syncAgents();
    return delay(structuredClone(agent));
  }

  putMenu(menu: AgentMenu) {
    const agent = this.agents.find((a) => a.backend === 'opencode');
    if (!agent) throw new ApiCallError('source_not_found');
    agent.menu = menu;
    this.syncAgents();
    return delay(structuredClone(agent));
  }

  addCustomModel(sourceId: string, draft: CustomModelCreate) {
    const source = this.sources.find((s) => s.id === sourceId);
    if (!source) throw new ApiCallError('source_not_found');
    const existing = source.models.find((m) => m.id === draft.model_id);
    if (existing) {
      existing.display_name = draft.display_name ?? existing.display_name;
      existing.origin = 'manual';
      existing.reasoning_efforts = [...draft.reasoning_efforts];
    } else {
      source.models.push({
        id: draft.model_id,
        display_name: draft.display_name ?? null,
        origin: 'manual',
        reasoning_efforts: [...draft.reasoning_efforts],
        discovered_at: null,
      });
    }
    return delay(structuredClone(source), 400);
  }

  updateModelReasoningEfforts(sourceId: string, modelId: string, reasoningEfforts: string[]) {
    const source = this.sources.find((s) => s.id === sourceId);
    if (!source) throw new ApiCallError('source_not_found');
    const model = source.models.find((item) => item.id === modelId);
    if (!model) throw new ApiCallError('source_not_found');
    model.reasoning_efforts = [...reasoningEfforts];
    return delay(structuredClone(source));
  }

  deleteCustomModel(sourceId: string, modelId: string, confirmation?: GuardConfirmation) {
    const source = this.sources.find((s) => s.id === sourceId);
    if (!source) throw new ApiCallError('source_not_found');
    const references: RouteHopRef[] = [];
    for (const agent of this.agents) {
      for (const [menuModel, route] of Object.entries(agent.routes ?? {})) {
        for (const [index, hop] of route.hops.entries()) {
          if (equalHopIdentity(hop, { source_id: sourceId, model_id: modelId })) {
            references.push({ backend: agent.backend, menu_model: menuModel, ...hop, position: index + 1 });
          }
        }
      }
    }
    const confirmed = confirmation !== undefined
      && JSON.stringify(confirmation.would_remove_hops) === JSON.stringify(references)
      && JSON.stringify(confirmation.would_interrupt) === JSON.stringify([]);
    if (references.length > 0 && !confirmed) {
      throw new ApiCallError('source_model_in_route_chain', undefined, true, [], [], references);
    }
    source.models = source.models.filter((m) => !(m.id === modelId && m.origin === 'manual'));
    if (confirmation) {
      for (const agent of this.agents) {
        agent.routes = Object.fromEntries(
          Object.entries(agent.routes ?? {}).map(([menuModel, route]) => [
            menuModel,
            { hops: route.hops.filter((hop) => !equalHopIdentity(hop, { source_id: sourceId, model_id: modelId })) },
          ]),
        );
      }
    }
    this.syncAgents();
    return delay(structuredClone(source));
  }

  scanMigration() {
    // Read-only: recompute the fixture each call so re-scans stay idempotent.
    return delay(buildMockMigration(), 500);
  }

  applyMigration(itemIds: string[]) {
    const scan = buildMockMigration();
    // reauth needs the interactive OAuth flow, so it is never bulk-applied here.
    const chosen = scan.items.filter((i) => itemIds.includes(i.id) && i.proposed_action !== 'reauth');
    // Copy-only: each selected native config materializes a new source; the
    // (simulated) originals are never touched. import lands on the hub channel;
    // keep_native registers a sanctioned native_cli source. (controlled_import
    // is deferred per the 2026-07-23 L6 finding, so it's never emitted here.)
    for (const item of chosen) {
      const isKey = item.kind === 'api_key' || item.kind === 'opencode_provider';
      const channel: SupplyChannel = item.proposed_action === 'keep_native' ? 'native_cli' : 'hub';
      this.sources.push({
        id: rid('src'),
        created_at: new Date().toISOString(),
        last_discovered_at: new Date().toISOString(),
        kind: isKey ? 'api_key' : 'subscription',
        vendor: item.backend === 'opencode' ? 'zhipuai' : item.backend === 'codex' ? 'openai' : 'anthropic',
        display_name: item.masked_detail.split(' · ')[0] || 'Imported',
        protocol: item.backend === 'codex' ? 'openai_responses' : 'anthropic',
        base_url: null,
        supply_channel: channel,
        billing: isKey ? 'metered' : 'monthly',
        state: { status: 'standby', retry_at: null, detail_key: null },
        usage: isKey ? { cycle_used_pct: null, month_spend_cents: 0, currency: 'USD' } : { cycle_used_pct: 0, month_spend_cents: null, currency: null },
        account_label: channel === 'native_cli' ? 'me@gmail.com' : null,
        masked_credential: isKey ? 'sk-…dd3c' : null,
        models: [{ id: item.backend === 'opencode' ? 'glm-5.2' : item.backend === 'codex' ? 'gpt-5.6' : 'claude-opus-4-6', display_name: null, origin: 'discovered', reasoning_efforts: [], discovered_at: new Date().toISOString() }],
        credential_ref: channel === 'hub' ? rid('cred') : null,
      });
    }
    // Enable hub on the backends that received a hub-channel import.
    for (const backend of new Set(chosen.filter((i) => i.proposed_action !== 'keep_native').map((i) => i.backend))) {
      const agent = this.agents.find((a) => a.backend === backend);
      if (agent) agent.mode = 'hub';
    }
    this.syncAgents();
    return delay({ applied: chosen.length, sources: structuredClone(this.sources) }, 700);
  }

  listEvents(limit = 20, before?: string) {
    const start = before ? this.events.findIndex((e) => e.id === before) + 1 : 0;
    return delay(structuredClone(this.events.slice(start, start + limit)));
  }

  getRuntimeStatus() {
    return delay(structuredClone(this.runtime));
  }

  installRuntime() {
    this.runtime.status.health = 'installing';
    this.runtime.status.error_key = null;
    setTimeout(() => {
      this.runtime.status.installed_version = this.runtime.manifest.version;
      this.runtime.status.verified = true;
      this.runtime.status.health = 'not_started';
    }, 1200);
    return delay(structuredClone(this.runtime));
  }

  startRuntime() {
    this.runtime.status.health = 'ok';
    this.runtime.status.listening = { host: '127.0.0.1', port: 15220 };
    return delay(structuredClone(this.runtime));
  }

  startOAuth(vendor: string, channel: SupplyChannel, clientNonce?: string) {
    if (clientNonce) {
      const existing = [...this.flows.values()].find((entry) =>
        entry.flow.client_nonce === clientNonce
        && entry.flow.vendor === vendor
        && entry.flow.channel === channel,
      );
      if (existing) return delay(structuredClone(existing.flow), 500);
    }
    const isDevice = vendor === 'openai';
    const flow: OAuthFlow = {
      flow_id: rid('oaf'),
      // Deterministic pending-source binding (schema: hub flows always set it),
      // consumed when the flow completes — mirrors the server, where the
      // materialized source takes source.id = flow.source_id.
      source_id: rid('src'),
      vendor,
      channel,
      client_nonce: clientNonce ?? null,
      state: 'awaiting_action',
      presentation: isDevice
        ? {
            auth_url: 'https://chatgpt.com/device',
            device_code: 'KDWT-GBSF',
            expects: 'none',
            instructions_key: 'settings.models.oauth.deviceCode.hint',
          }
        : {
            auth_url: 'https://claude.ai/oauth/authorize?code=true&client_id=avibe&scope=org%3Acreate_api_key',
            device_code: null,
            expects: 'paste_code',
            instructions_key: 'settings.models.oauth.pasteCode.hint',
          },
      error_key: null,
      expires_at: new Date(Date.now() + 15 * 60_000).toISOString(),
    };
    this.flows.set(flow.flow_id, { flow, polls: 0, submitted: false });
    return delay(structuredClone(flow), 500);
  }

  getOAuthStatus(flowId: string) {
    const entry = this.flows.get(flowId);
    if (!entry) throw new ApiCallError('flow_not_found');
    entry.polls += 1;
    const { flow } = entry;
    if (flow.state === 'success' || flow.state === 'failed' || flow.state === 'cancelled') {
      return delay(this.oauthResult(entry));
    }
    if (flow.presentation.expects === 'none') {
      // Device flow self-completes after a few polls.
      if (entry.polls >= 3) this.completeFlow(entry);
    } else if (entry.submitted) {
      // Paste flows: verifying → success on the next poll.
      this.completeFlow(entry);
    }
    return delay(this.oauthResult(entry));
  }

  submitOAuth(flowId: string, _value: string) {
    const entry = this.flows.get(flowId);
    if (!entry) throw new ApiCallError('flow_not_found');
    entry.submitted = true;
    entry.flow.state = 'verifying';
    return delay(this.oauthResult(entry));
  }

  cancelOAuth(flowId: string) {
    const entry = this.flows.get(flowId);
    if (entry) entry.flow.state = 'cancelled';
    return delay(undefined);
  }

  // Reaching `success` IS the creation, as on the server: status/submit
  // materialize the Source inside the same call that first reports success, and
  // consume the flow binding doing it. Splitting the two here is what let a
  // client that finalized with a second POST look correct against the mock while
  // failing `flow_not_found` against the real server.
  private completeFlow(entry: MockFlow) {
    entry.flow.state = 'success';
    const flow = entry.flow;
    const id = flow.source_id ?? rid('src');
    flow.source_id = id;
    const isOpenai = flow.vendor === 'openai';
    if (flow.intent === 'reauth') {
      // A reauth flow REPAIRS the source it bound to; it creates nothing. The
      // login is what puts the credential back, so the blocker clears and the
      // supply a native start had wiped comes back with it.
      const source = this.sources.find((s) => s.id === id);
      if (!source) return;
      source.account_label = 'me@gmail.com';
      source.state = { status: 'standby', retry_at: null, detail_key: null };
      source.last_discovered_at = new Date().toISOString();
      if (source.models.length === 0) {
        source.models = [
          {
            id: isOpenai ? 'gpt-5.6' : 'claude-opus-4-6',
            display_name: isOpenai ? 'GPT-5.6' : 'Opus 4.6',
            origin: 'discovered',
            reasoning_efforts: [],
            discovered_at: new Date().toISOString(),
          },
        ];
      }
      return;
    }
    // Idempotent, like `_create_oauth_source(idempotent=True)`: re-polling a
    // completed flow re-echoes the same source instead of creating a second one.
    if (this.sources.some((s) => s.id === id)) return;
    const source: Source = {
      id,
      created_at: new Date().toISOString(),
      last_discovered_at: new Date().toISOString(),
      kind: 'subscription',
      vendor: flow.vendor,
      display_name: isOpenai ? 'ChatGPT 订阅' : 'Claude 订阅',
      protocol: isOpenai ? 'openai_responses' : 'anthropic',
      base_url: null,
      supply_channel: flow.channel,
      billing: 'monthly',
      state: { status: 'standby', retry_at: null, detail_key: null },
      usage: { cycle_used_pct: 0, month_spend_cents: null, currency: null },
      // native_cli subscriptions surface the CLI account; gateway-held sources
      // may stay null until a later adapter revision publishes an account label.
      account_label: flow.channel === 'native_cli' ? 'me@gmail.com' : null,
      masked_credential: null,
      models: isOpenai
        ? [{ id: 'gpt-5.6', display_name: 'GPT-5.6', origin: 'discovered', reasoning_efforts: [], discovered_at: new Date().toISOString() }]
        : [{ id: 'claude-opus-4-6', display_name: 'Opus 4.6', origin: 'discovered', reasoning_efforts: [], discovered_at: new Date().toISOString() }],
      credential_ref: flow.channel === 'hub' ? rid('cred') : null,
    };
    this.sources.push(source);
    entry.placement = this.placeNewSource(source);
  }

  /**
   * The terminal envelope every status/submit response carries (api.md, "OAuth
   * completion"): the flow, plus the creation it performed once it succeeded.
   * Looked up by `source_id` rather than remembered from the completing call, so
   * a later poll on an already-finished flow answers the same thing the server's
   * idempotent path does instead of pretending nothing was created.
   */
  private oauthResult(entry: MockFlow): OAuthResult {
    const flow = structuredClone(entry.flow);
    const source = flow.state === 'success' ? this.sources.find((s) => s.id === flow.source_id) : undefined;
    if (!source) return { flow, created: null, repaired: null };
    // One intent, one tail — the same discrimination the live unwrap makes, so a
    // reauth never reports an adoption list and a create never reports recovery.
    if (flow.intent === 'reauth') {
      this.syncAgents();
      return {
        flow,
        created: null,
        repaired: {
          source: structuredClone(source),
          recovered: entry.recovered === true,
          interrupted_pairs: this.wouldInterrupt(this.sources),
        },
      };
    }
    return {
      flow,
      created: {
        source: structuredClone(source),
        ...(entry.placement ?? { added_to: [], adopted_by: [] }),
      },
      repaired: null,
    };
  }

  patchSource(id: string, patch: SourcePatch) {
    const source = this.sources.find((s) => s.id === id);
    if (!source) throw new ApiCallError('source_not_found');
    if (typeof patch.display_name === 'string') source.display_name = patch.display_name;
    if ('base_url' in patch && source.kind === 'api_key') source.base_url = patch.base_url ?? null;
    return delay(structuredClone(source), 300);
  }

  refreshSource(id: string, _confirmation?: GuardConfirmation) {
    const source = this.sources.find((s) => s.id === id);
    if (!source) throw new ApiCallError('source_not_found');
    // Native-CLI subscriptions can't be re-discovered (server rejects them);
    // the UI only offers this action for hub sources, but fail closed anyway.
    if (source.supply_channel === 'native_cli') throw new ApiCallError('discovery_failed');
    source.state = { status: 'standby', retry_at: null, detail_key: null };
    source.last_discovered_at = new Date().toISOString();
    return delay({ source: structuredClone(source), discovered: source.models.length }, 700);
  }
}

// §4.3's runnability half: retry-ready, and never needs_action / error. A
// cooling source stays visible in the chain but is skipped by the turn. Derived
// from the page's own predicate rather than restated, so the fake server and the
// UI cannot drift into disagreeing about which statuses can serve a turn.
const isRunnable = (s: Source): boolean => !isUnhealthy(s.state);

// SourceStatus → the chain link's health vocabulary (the two healthy statuses
// collapse; the three blockers map one-to-one).
const chainHealth = (s: Source): AgentChainLink['health'] =>
  s.state.status === 'cooldown' ? 'cooldown' : s.state.status === 'needs_action' ? 'needs_action' : s.state.status === 'error' ? 'error' : 'healthy';

function vendorLabel(vendor: string): string {
  const table: Record<string, string> = {
    anthropic: 'Anthropic API Key',
    openai: 'OpenAI API Key',
    zhipuai: '智谱 API Key',
    kimi: 'Kimi API Key',
    xai: 'xAI API Key',
  };
  return table[vendor] ?? `${vendor} API Key`;
}

// Non-reversible display mask (contract rule: ≤7-char prefix + "…" + last 4).
function maskKey(key: string): string {
  const k = key.trim();
  if (k.length <= 5) return `${k}…`;
  const prefix = k.slice(0, Math.min(7, k.length - 4));
  return `${prefix}…${k.slice(-4)}`;
}

function hostLabel(baseUrl: string | null | undefined): string {
  if (!baseUrl) return 'API Key';
  try {
    return new URL(baseUrl).host;
  } catch {
    return 'API Key';
  }
}

const mockStore = new MockStore();

const mockApi: ModelsApi = {
  listSources: () => mockStore.listSources(),
  observeApiKeySource: (draft, signal) => mockStore.observeApiKeySource(draft, signal),
  createApiKeySource: (draft) => mockStore.createApiKeySource(draft),
  patchSource: (id, patch) => mockStore.patchSource(id, patch),
  refreshSource: (id, confirmation) => mockStore.refreshSource(id, confirmation),
  deleteSource: (id, force) => mockStore.deleteSource(id, force),
  replaceCredential: (id, body) => mockStore.replaceCredential(id, body),
  reauthSource: (id) => mockStore.reauthSource(id),
  listAgents: () => mockStore.listAgents(),
  getAgentSources: (backend) => mockStore.getAgentSources(backend),
  putAgentSources: (backend, body) => mockStore.putAgentSources(backend, body),
  getAgentChain: (backend, model) => mockStore.getAgentChain(backend, model),
  putAgentChain: (backend, model, body) => mockStore.putAgentChain(backend, model, body),
  probeAgent: (backend, model) => mockStore.probeAgent(backend, model),
  setAgentMode: (backend, mode) => mockStore.setAgentMode(backend, mode),
  putMenu: (menu) => mockStore.putMenu(menu),
  addCustomModel: (sourceId, draft) => mockStore.addCustomModel(sourceId, draft),
  updateModelReasoningEfforts: (sourceId, modelId, reasoningEfforts) => mockStore.updateModelReasoningEfforts(sourceId, modelId, reasoningEfforts),
  deleteCustomModel: (sourceId, modelId, confirmation) => mockStore.deleteCustomModel(sourceId, modelId, confirmation),
  scanMigration: () => mockStore.scanMigration(),
  applyMigration: (itemIds) => mockStore.applyMigration(itemIds),
  listEvents: (limit, before) => mockStore.listEvents(limit, before),
  getRuntimeStatus: () => mockStore.getRuntimeStatus(),
  installRuntime: () => mockStore.installRuntime(),
  startRuntime: () => mockStore.startRuntime(),
  startOAuth: (vendor, channel, clientNonce) => mockStore.startOAuth(vendor, channel, clientNonce),
  getOAuthStatus: (flowId) => mockStore.getOAuthStatus(flowId),
  submitOAuth: (flowId, value) => mockStore.submitOAuth(flowId, value),
  cancelOAuth: (flowId) => mockStore.cancelOAuth(flowId),
};

/** The single client instance. Stable across renders (safe in effect deps). */
export const modelsApi: ModelsApi = isLive() ? liveApi : mockApi;
