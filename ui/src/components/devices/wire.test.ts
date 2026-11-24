import { describe, expect, it } from 'vitest';

import type { RemoteAccessStatus } from '@/context/ApiContext';
import {
  activityFromWire, addressFromWire, claimProblem, connectionFromWire, pairingHold, devicesFromWire, rebindFromWire, rebindRefusal, rebindUnavailable,
  renameRefusal, signedOutFromWire, tunnelFromStatus,
} from './wire';

// Contracts: docs/plans/instance-device-registry.md, device-functions-activity.md,
// device-account-session-sign-out.md, devices-connection-settings.md, instance-address-change.md,
// local-remote-access-rebind.md. An unknown value is carried as unknown, never as success.

const row = (extra: Record<string, unknown>) => ({
  id: 'd1', group: 'mine', kind: 'browser', display_name: 'Chrome', account: { email: 'max@example.test' }, functions: [], ...extra,
});

describe('devicesFromWire', () => {
  it('maps the registry kinds, the current device and the lists as the instance names them', () => {
    const snapshot = devicesFromWire({
      mine: [
        row({ id: 'host', kind: 'host', current: true }),
        row({ id: 'phone', kind: 'iphone_app', app_version: '3.2.8', system_version: 'iOS 26.1' }),
        row({ id: 'desk', kind: 'desktop_app' }),
        row({ id: 'pwa', kind: 'home_screen_app', display_name: null }),
      ],
      others: [row({ id: 'sara', group: 'others', account: { email: 'sara@example.test' } })],
      unattributed: [row({ id: 'push', group: 'unattributed', account: null, push_only: true })],
      bulk_action: 'all_remote',
    });
    const by = Object.fromEntries(snapshot.devices.map((each) => [each.id, each]));
    expect(by.host).toMatchObject({ kind: 'host', client: { kind: 'runtime' }, self: true, group: 'mine' });
    expect(by.phone).toMatchObject({ kind: 'iphone', client: { kind: 'app', version: '3.2.8', platform: 'iOS 26.1' } });
    expect(by.desk).toMatchObject({ kind: 'desktop', client: { kind: 'desktop' } });
    expect(by.pwa).toMatchObject({ kind: 'browser', client: { kind: 'homeScreen' }, name: null });
    expect(by.sara).toMatchObject({ group: 'others', person: 'sara@example.test' });
    expect(by.push).toMatchObject({ group: 'unattributed', pushOnly: true });
    expect(by.host.person).toBeUndefined();
    expect(snapshot.bulk).toBe('all_remote');
  });

  it('says a path only when the instance observed a connection', () => {
    const { devices } = devicesFromWire({
      mine: [
        row({ id: 'a', current_path: { kind: 'tunnel', family: null, rtt_ms: 120.4 } }),
        row({ id: 'b', current_path: null, last_active_at: 1_795_000_000 }),
        row({ id: 'c', current_path: { kind: 'teleport' } }),
      ],
    });
    expect(devices[0].online).toEqual({ path: 'tunnel', family: undefined, ms: 120 });
    expect(devices[1].online).toBeUndefined();
    expect(devices[1].lastActiveAt).toBe(new Date(1_795_000_000_000).toISOString());
    // A path this page does not know is not a connection it may name.
    expect(devices[2].online).toBeUndefined();
  });

  it('takes whether a function may be changed from the instance, and drops what it does not know', () => {
    const { devices } = devicesFromWire({
      mine: [row({ functions: [
        { kind: 'notifications', on: true, can_change: true },
        { kind: 'health', on: true, can_change: false, data: ['steps', 'heart'], syncedAt: '2026-11-21T01:00:00Z' },
        { kind: 'computerUse', on: true, fixed: true, status: 'surprising' },
        { kind: 'teleport', on: true, can_change: true },
        { kind: 'schedule', can_change: true },
      ] })],
    });
    expect(devices[0].functions).toEqual([
      { kind: 'notifications', on: true, canChange: true },
      { kind: 'health', on: true, canChange: false, data: ['steps'], syncedAt: '2026-11-21T01:00:00.000Z' },
      { kind: 'computerUse', status: 'unavailable' },
    ]);
  });

  it('never drops a device for a kind or a group it does not know: it is listed as a plain device', () => {
    // This list answers "which devices can open my Avibe": a row is left out only without an id.
    const { devices } = devicesFromWire({
      mine: [row({ id: '' }), row({ id: 'watch', kind: 'watch' }), row({ id: 'odd', group: 'elsewhere' })],
      others: [row({ id: 'theirs', group: undefined })],
    });
    expect(devices.map((each) => [each.id, each.kind, each.client.kind, each.group])).toEqual([
      ['watch', 'other', 'other', 'mine'],
      // A group the row does not name in words this page knows is the list it came in.
      ['odd', 'browser', 'browser', 'mine'],
      ['theirs', 'browser', 'browser', 'others'],
    ]);
    expect(devicesFromWire(null)).toEqual({ devices: [], unlinkedHealth: [], signedOut: [], bulk: null });
  });

  it('lists unproven Health pairings apart, by their own id', () => {
    const snapshot = devicesFromWire({ unlinked_health: [{ pairing_id: 'p1', device_name: 'iPhone 13', data: ['sleep'], last_seen_at: '2026-11-01T00:00:00Z' }, { device_name: 'no id' }] });
    expect(snapshot.unlinkedHealth).toEqual([{ id: 'p1', device: 'iPhone 13', data: ['sleep'], lastSeenAt: '2026-11-01T00:00:00.000Z' }]);
  });
});

