// The instance's answers for the Devices pages, as they come over HTTP, and their mapping onto the
// views' own shapes (`./types.ts`). Contracts: docs/plans/instance-device-registry.md,
// device-functions-activity.md, device-account-session-sign-out.md, instance-address-change.md,
// personal-installation-claims-runtime.md, devices-connection-settings.md,
// local-remote-access-rebind.md. An unknown field is ignored; an unknown value becomes `unknown`.
import type { RemoteAccessSettings, RemoteAccessStatus } from '@/context/ApiContext';
import { getTunnelQualityDisplayState, getTunnelRequestPathDisplayState } from '@/lib/tunnelQuality';
import type {
  AccountResult, AddressState, ClaimProblem, ConnectionState, Device, DeviceActivity, DeviceFunction, DeviceKind,
  DirectState, HealthData, ProxyAccess, RebindRefusal, RebindState, RelayState, RenameDevice, RenameRefusal, RetiredName,
  SignedOutDevice, SwitchSetting, TunnelChange, TunnelSetting, TunnelStatus, UnlinkedHealth,
} from './types';

const HEALTH: readonly HealthData[] = ['steps', 'activity', 'workouts', 'sleep'];
const record = (value: unknown): Record<string, unknown> => (value && typeof value === 'object' ? value as Record<string, unknown> : {});
const text = (value: unknown): string | undefined => (typeof value === 'string' && value ? value : undefined);
const list = (value: unknown): unknown[] => (Array.isArray(value) ? value : []);
const health = (value: unknown): HealthData[] => HEALTH.filter((each) => list(value).includes(each));
/** Unix seconds, or an ISO instant, as an ISO instant. */
const instant = (value: unknown): string | undefined => {
  if (typeof value === 'number' && Number.isFinite(value) && value > 0) return new Date(value * 1000).toISOString();
  if (typeof value === 'string' && Number.isFinite(Date.parse(value))) return new Date(Date.parse(value)).toISOString();
  return undefined;
};

// ── /api/devices ────────────────────────────────────────────────────────────────────────────────

const KINDS: Record<string, { kind: DeviceKind; client: Device['client']['kind'] }> = {
  host: { kind: 'host', client: 'runtime' },
  iphone_app: { kind: 'iphone', client: 'app' },
  desktop_app: { kind: 'desktop', client: 'desktop' },
  browser: { kind: 'browser', client: 'browser' },
  home_screen_app: { kind: 'browser', client: 'homeScreen' },
};

function deviceFunction(raw: unknown): DeviceFunction | null {
  const fn = record(raw);
  const canChange = fn.can_change === true;
  switch (fn.kind) {
    case 'notifications':
    case 'schedule':
      return typeof fn.on === 'boolean' ? { kind: fn.kind, on: fn.on, canChange } : null;
    case 'health':
      return typeof fn.on === 'boolean'
        ? { kind: 'health', on: fn.on, canChange, data: health(fn.data), syncedAt: instant(fn.syncedAt) }
        : null;
    case 'computerUse':
      return {
        kind: 'computerUse',
        status: fn.status === 'ready' || fn.status === 'needs_permission' || fn.status === 'off' ? fn.status : 'unavailable',
      };
    default:
      return null;
  }
}

/** A kind this page does not know is still a device that can open this Avibe: it is listed plainly. */
const OTHER_KIND = { kind: 'other', client: 'other' } as const;

