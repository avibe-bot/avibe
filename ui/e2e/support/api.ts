// A thin state client over the frozen `/api/models/*` surface.
//
// This is deliberately NOT the thing under test. The suite asserts what a user
// can do in the browser; the API is here to read what state the live instance is
// in, to establish a scenario's PRECONDITIONS, and to put back what a spec
// changed. Two reasons it is not more UI clicking:
//
//   - a teardown that clicked would fail for the same reason the test just
//     failed, and leave the instance dirty for every spec after it;
//   - a routing test whose setup re-drives the add-key dialog is really an
//     add-key test wearing a routing test's name, and it reports the add flow's
//     failures as routing failures.
//
// The line is: whatever the scenario is about goes through the browser.
import type { APIRequestContext, APIResponse } from '@playwright/test';

import { BASE_URL, E2E_SOURCE_PREFIX } from './env';

export type SourceState = { status: string; [key: string]: unknown };

export type Source = {
  id: string;
  display_name: string;
  kind: string;
  /** The catalog id a preset was added under, or `custom` for a compatible
   *  endpoint. It is what the vendor dropdown selected, not a UI label. */
  vendor: string;
  protocol: string;
  supply_channel: string;
  base_url?: string | null;
  credential_ref?: string | null;
  client_nonce?: string | null;
  models: {
    id: string;
    origin: string;
    reasoning_efforts: string[];
    /**
     * Which rung of the provenance ladder produced `reasoning_efforts`.
     * Absent on a server that predates the field — the editable case.
     */
    reasoning_efforts_source?: 'upstream' | 'catalog' | 'user' | null;
    [key: string]: unknown;
  }[];
  state: SourceState;
  adopted_by?: { backend: string; model: string }[];
};

export type Agent = {
  backend: string;
  mode: 'hub' | 'direct';
  cli_present: boolean;
  /**
   * The backend's model menu, each entry carrying how that model is routed.
   * This is where the agents payload names its models — there is no `models`
   * array on it, and a fixture that reads one silently skips every spec that
   * needs a route.
   *
   * Null outside Gateway mode — the server gates it on `mode == "hub"` — so it
   * answers "what does this backend route" only for a backend that already
   * routes. `routableModels` below is the question asked of any backend.
   */
  model_supply?: { model_id: string; chain_length: number; has_runnable_hop: boolean }[] | null;
  selected_model_id?: string | null;
  /** `fixed` for `claude`/`codex`, whose menu is a builtin list; `open` for
   *  `opencode`, whose menu is whatever the operator has ticked. */
  menu_kind?: 'fixed' | 'open';
  builtin_models?: string[] | null;
  menu?: { checked?: string[] } | null;
  [key: string]: unknown;
};

/**
 * One row of a backend's saved model list, as the server projects it back.
 *
 * Loose on purpose: the row carries the whole editor form, and a spec that
 * named every field would have to be revised by every change to it. What is
 * spelled out is the pair a projection assertion is about — the id the row is
 * stored under, and, on an OpenCode row, which of the two APIs Avibe answers
 * that model on. `native_protocol` is REQUIRED on an OpenCode row (C8) and
 * absent on every other backend's, so `undefined` here is a real answer about
 * the row, not a gap in this type.
 */
export type CatalogModel = {
  id: string;
  native_protocol?: string;
  [key: string]: unknown;
};

export type Probe = {
  reachable: boolean;
  source_id?: string | null;
  model_id?: string | null;
  error?: string | null;
};

export type Runtime = {
  enabled: boolean;
  status: {
    health: string;
    installed_version?: string | null;
    listening?: { host: string; port: number } | null;
  };
  manifest: { resolution?: string };
};

export type Capabilities = { model_hub?: { enabled?: boolean } };

export type RouteHop = { source_id: string; model_id: string };

/**
 * Whether a source is this suite's own — the one namespace declaration, so
 * "ours" cannot be spelled differently by the code that deletes sources and the
 * code that decides which route hops survive the suite.
 *
 * The prefix is the identity, not a heuristic: every create path in the suite
 * carries it, including the ones that expect to fail.
 */
