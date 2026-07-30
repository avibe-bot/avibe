// Model Hub API client. Presents ONE typed surface to the UI; internally it
// either serves in-memory fixtures (mock mode, default while L2 is unmerged) or
// calls the frozen `/api/models/*` REST endpoints (live mode). Components never
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
import { buildIdentifier } from './menus/identifiers';
import { canReauth, canReplaceKey, wasBlocked } from './repair';
import { isUnhealthy } from './supply';
import type {
  AdoptedBy,
  AgentBackend,
  AgentChain,
  AgentChainLink,
  AgentMapping,
  AgentMenu,
  AgentMode,
  AgentSourcesPut,
  AgentSupply,
  ApiKeySourceCreate,
  CredentialReplace,
  CustomModelCreate,
  MigrationApplyResult,
  MigrationScan,
  OAuthFlow,
  ProbeResult,
  ResolutionEvent,
  RuntimeDependency,
  SkippedBy,
  Source,
  SourcePatch,
  SourceRepaired,
  SupplyChannel,
  SupplyGap,
} from './types';
import { AGENT_CHAIN_CONTRACT_VERSION, PROBE_RESULT_CONTRACT_VERSION } from './types';

/**
 * The terminal result of BOTH creation paths (api.md 「The terminal result of both
 * ordinary API-key creation and OAuth creation is」).
 *
 * `adopted_by` travels with the source rather than being re-read from
 * `/agents` afterwards, and that is the whole point: it is a snapshot frozen at
 * commit time, listing only the eligible `follow` backends that took the source
 * in and at which one-based position. A `custom` backend is absent — not because
 * nothing happened to it, but because nothing did, which is exactly the case the
 * user has to be told about while the dialog is still open.
 *
 * `skipped_by` is the server naming that case: the backends that COULD have used
 * this source and were left out because they keep a `custom` order. It is carried
 * beside `adopted_by` rather than derived from it, because 「absent」 covers both
 * that and 「never eligible」, and only the server can tell the two apart. Kept
 * nullable through this reader: an absent array is a server that did not answer
 * the question, which is not the same as one answering 「nobody」.
 */
export type Adoption = { adopted_by: AdoptedBy[]; skipped_by: SkippedBy[] | null };
export type SourceCreated = { source: Source } & Adoption;

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
  createApiKeySource(draft: ApiKeySourceCreate): Promise<SourceCreated>;
  /** Rename / re-point a source (display_name, base_url). */
  patchSource(id: string, patch: SourcePatch): Promise<Source>;
  /** Re-run discovery on a hub source; resolves with the discovered count.
   *  Contractually ALSO the recovery test: run on a needs_action / error source
   *  it clears the blocker and returns the source to standby. v3 adds no second
   *  「recover」 endpoint, so this is the whole retry affordance. */
  testSource(id: string): Promise<number>;
  /** Delete a source. `force` overrides the only-supplier guard. */
  deleteSource(id: string, force?: boolean): Promise<void>;
  /** Replace the credential of a hub-channel api_key source. Refuses with
   *  `source_last_supplier` + `would_interrupt` when the replacement set would
   *  strand a selected model; `force` commits anyway. */
  replaceCredential(id: string, body: CredentialReplace): Promise<SourceRepaired>;
  /** Start re-authorization for a subscription source. Irreversible once it
   *  begins, which is why the acknowledgement is sent unconditionally — the
   *  server rejects a native source without it (`reauth_confirmation_required`).
   *  Resolves with the flow to drive; the repair tail arrives on its terminal
   *  status/submit response as `OAuthResult.repaired`. */
  reauthSource(id: string): Promise<OAuthFlow>;
  listAgents(): Promise<AgentSupply[]>;
  /** Per-backend enabled subset + order + policy (the 来源顺序 drawer's read). */
  getAgentSources(backend: AgentBackend): Promise<AgentSupply>;
  /** Total write: `follow` hands the order back to the server, `custom` freezes
   *  exactly the ids sent. The response re-echoes the canonical order. */
  putAgentSources(backend: AgentBackend, body: AgentSourcesPut): Promise<AgentSupply>;
  /** Resolution chain for one model. Hub mode only — direct answers `direct_mode`. */
  getAgentChain(backend: AgentBackend, model: string): Promise<AgentChain>;
  /** One real request through the chain. Hub mode only, same reason. */
  probeAgent(backend: AgentBackend, model?: string): Promise<ProbeResult>;
  setAgentMode(backend: AgentBackend, mode: AgentMode): Promise<AgentSupply>;
  putMappings(backend: AgentBackend, mappings: AgentMapping[]): Promise<AgentSupply>;
  putMenu(menu: AgentMenu): Promise<AgentSupply>;
  addCustomModel(draft: CustomModelCreate): Promise<Source>;
  deleteCustomModel(sourceId: string, modelId: string): Promise<Source>;
  scanMigration(): Promise<MigrationScan>;
  applyMigration(itemIds: string[]): Promise<MigrationApplyResult>;
  /** `before` is an event id cursor (「查看全部」 pagination). */
  listEvents(limit?: number, before?: string): Promise<ResolutionEvent[]>;
  getRuntimeStatus(): Promise<RuntimeDependency>;
  /** `experimentalConsent` MUST be true for a consent-gated hub-held
   *  subscription connect, or the server returns consent_required. */
  startOAuth(vendor: string, channel: SupplyChannel, experimentalConsent?: boolean): Promise<OAuthFlow>;
  getOAuthStatus(flowId: string): Promise<OAuthResult>;
  submitOAuth(flowId: string, value: string): Promise<OAuthResult>;
  cancelOAuth(flowId: string): Promise<void>;
};

