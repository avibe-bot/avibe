import { apiFetch, withApiDeadline } from './apiFetch';
import { hasNativeCapability, isNativeShell, nativeHealthConnect } from './native';

export type HealthRequest = {
  id: string;
  operation: 'health.connect' | 'health.refresh';
  state: 'queued' | 'claimed' | 'succeeded' | 'failed' | 'expired' | 'canceled';
  created_at: string;
  expires_at: string;
  requires_user: boolean;
  metrics: string[];
  error?: { code: string } | null;
};
export type HealthViewState = 'authorize' | 'waiting' | 'claimed' | 'succeeded' | 'connected' | 'declined' | 'failed' | 'expired' | 'canceled' | 'needsUpdate';

export function readHealthRequest(value: unknown): HealthRequest | null {
  if (!value || typeof value !== 'object') return null;
  const r = value as Partial<HealthRequest>;
  if (typeof r.id !== 'string' || !/^mreq_[0-9a-f]{32}$/.test(r.id)
    || !['health.connect', 'health.refresh'].includes(r.operation ?? '')
    || !['queued', 'claimed', 'succeeded', 'failed', 'expired', 'canceled'].includes(r.state ?? '')
    || typeof r.requires_user !== 'boolean' || typeof r.created_at !== 'string'
    || typeof r.expires_at !== 'string' || !Array.isArray(r.metrics)
    || !r.metrics.every((metric) => typeof metric === 'string')
    || (r.error != null && (typeof r.error !== 'object' || typeof r.error.code !== 'string'))) return null;
  return r as HealthRequest;
}

export function healthRequestView(request: HealthRequest): HealthViewState {
  if (request.state === 'failed') {
    if (request.error?.code === 'user_declined') return 'declined';
    if (request.error?.code === 'capability_unavailable') return 'needsUpdate';
    return 'failed';
  }
  if (request.state !== 'queued') return request.state;
  if (request.operation !== 'health.connect' || !request.requires_user) return 'waiting';
  if (typeof window !== 'undefined' && window.avibeNative?.platform === 'ios' && isNativeShell()) {
    return hasNativeCapability('health') ? 'authorize' : 'needsUpdate';
  }
  return 'waiting';
}

// Metadata and pairing are short requests. Cover CSRF, fetch and body decoding with one deadline.
const HEALTH_HTTP_DEADLINE_MS = 10_000;

export async function getHealthRequest(id: string, signal?: AbortSignal): Promise<HealthRequest> {
  return withApiDeadline(HEALTH_HTTP_DEADLINE_MS, signal, async (requestSignal) => {
    const response = await apiFetch(`/api/mobile/requests/${encodeURIComponent(id)}`, { signal: requestSignal, cache: 'no-store' });
    if (!response.ok) throw new Error('health_status_unavailable');
    const request = readHealthRequest(await response.json());
    requestSignal.throwIfAborted();
    if (!request || request.id !== id) throw new Error('health_status_invalid');
    return request;
  });
}

type ConnectionResult = 'connected' | 'declined' | 'failed';
type RequestKnowledge =
  | { readonly kind: 'receipt'; readonly result: ConnectionResult }
  | { readonly kind: 'terminal'; readonly request: HealthRequest };
type ConnectionAttempt = {
  readonly requestId: string;
  readonly phase: 'pairing' | 'native' | 'unresponsive';
  readonly cancelPairing: () => void;
};
type HealthConnections = {
  readonly active: ConnectionAttempt | null;
  readonly entries: ReadonlyMap<string, RequestKnowledge>;
};
// S3 admits one connect at a time. Completed request knowledge outlives that execution slot.
// Entries contain metadata only and live until this document ends; none confer authorization.
let connections: HealthConnections = { active: null, entries: new Map() };
const connectionListeners = new Set<() => void>();
export const getHealthConnection = () => connections;
export function subscribeHealthConnection(listener: () => void) {
  connectionListeners.add(listener);
  return () => { connectionListeners.delete(listener); };
}
function publishConnection(next: HealthConnections) {
  connections = next;
  connectionListeners.forEach(listener => listener());
}