export const isSuiteSource = (source: Source): boolean =>
  source.display_name.startsWith(E2E_SOURCE_PREFIX);

/**
 * The models a backend's routes are keyed by — the set `model_supply` holds
 * once that backend is in Gateway mode.
 *
 * An INSTALLED backend is not the same thing as a backend with a route to open,
 * and the difference is not exotic: `_agent_payload` keys `model_supply` on the
 * builtin list for a `fixed` menu and on `menu.checked` for an `open` one, so an
 * `opencode` whose menu nobody has ticked sits in Gateway mode with an empty
 * supply while a `claude` in Direct mode has a full one.
 *
 * `model_supply` is the server's own answer and is preferred wherever it exists
 * — but it is null outside Gateway mode, so a Direct backend can only be judged
 * before the switch by the menu that BECOMES it, which is what the fallback
 * reconstructs.
 *
 * Deliberately narrower than the row list the page renders: `listedModelIds`
 * adds the selected model and existing route keys, so the surface can show a row
 * for a model `model_supply` does not carry — and `model_supply` is what a route
 * fixture and its guards read.
 */
export const routableModels = (agent: Agent): string[] =>
  agent.model_supply?.map((entry) => entry.model_id)
  ?? (agent.menu_kind === 'fixed' ? agent.builtin_models ?? [] : agent.menu?.checked ?? []);

/** Which body `/settings/models` renders, once the gate and the runtime have
 *  already let the tab body render at all. */
export type SurfaceKind = 'gateway' | 'direct-home' | 'direct-no-backend';

/**
 * The page a spec lands on, answered ONCE from the two decisions the product
 * makes in `SettingsModelsPage.tsx`.
 *
 * A precondition is not a filter over instance facts, it is a claim about which
 * SURFACE those facts produce — and the product reaches its three surfaces
 * through two NESTED tests, so a spec that checks only the outer one admits an
 * instance whose page is a different shape. `directEmpty` (no sources AND every
 * backend on Direct) chooses `DirectHome` over the gateway overview; `DirectHome`
 * then renders `.model-hub-direct-empty` — an install prompt with no backend
 * card and no "Switch to gateway" button — when `installedAgents` is empty.
 *
 * Stated as a kind rather than as a boolean because the third case is not a
 * degenerate second one: "no backend installed" and "already a gateway" send
 * their reader to do opposite things, and a spec that reports one as the other
 * is worse than a spec that fails.
 */
export const surfaceKind = (agents: Agent[], sources: Source[]): SurfaceKind => {
  const directEmpty = sources.length === 0 && agents.every((agent) => agent.mode === 'direct');
  if (!directEmpty) return 'gateway';
  return agents.some((agent) => agent.cli_present) ? 'direct-home' : 'direct-no-backend';
};

/**
 * The plan a guarded mutation is refused with, read the way the route actually
 * sends it: `{ok: false, error: "source_in_route_chain", would_remove_hops: [],
 * would_interrupt: []}` — the two lists sit at the TOP level beside `error`,
 * which is a plain string, not an object with a `data` bag.
 *
 * Worth stating because getting it wrong is silent: a reader that looks under
 * `error.data` finds nothing, echoes two empty arrays, is refused a second time,
 * and reports the same `false` a genuinely un-forceable mutation would. That is
 * how a teardown leaves behind every source it was written to remove.
 */
const refusedPlan = (body: unknown): { would_remove_hops: unknown[]; would_interrupt: unknown[] } => {
  const refusal = (body ?? {}) as { would_remove_hops?: unknown[]; would_interrupt?: unknown[] };
  return {
    would_remove_hops: refusal.would_remove_hops ?? [],
    would_interrupt: refusal.would_interrupt ?? [],
  };
};