const isLive = () => MODELS_API_MODE === 'live';

// ── Live client ─────────────────────────────────────────────────────────
class ApiCallError extends Error {
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
  constructor(
    code: string,
    detail?: string,
    serverNamed = true,
    wouldInterrupt: SupplyGap[] = [],
    interrupted: SupplyGap[] = [],
  ) {
    super(detail || code);
    this.name = 'ApiCallError';
    this.code = code;
    this.detail = detail;
    this.serverNamed = serverNamed;
    this.wouldInterrupt = wouldInterrupt;
    this.interrupted = interrupted;
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
} | null =>
  err instanceof ApiCallError
    ? {
        code: err.code,
        detail: err.detail,
        serverNamed: err.serverNamed,
        wouldInterrupt: err.wouldInterrupt,
        interrupted: err.interrupted,
      }
    : null;

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await apiFetch(path, init);
  let payload: any = null;
  try {
    payload = await res.json();
  } catch {
    // Invented here, not read off the wire: the request may well have been
    // carried out and its answer lost coming back.
    throw new ApiCallError('bad_response', `Non-JSON response from ${path}`, false);
  }
  if (!res.ok || payload?.ok === false) {
    throw new ApiCallError(
      payload?.error || `http_${res.status}`,
      payload?.detail,
      // `payload.error` is the only thing that carries a route's own verdict, so
      // its presence IS the answer. `http_502` is this client summarizing a
      // response that never gave one.
      Boolean(payload?.error),
      supplyGaps(payload?.would_interrupt),
      supplyGaps(payload?.interrupted_pairs),
    );
  }
  return payload as T;
}

const jsonInit = (method: string, body?: unknown): RequestInit => ({
  method,
  headers: { 'Content-Type': 'application/json' },
  body: body === undefined ? undefined : JSON.stringify(body),
});

/**
 * The adoption tail, defaulted. Both creation routes answer with it — the plain
 * one and the oauth envelope — and the two defaults below are DIFFERENT rules, so
 * they get one reader rather than two copies that would eventually agree.
 *
 * `adopted_by` is a list of things that happened: absent can only mean none did.
 * (Absent-is-not-empty holds in the contract for reauth, which never reaches here.)
 *
 * `skipped_by` is the answer to 「who was left out」, and `[]` there is a positive
 * claim that nobody was. A server that never sent the field has not made that
 * claim, so silence stays null — defaulting it to `[]` would upgrade 「did not
 * say」 into 「fully covered」, which is the one thing the adoption note exists to
 * avoid.
 */