describe('signedOutFromWire', () => {
  const receipt = (results: unknown[], extra: Record<string, unknown> = {}) => signedOutFromWire({ operation_id: 'o1', display_name: 'Chrome', kind: 'browser', account_results: results, ...extra });

  it('never reads an accepted request as an ended sign-in, and keeps what it does not know unknown', () => {
    expect(receipt([{ status: 'pending', reason: 'queued' }])?.results).toEqual(['queued']);
    expect(receipt([{ status: 'pending', reason: 'retrying' }])?.results).toEqual(['retrying']);
    expect(receipt([{ status: 'accepted', reason: null }])?.results).toEqual(['accepted']);
    expect(receipt([{ status: 'not_applicable', reason: 'no_reference' }, { status: 'not_applicable', reason: 'not_eligible' }, { status: 'not_applicable', reason: 'binding_changed' }])?.results).toEqual(['localOnly']);
    expect(receipt([{ status: 'not_applicable', reason: 'shared_sign_in' }])?.results).toEqual(['sharedSignIn']);
    expect(receipt([{ status: 'not_applicable', reason: 'keeper_unknown' }])?.results).toEqual(['keeperUnknown']);
    expect(receipt([{ status: 'refused', reason: 'credential' }])?.results).toEqual(['refusedCredential']);
    expect(receipt([{ status: 'refused', reason: 'protocol' }])?.results).toEqual(['refused']);
    expect(receipt([{ status: 'unconfirmed', reason: 'retry_exhausted' }])?.results).toEqual(['unconfirmed']);
    expect(receipt([{ status: 'ended' }, {}])?.results).toEqual(['unknown']);
  });

  it('knows whose device it was only when the instance says so', () => {
    expect(receipt([], { group: 'mine' })?.owner).toBe('mine');
    expect(receipt([], { group: 'others' })?.owner).toBe('others');
    expect(receipt([], { group: null })?.owner).toBe('unknown');
    expect(receipt([])?.owner).toBe('unknown');
    expect(receipt([], { group: 'unattributed' })?.owner).toBe('unknown');
  });
});