export function observeHealthTerminal(request: HealthRequest) {
  if (['queued', 'claimed'].includes(request.state)
    || connections.entries.get(request.id)?.kind === 'terminal') return;
  // Copy only the value-free contract fields. A terminal entry is absorbing, even across remounts.
  const { id, operation, state, created_at, expires_at, requires_user, metrics, error } = request;
  const metadata: HealthRequest = { id, operation, state, created_at, expires_at, requires_user,
    metrics: [...metrics], error: error ? { code: error.code } : null };
  const entries = new Map(connections.entries);
  entries.set(id, { kind: 'terminal', request: metadata });
  publishConnection({ ...connections, entries });
  if (connections.active?.requestId === id) connections.active.cancelPairing();
  // cancelPairing cannot release Native: its exact attempt must still settle.
}

export async function connectHealthRequest(request: HealthRequest, signal: AbortSignal) {
  const previous = connections.entries.get(request.id);
  if (signal.aborted || healthRequestView(request) !== 'authorize'
    || connections.active || (previous && !(previous.kind === 'receipt' && previous.result === 'failed'))) return;
  const pairing = new AbortController();
  let nativeTimer: ReturnType<typeof setTimeout> | undefined;
  let attempt: ConnectionAttempt = {
    requestId: request.id,
    phase: 'pairing',
    cancelPairing: () => {
      if (connections.active !== attempt || attempt.phase !== 'pairing') return;
      pairing.abort();
      publishConnection({ ...connections, active: null });
    },
  };
  const settle = (result: ConnectionResult) => {
    if (getHealthConnection().active !== attempt) return;
    const entries = new Map(connections.entries);
    if (entries.get(request.id)?.kind !== 'terminal') entries.set(request.id, { kind: 'receipt', result });
    // Record the outcome and release only this reservation in one synchronous publication.
    publishConnection({ active: null, entries });
  };
  // Reserve synchronously, before POST, including when two consumers act in the same tick.
  const entries = new Map(connections.entries);
  if (previous?.kind === 'receipt' && previous.result === 'failed') entries.delete(request.id);
  publishConnection({ entries, active: attempt });
  signal.addEventListener('abort', attempt.cancelPairing, { once: true });
  try {
    const pairingCode = await withApiDeadline(HEALTH_HTTP_DEADLINE_MS, pairing.signal, async (requestSignal) => {
      const response = await apiFetch('/api/mobile/pairings', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ request_id: request.id }), signal: requestSignal,
      });
      if (!response.ok) throw new Error('health_pairing_failed');
      const value = await response.json() as { pairing_code?: unknown };
      requestSignal.throwIfAborted();
      if (typeof value.pairing_code !== 'string' || !value.pairing_code) throw new Error('health_pairing_invalid');
      return value.pairing_code;
    });
    pairing.signal.throwIfAborted();
    signal.throwIfAborted();
    if (getHealthConnection().active !== attempt) return;
    // The code stays invocation-local. Once dispatched, aborting a route cannot cancel Native.
    attempt = { ...attempt, phase: 'native' };
    publishConnection({ ...connections, active: attempt });
    signal.removeEventListener('abort', attempt.cancelPairing);
    // Older shells may never settle. Explain recovery without releasing their
    // still-live Native owner or admitting a concurrent connect.
    nativeTimer = setTimeout(() => {
      if (getHealthConnection().active !== attempt) return;
      attempt = { ...attempt, phase: 'unresponsive' };
      publishConnection({ ...connections, active: attempt });
    }, 100_000);
    const result = await nativeHealthConnect({ requestId: request.id, pairingCode });
    if (!result || !['connected', 'declined', 'failed'].includes(result.state)) throw new Error('health_connect_failed');
    settle(result.state);
  } catch {
    settle('failed');
  } finally {
    clearTimeout(nativeTimer);
    signal.removeEventListener('abort', attempt.cancelPairing);
  }
}
