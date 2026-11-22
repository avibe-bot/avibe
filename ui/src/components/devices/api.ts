// The Devices pages' HTTP client: one function per route, answering with the views' own shapes
// (`./wire.ts` maps them). A refusal is thrown as `DevicesApiError` with the instance's code; a
// response that never arrived is thrown as `LostAnswer`, which is not a failure of the change.
import { apiFetch } from '@/lib/apiFetch';
import type { RemoteAccessSettings, RemoteAccessStatus, TunnelConnectivityDiagnostics, TunnelNetworkInterface } from '@/context/ApiContext';
import {
  activityFromWire, addressFromWire, connectionFromWire, devicesFromWire, rebindFromWire, rebindUnavailable, renameDeviceFromWire,
  signedOutFromWire, type DevicesSnapshot,
} from './wire';
import type {
  AddressState, ConnectionSetting, ConnectionState, DeviceActivity, DeviceFunction, RebindState, RenameDevice,
  SignedOutDevice,
} from './types';

export class DevicesApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly body: Record<string, unknown>;

  constructor(status: number, code: string, body: Record<string, unknown>) {
    super(code);
    this.name = 'DevicesApiError';
    this.status = status;
    this.code = code;
    this.body = body;
  }
}

/** The request left and no answer came back: the outcome is not known. */
export class LostAnswer extends Error {
  constructor() {
    super('lost_answer');
    this.name = 'LostAnswer';
  }
}

async function call<T = Record<string, unknown>>(path: string, init: { method?: string; body?: unknown } = {}): Promise<{ status: number; body: T }> {
  let response: Response;
  try {
    response = await apiFetch(path, {
      method: init.method ?? 'GET',
      headers: init.body === undefined ? undefined : { 'Content-Type': 'application/json' },
      body: init.body === undefined ? undefined : JSON.stringify(init.body),
      cache: 'no-store',
    });
  } catch {
    throw new LostAnswer();
  }
  let body: Record<string, unknown> = {};
  try {
    const parsed: unknown = await response.json();
    if (parsed && typeof parsed === 'object') body = parsed as Record<string, unknown>;
  } catch {
    // A truncated or non-JSON answer to a successful status is a lost answer; to a failure, a bare status.
    if (response.ok) throw new LostAnswer();
  }
  if (!response.ok) {
    const code = typeof body.error === 'string' ? body.error : typeof body.code === 'string' ? body.code : `http_${response.status}`;
    throw new DevicesApiError(response.status, code, body);
  }
  return { status: response.status, body: body as T };
}

const get = async <T = Record<string, unknown>>(path: string) => (await call<T>(path)).body;
const post = async <T = Record<string, unknown>>(path: string, body: unknown = {}) => (await call<T>(path, { method: 'POST', body })).body;

export type SignOutAnswer = { signedOut: SignedOutDevice[]; healthCleanupPending: boolean };
const signOutAnswer = (body: Record<string, unknown>): SignOutAnswer => ({
  signedOut: (Array.isArray(body.sign_out_results) ? body.sign_out_results : [])
    .map(signedOutFromWire).filter((each): each is SignedOutDevice => each !== null),
  healthCleanupPending: body.health_cleanup_pending === true,
});

export type ClaimForm = { claimId: string; action: string; method: string; fields: Record<string, string>; referrerPolicy: string };
function claimForm(body: Record<string, unknown>): ClaimForm {
  const form = (body.form ?? {}) as Record<string, unknown>;
  const fields = (form.fields ?? {}) as Record<string, unknown>;
  if (typeof body.claim_id !== 'string' || typeof form.action !== 'string') throw new LostAnswer();
  return {
    claimId: body.claim_id,
    action: form.action,
    method: typeof form.method === 'string' ? form.method : 'POST',
    fields: Object.fromEntries(Object.entries(fields).filter((entry): entry is [string, string] => typeof entry[1] === 'string')),
    referrerPolicy: typeof form.referrer_policy === 'string' ? form.referrer_policy : 'strict-origin',
  };
}