/** `listed`: the list of the answer the row came in, which names its group where the row does not. */
export function deviceFromWire(raw: unknown, listed?: Device['group']): Device | null {
  const row = record(raw);
  const id = text(row.id);
  const kind = KINDS[String(row.kind)] ?? OTHER_KIND;
  const group = row.group === 'mine' || row.group === 'others' || row.group === 'unattributed' ? row.group : listed ?? null;
  if (!id || !group) return null;
  const path = record(row.current_path);
  const pathKind = path.kind === 'tunnel' || path.kind === 'lan' || path.kind === 'punched' || path.kind === 'relay' ? path.kind : null;
  return {
    id,
    group,
    kind: kind.kind,
    name: text(row.display_name) ?? null,
    client: { kind: kind.client, version: text(row.app_version), platform: text(row.system_version) },
    self: row.current === true || undefined,
    // A path is said only while a connection is observed; a valid sign-in alone is not one.
    online: pathKind ? {
      path: pathKind,
      family: path.family === 'ipv4' || path.family === 'ipv6' ? path.family : undefined,
      ms: typeof path.rtt_ms === 'number' ? Math.round(path.rtt_ms) : undefined,
    } : undefined,
    lastActiveAt: instant(row.last_active_at),
    signedInAt: instant(row.signed_in_at),
    functions: list(row.functions).map(deviceFunction).filter((each): each is DeviceFunction => each !== null),
    account: text(record(row.account).email),
    person: group === 'others' ? text(record(row.account).email) : undefined,
    pushOnly: row.push_only === true || undefined,
    // The instance's own answer, on the host row only, and only an exact `true` or `false`: null
    // (not known, or not Avibe's to close) and a missing field both leave the row silent.
    networkOpen: id === 'host' && kind.kind === 'host' && typeof row.network_open === 'boolean' ? row.network_open : undefined,
  };
}

function accountResult(raw: unknown): AccountResult {
  const { status, reason } = record(raw);
  if (status === 'pending') return reason === 'retrying' ? 'retrying' : 'queued';
  if (status === 'accepted') return 'accepted';
  if (status === 'not_applicable') return reason === 'shared_sign_in' ? 'sharedSignIn' : reason === 'keeper_unknown' ? 'keeperUnknown' : 'localOnly';
  if (status === 'refused') return reason === 'credential' ? 'refusedCredential' : 'refused';
  if (status === 'unconfirmed') return 'unconfirmed';
  return 'unknown';
}

export function signedOutFromWire(raw: unknown): SignedOutDevice | null {
  const row = record(raw);
  const id = text(row.operation_id);
  if (!id) return null;
  const results = [...new Set(list(row.account_results).map(accountResult))];
  return {
    id,
    deviceId: text(row.device_id),
    device: text(row.display_name) ?? null,
    kind: KINDS[String(row.kind)]?.kind ?? null,
    // The instance says whose it was when it can; nothing here is inferred from a name or a kind.
    owner: row.group === 'mine' || row.group === 'others' ? row.group : 'unknown',
    results,
  };
}

/** The declared-proxy fact as the instance computed it. Nothing is inferred: without at least one
 *  origin and a scope this page knows, it says nothing. */
function proxyAccessFromWire(raw: unknown): ProxyAccess | undefined {
  const fact = record(raw);
  const origins = list(fact.origins).filter((each): each is string => typeof each === 'string' && each.length > 0);
  if (origins.length === 0 || (fact.peer_scope !== 'single_address' && fact.peer_scope !== 'range')) return undefined;
  return { origins, peerScope: fact.peer_scope };
}

export type DevicesSnapshot = {
  devices: Device[];
  unlinkedHealth: UnlinkedHealth[];
  signedOut: SignedOutDevice[];
  bulk: 'other_sessions' | 'all_remote' | null;
  proxyAccess?: ProxyAccess;
  /** 「解除绑定」 as the instance reports it to the owner (`unbind`, for the current binding only). */
  unbind?: UnbindReport;
};

/** `unbinding`: running, continues by itself. `unbound`: done. `notUnbound`: avibe.bot refused it,
 *  as not eligible or because this computer's key no longer works. `repair`: only this computer can
 *  sort it out. */
export type UnbindReport =
  | { state: 'unbinding' | 'repair' }
  /** Done: the name it had and until when it stays the account's, where the instance says. */
  | { state: 'unbound'; host?: string; releaseAt?: string }
  | { state: 'notUnbound'; reason: 'ineligible' | 'invalidCredential' };