export type AgentChain = {
  backend: string;
  /** The menu model this chain routes. Named `model_id` on the wire — the
   *  server writes `{model_id, chain}`, so a reader keying on `model` finds
   *  nothing and quietly treats every existing route as empty. */
  model_id: string;
  chain: { source_id: string; model_id: string; [key: string]: unknown }[];
  [key: string]: unknown;
};

/**
 * The one read failure an operator can act on, spelled as an instruction.
 *
 * Worth lifting out of the generic throw because the intuition it contradicts
 * is a reasonable one: Playwright opens a FRESH browser context and the
 * `request` fixture keeps its own cookie jar, so being signed in to that
 * instance in your own browser carries nothing into a run. The refusal then
 * lands on a precondition read, before any test has named itself, and the
 * status alone reads like the instance is broken.
 *
 * Matched as a family rather than as one string: `vibe/ui_server.py` answers
 * 401 with `remote_access_login_required`, and `ui/src/lib/apiFetch.ts` treats
 * `remote_access_authorization_refresh_required`, `remote_access_revoked` and
 * `remote_access_authorization_unavailable` as the same class — the browser
 * must go and re-authorize. Not one of them is something this suite can do, so
 * what the operator needs to hear is the same sentence for all four.
 */
export const remoteAuthRefusal = (status: number, bodyText: string): string | null => {
  if (status !== 401) return null;
  let error: unknown;
  try {
    error = (JSON.parse(bodyText) as { error?: unknown }).error;
  } catch {
    return null;
  }
  if (typeof error !== 'string' || !error.startsWith('remote_access_')) return null;
  return (
    `The instance at ${BASE_URL} refused an unauthenticated read with \`${error}\`. `
    + 'This suite has no login step: Playwright opens a fresh browser context and its API context '
    + 'keeps a separate cookie jar, so a session in your own browser is not carried into the run. '
    + 'Point it at an endpoint that serves without a remote-access login \u2014 loopback, or the VM\'s '
    + 'own host:port on a network you trust \u2014 rather than at a tunnel fronted by one.'
  );
};

export class HubApi {
  private token: string | null = null;
  private readonly request: APIRequestContext;

  constructor(request: APIRequestContext) {
    this.request = request;
  }

  private async csrf(): Promise<string> {
    if (this.token) return this.token;
    const response = await this.request.get('/api/csrf-token');
    if (!response.ok()) throw new Error(`csrf-token ${response.status()}`);
    const payload = (await response.json()) as { csrf_token?: string };
    if (!payload.csrf_token) throw new Error('csrf-token response carried no token');
    this.token = payload.csrf_token;
    return this.token;
  }

  /**
   * GET returning the parsed body, and THROWING on any non-2xx.
   *
   * The throw is the point. These reads feed the precondition helpers, and an
   * earlier version collapsed every failure to `null` — so a 401, a 500, or a
   * `VIBE_E2E_BASE_URL` aimed at the wrong server all arrived as
   * `modelHubEnabled() === false` and `runtime() === null`, which the helpers
   * correctly treat as "this instance does not have the feature" and skip. A
   * broken run then reported itself as a clean one. An absent capability and an
   * unreachable API are different facts and are no longer spelled the same way.
   */
  async read<T>(path: string): Promise<T> {
    const response = await this.request.get(path);
    if (!response.ok()) throw await this.readFailure(path, response);
    return (await response.json()) as T;
  }

  /** The sentence a failed read owes the operator, kept in one place so a probe
   *  that must not throw for one particular answer still throws it for every
   *  other one. */
  private async readFailure(path: string, response: APIResponse): Promise<Error> {
    const text = await response.text();
    const refusal = remoteAuthRefusal(response.status(), text);
    if (refusal) return new Error(refusal);
    return new Error(
      `GET ${path} → ${response.status()} ${response.statusText()}. `
        + `This is the instance at ${BASE_URL} failing a read, not a missing capability: `
        + `${text.slice(0, 300)}`,
    );
  }

