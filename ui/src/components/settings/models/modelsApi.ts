// Model Hub API client. Presents one typed surface over the frozen
// `/api/models/*` REST endpoints.
//
// Methods unwrap the frozen envelope ({ok:true, …} | {ok:false, error}) and
// throw an Error carrying the machine code on failure, so callers work with
// plain domain objects.
import { apiFetch, isApiFetchDeadlineAbort, withApiDeadline } from '@/lib/apiFetch';
import type {
  AdoptedBy,
  AddedTo,
  AgentBackend,
  AgentChain,
  AgentChainMutation,
  AgentChainPut,
  AgentMode,
  AgentSourcesPut,
  AgentSupply,
  ApiKeySourceCreate,
  ApiKeySourceObservation,
  BackendModelsPut,
  CredentialReplace,
  CustomModelCreate,
  MigrationApplyResult,
  MigrationScan,
  ModelsDevMatch,
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
  UsageSummary,
} from './types';
import { USAGE_DEFAULT_WINDOW_DAYS } from './types';

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
  /** Deep CLI discovery used after the fast Agent snapshot has painted. */
  refreshAgentPresence(): Promise<AgentSupply[]>;
  /** Per-backend enabled subset + order (the 来源顺序 drawer's read). */
  getAgentSources(backend: AgentBackend): Promise<AgentSupply>;
  /** Total write of the exact stored source order. */
  putAgentSources(backend: AgentBackend, body: AgentSourcesPut): Promise<AgentSupply>;
  /** Store an optional source order and apply it to every existing route atomically. */
  reorderAgentChains(backend: AgentBackend, order?: string[]): Promise<AgentSupply>;
  /** Resolution chain for one model. Hub mode only — direct answers `direct_mode`. */
  getAgentChain(backend: AgentBackend, model: string): Promise<AgentChain>;
  /** Complete overview chain projection for one Hub backend. */
  getAgentChains(backend: AgentBackend): Promise<AgentChain[]>;
  /** Total replacement of the exact stored chain. */
  putAgentChain(backend: AgentBackend, model: string, body: AgentChainPut): Promise<AgentChainMutation>;
  /** One real request through the chain. Hub mode only, same reason. */
  probeAgent(backend: AgentBackend, model?: string): Promise<ProbeResult>;
  setAgentMode(backend: AgentBackend, mode: AgentMode): Promise<AgentSupply>;
  /** Apply the difference between `baseline` and `models` to the latest saved
   *  catalog. The echoed `AgentSupply.catalog_models` is canonical — including
   *  the order, the server-derived `locked`/`routeable`, and any concurrent
   *  edit this caller never saw. */
  putAgentModels(backend: AgentBackend, body: BackendModelsPut): Promise<AgentSupply>;
  /** Normalized models.dev candidates for a free-text query. A pure read: it
   *  persists nothing, so a fill the user abandons leaves no trace. */
  searchModelsDev(query: string, signal?: AbortSignal): Promise<ModelsDevMatch[]>;
  addCustomModel(sourceId: string, draft: CustomModelCreate): Promise<Source>;
  updateModelReasoningEfforts(sourceId: string, modelId: string, reasoningEfforts: string[]): Promise<Source>;
  deleteCustomModel(sourceId: string, modelId: string, confirmation?: GuardConfirmation): Promise<Source>;
  scanMigration(): Promise<MigrationScan>;
  applyMigration(itemIds: string[]): Promise<MigrationApplyResult>;
  /** `before` is an event id cursor (「查看全部」 pagination). */
  listEvents(limit?: number, before?: string): Promise<ResolutionEvent[]>;
  /** Metered token report over a trailing local-day window. `days` is a REQUEST:
   *  the server clamps it to retention and echoes what it served in
   *  `window_days`, which is the only number a view may display. */
  getUsageSummary(days?: number): Promise<UsageSummary>;
  getRuntimeStatus(): Promise<RuntimeDependency>;
  /** Start the contract-owned client installation transaction. */
  installRuntime(): Promise<RuntimeDependency>;
  startRuntime(): Promise<RuntimeDependency>;
  stopRuntime(): Promise<RuntimeDependency>;
  startOAuth(vendor: string, channel: SupplyChannel, clientNonce?: string): Promise<OAuthFlow>;
  getOAuthStatus(flowId: string): Promise<OAuthResult>;
  submitOAuth(flowId: string, value: string): Promise<OAuthResult>;
  cancelOAuth(flowId: string): Promise<void>;
};

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
   * `true` by default because a named route error IS an outcome. `call` marks
   * each client-invented transport summary as false at its construction site.
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

