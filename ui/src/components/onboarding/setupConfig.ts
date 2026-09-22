/**
 * The config evidence setup is allowed to act on, and the only way it reads it.
 *
 * C2's prerequisite is a persisted fact that another browser, another operator or
 * Settings can change while this flow is open. `api.getConfig()` answers from a 30s
 * cache, so calling it again is not a freshness test — it can hand back the world as
 * it was before the change. Everything that must not be wrong about the prerequisite
 * — the initial read, an explicit Retry and the completion boundary — comes through
 * the uncached GET here instead.
 *
 * Validation is deliberately narrow and shared: absent or malformed fields are UNREAD
 * state, not an opt-out, and that distinction is the whole reason a caller can tell
 * "the gateway is off" from "nobody could tell me". The same shape is already written
 * and published in L2's `providers/gatewayBootstrap.ts`; this is that parser, extracted
 * so both callers hold one policy. L2 deletes its copy and imports this in the
 * integration commit after #2087 lands.
 *
 * Stateless on purpose: no React, no cache, no owner. It returns one classified read.
 */
import { apiFetch } from '@/lib/apiFetch';

/**
 * The fields of `/api/config` this flow is allowed to read.
 *
 * Narrow on purpose: validating the whole config would couple setup to every future
 * field, and an absent or malformed one of these is unread state rather than an
 * opt-out — which is a distinction the caller has to be able to make.
 */
export type SetupConfigSnapshot = {
  version: 'v2';
  setup_completed: boolean;
  capabilityEnabled: boolean;
  savedIntentEnabled: boolean;
  primaryPlatform: string;
  enabledPlatforms: string[];
  /** The validated body, for the shell's existing server-config state. */
  raw: Record<string, unknown>;
};

/**
 * One read, classified in the terms the caller acts in. `unread` covers every way the
 * answer failed to arrive — transport, HTTP, JSON, shape — because they all leave the
 * prerequisite unknown, and unknown never becomes `false` or `true` by fallback.
 */
export type SetupConfigRead =
  | { state: 'read'; config: SetupConfigSnapshot }
  | { state: 'unread'; status?: number; detail?: string };

export type SetupConfigFetch = (input: string, init?: RequestInit) => Promise<Response>;

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

/** The server's error shape is a string on some handlers and an object on others. */
function errorDetail(body: unknown): string | undefined {
  if (!isRecord(body)) return undefined;
  const error = body.error;
  if (typeof error === 'string') return error;
  if (isRecord(error) && typeof error.message === 'string') return error.message;
  if (body.ok === false && typeof body.message === 'string') return body.message;
  return undefined;
}

const hasError = (body: unknown): boolean =>
  isRecord(body) && (body.ok === false || body.error !== undefined);

async function parseJson(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    return undefined;
  }
}

/**
 * Validate a config body, or explain what was missing by refusing it.
 *
 * Returns `null` rather than throwing so each caller decides what an invalid body
 * means at its own step.
 */
export function readSetupConfig(body: unknown): SetupConfigSnapshot | null {
  if (!isRecord(body) || hasError(body)) return null;
  if (body.version !== 'v2') return null;
  if (typeof body.setup_completed !== 'boolean') return null;

  const platforms = body.platforms;
  if (!isRecord(platforms)) return null;
  if (typeof platforms.primary !== 'string') return null;
  const enabled = platforms.enabled;
  if (!Array.isArray(enabled) || enabled.some((entry) => typeof entry !== 'string')) return null;

  if (!isRecord(body.runtime) || !isRecord(body.agents)) return null;

  const capabilities = body.capabilities;
  const capability = isRecord(capabilities) ? capabilities.model_hub : undefined;
  const saved = body.model_hub;
  if (!isRecord(capability) || typeof capability.enabled !== 'boolean') return null;
  if (!isRecord(saved) || typeof saved.enabled !== 'boolean') return null;

  return {
    version: 'v2',
    setup_completed: body.setup_completed,
    capabilityEnabled: capability.enabled,
    savedIntentEnabled: saved.enabled,
    primaryPlatform: platforms.primary,
    enabledPlatforms: enabled as string[],
    raw: body,
  };
}

/**
 * Read the config as it is right now.
 *
 * `cache: 'no-store'` is the point of the function: this is the read whose staleness
 * would let setup complete against a prerequisite somebody already turned off.
 */
export async function fetchSetupConfig(fetch: SetupConfigFetch = apiFetch): Promise<SetupConfigRead> {
  let response: Response;
  try {
    response = await fetch('/api/config', { cache: 'no-store' });
  } catch (cause) {
    return { state: 'unread', detail: String(cause) };
  }
  const body = await parseJson(response);
  if (!response.ok) return { state: 'unread', status: response.status, detail: errorDetail(body) };
  const config = readSetupConfig(body);
  // No status here: the transport succeeded, so `HTTP 200` would explain nothing about
  // a body that arrived and did not say what it had to say.
  if (!config) return { state: 'unread', detail: errorDetail(body) };
  return { state: 'read', config };
}