  private async mutate(
    method: 'post' | 'patch' | 'delete' | 'put',
    path: string,
    body?: unknown,
  ): Promise<{ ok: boolean; status: number; body: unknown }> {
    const response = await this.request[method](path, {
      headers: {
        'X-Vibe-CSRF-Token': await this.csrf(),
        'Content-Type': 'application/json',
        // A browser stamps `Origin` on every mutating fetch and the server
        // refuses one without it. Playwright's API context does not, so the
        // header is supplied here rather than discovered as a 403 later.
        Origin: BASE_URL,
      },
      ...(body === undefined ? {} : { data: body }),
    });
    let parsed: unknown = null;
    try {
      parsed = await response.json();
    } catch {
      parsed = null;
    }
    return { ok: response.ok(), status: response.status(), body: parsed };
  }

  /**
   * The same projection the browser reads: availability is the backend's to
   * declare, and `GET /api/config` is where it declares it.
   *
   * `/api/config` answers on every instance, capability on or off, so a failure
   * here is never "the feature is missing" — it is the read itself failing, and
   * `read` throws it.
   */
  async capabilities(): Promise<Capabilities> {
    return (await this.read<{ capabilities?: Capabilities }>('/api/config')).capabilities ?? {};
  }

  async modelHubEnabled(): Promise<boolean> {
    return (await this.capabilities()).model_hub?.enabled === true;
  }

  async sources(): Promise<Source[]> {
    return (await this.read<{ sources: Source[] }>('/api/models/sources')).sources ?? [];
  }

  async agents(): Promise<Agent[]> {
    return (await this.read<{ agents: Agent[] }>('/api/models/agents')).agents ?? [];
  }

  /**
   * Whether this instance answers the read the picker's built-in and provider
   * groups are made of.
   *
   * Judged on the PAYLOAD, because the status cannot say. `vibe/ui_server.py`
   * registers its static catch-all last, and that route's SPA fallback answers
   * any unmatched extension-less GET with `index.html` at 200 — so an instance
   * that has never heard of this path reports the same status as one serving
   * it, and the browser's own failure there is a non-JSON body rather than a
   * named 404. Any other refusal is a read failing and throws: whether the Hub
   * is on at all is `requireModelHub`'s question, already answered.
   */
  async servesModelCandidates(backend: string): Promise<boolean> {
    const path = `/api/models/agents/${backend}/models/candidates`;
    const response = await this.request.get(path);
    if (response.status() === 404) return false;
    if (!response.ok()) throw await this.readFailure(path, response);
    let payload: unknown;
    try {
      payload = (await response.json()) as unknown;
    } catch {
      return false;
    }
    const candidates = (payload as { candidates?: unknown }).candidates;
    return typeof candidates === 'object' && candidates !== null;
  }

  /**
   * A backend's saved model list, read where the product reads it.
   *
   * The list is not on `/api/models/agents`: the catalog dialog loads its
   * baseline from this backend-scoped read, and `catalog_models` is the
   * server's own projection of the rows — the same object a save echoes back.
   * Asking here rather than reconstructing the list from `model_supply` is what
   * makes a projection assertion an assertion about what was STORED; the supply
   * carries ids and routability, and none of the row's own fields.
   *
   * `null` when the payload carried no `catalog_models` — a server that
   * predates the catalog, which is a fact about the instance and a skip, not a
   * read that failed.
   */
  async catalogModels(backend: string): Promise<CatalogModel[] | null> {
    const payload = await this.read<{ agent?: { catalog_models?: CatalogModel[] | null } }>(
      `/api/models/agents/${backend}/sources`,
    );
    return payload.agent?.catalog_models ?? null;
  }