describe('activityFromWire', () => {
  const nameOf = (id: string | undefined) => (id === 'phone' ? 'iPhone' : id === 'host' ? 'MacBook' : null);
  it('names a device only while it is still listed, and leaves out events it does not know', () => {
    expect(activityFromWire({ id: 'e1', at: '2026-11-21T01:00:00Z', device_id: 'gone', kind: 'signedOut', actor_device_id: 'host' }, nameOf))
      .toEqual({ id: 'e1', deviceId: 'gone', at: '2026-11-21T01:00:00.000Z', device: null, event: { kind: 'signedOut', actor: 'MacBook' } });
    expect(activityFromWire({ id: 'e2', at: '2026-11-21T01:00:00Z', device_id: 'phone', kind: 'healthRead', data: ['steps'], from_day: '2026-11-15', to_day: '2026-11-21' }, nameOf)?.event)
      .toEqual({ kind: 'healthRead', data: ['steps'], fromDay: '2026-11-15', toDay: '2026-11-21', task: undefined });
    expect(activityFromWire({ id: 'e3', at: '2026-11-21T01:00:00Z', device_id: 'phone', kind: 'reminderCreated', confirmed: false }, nameOf)?.event)
      .toEqual({ kind: 'reminderCreated', confirmed: false, label: undefined });
    expect(activityFromWire({ id: 'e4', at: '2026-11-21T01:00:00Z', kind: 'teleported' }, nameOf)).toBeNull();
    expect(activityFromWire({ id: 'e5', at: 'not a time', kind: 'signedIn' }, nameOf)).toBeNull();
  });
});

describe('tunnelFromStatus', () => {
  const NOW = Date.parse('2026-11-22T00:00:00Z');
  const sample = (extra: Record<string, unknown> = {}) => ({
    schema_version: 2, state: 'healthy', grade: 'good', sampled_at: new Date(NOW - 20_000).toISOString(), protocol: 'quic',
    connector_count: 1, ha_connections: 4, rtt_ms: { min: 90, median: 120, max: 160 }, ...extra,
  });
  const read = (status: Record<string, unknown>, now = NOW) => (
    tunnelFromStatus({ ok: true, enabled: true, paired: true, running: true, ...status } as unknown as RemoteAccessStatus, now)
  );

  it('says "not connected" only on evidence that there is no connection', () => {
    expect(read({ running: false }).state).toBe('down');
    expect(read({ tunnel_quality: sample({ ha_connections: 0, state: 'degraded', grade: 'critical' }) }).state).toBe('down');
    expect(read({ tunnel_quality: sample({ request_path: { status: 'unavailable', confidence: 'high', success_count: 0, sample_count: 12 } }) }).state).toBe('down');
  });

  it('reads a slow or unsteady tunnel as connected, never as unreachable', () => {
    // Latency alone makes `critical` and `poor`; `degraded` and `recovering` are a tunnel that is up.
    expect(read({ tunnel_quality: sample({ grade: 'critical', rtt_ms: { min: 380, median: 420, max: 520 } }) })).toEqual({ state: 'slow', ms: 420, active: 'quic' });
    expect(read({ tunnel_quality: sample({ grade: 'poor' }) }).state).toBe('slow');
    expect(read({ tunnel_quality: sample({ state: 'degraded', grade: 'fair', ha_connections: 2 }) }).state).toBe('unsteady');
    expect(read({ tunnel_quality: sample({ state: 'degraded', grade: 'critical' }) }).state).toBe('unsteady');
    // An evaluation in progress is not unsteadiness: it is said as what it is.
    expect(read({ tunnel_quality: sample({ state: 'recovering' }) })).toEqual({ state: 'optimizing', ms: 120, active: 'quic' });
    expect(read({ tunnel_quality: sample() })).toEqual({ state: 'up', ms: 120, active: 'quic' });
    expect(read({ tunnel_quality: sample({ grade: 'fair' }) }).state).toBe('up');
  });

  it('never says a tunnel that has just started is lost: connecting until something is read from it', () => {
    // No reading, and the zero it began with: connecting just after this page started it; at any
    // other time the metrics cannot be read (they may never be), and that is what is said.
    const unread = { tunnel_quality: sample({ state: 'unknown', grade: 'unknown', ha_connections: 0, rtt_ms: null }) };
    expect(read(unread).state).toBe('unknown');
    expect(tunnelFromStatus({ ok: true, enabled: true, paired: true, running: true, ...unread } as unknown as RemoteAccessStatus, NOW, true).state).toBe('connecting');
    // Metrics read, still registering: lost, unless this page started it a moment ago.
    const registering = { tunnel_quality: sample({ state: 'degraded', grade: 'unknown', ha_connections: 0, rtt_ms: null }) };
    expect(read(registering).state).toBe('down');
    expect(tunnelFromStatus({ ok: true, enabled: true, paired: true, running: true, ...registering } as unknown as RemoteAccessStatus, NOW, true).state).toBe('connecting');
    // No sample yet: unknown, or connecting where this page started it.
    expect(tunnelFromStatus({ ok: true, enabled: true, paired: true, running: true } as unknown as RemoteAccessStatus, NOW, true).state).toBe('connecting');
    // Connected, its round trip not measured yet: connected, with no number.
    expect(read({ tunnel_quality: sample({ grade: 'unknown', ha_connections: 2, rtt_ms: null }) })).toEqual({ state: 'connected', ms: undefined, active: 'quic' });
    // Requests failing through it is lost however recent the start.
    const failing = { tunnel_quality: sample({ request_path: { status: 'unavailable', confidence: 'high', success_count: 0, sample_count: 12 } }) };
    expect(tunnelFromStatus({ ok: true, enabled: true, paired: true, running: true, ...failing } as unknown as RemoteAccessStatus, NOW, true).state).toBe('down');
  });

  it('says unknown where the state cannot be read, and lets an old sample stop speaking for now', () => {
    expect(read({ running: false, pid_state: 'unknown' }).state).toBe('unknown');
    expect(read({}).state).toBe('unknown');
    // Its metrics have not been read for a while: the connections it last had say nothing now.
    expect(read({ tunnel_quality: sample({ state: 'unknown', grade: 'unknown' }) }).state).toBe('unknown');
    expect(read({ tunnel_quality: sample() }, NOW + 200_000).state).toBe('unknown');
    expect(read({ paired: false, running: false }).state).toBe('unknown');
    expect(tunnelFromStatus(null).state).toBe('unknown');
  });
});