/** The single client instance. Stable across renders (safe in effect deps). */
export const modelsApi: ModelsApi = {
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
  refreshAgentPresence: () => call<{ agents: AgentSupply[] }>('/api/models/agents?refresh_cli_presence=1').then((r) => r.agents),
  getAgentSources: (backend) => call<{ agent: AgentSupply }>(`/api/models/agents/${backend}/sources`).then((r) => r.agent),
  // The body is TOTAL and closed: the route rejects unknown keys, so
  // `contract_version` is deliberately absent (unlike every other write here).
  putAgentSources: (backend, body) => call<{ agent: AgentSupply }>(`/api/models/agents/${backend}/sources`, jsonInit('PUT', body)).then((r) => r.agent),
  reorderAgentChains: (backend, order) => call<{ agent: AgentSupply }>(
    `/api/models/agents/${backend}/chains/reorder`,
    jsonInit('POST', order === undefined ? undefined : { order }),
  ).then((r) => r.agent),
  getAgentChain: (backend, model) => call<{ chain: AgentChain }>(`/api/models/agents/${backend}/chain?model=${encodeURIComponent(model)}`).then((r) => r.chain),
  getAgentChains: (backend) => call<{ chains: AgentChain[] }>(`/api/models/agents/${backend}/chains`).then((r) => r.chains),
  putAgentChain: (backend, model, body) => call<AgentChainMutation>(`/api/models/agents/${backend}/chain?model=${encodeURIComponent(model)}`, jsonInit('PUT', body)),
  probeAgent: (backend, model) => call<{ probe: ProbeResult }>(`/api/models/agents/${backend}/probe`, jsonInit('POST', model ? { model } : {})).then((r) => r.probe),
  setAgentMode: (backend, mode) => call<{ agent?: AgentSupply } & AgentSupply>(`/api/models/agents/${backend}/mode`, jsonInit('PATCH', { mode })).then((r) => (r.agent ?? r) as AgentSupply),
  putAgentModels: (backend, body) => call<{ agent?: AgentSupply } & AgentSupply>(`/api/models/agents/${backend}/models`, jsonInit('PUT', body)).then((r) => (r.agent ?? r) as AgentSupply),
  searchModelsDev: (query, signal) => call<{ matches: ModelsDevMatch[] }>(
    `/api/models/catalog/models-dev?query=${encodeURIComponent(query)}`,
    { signal },
  ).then((r) => r.matches ?? []),
  addCustomModel: (sourceId, draft) => call<{ source?: Source } & Source>(`/api/models/sources/${encodeURIComponent(sourceId)}/models`, jsonInit('POST', draft)).then((r) => (r.source ?? r) as Source),
  updateModelReasoningEfforts: (sourceId, modelId, reasoningEfforts) => call<{ source?: Source } & Source>(`/api/models/sources/${encodeURIComponent(sourceId)}/models/${encodeURIComponent(modelId)}`, jsonInit('PATCH', { reasoning_efforts: reasoningEfforts })).then((r) => (r.source ?? r) as Source),
  deleteCustomModel: (sourceId, modelId, confirmation) => call<{ source?: Source } & Source>(`/api/models/sources/${encodeURIComponent(sourceId)}/models/${encodeURIComponent(modelId)}`, jsonInit('DELETE', confirmation ?? {})).then((r) => (r.source ?? r) as Source),
  scanMigration: () => call<{ scan?: MigrationScan } & MigrationScan>('/api/models/migration/scan', jsonInit('POST')).then((r) => (r.scan ?? r) as MigrationScan),
  applyMigration: (itemIds) => call<MigrationApplyResult>('/api/models/migration/apply', jsonInit('POST', { item_ids: itemIds })),
  listEvents: (limit = 20, before) =>
    call<{ events: ResolutionEvent[] }>(
      `/api/models/events?limit=${limit}${before ? `&before=${encodeURIComponent(before)}` : ''}`,
    ).then((r) => r.events),
  getUsageSummary: (days = USAGE_DEFAULT_WINDOW_DAYS) =>
    call<{ usage: UsageSummary }>(`/api/models/usage?days=${days}`).then((r) => r.usage),
  getRuntimeStatus: () => call<{ runtime?: RuntimeDependency } & RuntimeDependency>('/api/models/runtime/status').then((r) => (r.runtime ?? r) as RuntimeDependency),
  installRuntime: () => call<{ runtime?: RuntimeDependency } & RuntimeDependency>('/api/models/runtime/install', jsonInit('POST')).then((r) => (r.runtime ?? r) as RuntimeDependency),
  startRuntime: () => call<{ runtime?: RuntimeDependency } & RuntimeDependency>('/api/models/runtime/start', jsonInit('POST')).then((r) => (r.runtime ?? r) as RuntimeDependency),
  stopRuntime: () => call<{ runtime?: RuntimeDependency } & RuntimeDependency>('/api/models/runtime/stop', jsonInit('POST')).then((r) => (r.runtime ?? r) as RuntimeDependency),
  startOAuth: (vendor, channel, clientNonce) =>
    call<{ flow?: OAuthFlow } & OAuthFlow>(
      '/api/models/oauth/start',
      jsonInit('POST', { vendor, channel, ...(clientNonce ? { client_nonce: clientNonce } : {}) }),
    ).then((r) => (r.flow ?? r) as OAuthFlow),
  getOAuthStatus: (flowId) => call<OAuthResultResponse>(`/api/models/oauth/status/${encodeURIComponent(flowId)}`).then(oauthResult),
  submitOAuth: (flowId, value) => call<OAuthResultResponse>('/api/models/oauth/submit', jsonInit('POST', { flow_id: flowId, value })).then(oauthResult),
  cancelOAuth: (flowId) => call('/api/models/oauth/cancel', jsonInit('POST', { flow_id: flowId })).then(() => undefined),
};