  /**
   * Puts a backend's model list back to `models`, and THROWS if it did not land.
   *
   * The baseline is re-read HERE rather than taken from the caller, because the
   * server checks the PUT against the list it currently holds and the caller is
   * teardown — which asks for this precisely when the spec that ran in between
   * has changed that list. A restore sent against a stale baseline is refused,
   * and a refusal a teardown swallows leaves the suite's rows on the instance
   * for every spec after it.
   *
   * Removing a row can take a route with it, which the server guards; teardown
   * is exactly the caller that means it, so the refusal's own plan is echoed
   * back the way `deleteSource` does.
   */
  async restoreCatalogModels(backend: string, models: CatalogModel[]): Promise<void> {
    const path = `/api/models/agents/${backend}/models`;
    const baseline = (await this.catalogModels(backend)) ?? [];
    const first = await this.mutate('put', path, { baseline, models });
    if (first.ok) return;
    const forced = await this.mutate('put', path, {
      baseline,
      models,
      force: true,
      ...refusedPlan(first.body),
    });
    if (forced.ok) return;
    throw new Error(
      `Could not restore backend ${backend}'s model list: first attempt ${first.status}, forced attempt `
        + `${forced.status} ${JSON.stringify(forced.body)}. The list this spec left is still on the `
        + 'instance, and every spec after this one inherits it.',
    );
  }

  /** `null` here means the payload carried no `runtime` — an answered read with
   *  nothing installed to describe, not a read that failed. */
  async runtime(): Promise<Runtime | null> {
    return (await this.read<{ runtime?: Runtime }>('/api/models/runtime/status')).runtime ?? null;
  }

  /**
   * Starts the gateway runtime, THROWING when the instance refuses.
   *
   * Named rather than toggled, and reached from `restoreRuntimeRunning`: the
   * caller is teardown, which asks for this precisely when the state it just
   * read may be wrong, and a start is the one request that means the same thing
   * whatever the instance currently believes about itself.
   */
  async startRuntime(): Promise<void> {
    const result = await this.mutate('post', '/api/models/runtime/start');
    if (!result.ok) {
      throw new Error(
        `Starting the gateway runtime failed (${result.status}): ${JSON.stringify(result.body)}.`,
      );
    }
  }

  /**
   * THROWS when the instance refuses the switch, rather than resolving over a
   * non-2xx answer.
   *
   * Both callers are teardown-adjacent (the gateway fixture putting a backend
   * back to direct, A3 un-arranging its blocked stop), so a swallowed failure
   * here reports a clean run while leaving the instance in Gateway mode — the
   * next spec inherits a shape it did not arrange and skips over a surface it
   * was supposed to check. The dirty state is named where it was made.
   */
  async setAgentMode(backend: string, mode: 'hub' | 'direct'): Promise<void> {
    const result = await this.mutate('patch', `/api/models/agents/${backend}/mode`, { mode });
    if (!result.ok) {
      throw new Error(
        `Switching backend ${backend} to ${mode} mode failed (${result.status}): `
          + `${JSON.stringify(result.body)}. The instance may still hold the previous mode.`,
      );
    }
  }

  async chains(backend: string): Promise<AgentChain[]> {
    return (await this.read<{ chains: AgentChain[] }>(`/api/models/agents/${backend}/chains`)).chains ?? [];
  }

  /** Replaces one model's chain outright. Used to arrange the preconditions a
   *  guard scenario needs — a source that supplies a live route cannot be
   *  removed without the guard, which is the whole point of the scenario. */
  async putAgentChain(backend: string, model: string, hops: RouteHop[]): Promise<boolean> {
    const path = `/api/models/agents/${backend}/chain?model=${encodeURIComponent(model)}`;
    const first = await this.mutate('put', path, { hops });
    if (first.ok) return true;
    const forced = await this.mutate('put', path, {
      hops,
      force: true,
      ...refusedPlan(first.body),
    });
    return forced.ok;
  }