export function unbindFromWire(raw: unknown): UnbindReport | undefined {
  const body = record(raw);
  switch (body.state) {
    case 'unbinding':
      return { state: 'unbinding' };
    case 'unbound': {
      const host = text(body.host) ?? undefined;
      const releaseAt = instant(body.release_at) ?? undefined;
      return { state: 'unbound', ...(host ? { host } : {}), ...(releaseAt ? { releaseAt } : {}) };
    }
    case 'reset_repair_required':
      return { state: 'repair' };
    case 'not_unbound':
      return body.reason_code === 'ineligible' ? { state: 'notUnbound', reason: 'ineligible' }
        : body.reason_code === 'invalid_credential' ? { state: 'notUnbound', reason: 'invalidCredential' } : undefined;
    default:
      return undefined;
  }
}

export function devicesFromWire(raw: unknown): DevicesSnapshot {
  const body = record(raw);
  const devices = (['mine', 'others', 'unattributed'] as const)
    .flatMap((group) => list(body[group]).map((each) => deviceFromWire(each, group)))
    .filter((each): each is Device => each !== null);
  return {
    devices,
    unlinkedHealth: list(body.unlinked_health).flatMap((each) => {
      const row = record(each);
      const id = text(row.pairing_id);
      return id ? [{ id, device: text(row.device_name) ?? '', data: health(row.data), lastSeenAt: instant(row.last_seen_at) }] : [];
    }),
    signedOut: list(body.recent_sign_outs).map(signedOutFromWire).filter((each): each is SignedOutDevice => each !== null),
    bulk: body.bulk_action === 'other_sessions' || body.bulk_action === 'all_remote' ? body.bulk_action : null,
    proxyAccess: proxyAccessFromWire(body.unauthenticated_proxy_access),
    unbind: unbindFromWire(body.unbind),
  };
}

/** One entry of `/api/devices/activity`, with the device's name looked up where it is still known. */
export function activityFromWire(raw: unknown, nameOf: (deviceId: string | undefined) => string | null): DeviceActivity | null {
  const row = record(raw);
  const id = text(row.id);
  const at = instant(row.at);
  if (!id || !at) return null;
  const deviceId = text(row.device_id);
  const base = { id, at, deviceId, device: nameOf(deviceId) };
  switch (row.kind) {
    case 'signedIn':
      return { ...base, event: { kind: 'signedIn' } };
    case 'signedOut': {
      const actor = text(row.actor_device_id);
      return { ...base, event: { kind: 'signedOut', actor: actor ? nameOf(actor) : null } };
    }
    case 'healthRead': {
      const fromDay = text(row.from_day);
      const toDay = text(row.to_day);
      return fromDay && toDay
        ? { ...base, event: { kind: 'healthRead', data: health(row.data), fromDay, toDay, task: text(row.task) } }
        : null;
    }
    case 'alarmCreated':
    case 'reminderCreated':
      return { ...base, event: { kind: row.kind, confirmed: row.confirmed === true, label: text(row.label) } };
    case 'remoteAccessOn':
    case 'remoteAccessOff':
      return { ...base, event: { kind: row.kind } };
    default:
      // An event this page does not know is left out of the log rather than misnamed.
      return null;
  }
}

// ── /api/remote-access/status ───────────────────────────────────────────────────────────────────

/**
 * The tunnel, said only as strongly as the status supports. "Not connected" needs evidence that
 * there is no connection: the connector is not running, it registered none while its metrics were
 * read, or requests through it are failing. A connector this page itself started a moment ago
 * (`justStarted`) gives no such evidence: its first samples carry no reading, or no connection
 * yet, and it is connecting. At any other time a sample with no reading is unknown. Slow, short of its connections
 * or still settling is a connected tunnel; connected with no round trip measured yet is connected.
 * A connector whose process or metrics cannot be read, or a sample too old to speak for now, is
 * unknown.
 */
