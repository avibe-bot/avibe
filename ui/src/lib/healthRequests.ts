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
type ConnectionAttempt = {
  requestId: string;
  phase: 'pairing' | 'native' | 'settled';
  result: ConnectionResult | null;
};
// S3 admits one connect at a time. This document owns that attempt, not a visible route.
let connection: ConnectionAttempt | null = null;
let connectionOwner: { cancelPairing: () => void; terminal: boolean } | null = null;
const connectionListeners = new Set<() => void>();
export const getHealthConnection = () => connection;
export function subscribeHealthConnection(listener: () => void) {
  connectionListeners.add(listener);
  return () => { connectionListeners.delete(listener); };
}
function publishConnection(next: ConnectionAttempt | null) {
  connection = next;
  connectionListeners.forEach(listener => listener());
}

export function observeHealthTerminal(id: string) {
  if (connection?.requestId !== id || !connectionOwner) return;
  connectionOwner.terminal = true;
  if (connection.phase === 'pairing') connectionOwner.cancelPairing();
  // A terminal request supersedes the receipt, but cannot end an executing Native call.
  else if (connection.result) publishConnection({ ...connection, result: null });
}

export async function connectHealthRequest(request: HealthRequest, signal: AbortSignal) {
  if (signal.aborted || healthRequestView(request) !== 'authorize'
    || (connection && connection.phase !== 'settled')
    || (connection?.requestId === request.id && connection.result)) return;
  const pairing = new AbortController();
  const owner = {
    terminal: false,
    cancelPairing: () => {
      if (connectionOwner !== owner || connection?.phase !== 'pairing') return;
      pairing.abort();
      connectionOwner = null;
      publishConnection(null);
    },
  };
  // Reserve synchronously, before POST, including when two consumers act in the same tick.
  connectionOwner = owner;
  publishConnection({ requestId: request.id, phase: 'pairing', result: null });
  signal.addEventListener('abort', owner.cancelPairing, { once: true });
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
    if (connectionOwner !== owner) return;
    // The code stays invocation-local. Once dispatched, aborting a route cannot cancel Native.
    publishConnection({ requestId: request.id, phase: 'native', result: null });
    signal.removeEventListener('abort', owner.cancelPairing);
    const result = await nativeHealthConnect({ requestId: request.id, pairingCode });
    if (!result || !['connected', 'declined', 'failed'].includes(result.state)) throw new Error('health_connect_failed');
    publishConnection({ requestId: request.id, phase: 'settled', result: owner.terminal ? null : result.state });
  } catch {
    if (connectionOwner === owner) {
      publishConnection({ requestId: request.id, phase: 'settled', result: owner.terminal ? null : 'failed' });
    }
  } finally {
    signal.removeEventListener('abort', owner.cancelPairing);
  }
}