  /**
   * Runs the backend's dry run down its own chain, and reports what the far end
   * said.
   *
   * This is the only way to make a source's CREDENTIAL fail from outside the
   * product: `POST …/refresh` re-reads the model list, which an upstream that
   * rejects completions still serves, so a refetch leaves the source healthy.
   * Only a real request through the chain reaches `_probe_failure`, and only
   * that writes `needs_action.credential_revoked` — the one state in which a
   * source offers to have its key replaced.
   *
   * Returns `null` when the route has no candidate to try — the chain is empty,
   * cooling, or interrupted by the very failure a previous probe recorded. That
   * is a `409 probe_no_candidate` and carries no verdict, so a caller waiting on
   * a state change must read the SOURCE and use the probe only to drive it.
   */
  async probeAgent(backend: string, model: string): Promise<Probe | null> {
    const result = await this.mutate('post', `/api/models/agents/${backend}/probe`, { model });
    return (result.body as { probe?: Probe } | null)?.probe ?? null;
  }

  /**
   * Creates an api_key source directly, for specs whose subject is what happens
   * AFTER a source exists. `display_name` carries the suite prefix so teardown
   * can find it even if the spec died halfway through.
   *
   * Returns `null` when the instance refused. Callers reach this through
   * `requireSource`, which FAILS on that null: by the time a spec asks, the mock
   * has already answered its own control plane, so a refusal is the product
   * refusing a healthy upstream — not a missing precondition (§5a).
   */
  async createApiKeySource(
    displayName: string,
    baseUrl: string,
    key = 'e2e-key',
  ): Promise<Source | null> {
    const created = await this.mutate('post', '/api/models/sources', {
      kind: 'api_key',
      vendor: 'custom',
      display_name: displayName,
      base_url: baseUrl,
      key,
      accept_unavailable_inventory: true,
    });
    if (!created.ok) return null;
    const body = created.body as { source?: Source } & Partial<Source>;
    return (body?.source ?? (body as Source)) ?? null;
  }

  /**
   * Removes a source, forcing past the route guard when there is one, and
   * THROWS if it is still there afterwards.
   *
   * The forced attempt's result used to be discarded, which made teardown
   * unfalsifiable: a stale guard echo, an engine failure, or a 500 all left the
   * source and its route state behind while cleanup reported success, and the
   * next spec inherited it as a mysterious precondition. A cleanup that cannot
   * say whether it cleaned is worse than none, because it is believed.
   *
   * A 404 is success, not failure: the postcondition is that the source is
   * gone, and one already deleted by a suite sweep satisfies it.
   */
  async deleteSource(id: string): Promise<void> {
    const path = `/api/models/sources/${encodeURIComponent(id)}`;
    const first = await this.mutate('delete', path);
    if (first.ok || first.status === 404) return;
    // A source that still supplies a route is refused until the caller echoes
    // the server's own plan back. Teardown is exactly the caller that means it.
    const forced = await this.mutate('delete', `${path}?force=true`, refusedPlan(first.body));
    if (forced.ok || forced.status === 404) return;
    throw new Error(
      `Could not delete source ${id}: first attempt ${first.status}, forced attempt `
        + `${forced.status} ${JSON.stringify(forced.body)}. It is still on the instance, and every `
        + 'spec after this one inherits it.',
    );
  }

  /**
   * Removes every source this suite created, whatever spec left it behind.
   *
   * Returns quietly when the capability is off — a hub-disabled instance has no
   * `/api/models/*` to sweep, and that is the one case where nothing to clean is
   * a fact about the instance rather than a failed read.
   */
  async removeSuiteSources(): Promise<void> {
    if (!(await this.modelHubEnabled())) return;
    // Every match is attempted before any failure is raised: the gateway
    // fixture leaves two sources behind, and a first delete that throws used
    // to strand the second on the shared instance — poisoning the source
    // ordering and routing preconditions of every later spec — alongside the
    // failure that already stopped this one.
    const failures: string[] = [];
    for (const source of await this.sources()) {
      if (!isSuiteSource(source)) continue;
      try {
        await this.deleteSource(source.id);
      } catch (error) {
        failures.push(`${source.id}: ${error instanceof Error ? error.message : String(error)}`);
      }
    }
    if (failures.length) {
      throw new Error(
        `Suite-source sweep left ${failures.length} source(s) on the instance:\n  `
          + failures.join('\n  '),
      );
    }
  }
}