describe('pairingHold', () => {
  const of = (pending: Record<string, unknown> | null) => pairingHold({ pending_pairing: pending as never });
  it('holds the button only where the person has nothing to decide', () => {
    expect(of({ phase: 'prepared', can_resume: true, replay_state: 'scheduled', next_retry_at: 1_700_000_000 })).toEqual({ kind: 'finishing' });
    expect(of({ phase: 'unknown', can_resume: false, replay_state: 'needs_reconciliation' })).toEqual({ kind: 'support' });
    expect(of({ phase: 'prepared', can_resume: false, replay_state: 'terminated', operation_owner: 'desktop_app' })).toEqual({ kind: 'desktop' });
    expect(of({ phase: 'prepared', can_resume: true, replay_state: 'manual', operation_owner: 'desktop_app' })).toEqual({ kind: 'desktop' });
    expect(of({ phase: 'prepared', can_resume: true, replay_state: 'scheduled', operation_owner: 'desktop_app' })).toEqual({ kind: 'desktop' });
    expect(of({ phase: 'applied', can_resume: true, replay_state: 'local_archive', operation_owner: 'desktop_app' })).toEqual({ kind: 'desktop' });
    expect(of({ phase: 'prepared', can_resume: false, replay_state: 'terminated', operation_owner: 'cli' })).toEqual({ kind: 'support' });
    expect(of({ phase: 'prepared', can_resume: false, replay_state: 'terminated' })).toEqual({ kind: 'support' });
  });
  it('leaves 「开启远程访问」 where pressing it continues, and for none or a state it does not know', () => {
    expect(of({ phase: 'prepared', can_resume: true, replay_state: 'manual' })).toBeUndefined();
    expect(of({ phase: 'applied', can_resume: true, replay_state: 'local_archive' })).toBeUndefined();
    // Every readable record names its owner now; the owner alone holds nothing.
    expect(of({ phase: 'prepared', can_resume: true, replay_state: 'manual', operation_owner: 'personal_claim' })).toBeUndefined();
    expect(of({ phase: 'applied', can_resume: true, replay_state: 'local_archive', operation_owner: 'cli' })).toBeUndefined();
    expect(of({ phase: 'revoked', can_resume: false, operation_owner: 'desktop_app' })).toBeUndefined();
    expect(of({ phase: 'prepared', can_resume: false, replay_state: 'terminated', operation_owner: 'personal_claim' })).toBeUndefined();
    expect(of(null)).toBeUndefined();
    expect(of({ phase: 'prepared', can_resume: true })).toBeUndefined();
    expect(of({ phase: 'invalid', can_resume: false })).toBeUndefined();
    expect(of({ phase: 'prepared', can_resume: false, replay_state: 'newer' })).toBeUndefined();
    expect(pairingHold(null)).toBeUndefined();
  });
});

