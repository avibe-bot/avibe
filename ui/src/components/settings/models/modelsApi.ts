// Model Hub API client. Presents one typed live surface over the frozen
// `/api/models/*` REST endpoints. Hermetic tests load the recorded replay client
// through `modelsApi.mockEntry.ts`, which keeps its corpus outside live bundles.
//
// Methods unwrap the frozen envelope ({ok:true, …} | {ok:false, error}) and
// throw an Error carrying the machine code on failure, so callers work with
// plain domain objects.
import { apiFetch, isApiFetchDeadlineAbort, withApiDeadline } from '@/lib/apiFetch';
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
export const supplyGaps = (raw: unknown): SupplyGap[] =>
  Array.isArray(raw)
    ? raw
        .filter((g): g is Record<string, unknown> => Boolean(g) && typeof g === 'object')
        .map((g) => ({
          backend: g.backend as SupplyGap['backend'],
          model_id: String(g.model_id ?? ''),
          agents: Array.isArray(g.agents) ? g.agents.map(String) : [],
        }))
    : [];

export const routeHopRefs = (raw: unknown): RouteHopRef[] =>
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

export type ModelHubOperation = keyof ModelsApi;
type ModelHubOperationResult<Operation extends ModelHubOperation> =
  Awaited<ReturnType<ModelsApi[Operation]>>;
type ModelHubOperationRegistry = {
  [Operation in ModelHubOperation]: {
    responseTransform: (response: unknown) => ModelHubOperationResult<Operation>;
  };
};

const responseAs = <Response>(response: unknown): Response => response as Response;

/**
 * The one client-response boundary for live calls and recorded service bodies.
 * Existing bare-body fallbacks remain live compatibility behavior, not mock policy.
 */
export const modelHubOperationRegistry = {
  listSources: {
    responseTransform: (response) => responseAs<{ sources: Source[] }>(response).sources,
  },
  observeApiKeySource: {
    responseTransform: (response) =>
      responseAs<{ observation: SourceObservation }>(response).observation,
  },
  createApiKeySource: {
    responseTransform: (response) => created(responseAs<SourceCreatedResponse>(response)),
  },
  patchSource: {
    responseTransform: (response) => sourcePatched(responseAs<SourcePatchedResponse>(response)),
  },
  refreshSource: {
    responseTransform: (response) => responseAs<SourceRefresh>(response),
  },
  deleteSource: {
    responseTransform: (response) => sourceDeleted(responseAs<SourceDeletedResponse>(response)),
  },
  replaceCredential: {
    responseTransform: (response) =>
      credentialReplacement(responseAs<CredentialReplacementResponse>(response)),
  },
  reauthSource: {
    responseTransform: (response) => {
      const value = responseAs<{ flow?: OAuthFlow } & OAuthFlow>(response);
      return (value.flow ?? value) as OAuthFlow;
    },
  },
  listAgents: {
    responseTransform: (response) => responseAs<{ agents: AgentSupply[] }>(response).agents,
  },
  getAgentSources: {
    responseTransform: (response) => responseAs<{ agent: AgentSupply }>(response).agent,
  },
  putAgentSources: {
    responseTransform: (response) => responseAs<{ agent: AgentSupply }>(response).agent,
  },
  getAgentChain: {
    responseTransform: (response) => responseAs<{ chain: AgentChain }>(response).chain,
  },
  putAgentChain: {
    responseTransform: (response) => responseAs<AgentChainMutation>(response),
  },
  probeAgent: {
    responseTransform: (response) => responseAs<{ probe: ProbeResult }>(response).probe,
  },
  setAgentMode: {
    responseTransform: (response) => {
      const value = responseAs<{ agent?: AgentSupply } & AgentSupply>(response);
      return (value.agent ?? value) as AgentSupply;
    },
  },
  putMenu: {
    responseTransform: (response) => {
      const value = responseAs<{ agent?: AgentSupply } & AgentSupply>(response);
      return (value.agent ?? value) as AgentSupply;
    },
  },
  addCustomModel: {
    responseTransform: (response) => {
      const value = responseAs<{ source?: Source } & Source>(response);
      return (value.source ?? value) as Source;
    },
  },
  updateModelReasoningEfforts: {
    responseTransform: (response) => {
      const value = responseAs<{ source?: Source } & Source>(response);
      return (value.source ?? value) as Source;
    },
  },
  deleteCustomModel: {
    responseTransform: (response) => {
      const value = responseAs<{ source?: Source } & Source>(response);
      return (value.source ?? value) as Source;
    },
  },
  scanMigration: {
    responseTransform: (response) => {
      const value = responseAs<{ scan?: MigrationScan } & MigrationScan>(response);
      return (value.scan ?? value) as MigrationScan;
    },
  },
  applyMigration: {
    responseTransform: (response) => responseAs<MigrationApplyResult>(response),
  },
  listEvents: {
    responseTransform: (response) =>
      responseAs<{ events: ResolutionEvent[] }>(response).events,
  },
  getRuntimeStatus: {
    responseTransform: (response) => {
      const value = responseAs<{ runtime?: RuntimeDependency } & RuntimeDependency>(response);
      return (value.runtime ?? value) as RuntimeDependency;
    },
  },
  installRuntime: {
    responseTransform: (response) => {
      const value = responseAs<{ runtime?: RuntimeDependency } & RuntimeDependency>(response);
      return (value.runtime ?? value) as RuntimeDependency;
    },
  },
  startRuntime: {
    responseTransform: (response) => {
      const value = responseAs<{ runtime?: RuntimeDependency } & RuntimeDependency>(response);
      return (value.runtime ?? value) as RuntimeDependency;
    },
  },
  startOAuth: {
    responseTransform: (response) => {
      const value = responseAs<{ flow?: OAuthFlow } & OAuthFlow>(response);
      return (value.flow ?? value) as OAuthFlow;
    },
  },
  getOAuthStatus: {
    responseTransform: (response) => oauthResult(responseAs<OAuthResultResponse>(response)),
  },
  submitOAuth: {
    responseTransform: (response) => oauthResult(responseAs<OAuthResultResponse>(response)),
  },
  cancelOAuth: {
    responseTransform: (_response) => undefined,
  },
} satisfies ModelHubOperationRegistry;

