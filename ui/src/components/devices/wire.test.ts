import { describe, expect, it } from 'vitest';

import {
  activityFromWire, addressFromWire, claimProblem, connectionFromWire, devicesFromWire, rebindFromWire, renameRefusal, signedOutFromWire,
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

  it('drops a row without an id, a known kind or a known list rather than guessing', () => {
    expect(devicesFromWire({ mine: [row({ id: '' }), row({ kind: 'watch' }), row({ group: 'elsewhere' })] }).devices).toEqual([]);
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
    expect(rebindFromWire({ binding_state: 'a_new_state' })).toEqual({ state: 'unknown' });
    expect(rebindFromWire(null)).toEqual({ state: 'unknown' });
  });
});
