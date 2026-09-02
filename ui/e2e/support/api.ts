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
import type { APIRequestContext } from '@playwright/test';

import { BASE_URL, E2E_SOURCE_PREFIX } from './env';

export type SourceState = { status: string; [key: string]: unknown };

export type Source = {
  id: string;
  display_name: string;
  kind: string;
  protocol: string;
  supply_channel: string;
  base_url?: string | null;
  credential_ref?: string | null;
  client_nonce?: string | null;
  models: { id: string; origin: string; reasoning_efforts: string[]; [key: string]: unknown }[];
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
   */
  model_supply?: { model_id: string; chain_length: number; has_runnable_hop: boolean }[];
  selected_model_id?: string | null;
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
  model: string;
  hops: { source_id: string; model_id: string; [key: string]: unknown }[];
  [key: string]: unknown;
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

  /** GET returning the parsed body, or `null` on any non-2xx (the caller
   *  decides whether an absent read is a skip or a failure). */
  async read<T>(path: string): Promise<T | null> {
    const response = await this.request.get(path);
    if (!response.ok()) return null;
    return (await response.json()) as T;
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

  /** The same projection the browser reads: availability is the backend's to
   *  declare, and `GET /api/config` is where it declares it. */
  async capabilities(): Promise<Capabilities> {
    const config = await this.read<{ capabilities?: Capabilities }>('/api/config');
    return config?.capabilities ?? {};
  }

  async modelHubEnabled(): Promise<boolean> {
    return (await this.capabilities()).model_hub?.enabled === true;
  }

  async sources(): Promise<Source[]> {
    return (await this.read<{ sources: Source[] }>('/api/models/sources'))?.sources ?? [];
  }

  async agents(): Promise<Agent[]> {
    return (await this.read<{ agents: Agent[] }>('/api/models/agents'))?.agents ?? [];
  }

  async runtime(): Promise<Runtime | null> {
    return (await this.read<{ runtime: Runtime }>('/api/models/runtime/status'))?.runtime ?? null;
  }

  async setAgentMode(backend: string, mode: 'hub' | 'direct'): Promise<void> {
    await this.mutate('patch', `/api/models/agents/${backend}/mode`, { mode });
  }

  async chains(backend: string): Promise<AgentChain[]> {
    return (
      (await this.read<{ chains: AgentChain[] }>(`/api/models/agents/${backend}/chains`))?.chains ?? []
    );
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
   * Returns `null` when the upstream refused — the caller turns that into a skip
   * with a message about the mock, not into a failure of the feature it meant
   * to test.
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

  async deleteSource(id: string): Promise<void> {
    const first = await this.mutate('delete', `/api/models/sources/${encodeURIComponent(id)}`);
    if (first.ok) return;
    // A source that still supplies a route is refused until the caller echoes
    // the server's own plan back. Teardown is exactly the caller that means it.
    await this.mutate(
      'delete',
      `/api/models/sources/${encodeURIComponent(id)}?force=true`,
      refusedPlan(first.body),
    );
  }

  /** Removes every source this suite created, whatever spec left it behind. */
  async removeSuiteSources(): Promise<void> {
    for (const source of await this.sources()) {
      if (source.display_name.startsWith(E2E_SOURCE_PREFIX)) await this.deleteSource(source.id);
    }
  }
}