export function tunnelFromStatus(status: RemoteAccessStatus | null, now: number = Date.now(), justStarted = false): TunnelStatus {
  if (!status || !status.paired || status.pid_state === 'unknown') return { state: 'unknown' };
  if (!status.running) return { state: 'down' };
  const quality = status.tunnel_quality;
  if (!quality) return { state: justStarted ? 'connecting' : 'unknown' };
  if (!(now - Date.parse(quality.sampled_at) <= 150_000)) return { state: 'unknown' };
  const effective = quality.transport?.effective || quality.protocol;
  const active = effective === 'quic' || effective === 'http2' ? effective : null;
  const connections = quality.ha_connections ?? 0;
  // Nothing read from the connector: just after this page started it, that is the start; at any
  // other time its metrics cannot be read (they may never be), which is said as it is.
  if (quality.state === 'unknown' && connections === 0) return { state: justStarted ? 'connecting' : 'unknown', active };
  if (connections === 0) return { state: justStarted ? 'connecting' : 'down', active };
  if (getTunnelRequestPathDisplayState(quality.request_path) === 'unavailable') return { state: 'down', active };
  // Its metrics could not be read for a while: the connections it last had say nothing about now.
  if (quality.state === 'unknown') return { state: 'unknown', active };
  // An optimization that is running (`recovering`) is not a fault: the tunnel is connected and a
  // better route is being evaluated, which is what the page says.
  const grade = getTunnelQualityDisplayState(quality, true);
  const latency = quality.request_path?.latency_ms?.p95 ?? quality.rtt_ms?.median;
  return {
    state: grade === 'good' || grade === 'fair' ? 'up'
      : grade === 'poor' || grade === 'critical' ? 'slow'
        : grade === 'degraded' ? 'unsteady'
          : grade === 'recovering' ? 'optimizing' : 'connected',
    ms: typeof latency === 'number' ? Math.round(latency) : undefined,
    active,
  };
}

// ── claims ──────────────────────────────────────────────────────────────────────────────────────

const CLAIM_CODES = new Set([
  'link_required', 'expired', 'declined', 'pairing_recovery_required', 'fresh_claim_required', 'name_taken',
  'binding_unavailable', 'paired_installation_loopback_required', 'active_pairing_recent', 'local_binding_changed',
  'invalid_return',
]);