describe('claimProblem', () => {
  it('keeps the local outcome names and says an unknown code with the code', () => {
    expect(claimProblem('link_required')).toEqual({ code: 'link_required' });
    expect(claimProblem('claim_in_progress', { claimId: 'c1' })).toEqual({ code: 'claim_in_progress', claimId: 'c1' });
    expect(claimProblem('rate_limited', { seconds: 30 })).toEqual({ code: 'rate_limited', seconds: 30 });
    expect(claimProblem('control_plane_unreachable')).toEqual({ code: 'unreachable' });
    expect(claimProblem('claim_unavailable')).toEqual({ code: 'expired' });
    expect(claimProblem('subject_mismatch')).toEqual({ code: 'unknown', raw: 'subject_mismatch' });
    expect(claimProblem(undefined)).toEqual({ code: 'unknown', raw: 'unknown' });
  });
});

describe('connectionFromWire', () => {
  const settings = { transport_protocol: 'http2', auto_recovery: true, optimization_profile: 'balanced', edge_ip_version: 'auto', edge_bind_address: '' };
  const read = (extra: Record<string, unknown>) => connectionFromWire({ settings, direct: 'ready', relay: null, operation: null, outcome: null, returnedToAutomatic: null, temporaryRoute: null, ...extra }, []);

  it('reads a running change before the last outcome, and each way a change can end', () => {
    expect(read({ operation: { setting: 'protocol', from: 'auto', to: 'http2', state: 'verifying' }, outcome: { setting: 'ipFamily', result: 'applied' } })?.change)
      .toEqual({ setting: 'protocol', state: 'verifying', to: 'http2' });
    expect(read({ operation: { setting: 'protocol', from: 'auto', state: 'restoring' } })?.change).toEqual({ setting: 'protocol', state: 'restoring', from: 'auto' });
    expect(read({ outcome: { setting: 'protocol', from: 'auto', result: 'applied' } })?.change).toEqual({ setting: 'protocol', state: 'applied' });
    expect(read({ outcome: { setting: 'protocol', from: 'auto', result: 'applied', reason: 'verified' } })?.change).toEqual({ setting: 'protocol', state: 'applied' });
    // Stored with no tunnel running: saved, which is not the same as in effect.
    expect(read({ outcome: { setting: 'protocol', from: 'auto', to: 'http2', result: 'applied', reason: 'saved_without_connector' } })?.change)
      .toEqual({ setting: 'protocol', state: 'applied', to: 'http2', saved: true });
    expect(read({ outcome: { setting: 'protocol', from: 'auto', result: 'restored', reason: 'verification_failed' } })?.change)
      .toEqual({ setting: 'protocol', state: 'restored', reason: 'verification_failed', from: 'auto' });
    expect(read({ outcome: { setting: 'outbound', from: '', result: 'failed', reason: 'storage_restore_failed' } })?.change)
      .toEqual({ setting: 'outbound', state: 'failed', reason: 'storage_restore_failed', from: '' });
  });

  it('keeps a state, a result or a reason it does not know as unknown, never as applied', () => {
    expect(read({ operation: { setting: 'protocol', state: 'celebrating' } })?.change).toEqual({ setting: 'protocol', state: 'unknown' });
    expect(read({ outcome: { setting: 'protocol', result: 'celebrated' } })?.change).toEqual({ setting: 'protocol', state: 'unknown' });
    expect(read({ outcome: { setting: 'protocol', from: 'auto', result: 'failed', reason: 'new_reason' } })?.change)
      .toEqual({ setting: 'protocol', state: 'failed', reason: 'unknown', from: 'auto' });
    expect(read({ direct: 'levitating' })?.direct).toBe('unknown');
    expect(read({ outcome: { setting: 'direct', result: 'applied' } })?.change).toBeUndefined();
  });

  it('reads a change of direct or the relay that the instance accepted and has not written, and how the last one ended', () => {
    // #4114: a switch answers `202` and is written afterwards; `operation` and `outcome` carry it.
    const waiting = read({ operation: { id: 'op-1', setting: 'direct', from: true, to: false, state: 'applying' } });
    expect(waiting?.switching).toEqual({ setting: 'direct', id: 'op-1' });
    expect(waiting?.change).toBeUndefined();
    expect(read({ outcome: { id: 'op-1', setting: 'direct', from: true, to: false, result: 'applied', reason: 'saved', at: '2026-11-22T01:00:00Z' } })?.switched)
      .toEqual({ setting: 'direct', id: 'op-1', failed: false, reason: 'saved' });
    expect(read({ outcome: { id: 'op-2', setting: 'relay', from: false, to: true, result: 'failed', reason: 'tunnel_unhealthy' } })?.switched)
      .toEqual({ setting: 'relay', id: 'op-2', failed: true, reason: 'tunnel_unhealthy' });
    // A tunnel setting's change is not a switch's.
    expect(read({ operation: { id: 'op-3', setting: 'protocol', state: 'verifying', to: 'http2' } })?.switching).toBeUndefined();
  });

  it('shows the relay only where the instance reports one, and exactly the settings returned to automatic', () => {
    expect(read({})?.relay).toBeUndefined();
    expect(read({ relay: { enabled: false, connected: false, canEnable: false, unavailableReason: 'direct_permission_required' } })?.relay)
      .toEqual({ enabled: false, connected: false, unavailable: 'direct_permission_required' });
    expect(read({ relay: { enabled: true, connected: true, canEnable: true } })?.relay).toEqual({ enabled: true, connected: true, unavailable: undefined });
    expect(read({ returnedToAutomatic: { at: '2026-11-21T01:00:00Z', reason: 'tunnel_unreachable', changes: [{ setting: 'protocol', from: 'http2', to: 'auto' }, { setting: 'made_up', from: 'x' }] } })?.returnedToAutomatic)
      .toEqual({ at: '2026-11-21T01:00:00.000Z', changes: [{ setting: 'protocol', from: 'http2' }] });
    expect(read({ temporaryRoute: { phase: 'comparing', protocol: 'http2', differences: ['protocol', 'ipFamily'] } })?.temporaryRoute)
      .toEqual({ protocol: 'http2', differences: ['protocol', 'ipFamily'] });
  });

  it('is not a state at all without settings', () => {
    expect(connectionFromWire({ direct: 'ready' }, [])).toBeNull();
  });
});

