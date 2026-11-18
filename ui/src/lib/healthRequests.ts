import { apiFetch } from './apiFetch';
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

export async function getHealthRequest(id: string, signal?: AbortSignal): Promise<HealthRequest> {
  const response = await apiFetch(`/api/mobile/requests/${encodeURIComponent(id)}`, { signal, cache: 'no-store' });
  if (!response.ok) throw new Error('health_status_unavailable');
  const request = readHealthRequest(await response.json());
  if (!request || request.id !== id) throw new Error('health_status_invalid');
  return request;
}

export async function connectHealthRequest(request: HealthRequest) {
  if (healthRequestView(request) !== 'authorize') throw new Error('health_unavailable');
  const response = await apiFetch('/api/mobile/pairings', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ request_id: request.id }),
  });
  if (!response.ok) throw new Error('health_pairing_failed');
  const pairing = await response.json() as { pairing_code?: unknown };
  if (typeof pairing.pairing_code !== 'string' || !pairing.pairing_code) throw new Error('health_pairing_invalid');
  // This one-time code stays in this call, never in UI state, URLs or storage.
  const result = await nativeHealthConnect({ requestId: request.id, pairingCode: pairing.pairing_code });
  if (!result || !['connected', 'declined', 'failed'].includes(result.state)) throw new Error('health_connect_failed');
  return result;
}