type AdoptionTail = { adopted_by?: AdoptedBy[]; skipped_by?: SkippedBy[] };
const adoption = (r: AdoptionTail): Adoption => ({
  adopted_by: r.adopted_by ?? [],
  skipped_by: r.skipped_by ?? null,
});

/** api.md pins the shape to `{source, adopted_by, skipped_by}` with no extra
 *  nesting; the bare-`Source` arm is the same tolerance every other write here
 *  keeps. */
type SourceCreatedResponse = { source?: Source } & AdoptionTail & Source;
const created = (r: SourceCreatedResponse): SourceCreated => ({
  source: (r.source ?? r) as Source,
  ...adoption(r),
});

/** api.md "recovery symmetry": both repair routes answer with this same tail,
 *  so both unwrap through one reader. */
type SourceRepairedResponse = { source?: Source; recovered?: boolean; interrupted_pairs?: SupplyGap[] } & Source;
const repaired = (r: SourceRepairedResponse): SourceRepaired => ({
  source: (r.source ?? r) as Source,
  recovered: r.recovered === true,
  interrupted_pairs: supplyGaps(r.interrupted_pairs),
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
  // Both keep `adopted_by`. The old unwrap-to-`source` dropped it on the floor,
  // and no later read can put it back: `/agents` shows today's orders, not which
  // of them this commit changed.
  createApiKeySource: (draft) => call<SourceCreatedResponse>('/api/models/sources', jsonInit('POST', draft)).then(created),
  patchSource: (id, patch) => call<{ source?: Source } & Source>(`/api/models/sources/${encodeURIComponent(id)}`, jsonInit('PATCH', patch)).then((r) => (r.source ?? r) as Source),
  testSource: (id) => call<{ discovered: number }>(`/api/models/sources/${encodeURIComponent(id)}/test`, jsonInit('POST')).then((r) => r.discovered),
  deleteSource: (id, force) => call(`/api/models/sources/${encodeURIComponent(id)}${force ? '?force=true' : ''}`, jsonInit('DELETE')).then(() => undefined),
  // Both repair routes reject unknown body keys outright (`discovery_failed` /
  // `reauth_confirmation_required`), so these bodies are exactly the contract's
  // and carry no `contract_version` — the same closed-body rule as putAgentSources.
  replaceCredential: (id, body) => call<SourceRepairedResponse>(`/api/models/sources/${encodeURIComponent(id)}/credential`, jsonInit('PUT', body)).then(repaired),
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
  probeAgent: (backend, model) => call<{ probe: ProbeResult }>(`/api/models/agents/${backend}/probe`, jsonInit('POST', model ? { model } : {})).then((r) => r.probe),
  setAgentMode: (backend, mode) => call<{ agent?: AgentSupply } & AgentSupply>(`/api/models/agents/${backend}/mode`, jsonInit('PATCH', { mode })).then((r) => (r.agent ?? r) as AgentSupply),
  putMappings: (backend, mappings) => call<{ agent?: AgentSupply } & AgentSupply>(`/api/models/agents/${backend}/mappings`, jsonInit('PUT', { mappings })).then((r) => (r.agent ?? r) as AgentSupply),
  putMenu: (menu) => call<{ agent?: AgentSupply } & AgentSupply>('/api/models/agents/opencode/menu', jsonInit('PUT', { menu })).then((r) => (r.agent ?? r) as AgentSupply),
  addCustomModel: (draft) => call<{ source?: Source } & Source>('/api/models/custom-models', jsonInit('POST', draft)).then((r) => (r.source ?? r) as Source),
  deleteCustomModel: (sourceId, modelId) => call<{ source?: Source } & Source>('/api/models/custom-models', jsonInit('DELETE', { source_id: sourceId, model_id: modelId })).then((r) => (r.source ?? r) as Source),
  scanMigration: () => call<{ scan?: MigrationScan } & MigrationScan>('/api/models/migration/scan', jsonInit('POST')).then((r) => (r.scan ?? r) as MigrationScan),
  applyMigration: (itemIds) => call<MigrationApplyResult>('/api/models/migration/apply', jsonInit('POST', { item_ids: itemIds })),
  listEvents: (limit = 20, before) =>
    call<{ events: ResolutionEvent[] }>(
      `/api/models/events?limit=${limit}${before ? `&before=${encodeURIComponent(before)}` : ''}`,
    ).then((r) => r.events),
  getRuntimeStatus: () => call<{ runtime?: RuntimeDependency } & RuntimeDependency>('/api/models/runtime/status').then((r) => (r.runtime ?? r) as RuntimeDependency),
  startOAuth: (vendor, channel, experimentalConsent) =>
    call<{ flow?: OAuthFlow } & OAuthFlow>(
      '/api/models/oauth/start',
      jsonInit('POST', { vendor, channel, ...(experimentalConsent ? { experimental_consent: true } : {}) }),
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
type MockFlow = { flow: OAuthFlow; polls: number; submitted: boolean; recovered?: boolean };

const rid = (prefix: string) => `${prefix}_${Math.random().toString(36).slice(2, 10)}`;
const delay = <T>(value: T, ms = 260): Promise<T> => new Promise((r) => setTimeout(() => r(value), ms));

class MockStore {
  sources = buildMockSources();
  agents = buildMockAgents(this.sources);
  events = buildMockEvents();
  runtime = buildMockRuntime();
  flows = new Map<string, MockFlow>();

  // ── Fake server-side recomputation ───────────────────────────────────
  // Every read of an agent re-derives what the real server derives: the
  // per-backend order (recommended under `follow`, pruned under `custom`),
  // eligibility, and the supply rollup. That is what makes a drag-reorder or a
  // source deletion move 使用中 in the demo instead of leaving it stale.
  private syncAgents() {
    for (const a of this.agents) {
      if (a.mode === 'direct') {
        a.sources = null;
      } else {
        const policy = a.sources?.policy ?? 'follow';
        const eligibility = mockEligibility(this.sources, a.backend);
        const eligible = new Set(eligibility.filter((e) => e.eligible).map((e) => e.source_id));
        const order =
          policy === 'follow'
            ? mockRecommendedOrder(this.sources, a.backend)
            : // A `custom` subset is frozen, never extended — but a deleted or
              // newly ineligible id drops out (the invariant the server enforces).
              (a.sources?.order ?? []).filter((id) => eligible.has(id));
        a.sources = { policy, order, eligibility };
      }
      this.deriveSupply(a);
    }
  }

  /** §4.3 + §4.5 in miniature: capability (supplies the mapped id) split from
   *  runnability (not blocked), then the rollup over the resulting chain. */
  private deriveSupply(a: AgentSupply) {
    if (a.mode === 'direct') {
      a.current = null;
      a.selected_model_id = null;
      a.selected_model_explicit = false;
      a.selected_by_agent = null;
      a.supply_status = null;
      a.model_supply = null;
      a.named_agents = (a.named_agents ?? []).map((n) => ({ ...n, effective_model_id: null, supply_status: null }));
      return;
    }
    const byId = new Map(this.sources.map((s) => [s.id, s]));
    const order = (a.sources?.order ?? []).map((id) => byId.get(id)).filter((s): s is Source => Boolean(s));
    const target = (model: string) => {
      const m = a.mappings?.find((x) => x.builtin_id === model && x.enabled && x.target_model_id);
      return m ? m.target_model_id : model;
    };
    const chainFor = (model: string) => order.filter((s) => s.models.some((mm) => mm.id === target(model)));
    if (a.builtin_models) {
      a.model_supply = a.builtin_models.map((m) => ({ model_id: m, chain_length: chainFor(m).length }));
    }
    const selected = a.selected_model_id ?? null;
    if (!selected) {
      a.current = null;
      a.supply_status = null;
    } else {
      const chain = chainFor(selected);
      const head = chain.find(isRunnable) ?? null;
      const blocked = chain.filter((s) => !isRunnable(s));
      if (!head) {
        a.current = null;
        a.supply_status =
          chain.length > 0 && blocked.every((s) => s.state.status === 'cooldown') ? 'waiting' : 'interrupted';
      } else {
        a.current = { model_id: selected, source_id: head.id, channel: head.supply_channel };
        a.supply_status = head.id === chain[0]?.id && blocked.length === 0 ? 'ok' : 'degraded';
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

  /**
   * The `adopted_by` projection, derived the same way the server derives it:
   * re-run the recommendation, then report where the new id actually landed.
   *
   * It is deliberately NOT a guess from the source's vendor — a `follow` backend
   * adopts only what its eligibility admits, so the answer for one credential can
   * be 「claude 第 2 位」 and nothing at all for opencode. Anything on `custom` is
   * omitted by the contract; that omission is the signal the dialogs read.
   */
  private adoptionOf(sourceId: string): AdoptedBy[] {
    this.syncAgents();
    return this.agents
      .filter((a) => a.mode === 'hub' && a.sources?.policy === 'follow')
      .map((a) => ({ backend: a.backend, order: a.sources?.order ?? [] }))
      .filter(({ order }) => order.includes(sourceId))
      .map(({ backend, order }) => ({
        backend,
        policy: 'follow' as const,
        position: order.indexOf(sourceId) + 1, // one-based, per api.md
      }));
  }

  /**
   * The complement, derived the way `_skipped_by` derives it: ELIGIBLE for this
   * backend, on a `custom` order, and not in it. The eligibility filter is what
   * makes the two lists different from `MODEL_HUB_BACKENDS` minus `adopted_by` —
   * a backend that could never use the credential belongs to neither.
   */
  private skippedOf(sourceId: string): SkippedBy[] {
    this.syncAgents();
    return this.agents
      .filter(
        (a) =>
          a.mode === 'hub' &&
          a.sources?.policy === 'custom' &&
          !a.sources.order.includes(sourceId) &&
          (a.sources.eligibility ?? []).some((e) => e.source_id === sourceId && e.eligible),
      )
      .map((a) => ({ backend: a.backend, reason: 'custom_order' as const }));
  }

  /** Both creation routes answer with the same pair. */
  private adoptionTail(sourceId: string) {
    return { adopted_by: this.adoptionOf(sourceId), skipped_by: this.skippedOf(sourceId) };
  }

  createApiKeySource(draft: ApiKeySourceCreate) {
    const count = mockDiscoveredCount(draft.vendor);
    const source: Source = {
      id: rid('src'),
      created_at: new Date().toISOString(),
      kind: 'api_key',
      vendor: draft.vendor,
      display_name: draft.vendor === 'custom' ? hostLabel(draft.base_url) : vendorLabel(draft.vendor),
      protocol: draft.vendor === 'anthropic' ? 'anthropic' : 'openai_compatible',
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
        provenance: 'discovered' as const,
        discovered_at: new Date().toISOString(),
      })),
      credential_ref: rid('cred'),
    };
    this.sources.push(source);
    // simulate probe latency
    return delay({ source: structuredClone(source), ...this.adoptionTail(source.id) }, 900);
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
      const mapping = a.mappings?.find((x) => x.builtin_id === model && x.enabled && x.target_model_id);
      const resolved = mapping ? mapping.target_model_id : model;
      // A `follow` order only ever LOSES the removed id (the recommendation never
      // gains a source from a write), so filtering the live order through the
      // candidate set is the same answer refresh_follow_orders would give.
      const survives = (a.sources?.order ?? [])
        .map((id) => byId.get(id))
        .some((s) => s !== undefined && s.models.some((mm) => mm.id === resolved) && isRunnable(s));
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
    // Atomic commit, standby-clearing semantics shared with testSource: a
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
    return delay(
      { source: structuredClone(source), recovered, interrupted_pairs: interrupted },
      700,
    );
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
    if (body.policy === 'follow') {
      // 恢复推荐顺序 discards the frozen subset; the order comes back from §4.2.
      agent.sources = { policy: 'follow', order: [], eligibility: null };
    } else {
      // §4.4's invariants, server-side: every id exists, is eligible here, and
      // appears once. Omitting one is how the user says 未启用 — not an error.
      const eligible = new Set(
        mockEligibility(this.sources, backend).filter((e) => e.eligible).map((e) => e.source_id),
      );
      const seen = new Set<string>();
      for (const id of body.order) {
        if (!eligible.has(id) || seen.has(id)) throw new ApiCallError('invalid_source_order', id);
        seen.add(id);
      }
      agent.sources = { policy: 'custom', order: [...body.order], eligibility: null };
    }
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
    const mapping = agent.mappings?.find((x) => x.builtin_id === model && x.enabled && x.target_model_id);
    const resolved = mapping ? mapping.target_model_id : model;
    const chain: AgentChainLink[] = (agent.sources?.order ?? [])
      .map((id) => byId.get(id))
      .filter((s): s is Source => s !== undefined && s.models.some((mm) => mm.id === resolved))
      .map((s) => ({
        source_id: s.id,
        channel: s.supply_channel,
        via_mapping: Boolean(mapping),
        resolved_model_id: mapping ? resolved : null,
        health: chainHealth(s),
        runnable: isRunnable(s),
        // v4: process availability is a fact about the serving process — which
        // native CLI it can launch under its own login — and a browser mock has
        // no way to observe it. So it stands in for a runtime where every
        // configured CLI is launchable, rather than inventing an outage. The
        // unavailable branch is asserted in the unit tests, which can state the
        // fact instead of guessing it.
        reason: null,
        retry_at: s.state.status === 'cooldown' ? s.state.retry_at ?? null : null,
      }));
    const supply_state: AgentChain['supply_state'] = chain.some((l) => l.runnable)
      ? 'ok'
      : chain.length > 0 && chain.every((l) => l.health === 'cooldown' && l.reason === null)
        ? 'waiting'
        : 'interrupted';
    return { chain, supply_state };
  }

  getAgentChain(backend: AgentBackend, model: string) {
    this.syncAgents();
    const agent = this.agentOr404(backend);
    // AC-7: direct mode has no src_* identity to report, so the route refuses
    // rather than answering with an empty (falsely alarming) chain.
    if (agent.mode === 'direct') throw new ApiCallError('direct_mode');
    const { chain, supply_state } = this.chainFor(agent, model);
    return delay({
      contract_version: AGENT_CHAIN_CONTRACT_VERSION,
      backend,
      model_id: model,
      chain,
      supply_state,
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
      // The resolved id, mapping applied — what the server reports, and not the
      // requested id it was rewritten from.
      model_id: head.resolved_model_id ?? modelId,
      latency_ms: native ? null : 180 + Math.floor(Math.random() * 420),
      via_mapping: head.via_mapping,
      error: null,
    };
    // A real upstream request takes a real moment; a local readiness check does not.
    return delay(probe, native ? 400 : 1200);
  }

  setAgentMode(backend: AgentBackend, mode: AgentMode) {
    const agent = this.agentOr404(backend);
    agent.mode = mode;
    if (mode === 'hub') {
      // Rejoining the hub starts on the recommendation, and picks up whatever
      // model the backend defaults to (first built-in / first supplied id).
      agent.sources = { policy: 'follow', order: [], eligibility: null };
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

  /**
   * `_enroll_target_sources` in miniature (api.md → "Mapping and menu
   * enrollment"): a target whose suppliers are all outside the order pulls in
   * EXACTLY ONE — the first in the recommendation — and the order forks to
   * `custom` only when something was actually appended.
   *
   * Modelled here rather than left as a no-op for the reason `oauthResult`
   * documents: a mock that treats a mutation and its side effect as separate is
   * a mock that cannot fail the way the product does, and the drawers' 完成
   * notice exists only because of this side effect.
   */
  private enrollTargets(agent: AgentSupply, targetGroups: string[][]) {
    const order = agent.sources?.order ?? [];
    const enrolled = new Set(order);
    const appended: string[] = [];
    for (const group of targetGroups) {
      if (group.length === 0 || group.some((id) => enrolled.has(id))) continue;
      enrolled.add(group[0]);
      appended.push(group[0]);
    }
    if (appended.length === 0) return;
    agent.sources = { policy: 'custom', order: [...order, ...appended], eligibility: null };
  }

  /** The suppliers of one target, in the recommendation's order — the list the
   *  server picks its single enrollee from. */
  private suppliersOf(backend: AgentBackend, carries: (source: Source) => boolean): string[] {
    const byId = new Map(this.sources.map((s) => [s.id, s]));
    return mockRecommendedOrder(this.sources, backend).filter((id) => {
      const source = byId.get(id);
      return source ? carries(source) : false;
    });
  }

  putMappings(backend: AgentBackend, mappings: AgentMapping[]) {
    const agent = this.agents.find((a) => a.backend === backend);
    if (!agent) throw new ApiCallError('source_not_found');
    this.enrollTargets(
      agent,
      mappings
        .filter((m) => m.enabled)
        .map((m) => this.suppliersOf(backend, (s) => s.models.some((mm) => mm.id === m.target_model_id))),
    );
    agent.mappings = mappings;
    this.syncAgents();
    return delay(structuredClone(agent));
  }

  putMenu(menu: AgentMenu) {
    const agent = this.agents.find((a) => a.backend === 'opencode');
    if (!agent) throw new ApiCallError('source_not_found');
    const standardVendors = new Set(agent.standard_vendors ?? []);
    this.enrollTargets(
      agent,
      menu.checked.map((identifier) =>
        this.suppliersOf('opencode', (s) =>
          s.models.some((mm) => buildIdentifier(s.vendor, mm.id, standardVendors) === identifier),
        ),
      ),
    );
    agent.menu = menu;
    this.syncAgents();
    return delay(structuredClone(agent));
  }

  addCustomModel(draft: CustomModelCreate) {
    const source = this.sources.find((s) => s.id === draft.source_id);
    if (!source) throw new ApiCallError('source_not_found');
    const existing = source.models.find((m) => m.id === draft.model_id);
    if (existing) {
      existing.display_name = draft.display_name ?? existing.display_name;
      existing.provenance = 'manual';
    } else {
      source.models.push({
        id: draft.model_id,
        display_name: draft.display_name ?? null,
        provenance: 'manual',
        discovered_at: null,
      });
    }
    return delay(structuredClone(source), 400);
  }

  deleteCustomModel(sourceId: string, modelId: string) {
    const source = this.sources.find((s) => s.id === sourceId);
    if (!source) throw new ApiCallError('source_not_found');
    source.models = source.models.filter((m) => !(m.id === modelId && m.provenance === 'manual'));
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
        kind: isKey ? 'api_key' : 'subscription',
        vendor: item.backend === 'opencode' ? 'zhipuai' : item.backend === 'codex' ? 'openai' : 'anthropic',
        display_name: item.masked_detail.split(' · ')[0] || 'Imported',
        protocol: item.backend === 'codex' ? 'openai_responses' : 'anthropic',
        base_url: null,
        supply_channel: channel,
        // No hub-held subscription is created by migration, so never consented.
        experimental_consent_at: null,
        billing: isKey ? 'metered' : 'monthly',
        state: { status: 'standby', retry_at: null, detail_key: null },
        usage: isKey ? { cycle_used_pct: null, month_spend_cents: 0, currency: 'USD' } : { cycle_used_pct: 0, month_spend_cents: null, currency: null },
        account_label: channel === 'native_cli' ? 'me@gmail.com' : null,
        masked_credential: isKey ? 'sk-…dd3c' : null,
        models: [{ id: item.backend === 'opencode' ? 'glm-5.2' : item.backend === 'codex' ? 'gpt-5.6' : 'claude-opus-4-6', display_name: null, provenance: 'discovered', discovered_at: new Date().toISOString() }],
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

  startOAuth(vendor: string, channel: SupplyChannel, experimentalConsent?: boolean) {
    // Mirror the server: a hub-held subscription connect requires recorded consent.
    if (channel === 'hub' && !experimentalConsent) throw new ApiCallError('consent_required');
    const isDevice = vendor === 'openai';
    const flow: OAuthFlow = {
      flow_id: rid('oaf'),
      // Deterministic pending-source binding (schema: hub flows always set it),
      // consumed when the flow completes — mirrors the server, where the
      // materialized source takes source.id = flow.source_id.
      source_id: rid('src'),
      vendor,
      channel,
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
      if (source.models.length === 0) {
        source.models = [
          {
            id: isOpenai ? 'gpt-5.6' : 'claude-opus-4-6',
            display_name: isOpenai ? 'GPT-5.6' : 'Opus 4.6',
            provenance: 'discovered',
            discovered_at: new Date().toISOString(),
          },
        ];
      }
      return;
    }
    // Idempotent, like `_create_oauth_source(idempotent=True)`: re-polling a
    // completed flow re-echoes the same source instead of creating a second one.
    if (this.sources.some((s) => s.id === id)) return;
    this.sources.push({
      id,
      created_at: new Date().toISOString(),
      kind: 'subscription',
      vendor: flow.vendor,
      display_name: isOpenai ? 'ChatGPT 订阅' : 'Claude 订阅',
      protocol: isOpenai ? 'openai_responses' : 'anthropic',
      base_url: null,
      supply_channel: flow.channel,
      experimental_consent_at: flow.channel === 'hub' ? new Date().toISOString() : null,
      billing: 'monthly',
      state: { status: 'standby', retry_at: null, detail_key: null },
      usage: { cycle_used_pct: 0, month_spend_cents: null, currency: null },
      // native_cli subscriptions surface the sanctioned CLI account; hub-held
      // experimental sources may stay null until a later adapter rev (schema).
      account_label: flow.channel === 'native_cli' ? 'me@gmail.com' : null,
      masked_credential: null,
      models: isOpenai
        ? [{ id: 'gpt-5.6', display_name: 'GPT-5.6', provenance: 'discovered', discovered_at: new Date().toISOString() }]
        : [{ id: 'claude-opus-4-6', display_name: 'Opus 4.6', provenance: 'discovered', discovered_at: new Date().toISOString() }],
      credential_ref: flow.channel === 'hub' ? rid('cred') : null,
    });
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
      created: { source: structuredClone(source), ...this.adoptionTail(source.id) },
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

  testSource(id: string) {
    const source = this.sources.find((s) => s.id === id);
    if (!source) throw new ApiCallError('source_not_found');
    // Native-CLI subscriptions can't be re-discovered (server rejects them);
    // the UI only offers this action for hub sources, but fail closed anyway.
    if (source.supply_channel === 'native_cli') throw new ApiCallError('discovery_failed');
    source.state = { status: 'standby', retry_at: null, detail_key: null };
    return delay(source.models.length, 700);
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
  createApiKeySource: (draft) => mockStore.createApiKeySource(draft),
  patchSource: (id, patch) => mockStore.patchSource(id, patch),
  testSource: (id) => mockStore.testSource(id),
  deleteSource: (id, force) => mockStore.deleteSource(id, force),
  replaceCredential: (id, body) => mockStore.replaceCredential(id, body),
  reauthSource: (id) => mockStore.reauthSource(id),
  listAgents: () => mockStore.listAgents(),
  getAgentSources: (backend) => mockStore.getAgentSources(backend),
  putAgentSources: (backend, body) => mockStore.putAgentSources(backend, body),
  getAgentChain: (backend, model) => mockStore.getAgentChain(backend, model),
  probeAgent: (backend, model) => mockStore.probeAgent(backend, model),
  setAgentMode: (backend, mode) => mockStore.setAgentMode(backend, mode),
  putMappings: (backend, mappings) => mockStore.putMappings(backend, mappings),
  putMenu: (menu) => mockStore.putMenu(menu),
  addCustomModel: (draft) => mockStore.addCustomModel(draft),
  deleteCustomModel: (sourceId, modelId) => mockStore.deleteCustomModel(sourceId, modelId),
  scanMigration: () => mockStore.scanMigration(),
  applyMigration: (itemIds) => mockStore.applyMigration(itemIds),
  listEvents: (limit, before) => mockStore.listEvents(limit, before),
  getRuntimeStatus: () => mockStore.getRuntimeStatus(),
  startOAuth: (vendor, channel, experimentalConsent) => mockStore.startOAuth(vendor, channel, experimentalConsent),
  getOAuthStatus: (flowId) => mockStore.getOAuthStatus(flowId),
  submitOAuth: (flowId, value) => mockStore.submitOAuth(flowId, value),
  cancelOAuth: (flowId) => mockStore.cancelOAuth(flowId),
};

/** The single client instance. Stable across renders (safe in effect deps). */
export const modelsApi: ModelsApi = isLive() ? liveApi : mockApi;