/** A claim's local outcome name (never the hosted user code) as what the page says. */
// The confirming account's address as the instance checked it: printable, no spaces, quotes or
// angle brackets, one `@` and a dotted domain. Anything else is the unexplained refusal.
const CONFIRMING_EMAIL = /^[^\s@<>"]{1,64}@[^\s@<>"]+\.[^\s@<>".]+$/;
// Control, zero-width, direction and other invisible format characters (Unicode Cc and Cf).
const unprintable = (value: string) => [...value].some((c) => /\p{Cc}|\p{Cf}/u.test(c));
const confirmingEmail = (value: string | undefined) =>
  value && value.length <= 254 && CONFIRMING_EMAIL.test(value) && !unprintable(value)
    && !value.split('@')[1].includes('..') ? value : undefined;

export function claimProblem(
  code: string | undefined,
  extra: { claimId?: string; seconds?: number; confirmingEmail?: string } = {},
): ClaimProblem {
  if (code === 'other_account') {
    const email = confirmingEmail(extra.confirmingEmail);
    return email ? { code, email } : { code: 'binding_unavailable' };
  }
  if (code === 'claim_in_progress') return { code, claimId: extra.claimId };
  if (code === 'rate_limited') return { code, seconds: extra.seconds };
  if (code === 'control_plane_unreachable') return { code: 'unreachable' };
  if (code === 'cancelled') return { code: 'declined' };
  if (code === 'claim_unavailable') return { code: 'expired' };
  if (code && CLAIM_CODES.has(code)) return { code } as ClaimProblem;
  return { code: 'unknown', raw: code ?? 'unknown' };
}

// ── /api/devices/connection ─────────────────────────────────────────────────────────────────────

const TUNNEL_SETTINGS: readonly TunnelSetting[] = ['protocol', 'ipFamily', 'outbound'];
const tunnelSetting = (value: unknown): TunnelSetting | null => (TUNNEL_SETTINGS.includes(value as TunnelSetting) ? value as TunnelSetting : null);
const DIRECT: readonly DirectState[] = ['ready', 'off', 'awaiting-permission', 'denied', 'awaiting-certificate', 'not-running'];
const value = (raw: unknown): string => (typeof raw === 'string' ? raw : typeof raw === 'boolean' ? String(raw) : '');

function tunnelChange(operation: unknown, outcome: unknown): TunnelChange | undefined {
  const running = record(operation);
  const setting = tunnelSetting(running.setting);
  if (setting) {
    const to = 'to' in running ? value(running.to) : undefined;
    if (running.state === 'applying' || running.state === 'verifying') return { setting, state: running.state, to };
    if (running.state === 'restoring') return { setting, state: 'restoring', from: value(running.from), to };
    return { setting, state: 'unknown', to };
  }
  const last = record(outcome);
  const done = tunnelSetting(last.setting);
  if (!done) return undefined;
  const from = value(last.from);
  const to = 'to' in last ? value(last.to) : undefined;
  switch (last.result) {
    case 'applied':
      return { setting: done, state: 'applied', to, saved: last.reason === 'saved_without_connector' || undefined };
    case 'superseded':
      return { setting: done, state: 'superseded', to };
    case 'restored':
      return {
        setting: done, state: 'restored', from, to,
        reason: last.reason === 'verification_failed' || last.reason === 'verification_unavailable' ? last.reason : 'unknown',
      };
    case 'failed': {
      const reasons = ['saved_restored_runtime_pending', 'storage_restore_failed', 'candidate_not_ready', 'connection_change_busy', 'settings_not_applied'] as const;
      const reason = reasons.find((each) => each === last.reason) ?? 'unknown';
      return { setting: done, state: 'failed', from, to, reason };
    }
    default:
      return { setting: done, state: 'unknown', to };
  }
}

const switchSetting = (raw: unknown): SwitchSetting | null => (raw === 'direct' || raw === 'relay' ? raw : null);

/** Direct and the relay are written after the instance has answered: its operation says a change
 *  is still on its way, its outcome how the last one ended. */
function switchChange(operation: unknown, outcome: unknown): Pick<ConnectionState, 'switching' | 'switched'> {
  const running = record(operation);
  const last = record(outcome);
  const now = switchSetting(running.setting);
  const done = switchSetting(last.setting);
  return {
    switching: now ? { setting: now, id: text(running.id) } : undefined,
    switched: done ? { setting: done, id: text(last.id), failed: last.result === 'failed', reason: text(last.reason) ?? 'unknown' } : undefined,
  };
}

function relayFromWire(raw: unknown): RelayState | undefined {
  if (!raw || typeof raw !== 'object') return undefined;
  const relay = record(raw);
  const reason = text(relay.unavailableReason);
  return {
    enabled: relay.enabled === true,
    connected: relay.connected === true,
    unavailable: relay.canEnable === false && relay.enabled !== true
      ? reason === 'direct_permission_required' || reason === 'relay_transport_unavailable' ? reason : 'unknown'
      : undefined,
  };
}

export function connectionFromWire(raw: unknown, interfaces: ConnectionState['interfaces']): ConnectionState | null {
  const body = record(raw);
  const settings = record(body.settings);
  if (typeof settings.transport_protocol !== 'string') return null;
  const returned = record(body.returnedToAutomatic);
  const temporary = record(body.temporaryRoute);
  const returnedAt = instant(returned.at);
  return {
    settings: settings as unknown as RemoteAccessSettings,
    interfaces,
    direct: DIRECT.find((each) => each === body.direct) ?? 'unknown',
    relay: relayFromWire(body.relay),
    change: tunnelChange(body.operation, body.outcome),
    ...switchChange(body.operation, body.outcome),
    returnedToAutomatic: returnedAt ? {
      at: returnedAt,
      changes: list(returned.changes).flatMap((each) => {
        const change = record(each);
        const setting = tunnelSetting(change.setting);
        return setting ? [{ setting, from: value(change.from) }] : [];
      }),
    } : undefined,
    temporaryRoute: body.temporaryRoute ? {
      protocol: temporary.protocol === 'quic' || temporary.protocol === 'http2' ? temporary.protocol : null,
      differences: list(temporary.differences).map(tunnelSetting).filter((each): each is TunnelSetting => each !== null),
    } : undefined,
  };
}

// ── /api/instance-address ───────────────────────────────────────────────────────────────────────

const RENAME_CODES = new Set([
  'taken', 'invalid_prefix', 'same_address', 'confirmation_expired', 'operation_in_progress', 'address_changed',
  'pairing_invalid', 'remote_access_configuration_mismatch', 'rebind_required', 'unavailable', 'owner_required', 'rename_ineligible',
]);

export function renameRefusal(body: unknown): RenameRefusal {
  const error = record(body);
  const code = text(error.error) ?? 'unknown';
  if (code === 'name_holding_limit_reached') {
    return { code, held: typeof error.held_count === 'number' ? error.held_count : undefined, earliestReleaseAt: instant(error.earliest_release_at) };
  }
  return RENAME_CODES.has(code) ? { code } as RenameRefusal : { code: 'unknown', raw: code };
}

export function renameDeviceFromWire(raw: unknown, selfId?: string): RenameDevice | null {
  const row = record(raw);
  const id = text(row.id);
  if (!id) return null;
  const kind = KINDS[String(row.kind)];
  return {
    id,
    name: text(row.display_name) ?? null,
    kind: row.kind === 'home_screen_app' ? 'homeScreen' : kind?.kind ?? 'other',
    self: id === selfId || undefined,
    pushOnly: row.push_only === true || undefined,
    account: text(record(row.account).email),
    cleanup: row.state === 'cleared' ? 'cleared' : row.state === 'unconfirmed' ? 'unconfirmed' : undefined,
    lastActiveAt: instant(row.last_active_at),
  };
}

function retiredFromWire(raw: unknown): RetiredName | null {
  const row = record(raw);
  const hostname = text(row.hostname);
  if (!hostname) return null;
  return {
    hostname,
    releaseAt: instant(row.release_at),
    devices: list(row.devices).map((each) => renameDeviceFromWire(each)).filter((each): each is RenameDevice => each !== null),
  };
}

export function addressFromWire(raw: unknown): AddressState | null {
  const body = record(raw);
  const hostname = text(body.hostname);
  if (!hostname) return null;
  const operation = record(body.operation);
  const change = record(body.unrecognized_change);
  const operationState = operation.state === 'pending' || operation.state === 'applying' || operation.state === 'failed' ? operation.state : null;
  return {
    hostname,
    eligible: body.eligible === true,
    ineligibleReason: text(body.ineligible_reason),
    stateError: body.state_error === 'rebind_required' ? 'rebind_required' : undefined,
    holdDays: typeof body.hold_days === 'number' ? body.hold_days : undefined,
    releaseAtIfChangedNow: instant(body.release_at_if_changed_now),
    operation: operationState && text(operation.hostname) ? {
      state: operationState,
      hostname: text(operation.hostname)!,
      // A refused change carries its extra fields (the holding limit's numbers) beside its code.
      error: operation.error ? renameRefusal(operation) : undefined,
    } : undefined,
    unrecognizedChange: text(change.hostname) ? { previousHostname: text(change.previous_hostname) ?? '', hostname: text(change.hostname)! } : undefined,
    retired: list(body.cleanup).map(retiredFromWire).filter((each): each is RetiredName => each !== null),
    healthConnected: record(body.health).connected === true,
  };
}

// ── /api/remote-access/rebind ───────────────────────────────────────────────────────────────────

const REBIND_CODES = new Set([
  'active_pairing_recent', 'binding_unavailable', 'issuer_replay_refused', 'expired', 'declined',
  'local_binding_changed', 'operation_in_progress', 'claim_in_progress', 'confirmation_expired', 'invalid_return',
]);

export function rebindRefusal(code: string | undefined, seconds?: number): RebindRefusal {
  if (code === 'rate_limited') return { code, seconds };
  if (code === 'control_plane_unreachable') return { code: 'unreachable' };
  if (code === 'cancelled') return { code: 'declined' };
  if (code === 'claim_unavailable') return { code: 'expired' };
  if (code && REBIND_CODES.has(code)) return { code } as RebindRefusal;
  return { code: 'unknown', raw: code ?? 'unknown' };
}

/** The bind-again overview as what the page may say and offer. Only the instance's own
 *  `next_action: confirm_as_new` offers binding as a new computer; nothing else unlocks it. */
export function rebindFromWire(raw: unknown): RebindState {
  const body = record(raw);
  const attempt = record(body.attempt);
  const reason = text(body.reason_code);
  switch (body.binding_state) {
    case 'unpaired':
      return { state: 'unpaired' };
    case 'pairing_recovery_required':
      return { state: 'recovering' };
    case 'resetting':
      return { state: 'resetting' };
    case 'reset_repair_required':
      return { state: 'needsTerminal' };
    case 'bound':
    case 'binding_rejected': {
      if (body.next_action === 'confirm_as_new' && (reason === 'predecessor_unrecoverable' || reason === 'other_account')) {
        return { state: 'asNewOffered', reason, hint: reason === 'other_account' ? text(attempt.owner_hint) : undefined };
      }
      if (body.unavailable_reason === 'operation_in_progress') return { state: 'busy' };
      const attemptCode = attempt.state === 'error' ? text(attempt.code) : undefined;
      // A refusal with a reason this page knows keeps its sentence; one that needs a qualifying
      // reason it lacks (`installation_unverified` with nothing offered) is said as unexplained.
      const refusal = attemptCode && attemptCode !== 'issuer_replay_refused'
        ? rebindRefusal(attemptCode === 'installation_unverified' ? (reason === 'active_pairing_recent' ? reason : 'binding_unavailable') : attemptCode,
          typeof attempt.retry_after_seconds === 'number' ? attempt.retry_after_seconds : undefined)
        : undefined;
      return {
        state: 'ready',
        rejected: body.binding_state === 'binding_rejected' || undefined,
        tunnelRunning: body.connector_still_running === true || undefined,
        replayRefused: reason === 'issuer_replay_refused' || undefined,
        refusal,
      };
    }
    default:
      return { state: 'unknown' };
  }
}

/** The overview's own refusals that mean "this page cannot do it": a damaged or unreadable binding
 *  record is the terminal command's to handle, and anywhere but this computer it is said as such. */
export function rebindUnavailable(code: string): RebindState | null {
  if (code === 'direct_loopback_required' || code === 'local_operator_required') return { state: 'remote' };
  // The three ways this computer's own record of its binding cannot be read: the terminal's.
  if (code === 'local_binding_unavailable' || code === 'local_identity_unavailable' || code === 'local_enrollment_unavailable') {
    return { state: 'needsTerminal' };
  }
  // Any other refusal is not the overview's own word: the role gate in front of every route
  // (`instance_access_forbidden`), the tunnel's own 502, a build that has no such route. None of
  // them says that this instance has binding again.
  return null;
}
