// Model Hub API client. Presents ONE typed surface to the UI; internally it
// either replays server-recorded fixtures for hermetic tests or calls the frozen
// `/api/models/*` REST endpoints (live mode). Components never
// branch on the mode — flip `MODELS_API_MODE` in featureFlags.ts to switch.
//
// Methods unwrap the frozen envelope ({ok:true, …} | {ok:false, error}) and
// throw an Error carrying the machine code on failure, so callers work with
// plain domain objects.
import { apiFetch, isApiFetchDeadlineAbort, withApiDeadline } from '@/lib/apiFetch';
import { MODELS_API_MODE } from './featureFlags';
import mockCorpusJson from './modelHubMockCorpus.json';
import { buildMockEvents, buildMockRuntime } from './mockData';
import type {
  AddedTo,
  AdoptedBy,
  AgentBackend,
  AgentChain,
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

/** Add-time Route placement returned by both source-creation paths. */
export type Adoption = { added_to: AddedTo[]; adopted_by: AdoptedBy[] };
export type SourceCreated = { source: Source } & Adoption;
export type SourceRefresh = { source: Source; discovered: number };
export type CredentialReplacement = {
  source: Source;
  removed_hops: RouteHopRef[];
  interrupted: SupplyGap[];
};
export type SourcePatched = {
  source: Source;
  removed_hops: RouteHopRef[];
  interrupted: SupplyGap[];
};
export type SourceDeleted = {
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
  patchSource(id: string, patch: SourcePatch): Promise<SourcePatched>;
  /** Re-run discovery on a hub source; resolves with the updated source and count.
   *  Contractually ALSO the recovery test: run on a needs_action / error source
   *  it clears the blocker and returns the source to standby. v3 adds no second
   *  「recover」 endpoint, so this is the whole retry affordance. */
  refreshSource(id: string, confirmation?: GuardConfirmation): Promise<SourceRefresh>;
  /** Delete a source. A destructive retry echoes the server's exact guard plan. */
  deleteSource(id: string, confirmation?: GuardConfirmation): Promise<SourceDeleted>;
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

// The server owns the execution ceiling; this browser deadline is only a
// backstop for a server that never answers, so it must outlast that controlled
// failure. The margin covers CSRF acquisition, both transits (including a
// tunnel), one fast CSRF-rejected attempt and replay, handler dispatch, and body
// decoding. Keep the ceiling aligned with model_hub_client.py:_RPC_TIMEOUT_SECONDS.
export const MODEL_HUB_RPC_CEILING_MS = 300_000;
const TRANSPORT_MARGIN_MS = 30_000;
export const MODEL_HUB_REQUEST_DEADLINE_MS =
  MODEL_HUB_RPC_CEILING_MS + TRANSPORT_MARGIN_MS;

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  try {
    return await withApiDeadline(
      MODEL_HUB_REQUEST_DEADLINE_MS,
      init?.signal ?? undefined,
      async (signal) => {
        const res = await apiFetch(path, { ...init, signal });
        let payload: unknown = null;
        try {
          payload = await res.json();
        } catch (error) {
          if (signal.aborted) {
            throw signal.reason ?? error;
          }
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
      },
    );
  } catch (error) {
    if (!isApiFetchDeadlineAbort(error)) throw error;
    // The deadline says the answer did not arrive, not whether the route wrote.
    throw new ApiCallError('bad_response', `Request deadline exceeded for ${path}`, false);
  }
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

type SourcePatchedResponse = {
  source?: Source;
  removed_hops?: RouteHopRef[];
  interrupted?: SupplyGap[];
} & Source;

const sourcePatched = (r: SourcePatchedResponse): SourcePatched => ({
  source: (r.source ?? r) as Source,
  removed_hops: routeHopRefs(r.removed_hops),
  interrupted: supplyGaps(r.interrupted),
});

type SourceDeletedResponse = {
  removed_hops?: RouteHopRef[];
  interrupted?: SupplyGap[];
};

const sourceDeleted = (r: SourceDeletedResponse): SourceDeleted => ({
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
  patchSource: (id, patch) => call<SourcePatchedResponse>(`/api/models/sources/${encodeURIComponent(id)}`, jsonInit('PATCH', patch)).then(sourcePatched),
  refreshSource: (id, confirmation) => call<SourceRefresh>(`/api/models/sources/${encodeURIComponent(id)}/refresh`, jsonInit('POST', confirmation ?? {})),
  deleteSource: (id, confirmation) => call<SourceDeletedResponse>(
    `/api/models/sources/${encodeURIComponent(id)}${confirmation ? '?force=true' : ''}`,
    jsonInit('DELETE', confirmation ? {
      would_remove_hops: confirmation.would_remove_hops,
      would_interrupt: confirmation.would_interrupt,
    } : undefined),
  ).then(sourceDeleted),
  // Both repair routes reject unknown body keys outright (`discovery_failed` /
  // `reauth_confirmation_required`), so these bodies are exactly the contract's
  // and carry no `contract_version` — the same closed-body rule as putAgentSources.
  replaceCredential: (id, body) => call<CredentialReplacementResponse>(`/api/models/sources/${encodeURIComponent(id)}/credential`, jsonInit('PUT', body)).then(credentialReplacement),
  // The OAuth acknowledgement is unconditional because beginning reauth may
  // irreversibly replace grant material. It does not stand in for destructive
  // supply consent: guarded inventory mutations separately echo the server plan.
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

// ── Recorded mock client ─────────────────────────────────────────────────
type JsonObject = Record<string, unknown>;
type RecordedBody =
  | { present: false }
  | { present: true; value: unknown };
type RecordedRequest = {
  operation: string;
  path: JsonObject;
  query: Record<string, { present: boolean; value?: unknown }>;
  body: RecordedBody;
};
type RecordedReads = {
  sources: Source[];
  agents: AgentSupply[];
  agent_sources: Record<AgentBackend, AgentSupply>;
  agent_chains: Partial<Record<AgentBackend, Record<string, AgentChain>>>;
};
type RecordedOutcome =
  | { kind: 'success'; value: unknown }
  | {
      kind: 'error';
      error: string;
      detail: string;
      status: number;
      data: JsonObject;
    };
type RecordedTransition = {
  key: {
    id: string;
    pre: {
      model_hub_config_sha256: string;
      fixture_world_sha256: string;
    };
    request: RecordedRequest;
  };
  outcome: RecordedOutcome;
  post: {
    model_hub_config_sha256: string;
    fixture_world_sha256: string;
    config: unknown;
    fixture_world: unknown;
    reads: RecordedReads;
  };
};
export type MockCorpus = {
  generator: string;
  recording_operations: Array<{
    operation: string;
    request: RecordedRequest;
  }>;
  seed: {
    model_hub_config_sha256: string;
    fixture_world_sha256: string;
    config: unknown;
    fixture_world: unknown;
    reads: RecordedReads;
  };
  transitions: RecordedTransition[];
};

const mockCorpus = mockCorpusJson as unknown as MockCorpus;
const clone = <T>(value: T): T => structuredClone(value);

const canonicalJson = (value: unknown): string => {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`;
  if (value !== null && typeof value === 'object') {
    return `{${Object.entries(value as JsonObject)
      .sort(([left], [right]) => left < right ? -1 : left > right ? 1 : 0)
      .map(([key, item]) => `${JSON.stringify(key)}:${canonicalJson(item)}`)
      .join(',')}}`;
  }
  return JSON.stringify(value);
};

const base64Url = (value: string): string => {
  const bytes = new TextEncoder().encode(value);
  let binary = '';
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return globalThis.btoa(binary)
    .replaceAll('+', '-')
    .replaceAll('/', '_')
    .replace(/=+$/, '');
};

const normalizedRequest = (
  operation: string,
  path: JsonObject = {},
  body: RecordedBody = { present: false },
  query: RecordedRequest['query'] = {},
): RecordedRequest => ({ operation, path, query, body });

export class UncontractedMockTransitionError extends Error {
  readonly code = 'uncontracted_mock_transition';
  readonly missingKey: string;
  readonly operation: string;
  readonly generatorCommand: string | null;

  constructor(
    missingKey: string,
    operation: string,
    generatorCommand: string | null,
  ) {
    super(
      generatorCommand
        ? [
            'uncontracted_mock_transition',
            `Missing key: ${missingKey}`,
            `Operation: ${operation}`,
            `Record it: ${generatorCommand}`,
          ].join('\n')
        : [
            'uncontracted_mock_transition',
            `Missing key: ${missingKey}`,
            `Operation: ${operation}`,
            'No authoritative server dispatch exists for this operation; add or restore that server operation before recording mock evidence.',
          ].join('\n'),
    );
    this.name = 'UncontractedMockTransitionError';
    this.missingKey = missingKey;
    this.operation = operation;
    this.generatorCommand = generatorCommand;
  }
}

/**
 * A replay store, not a second Model Hub service.
 *
 * Every policy-bearing mutation must match a transition produced by the Python
 * service. Reads are the server projections recorded at that exact post-state.
 */
export class MockStore {
  private reads: RecordedReads;
  private configHash: string;
  private fixtureWorldHash: string;
  private readonly corpus: MockCorpus;
  private readonly transitions: Map<string, RecordedTransition>;
  private readonly events = buildMockEvents();
  private readonly runtime = buildMockRuntime();

  constructor(corpus: MockCorpus = mockCorpus) {
    this.corpus = corpus;
    this.reads = clone(corpus.seed.reads);
    this.configHash = corpus.seed.model_hub_config_sha256;
    this.fixtureWorldHash = corpus.seed.fixture_world_sha256;
    this.transitions = new Map(
      corpus.transitions.map((transition) => [transition.key.id, transition]),
    );
  }

  get sources(): Source[] {
    return clone(this.reads.sources);
  }

  get agents(): AgentSupply[] {
    return clone(this.reads.agents);
  }

  private transitionKey(request: RecordedRequest) {
    const key = {
      version: 1,
      pre: {
        model_hub_config_sha256: this.configHash,
        fixture_world_sha256: this.fixtureWorldHash,
      },
      request,
    };
    return base64Url(canonicalJson(key));
  }

  private replay<T>(request: RecordedRequest): Promise<T> {
    const missingKey = this.transitionKey(request);
    const transition = this.transitions.get(missingKey);
    if (!transition) {
      const generatorCommand = this.corpus.recording_operations.some(
        ({ operation }) => operation === request.operation,
      )
        ? `${this.corpus.generator} --record-miss ${missingKey}`
        : null;
      throw new UncontractedMockTransitionError(
        missingKey,
        request.operation,
        generatorCommand,
      );
    }

    this.reads = clone(transition.post.reads);
    this.configHash = transition.post.model_hub_config_sha256;
    this.fixtureWorldHash = transition.post.fixture_world_sha256;

    if (transition.outcome.kind === 'error') {
      const data = transition.outcome.data;
      throw new ApiCallError(
        transition.outcome.error,
        transition.outcome.detail,
        true,
        supplyGaps(data.would_interrupt),
        supplyGaps(data.interrupted_pairs),
        routeHopRefs(data.would_remove_hops),
        transition.outcome.status,
        typeof data.observation === 'object' && data.observation !== null
          ? data.observation as SourceObservation
          : undefined,
      );
    }
    return Promise.resolve(clone(transition.outcome.value) as T);
  }

  listSources(): Promise<Source[]> {
    return Promise.resolve(this.sources);
  }

  listAgents(): Promise<AgentSupply[]> {
    return Promise.resolve(this.agents);
  }

  getAgentSources(backend: AgentBackend): Promise<AgentSupply> {
    const projection = this.reads.agent_sources[backend];
    if (projection) return Promise.resolve(clone(projection));
    return this.replay(normalizedRequest(
      'getAgentSources',
      { backend },
    ));
  }

  getAgentChain(backend: AgentBackend, model: string): Promise<AgentChain> {
    const projection = this.reads.agent_chains[backend]?.[model];
    if (projection) return Promise.resolve(clone(projection));
    return this.replay(normalizedRequest(
      'getAgentChain',
      { backend },
      { present: false },
      { model: { present: true, value: model } },
    ));
  }

  listEvents(limit = 20, before?: string): Promise<ResolutionEvent[]> {
    const start = before
      ? Math.max(0, this.events.findIndex((event) => event.id === before) + 1)
      : 0;
    return Promise.resolve(clone(this.events.slice(start, start + limit)));
  }

  getRuntimeStatus(): Promise<RuntimeDependency> {
    return Promise.resolve(clone(this.runtime));
  }

  observeApiKeySource(
    draft: ApiKeySourceObservation,
    _signal?: AbortSignal,
  ): Promise<SourceObservation> {
    return this.replay(normalizedRequest(
      'observeApiKeySource',
      {},
      { present: true, value: draft },
    ));
  }

  createApiKeySource(draft: ApiKeySourceCreate): Promise<SourceCreated> {
    return this.replay(normalizedRequest(
      'createApiKeySource',
      {},
      { present: true, value: draft },
    ));
  }

  patchSource(id: string, body: SourcePatch): Promise<SourcePatched> {
    return this.replay(normalizedRequest(
      'patchSource',
      { id },
      { present: true, value: body },
    ));
  }

  refreshSource(
    id: string,
    confirmation?: GuardConfirmation,
  ): Promise<SourceRefresh> {
    return this.replay(normalizedRequest(
      'refreshSource',
      { id },
      { present: true, value: confirmation ?? {} },
    ));
  }

  deleteSource(
    id: string,
    confirmation?: GuardConfirmation,
  ): Promise<SourceDeleted> {
    return this.replay(normalizedRequest(
      'deleteSource',
      { id },
      confirmation
        ? {
            present: true,
            value: {
              would_remove_hops: confirmation.would_remove_hops,
              would_interrupt: confirmation.would_interrupt,
            },
          }
        : { present: false },
      confirmation
        ? { force: { present: true, value: true } }
        : {},
    ));
  }

  replaceCredential(
    id: string,
    body: CredentialReplace,
  ): Promise<CredentialReplacement> {
    return this.replay(normalizedRequest(
      'replaceCredential',
      { id },
      { present: true, value: body },
    ));
  }

  reauthSource(id: string): Promise<OAuthFlow> {
    return this.replay(normalizedRequest(
      'reauthSource',
      { id },
      { present: true, value: { acknowledge_irreversible: true } },
    ));
  }

  putAgentSources(
    backend: AgentBackend,
    body: AgentSourcesPut,
  ): Promise<AgentSupply> {
    return this.replay(normalizedRequest(
      'putAgentSources',
      { backend },
      { present: true, value: body },
    ));
  }

  putAgentChain(
    backend: AgentBackend,
    model: string,
    body: AgentChainPut,
  ): Promise<AgentChainMutation> {
    return this.replay(normalizedRequest(
      'putAgentChain',
      { backend },
      { present: true, value: body },
      { model: { present: true, value: model } },
    ));
  }

  probeAgent(
    backend: AgentBackend,
    model?: string,
  ): Promise<ProbeResult> {
    return this.replay(normalizedRequest(
      'probeAgent',
      { backend },
      { present: true, value: model ? { model } : {} },
    ));
  }

  setAgentMode(
    backend: AgentBackend,
    mode: AgentMode,
  ): Promise<AgentSupply> {
    return this.replay(normalizedRequest(
      'setAgentMode',
      { backend },
      { present: true, value: { mode } },
    ));
  }

  putMenu(menu: AgentMenu): Promise<AgentSupply> {
    return this.replay(normalizedRequest(
      'putMenu',
      { backend: 'opencode' },
      { present: true, value: { menu } },
    ));
  }

  addCustomModel(
    sourceId: string,
    draft: CustomModelCreate,
  ): Promise<Source> {
    return this.replay(normalizedRequest(
      'addCustomModel',
      { sourceId },
      { present: true, value: draft },
    ));
  }

  updateModelReasoningEfforts(
    sourceId: string,
    modelId: string,
    reasoningEfforts: string[],
  ): Promise<Source> {
    return this.replay(normalizedRequest(
      'updateModelReasoningEfforts',
      { sourceId, modelId },
      { present: true, value: { reasoning_efforts: reasoningEfforts } },
    ));
  }

  deleteCustomModel(
    sourceId: string,
    modelId: string,
    confirmation?: GuardConfirmation,
  ): Promise<Source> {
    return this.replay(normalizedRequest(
      'deleteCustomModel',
      { sourceId, modelId },
      { present: true, value: confirmation ?? {} },
    ));
  }

  scanMigration(): Promise<MigrationScan> {
    return this.replay(normalizedRequest('scanMigration'));
  }

  applyMigration(itemIds: string[]): Promise<MigrationApplyResult> {
    return this.replay(normalizedRequest(
      'applyMigration',
      {},
      { present: true, value: { item_ids: itemIds } },
    ));
  }

  installRuntime(): Promise<RuntimeDependency> {
    return this.replay(normalizedRequest('installRuntime'));
  }

  startRuntime(): Promise<RuntimeDependency> {
    return this.replay(normalizedRequest('startRuntime'));
  }

  startOAuth(
    vendor: string,
    channel: SupplyChannel,
    clientNonce?: string,
  ): Promise<OAuthFlow> {
    return this.replay(normalizedRequest(
      'startOAuth',
      {},
      {
        present: true,
        value: {
          vendor,
          channel,
          ...(clientNonce ? { client_nonce: clientNonce } : {}),
        },
      },
    ));
  }

  getOAuthStatus(flowId: string): Promise<OAuthResult> {
    return this.replay(normalizedRequest(
      'getOAuthStatus',
      { flowId },
    ));
  }

  submitOAuth(flowId: string, value: string): Promise<OAuthResult> {
    return this.replay(normalizedRequest(
      'submitOAuth',
      {},
      { present: true, value: { flow_id: flowId, value } },
    ));
  }

  cancelOAuth(flowId: string): Promise<void> {
    return this.replay(normalizedRequest(
      'cancelOAuth',
      {},
      { present: true, value: { flow_id: flowId } },
    ));
  }
}

const mockStore = new MockStore();

const mockApi: ModelsApi = {
  listSources: () => mockStore.listSources(),
  observeApiKeySource: (draft, signal) => mockStore.observeApiKeySource(draft, signal),
  createApiKeySource: (draft) => mockStore.createApiKeySource(draft),
  patchSource: (id, patch) => mockStore.patchSource(id, patch),
  refreshSource: (id, confirmation) => mockStore.refreshSource(id, confirmation),
  deleteSource: (id, confirmation) => mockStore.deleteSource(id, confirmation),
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