describe('addressFromWire', () => {
  it('reads more than one binding record at avibe.bot as its own refusal and its own state', () => {
    expect(renameRefusal({ error: 'rebind_required' })).toEqual({ code: 'rebind_required' });
    expect(renameRefusal({ error: 'remote_access_configuration_mismatch' })).toEqual({ code: 'remote_access_configuration_mismatch' });
    const base = { hostname: 'max-app.avibe.bot', eligible: false, cleanup: [] };
    expect(addressFromWire({ ...base, state_error: 'rebind_required' })?.stateError).toBe('rebind_required');
    expect(addressFromWire({ ...base, state_error: null })?.stateError).toBeUndefined();
    expect(addressFromWire({ ...base, state_error: 'something_new' })?.stateError).toBeUndefined();
    // An instance avibe.bot has never answered about its name: not eligible, by a reason of its own.
    expect(addressFromWire({ ...base, ineligible_reason: 'address_state_unknown', state_error: null }))
      .toMatchObject({ eligible: false, ineligibleReason: 'address_state_unknown', retired: [] });
    expect(renameRefusal({ error: 'rename_ineligible', reason: 'address_state_unknown' })).toEqual({ code: 'rename_ineligible' });
    expect(addressFromWire({ ...base, operation: { state: 'failed', hostname: 'maxim-app.avibe.bot', error: 'rebind_required' } })?.operation?.error)
      .toEqual({ code: 'rebind_required' });
  });

  it('passes the real release days through and reads a refused change with its numbers', () => {
    const state = addressFromWire({
      hostname: 'max-app.avibe.bot', eligible: true, hold_days: 30, release_at_if_changed_now: '2026-12-23T00:00:00Z',
      operation: { state: 'failed', hostname: 'maxim-app.avibe.bot', error: 'name_holding_limit_reached', limit: 5, held_count: 6, earliest_release_at: '2026-12-21T00:00:00Z' },
      unrecognized_change: { previous_hostname: 'max-app.avibe.bot', hostname: 'sam-app.avibe.bot', noticed_at: 1 },
      cleanup: [{ hostname: 'studio-app.avibe.bot', release_at: '2026-12-21T00:00:00Z', complete: false, devices: [
        { id: 'd1', kind: 'home_screen_app', display_name: null, state: 'cleared' },
        { id: 'd2', kind: 'browser', display_name: 'Chrome', state: 'unconfirmed', last_active_at: 1_795_000_000 },
      ] }],
      health: { connected: true, cleanup: 'none' },
    });
    expect(state?.releaseAtIfChangedNow).toBe('2026-12-23T00:00:00.000Z');
    expect(state?.operation).toEqual({ state: 'failed', hostname: 'maxim-app.avibe.bot', error: { code: 'name_holding_limit_reached', held: 6, earliestReleaseAt: '2026-12-21T00:00:00.000Z' } });
    expect(state?.unrecognizedChange).toEqual({ previousHostname: 'max-app.avibe.bot', hostname: 'sam-app.avibe.bot' });
    expect(state?.retired[0]).toMatchObject({ hostname: 'studio-app.avibe.bot', releaseAt: '2026-12-21T00:00:00.000Z' });
    expect(state?.retired[0].devices.map((each) => [each.kind, each.cleanup])).toEqual([['homeScreen', 'cleared'], ['browser', 'unconfirmed']]);
    // Whose a listed device was is the instance's to say.
    expect(addressFromWire({ hostname: 'a-app.avibe.bot', cleanup: [{ hostname: 'b-app.avibe.bot', devices: [{ id: 'd3', kind: 'browser', account: { email: 'sara@example.test' }, state: 'cleared' }, { id: 'd4', kind: 'watch', account: null }] }] })?.retired[0].devices)
      .toMatchObject([{ id: 'd3', account: 'sara@example.test' }, { id: 'd4', kind: 'other', account: undefined }]);
    expect(state?.healthConnected).toBe(true);
  });

  it('gives no date where avibe.bot gave none', () => {
    const state = addressFromWire({ hostname: 'max-app.avibe.bot', eligible: true, hold_days: null, release_at_if_changed_now: null, cleanup: [{ hostname: 'old-app.avibe.bot', release_at: null, devices: [] }] });
    expect(state?.releaseAtIfChangedNow).toBeUndefined();
    expect(state?.holdDays).toBeUndefined();
    expect(state?.retired[0].releaseAt).toBeUndefined();
  });

  it('maps the refusals, the new configuration mismatch among them', () => {
    expect(renameRefusal({ error: 'remote_access_configuration_mismatch' })).toEqual({ code: 'remote_access_configuration_mismatch' });
    expect(renameRefusal({ error: 'taken' })).toEqual({ code: 'taken' });
    expect(renameRefusal({ error: 'name_holding_limit_reached', held_count: 6, earliest_release_at: null })).toEqual({ code: 'name_holding_limit_reached', held: 6, earliestReleaseAt: undefined });
    expect(renameRefusal({ error: 'frozen' })).toEqual({ code: 'unknown', raw: 'frozen' });
    expect(renameRefusal({})).toEqual({ code: 'unknown', raw: 'unknown' });
  });
});