const liveApi: ModelsApi = {
  listSources: () => call<{ sources: Source[] }>('/api/models/sources')
    .then(modelHubOperationRegistry.listSources.responseTransform),
  observeApiKeySource: (draft, signal) =>
    call<{ observation: SourceObservation }>('/api/models/sources/observe', {
      ...jsonInit('POST', draft),
      signal,
    }).then(modelHubOperationRegistry.observeApiKeySource.responseTransform),
  // Both keep `adopted_by`. The old unwrap-to-`source` dropped it on the floor,
  // and no later read can put it back: `/agents` shows today's orders, not which
  // of them this commit changed.
  createApiKeySource: (draft) => call<SourceCreatedResponse>('/api/models/sources', jsonInit('POST', draft)).then(modelHubOperationRegistry.createApiKeySource.responseTransform),
  patchSource: (id, patch) => call<SourcePatchedResponse>(`/api/models/sources/${encodeURIComponent(id)}`, jsonInit('PATCH', patch)).then(modelHubOperationRegistry.patchSource.responseTransform),
  refreshSource: (id, confirmation) => call<SourceRefresh>(`/api/models/sources/${encodeURIComponent(id)}/refresh`, jsonInit('POST', confirmation ?? {})).then(modelHubOperationRegistry.refreshSource.responseTransform),
  deleteSource: (id, confirmation) => call<SourceDeletedResponse>(
    `/api/models/sources/${encodeURIComponent(id)}${confirmation ? '?force=true' : ''}`,
    jsonInit('DELETE', confirmation ? {
      would_remove_hops: confirmation.would_remove_hops,
      would_interrupt: confirmation.would_interrupt,
    } : undefined),
  ).then(modelHubOperationRegistry.deleteSource.responseTransform),
  // Both repair routes reject unknown body keys outright (`discovery_failed` /
  // `reauth_confirmation_required`), so these bodies are exactly the contract's
  // and carry no `contract_version` — the same closed-body rule as putAgentSources.
  replaceCredential: (id, body) => call<CredentialReplacementResponse>(`/api/models/sources/${encodeURIComponent(id)}/credential`, jsonInit('PUT', body)).then(modelHubOperationRegistry.replaceCredential.responseTransform),
  // The OAuth acknowledgement is unconditional because beginning reauth may
  // irreversibly replace grant material. It does not stand in for destructive
  // supply consent: guarded inventory mutations separately echo the server plan.
  reauthSource: (id) => call<{ flow?: OAuthFlow } & OAuthFlow>(`/api/models/sources/${encodeURIComponent(id)}/reauth`, jsonInit('POST', { acknowledge_irreversible: true })).then(modelHubOperationRegistry.reauthSource.responseTransform),
  listAgents: () => call<{ agents: AgentSupply[] }>('/api/models/agents').then(modelHubOperationRegistry.listAgents.responseTransform),
  getAgentSources: (backend) => call<{ agent: AgentSupply }>(`/api/models/agents/${backend}/sources`).then(modelHubOperationRegistry.getAgentSources.responseTransform),
  // The body is TOTAL and closed: the route rejects unknown keys, so
  // `contract_version` is deliberately absent (unlike every other write here).
  putAgentSources: (backend, body) => call<{ agent: AgentSupply }>(`/api/models/agents/${backend}/sources`, jsonInit('PUT', body)).then(modelHubOperationRegistry.putAgentSources.responseTransform),
  getAgentChain: (backend, model) => call<{ chain: AgentChain }>(`/api/models/agents/${backend}/chain?model=${encodeURIComponent(model)}`).then(modelHubOperationRegistry.getAgentChain.responseTransform),
  putAgentChain: (backend, model, body) => call<AgentChainMutation>(`/api/models/agents/${backend}/chain?model=${encodeURIComponent(model)}`, jsonInit('PUT', body)).then(modelHubOperationRegistry.putAgentChain.responseTransform),
  probeAgent: (backend, model) => call<{ probe: ProbeResult }>(`/api/models/agents/${backend}/probe`, jsonInit('POST', model ? { model } : {})).then(modelHubOperationRegistry.probeAgent.responseTransform),
  setAgentMode: (backend, mode) => call<{ agent?: AgentSupply } & AgentSupply>(`/api/models/agents/${backend}/mode`, jsonInit('PATCH', { mode })).then(modelHubOperationRegistry.setAgentMode.responseTransform),
  putMenu: (menu) => call<{ agent?: AgentSupply } & AgentSupply>('/api/models/agents/opencode/menu', jsonInit('PUT', { menu })).then(modelHubOperationRegistry.putMenu.responseTransform),
  addCustomModel: (sourceId, draft) => call<{ source?: Source } & Source>(`/api/models/sources/${encodeURIComponent(sourceId)}/models`, jsonInit('POST', draft)).then(modelHubOperationRegistry.addCustomModel.responseTransform),
  updateModelReasoningEfforts: (sourceId, modelId, reasoningEfforts) => call<{ source?: Source } & Source>(`/api/models/sources/${encodeURIComponent(sourceId)}/models/${encodeURIComponent(modelId)}`, jsonInit('PATCH', { reasoning_efforts: reasoningEfforts })).then(modelHubOperationRegistry.updateModelReasoningEfforts.responseTransform),
  deleteCustomModel: (sourceId, modelId, confirmation) => call<{ source?: Source } & Source>(`/api/models/sources/${encodeURIComponent(sourceId)}/models/${encodeURIComponent(modelId)}`, jsonInit('DELETE', confirmation ?? {})).then(modelHubOperationRegistry.deleteCustomModel.responseTransform),
  scanMigration: () => call<{ scan?: MigrationScan } & MigrationScan>('/api/models/migration/scan', jsonInit('POST')).then(modelHubOperationRegistry.scanMigration.responseTransform),
  applyMigration: (itemIds) => call<MigrationApplyResult>('/api/models/migration/apply', jsonInit('POST', { item_ids: itemIds })).then(modelHubOperationRegistry.applyMigration.responseTransform),
  listEvents: (limit = 20, before) =>
    call<{ events: ResolutionEvent[] }>(
      `/api/models/events?limit=${limit}${before ? `&before=${encodeURIComponent(before)}` : ''}`,
    ).then(modelHubOperationRegistry.listEvents.responseTransform),
  getRuntimeStatus: () => call<{ runtime?: RuntimeDependency } & RuntimeDependency>('/api/models/runtime/status').then(modelHubOperationRegistry.getRuntimeStatus.responseTransform),
  installRuntime: () => call<{ runtime?: RuntimeDependency } & RuntimeDependency>('/api/models/runtime/install', jsonInit('POST')).then(modelHubOperationRegistry.installRuntime.responseTransform),
  startRuntime: () => call<{ runtime?: RuntimeDependency } & RuntimeDependency>('/api/models/runtime/start', jsonInit('POST')).then(modelHubOperationRegistry.startRuntime.responseTransform),
  startOAuth: (vendor, channel, clientNonce) =>
    call<{ flow?: OAuthFlow } & OAuthFlow>(
      '/api/models/oauth/start',
      jsonInit('POST', { vendor, channel, ...(clientNonce ? { client_nonce: clientNonce } : {}) }),
    ).then(modelHubOperationRegistry.startOAuth.responseTransform),
  getOAuthStatus: (flowId) => call<OAuthResultResponse>(`/api/models/oauth/status/${encodeURIComponent(flowId)}`).then(modelHubOperationRegistry.getOAuthStatus.responseTransform),
  submitOAuth: (flowId, value) => call<OAuthResultResponse>('/api/models/oauth/submit', jsonInit('POST', { flow_id: flowId, value })).then(modelHubOperationRegistry.submitOAuth.responseTransform),
  cancelOAuth: (flowId) => call('/api/models/oauth/cancel', jsonInit('POST', { flow_id: flowId })).then(modelHubOperationRegistry.cancelOAuth.responseTransform),
};

/** The single client instance. Stable across renders (safe in effect deps). */
export const modelsApi: ModelsApi = liveApi;
