// D11: making the controller answerable before the provider screen asks it anything.
//
// Hub reads are IPC-backed, so a stopped application cannot serve them and a missing
// config file means there is nothing for the controller to load. This runs once on
// active provider entry and establishes both, in the only order that can prove them:
// seed config, read it back uncached, prove it actually persisted, start a controller
// that is confirmed stopped, then observe the runtime the controller's own recovery
// has already been working on.
//
// It is deliberately stateless. The shell owns the `RegionRead` this feeds; this
// module returns one validated snapshot or throws one classified error, so the same
// sequence can be tested without a React tree and the region has exactly one owner.
//
// Two things it must not do, both of which look like improvements and are not:
//
//  - Repeat the POST after an unknown outcome. The write may have landed; a blind
//    retry is a second write against state the first one may already own. An unknown
//    outcome is settled by reading, and only an explicit retry reseeds.
//  - Ensure or start the engine. Controller startup already ran
//    `_recover_runtime_owners()` with the installer admission that belongs on the
//    server. This observes that outcome; asking for a second unconditional install
//    would race the recovery that is still finishing.
import type { AgentBackend, RuntimeDependency } from '@/components/settings/models/types';
import type { BackendConnectionState } from '@/context/ApiContext';
import {
  errorDetail,
  hasError,
  parseJson,
  readSetupConfig,
  type SetupConfigSnapshot,
} from '../setupConfig';

/**
 * Why bootstrap could not finish, in the terms the caller has to act in.
 *
 * `disabled` is not a failure: it is C2's prerequisite boundary, and the caller
 * shows the configuration path rather than a retry. The rest differ in what is
 * outstanding — `refused` is a definitive no and blocks, `unknown` leaves a write
 * nobody can account for, `unread` means no write is outstanding and a read this
 * flow depends on did not answer.
 */
export type GatewayBootstrapReason = 'refused' | 'unknown' | 'unread' | 'disabled';

export class GatewayBootstrapError extends Error {
  readonly reason: GatewayBootstrapReason;
  /** The step that produced it, for the caller's error surface. */
  readonly step: 'seed' | 'readback' | 'capability' | 'persistence' | 'start' | 'runtime';
  readonly status?: number;
  readonly detail?: string;

  constructor(
    reason: GatewayBootstrapReason,
    step: GatewayBootstrapError['step'],
    options: { status?: number; detail?: string } = {},
  ) {
    super(`gateway bootstrap ${step}: ${reason}`);
    this.name = 'GatewayBootstrapError';
    this.reason = reason;
    this.step = step;
    this.status = options.status;
    this.detail = options.detail;
  }
}

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

export type GatewayBootstrapDeps = {
  /** The CSRF-aware `apiFetch`, injected so the sequence is testable as itself. */
  fetch: (input: string, init?: RequestInit) => Promise<Response>;
  getBackendConnection: (backend: AgentBackend) => Promise<BackendConnectionState>;
  /** `useStatus().control`, which already rejects a non-2xx response. */
  control: (action: string) => Promise<unknown>;
  getRuntimeStatus: () => Promise<RuntimeDependency>;
};

export type GatewayBootstrapResult = { config: SetupConfigSnapshot; runtime: RuntimeDependency };

/**
 * @param backend the assistant whose connection read proves the config file exists.
 *   Any one would do — the handler calls `load_config()` either way — so the caller
 *   passes the one it is about to work with rather than this module picking.
 */