export type ClaimState = {
  state: string;
  code?: string;
  reasonCode?: string;
  ownerHint?: string;
  hostname?: string;
  disposition?: 'existing' | 'new';
  retryAfterSeconds?: number;
};
const claimState = (body: Record<string, unknown>): ClaimState => ({
  state: typeof body.state === 'string' ? body.state : 'unknown',
  code: typeof body.code === 'string' ? body.code : undefined,
  reasonCode: typeof body.reason_code === 'string' ? body.reason_code : undefined,
  ownerHint: typeof body.owner_hint === 'string' ? body.owner_hint : undefined,
  hostname: typeof body.hostname === 'string' ? body.hostname : undefined,
  disposition: body.binding_disposition === 'existing' || body.binding_disposition === 'new' ? body.binding_disposition : undefined,
  retryAfterSeconds: typeof body.retry_after_seconds === 'number' ? body.retry_after_seconds : undefined,
});

export type RenamePreview = {
  confirmationId: string;
  hostname: string;
  isReturn: boolean;
  holdDays?: number;
  releaseAt?: string;
  healthConnected: boolean;
  devices: RenameDevice[];
};

export const devicesApi = {
  status: () => get<RemoteAccessStatus>('/api/remote-access/status'),
  start: () => post<RemoteAccessStatus>('/api/remote-access/start'),
  stop: () => post<RemoteAccessStatus>('/api/remote-access/stop'),

  devices: async (): Promise<DevicesSnapshot> => devicesFromWire(await get('/api/devices')),
  /** One page of the activity, newest first; `nextCursor` asks for the page of older entries. */
  activity: async (nameOf: (id: string | undefined) => string | null, cursor?: string, limit = 50): Promise<{ items: DeviceActivity[]; nextCursor: string | null }> => {
    const body = await get(`/api/devices/activity?limit=${limit}${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`);
    return {
      items: (Array.isArray(body.items) ? body.items : []).map((each) => activityFromWire(each, nameOf))
        .filter((each): each is DeviceActivity => each !== null),
      nextCursor: typeof body.next_cursor === 'string' && body.next_cursor ? body.next_cursor : null,
    };
  },
  signOut: async (id: string) => signOutAnswer(await post(`/api/devices/${encodeURIComponent(id)}/sign-out`)),
  signOutOthers: async () => signOutAnswer(await post('/api/devices/sign-out-others')),
  setFunction: async (id: string, kind: 'notifications' | 'schedule', on: boolean) => {
    await call(`/api/devices/${encodeURIComponent(id)}/${kind}`, { method: 'PUT', body: { on } });
  },
  revokeHealth: async (id: string) => { await post(`/api/devices/${encodeURIComponent(id)}/health/revoke`); },
  revokeUnlinkedHealth: async (id: string) => { await post(`/api/devices/unlinked-health/${encodeURIComponent(id)}/revoke`); },

  startClaim: async (locale: string) => claimForm(await post('/api/remote-access/claims', { locale })),
  claim: async (id: string) => claimState(await get(`/api/remote-access/claims/${encodeURIComponent(id)}`)),
  cancelClaim: async (id: string) => { await post(`/api/remote-access/claims/${encodeURIComponent(id)}/cancel`); },

  connection: async (interfaces: TunnelNetworkInterface[]): Promise<ConnectionState | null> =>
    connectionFromWire(await get('/api/devices/connection'), interfaces),
  /**
   * `202`: accepted, not applied; the result is read from the state. `operationId` names the
   * change the instance took on for this setting, when it took one on: how it ended is the
   * outcome that carries the same id.
   */
  changeConnection: async (setting: ConnectionSetting, to: RemoteAccessSettings[keyof RemoteAccessSettings] | boolean): Promise<{ operationId: string | null }> => {
    const { body } = await call('/api/devices/connection', { method: 'PATCH', body: { setting, to } });
    const operation = body.operation && typeof body.operation === 'object' ? body.operation as Record<string, unknown> : {};
    return { operationId: operation.setting === setting && typeof operation.id === 'string' ? operation.id : null };
  },
  interfaces: async (): Promise<TunnelNetworkInterface[]> => {
    const body = await get<{ interfaces?: TunnelNetworkInterface[] }>('/api/remote-access/network-interfaces');
    return Array.isArray(body.interfaces) ? body.interfaces : [];
  },
  /** A refusal (`409`) carries the reason and when to try again: it is an answer, not a failure. */
  optimizeRoute: async (): Promise<RemoteAccessStatus> => {
    try {
      return await post<RemoteAccessStatus>('/api/remote-access/optimize-route');
    } catch (error) {
      if (error instanceof DevicesApiError && error.status === 409) return { ...error.body, ok: false } as RemoteAccessStatus;
      throw error;
    }
  },
  diagnose: () => post<TunnelConnectivityDiagnostics>('/api/remote-access/diagnostics'),

  address: async (): Promise<AddressState | null> => addressFromWire(await get('/api/instance-address')),
  previewRename: async (prefix: string, selfId?: string): Promise<RenamePreview> => {
    const body = await post('/api/instance-address/preview', { prefix });
    if (typeof body.confirmation_id !== 'string' || typeof body.hostname !== 'string') throw new LostAnswer();
    return {
      confirmationId: body.confirmation_id,
      hostname: body.hostname,
      isReturn: body.is_return === true,
      holdDays: typeof body.hold_days === 'number' ? body.hold_days : undefined,
      releaseAt: typeof body.release_at_if_changed_now === 'string' ? body.release_at_if_changed_now : undefined,
      healthConnected: body.health_connected === true,
      devices: (Array.isArray(body.devices) ? body.devices : []).map((each) => renameDeviceFromWire(each, selfId))
        .filter((each): each is RenameDevice => each !== null),
    };
  },
  /** `applied` with the new address, or `pending`: the page then polls the address state. */
  changeAddress: async (confirmationId: string): Promise<{ state: 'applied'; hostname: string; publicUrl: string } | { state: 'pending' }> => {
    const body = await post('/api/instance-address/change', { confirmation_id: confirmationId });
    return body.state === 'applied' && typeof body.hostname === 'string' && typeof body.public_url === 'string'
      ? { state: 'applied', hostname: body.hostname, publicUrl: body.public_url }
      : { state: 'pending' };
  },

  rebind: async (): Promise<RebindState> => rebindFromWire(await get('/api/remote-access/rebind')),
  /**
   * Whether this instance has binding again at all (a build before it answers 404). True only on
   * the instance's own word, an overview or one of its refusals; no such route, an answer that is
   * not its JSON, or no answer, is not that word, so nothing is said that points there.
   */
  rebindOffered: async (): Promise<boolean> => {
    try {
      await get('/api/remote-access/rebind');
      return true;
    } catch (error) {
      // Only the overview's own refusals: the role gate answers a member before any route is
      // looked up, and the tunnel answers for an instance it cannot reach.
      return error instanceof DevicesApiError && rebindUnavailable(error.code) !== null;
    }
  },
  startRebind: async (locale: string) => claimForm(await post('/api/remote-access/rebind/claims', { locale })),
  rebindClaim: async (id: string) => claimState(await get(`/api/remote-access/rebind/claims/${encodeURIComponent(id)}`)),
  cancelRebind: async (id: string) => { await post(`/api/remote-access/rebind/claims/${encodeURIComponent(id)}/cancel`); },
  previewReset: async (id: string): Promise<{ confirmationId: string; healthConnected: boolean }> => {
    const body = await post(`/api/remote-access/rebind/claims/${encodeURIComponent(id)}/reset/preview`);
    if (typeof body.confirmation_id !== 'string') throw new LostAnswer();
    return { confirmationId: body.confirmation_id, healthConnected: body.health_connected === true };
  },
  /** `unpaired` only when the reset finished; `resetting` when Avibe carries on with it. */
  reset: async (id: string, confirmationId: string): Promise<'unpaired' | 'resetting'> => {
    const body = await post(`/api/remote-access/rebind/claims/${encodeURIComponent(id)}/reset`, { confirmation_id: confirmationId });
    return body.state === 'unpaired' ? 'unpaired' : 'resetting';
  },
};

export type DevicesApi = typeof devicesApi;
export type { DeviceFunction };