describe('rebindFromWire', () => {
  it('offers binding as a new computer only on the instance\'s own word, with the masked hint for another account', () => {
    expect(rebindFromWire({ binding_state: 'bound', next_action: 'confirm_as_new', reason_code: 'other_account', attempt: { state: 'error', code: 'installation_unverified', owner_hint: 'm***@gmail.com' } }))
      .toEqual({ state: 'asNewOffered', reason: 'other_account', hint: 'm***@gmail.com' });
    expect(rebindFromWire({ binding_state: 'binding_rejected', next_action: 'confirm_as_new', reason_code: 'predecessor_unrecoverable', attempt: { owner_hint: 'ignored' } }))
      .toEqual({ state: 'asNewOffered', reason: 'predecessor_unrecoverable', hint: undefined });
    // A refusal that does not allow it offers nothing, whatever else the answer carries.
    expect(rebindFromWire({ binding_state: 'bound', reason_code: 'active_pairing_recent', attempt: { state: 'error', code: 'installation_unverified' } }))
      .toMatchObject({ state: 'ready', refusal: { code: 'active_pairing_recent' } });
    expect(rebindFromWire({ binding_state: 'bound', attempt: { state: 'error', code: 'installation_unverified' } }))
      .toMatchObject({ state: 'ready', refusal: { code: 'binding_unavailable' } });
    expect(rebindFromWire({ binding_state: 'bound', next_action: 'confirm_as_new', reason_code: 'active_pairing_recent' })).toMatchObject({ state: 'ready' });
  });

  it('says an earlier refusal for good once, and continues an unfinished attempt instead of starting one', () => {
    expect(rebindFromWire({ binding_state: 'bound', reason_code: 'issuer_replay_refused', attempt: { state: 'error', code: 'issuer_replay_refused' } }))
      .toEqual({ state: 'ready', rejected: undefined, tunnelRunning: undefined, replayRefused: true, refusal: undefined });
    expect(rebindFromWire({ binding_state: 'pairing_recovery_required' })).toEqual({ state: 'recovering' });
    expect(rebindFromWire({ binding_state: 'binding_rejected', connector_still_running: true })).toMatchObject({ state: 'ready', rejected: true, tunnelRunning: true });
    expect(rebindFromWire({ binding_state: 'bound', unavailable_reason: 'operation_in_progress' })).toEqual({ state: 'busy' });
    expect(rebindFromWire({ binding_state: 'resetting' })).toEqual({ state: 'resetting' });
    expect(rebindFromWire({ binding_state: 'reset_repair_required' })).toEqual({ state: 'needsTerminal' });
    expect(rebindFromWire({ binding_state: 'unpaired' })).toEqual({ state: 'unpaired' });
    // A damaged record is not a state the overview answers with: it refuses, and that refusal is
    // the terminal command's to handle, not "try again later".
    expect(rebindUnavailable('local_binding_unavailable')).toEqual({ state: 'needsTerminal' });
    expect(rebindUnavailable('local_identity_unavailable')).toEqual({ state: 'needsTerminal' });
    // An unfinished pairing record that cannot be read (the overview's own 503): the same.
    expect(rebindUnavailable('local_enrollment_unavailable')).toEqual({ state: 'needsTerminal' });
    expect(rebindUnavailable('direct_loopback_required')).toEqual({ state: 'remote' });
    expect(rebindUnavailable('local_operator_required')).toEqual({ state: 'remote' });
    // Not the overview's own word: nothing is said on its strength.
    expect(rebindUnavailable('http_500')).toBeNull();
    expect(rebindUnavailable('instance_access_forbidden')).toBeNull();
    expect(rebindFromWire({ binding_state: 'damaged' })).toEqual({ state: 'unknown' });
    // The computer's own refusals keep their code and are not said as avibe.bot's.
    expect(rebindRefusal('reset_not_eligible')).toEqual({ code: 'unknown', raw: 'reset_not_eligible' });
    expect(rebindRefusal('claim_unavailable')).toEqual({ code: 'expired' });
    expect(rebindRefusal('invalid_return')).toEqual({ code: 'invalid_return' });
    expect(rebindFromWire({ binding_state: 'a_new_state' })).toEqual({ state: 'unknown' });
    expect(rebindFromWire(null)).toEqual({ state: 'unknown' });
  });
});