export async function bootstrapGateway(
  deps: GatewayBootstrapDeps,
  backend: AgentBackend,
): Promise<GatewayBootstrapResult> {
  // 1. Seed, exactly once. A definitive refusal throws from inside it; anything else
  //    leaves a write to account for, and accounting for a write is reading.
  const outstanding = await seedConfig(deps);

  // 2. Read back, uncached and directly: this POST cleared no `ApiContext` cache and
  //    emitted no convergence event, so a cached read here would describe the world
  //    before the write. An acknowledged write and an unaccounted one take the same
  //    read, because they ask the server the same question — what does the config say
  //    now — and only the answer can tell them apart.
  let readback: Response;
  try {
    readback = await deps.fetch('/api/config', { cache: 'no-store' });
  } catch {
    // An outstanding write is the more specific answer, and it is why a retry here
    // may reseed at all. Only a read that succeeds retires it.
    throw outstanding ?? new GatewayBootstrapError('unread', 'readback');
  }
  const config = readback.ok ? readSetupConfig(await parseJson(readback)) : null;
  if (!config) {
    throw outstanding ?? new GatewayBootstrapError('unread', 'readback', { status: readback.status });
  }

  // 3. The prerequisite boundary. Stop here without touching the preference: a
  //    person who turned the gateway off in Settings did so deliberately, and setup
  //    silently turning it back on would be the worst possible answer.
  //
  //    This answers the attempt whatever became of the write. Reporting an unaccounted
  //    seed instead would send someone to retry a write whose fate has stopped
  //    mattering, when what they are owed is the configuration path.
  if (!config.capabilityEnabled || !config.savedIntentEnabled) {
    throw new GatewayBootstrapError('disabled', 'capability');
  }

  // 4. Prove persistence. A valid GET cannot do it alone — the server answers with an
  //    in-memory default when no file exists, so the seed's outcome is still open
  //    until a handler that actually calls `load_config()` succeeds.
  //
  //    Which is also what retires an unaccounted seed: the empty patch's only intended
  //    effect was a config file the server can load, and a handler that just loaded one
  //    is that effect, observed. Everything after this line answers for itself.
  let connection: BackendConnectionState;
  try {
    connection = await readConnection(deps, backend);
  } catch (error) {
    throw outstanding ?? error;
  }

  // 5. Start, but only a controller confirmed stopped. Draining, failed or unknown
  //    are somebody's existing recovery path; inferring a restart from them would
  //    interrupt whatever is really happening.
  if (connection.application === 'stopped') {
    const result = await deps.control('start').catch(() => {
      throw new GatewayBootstrapError('unknown', 'start');
    });
    if (!isRecord(result) || result.ok !== true || result.action !== 'start') {
      throw new GatewayBootstrapError('unknown', 'start', { detail: errorDetail(result) });
    }
    connection = await readConnection(deps, backend);
  }
  if (connection.application !== 'applied') {
    throw new GatewayBootstrapError('unknown', 'start', { detail: connection.application });
  }

  // 6. Observe. Controller startup has already run its own recovery with the
  //    installer admission the server owns; this reads what came of it.
  const runtime = await readRuntimeObservation(deps);

  return { config, runtime };
}

/**
 * The runtime read the sequence ends on, on its own.
 *
 * Once bootstrap has run for a mount, a later refresh has nothing left to establish:
 * the config exists, the controller is up, and re-seeding or re-starting would act on
 * state that is already proven. So the caller's refresh path is this step alone, and
 * it is this step — the same validation, the same classified failure — rather than a
 * second call policy that could drift from it.
 */
export async function readRuntimeObservation(
  deps: Pick<GatewayBootstrapDeps, 'getRuntimeStatus'>,
): Promise<RuntimeDependency> {
  let runtime: RuntimeDependency;
  try {
    runtime = await deps.getRuntimeStatus();
  } catch {
    throw new GatewayBootstrapError('unread', 'runtime');
  }
  // A body without a status is a body that did not answer: every consumer of this
  // reads `status.health`, and inventing one would report an unknown engine as fine.
  if (!isRecord(runtime) || !isRecord((runtime as unknown as Record<string, unknown>).status)) {
    throw new GatewayBootstrapError('unread', 'runtime');
  }
  return runtime;
}

/**
 * Send the empty patch once, and say what is still unaccounted for afterwards.
 *
 * An empty patch, because `configMutationsToPayload` rejects an empty mutation list
 * before it ever reaches HTTP — the general validator stays as it is, and this
 * composition goes around it rather than through it.
 *
 * Returns the error that stays outstanding, or `null` when the server acknowledged
 * the write in terms this flow can read. Throws only for a definitive refusal, which
 * no later read may clear: a server that says no has answered, and reading the config
 * afterwards describes what exists, not what it declined to do.
 */
async function seedConfig(deps: GatewayBootstrapDeps): Promise<GatewayBootstrapError | null> {
  let seeded: Response;
  try {
    seeded = await deps.fetch('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    });
  } catch {
    // The request may never have reached the server, or may have committed and lost
    // its reply on the way back. Unknown, not refused — and never sent again.
    return new GatewayBootstrapError('unknown', 'seed');
  }
  const body = await parseJson(seeded);
  if (!seeded.ok || hasError(body)) {
    const evidence = { status: seeded.status, detail: errorDetail(body) };
    // A server that says no is definitive; a server that broke is not.
    if (seeded.status < 500) throw new GatewayBootstrapError('refused', 'seed', evidence);
    return new GatewayBootstrapError('unknown', 'seed', evidence);
  }
  // A 2xx is an acknowledgement only if it carries the config these handlers return.
  // A truncated or unrecognisable body says nothing about what was written, so it is
  // reconciled by reading like any other unknown rather than believed like a success.
  // The status stays as the evidence it is: the server did answer, and this is what
  // it answered with.
  if (!readSetupConfig(body)) {
    return new GatewayBootstrapError('unknown', 'seed', { status: seeded.status });
  }
  return null;
}

async function readConnection(
  deps: GatewayBootstrapDeps,
  backend: AgentBackend,
): Promise<BackendConnectionState> {
  let connection: BackendConnectionState;
  try {
    connection = await deps.getBackendConnection(backend);
  } catch {
    throw new GatewayBootstrapError('unknown', 'persistence');
  }
  if (!connection?.ok || typeof connection.application !== 'string') {
    throw new GatewayBootstrapError('unknown', 'persistence');
  }
  return connection;
}
