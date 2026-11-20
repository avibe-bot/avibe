// The instance's section of the update panel, from what the shell reports for the selected instance.
// ui/design: update/instance-and-app
import { describe, expect, it } from 'vitest';
import type { InstanceUpdateState } from './bridge';
import { instanceSection } from './instanceUpdateModel';

const AT = '2026-11-20T07:03:17Z';
// A minute and a half after the action below was sent.
const NOW = Date.parse(AT) + 90_000;
type Request = NonNullable<InstanceUpdateState['request']>;
type Snapshot = NonNullable<InstanceUpdateState['snapshot']>;
const state = (extra: Partial<InstanceUpdateState> = {}): InstanceUpdateState => ({
  instance_id: 'alex', status: 'ready', can_manage: true, request: null, fetched_at: AT,
  snapshot: { current: '3.2.8rc2', latest: '3.2.8rc4', has_update: true, error: null, prereleases: true, auto_update: false },
  ...extra,
});
/** An install this shell sent while the instance ran 3.2.8rc2. The shell follows it from `sending` until it ends. */
const install = (phase: Request['phase'], following: boolean, extra: Partial<Request> = {}): Request => ({
  action: 'install', phase, target: '3.2.8rc4', error_code: null, updated_at: AT, following,
  installed_from: phase === 'sending' ? null : '3.2.8rc2', ...extra,
});
const OLD: Snapshot = { current: '3.2.8rc2', latest: '3.2.8rc4', has_update: true, error: null };
const NEW: Snapshot = { current: '3.2.8rc4', latest: '3.2.8rc4', has_update: false, error: null };

describe('a standalone instance', () => {
  it('offers the version on screen to an account that can manage it', () => {
    const shown = instanceSection(state(), NOW);
    expect(shown.model?.status).toEqual({ kind: 'available', version: '3.2.8rc4' });
    expect(shown).toMatchObject({ target: '3.2.8rc4', canUpdate: true, canCheck: true, canSet: { prereleases: true, autoUpdate: true } });
  });

  it('shows its state to a member and offers nothing to change', () => {
    const shown = instanceSection(state({ can_manage: false }), NOW);
    expect(shown.model?.status?.kind).toBe('available');
    // The shell refuses a member's check, so the panel's check only reads the instance again.
    expect(shown).toMatchObject({ canUpdate: false, canCheck: false, canSet: { prereleases: false, autoUpdate: false } });
    expect(shown.model?.prereleases).toBeNull();
  });

  it('follows an install the shell sent for as long as the shell says it follows it', () => {
    expect(instanceSection(state({ request: install('sending', true) }), NOW).model?.status).toEqual({ kind: 'installing' });
    expect(instanceSection(state({ request: install('sending', true) }), NOW)).toMatchObject({ canUpdate: false, canCheck: false, busy: true });
    // Accepted and followed: the Runtime restarts into it, whether it still answers on the old version or not.
    for (const status of ['ready', 'unreachable'] as const) {
      const restarting = instanceSection(state({ status, request: install('accepted', true), snapshot: OLD }), NOW);
      expect(restarting.model?.status).toEqual({ kind: 'updated', version: '3.2.8rc4', restarting: true });
      expect(restarting).toMatchObject({ canUpdate: false, canCheck: false, busy: true });
    }
    // The Runtime's own report of it says the same in its own words.
    const reported = instanceSection(state({
      request: install('accepted', true),
      snapshot: { ...OLD, install: { phase: 'restarting', target: '3.2.8rc4', started_at: AT } },
    }), NOW);
    expect(reported.model?.status).toEqual({ kind: 'updated', version: '3.2.8rc4', restarting: true });
    expect(reported.busy).toBe(true);
  });

  it('names the version it runs once the shell has stopped following, which a standalone install does not pin', () => {
    const done = instanceSection(state({
      request: install('accepted', false),
      snapshot: { current: '3.2.8rc5', latest: '3.2.8rc5', has_update: false, error: null },
    }), NOW);
    expect(done.model?.status).toEqual({ kind: 'updated', version: '3.2.8rc5' });
    expect(done.busy).toBe(false);
  });

  it('says the install did not finish, with a retry the shell takes, when it stopped following and nothing changed', () => {
    // The Runtime accepted, kept answering on the old version, and never restarted: the shell gave it up.
    const still = instanceSection(state({ request: install('accepted', false), snapshot: OLD }), NOW);
    expect(still.model?.status).toEqual({ kind: 'update-failed', reason: 'install', retry: true });
    expect(still).toMatchObject({ target: '3.2.8rc4', canUpdate: true, canCheck: true, busy: false });
  });

  it('offers a release that appeared after the install it sent, instead of repeating that result', () => {
    const later = instanceSection(state({
      request: install('accepted', false),
      snapshot: { current: '3.2.8rc4', latest: '3.2.8rc5', has_update: true, error: null },
    }), NOW);
    expect(later.model?.status).toEqual({ kind: 'available', version: '3.2.8rc5' });
    expect(later).toMatchObject({ target: '3.2.8rc5', canUpdate: true, busy: false });
  });

  it('is a lost connection, not that restart again, when it stops answering after the install has settled', () => {
    expect(instanceSection(state({ status: 'unreachable', request: install('accepted', false), snapshot: NEW }), NOW))
      .toMatchObject({ model: null, busy: false });
  });

  it('keeps an install the instance reports itself, started from anywhere, also while it restarts', () => {
    const elsewhere = { ...OLD, install: { phase: 'restarting' as const, target: '3.2.8rc4', started_at: AT } };
    const shown = instanceSection(state({ snapshot: { ...OLD, install: { phase: 'installing', target: '3.2.8rc4', started_at: AT } } }), NOW);
    expect(shown.model?.status).toEqual({ kind: 'installing' });
    expect(shown).toMatchObject({ canUpdate: false, busy: true });
    expect(instanceSection(state({ status: 'unreachable', snapshot: elsewhere }), NOW).model?.status).toMatchObject({ kind: 'updated', restarting: true });
    // It never came back: a lost connection once that report is half an hour old.
    expect(instanceSection(state({ status: 'unreachable', snapshot: elsewhere }), Date.parse(AT) + 31 * 60_000)).toMatchObject({ model: null, busy: false });
  });

  it('says a failed install failed, and can be tried again', () => {
    const shown = instanceSection(state({ request: install('failed', false, { error_code: 'request_failed' }) }), NOW);
    expect(shown.model?.status).toEqual({ kind: 'update-failed', reason: 'install', retry: true });
    expect(shown.canUpdate).toBe(true);
  });
});

describe('an instance that cannot be read', () => {
  it('is a lost connection when it does not answer and nothing of ours is under way', () => {
    expect(instanceSection(state({ status: 'unreachable' }), NOW)).toMatchObject({ model: null, canUpdate: false, canCheck: false });
    expect(instanceSection(state({ status: 'unreachable', snapshot: null }), NOW)).toMatchObject({ model: null });
  });

  it('says so when it no longer accepts the sign-in, and shows nothing it last said', () => {
    expect(instanceSection(state({ status: 'unauthorized' }), NOW)).toMatchObject({ model: null, unavailable: 'unauthorized', canUpdate: false });
  });

  it('is being read before its first answer', () => {
    expect(instanceSection(state({ status: 'loading', snapshot: null }), NOW).model?.status).toEqual({ kind: 'checking' });
  });
});

describe('a desktop-managed instance', () => {
  const managed = { current: '3.2.6', latest: '3.2.7', has_update: true, error: null, managed_by: 'desktop' as const, phase: 'available' as const, desktop_version: '3.2.6', prereleases: false, auto_update: false, checked_at: AT };
  const takesAll = { ...managed, host_actions: ['check' as const, 'prereleases' as const, 'auto_update' as const, 'install' as const] };
  const sent = (phase: Request['phase'], following: boolean) => install(phase, following, { target: '3.2.7', installed_from: phase === 'sending' ? null : '3.2.6' });
  const receipt = (phase: NonNullable<Snapshot['remote_request']>['phase']) => ({ request_id: 'r1', target: '3.2.7', phase, progress: phase === 'downloading' ? 42 : null, error_code: null, updated_at: AT });

  it('offers only what its desktop app takes from here', () => {
    const old = instanceSection(state({ snapshot: managed }), NOW);
    expect(old.model?.subject).toBe('host-desktop');
    expect(old).toMatchObject({ canUpdate: false, canCheck: false, canSet: { prereleases: false, autoUpdate: false } });
    expect(instanceSection(state({ snapshot: takesAll }), NOW))
      .toMatchObject({ target: '3.2.7', canUpdate: true, canCheck: true, canSet: { prereleases: true, autoUpdate: true } });
  });

  it('shows a member its state and no switches', () => {
    const member = instanceSection(state({ can_manage: false, snapshot: takesAll }), NOW);
    expect(member.model?.status?.kind).toBe('available');
    expect(member.model).toMatchObject({ prereleases: null, autoUpdate: null });
    expect(member).toMatchObject({ canUpdate: false, canCheck: false });
  });

  it("keeps the request on its way until the host's own receipt shows, then relays that receipt", () => {
    expect(instanceSection(state({ snapshot: takesAll, request: sent('sending', true) }), NOW).model?.status).toEqual({ kind: 'requesting' });
    // Accepted and followed, and the answer on screen shows no receipt yet.
    const before = instanceSection(state({ snapshot: takesAll, request: sent('accepted', true) }), NOW);
    expect(before.model?.status).toEqual({ kind: 'requesting' });
    expect(before).toMatchObject({ canUpdate: false, busy: true });
    const relayed = instanceSection(state({ snapshot: { ...takesAll, remote_request: receipt('downloading') }, request: sent('accepted', true) }), NOW);
    expect(relayed.model?.status).toEqual({ kind: 'downloading', progress: 42 });
    expect(relayed).toMatchObject({ canUpdate: false, busy: true });
  });

  it('relays a later install started from another surface as what it is, after its own has settled', () => {
    // This shell's install of 3.2.7 is done; the host now downloads 3.2.8, asked for from a browser.
    const later = instanceSection(state({
      snapshot: { ...takesAll, current: '3.2.7', desktop_version: '3.2.7', latest: '3.2.8', remote_request: { ...receipt('downloading'), request_id: 'r2', target: '3.2.8' } },
      request: sent('accepted', false),
    }), NOW);
    expect(later.model?.status).toEqual({ kind: 'downloading', progress: 42 });
    expect(later).toMatchObject({ canUpdate: false, canCheck: false, busy: true });
  });

  it.each(['failed', 'declined', 'expired'] as const)("is free to retry once the host's own receipt ends %s and the shell has stopped following", (phase) => {
    const ended = instanceSection(state({ snapshot: { ...takesAll, remote_request: receipt(phase) }, request: sent('accepted', false) }), NOW);
    expect(ended.model?.status).toMatchObject({ kind: 'update-failed', retry: true });
    expect(ended).toMatchObject({ target: '3.2.7', canUpdate: true, busy: false });
  });
});

// The section offers an action exactly when the shell would take it, on the shell's own word (`following`).
// Earlier heads worked that out from less than the shell knows and each missed a combination, so the
// combinations are walked here and held to what must be true of every one of them.
describe('every combination of what the shell can report', () => {
  const host = { ...OLD, managed_by: 'desktop' as const, phase: 'available' as const, desktop_version: '3.2.8rc2', host_actions: ['check' as const, 'prereleases' as const, 'auto_update' as const, 'install' as const] };
  const receipt = (phase: NonNullable<Snapshot['remote_request']>['phase']) => ({ request_id: 'r1', target: '3.2.8rc4', phase, progress: null, error_code: null, updated_at: AT });
  const snapshots: Snapshot[] = [
    OLD, NEW,
    { ...OLD, install: { phase: 'installing', target: '3.2.8rc4', started_at: AT } },
    { ...OLD, install: { phase: 'restarting', target: '3.2.8rc4', started_at: AT } },
    { current: '3.2.8rc4', latest: '3.2.8rc5', has_update: true, error: null },
    { current: '3.2.8rc2', latest: '3.2.8rc5', has_update: true, error: null },
    host,
    ...(['checking', 'awaiting_host', 'downloading', 'restarting', 'completed', 'failed', 'declined', 'expired'] as const)
      .map((phase) => ({ ...host, remote_request: receipt(phase) })),
    { ...host, current: '3.2.8rc4', desktop_version: '3.2.8rc4', has_update: false, phase: 'current' as const, remote_request: receipt('completed') },
    // A later install of another version, asked for from another surface.
    { ...host, current: '3.2.8rc4', desktop_version: '3.2.8rc4', latest: '3.2.8rc5', remote_request: { ...receipt('downloading'), request_id: 'r2', target: '3.2.8rc5' } },
  ];
  const other = (action: 'check' | 'prereleases', phase: Request['phase'], following: boolean): Request =>
    ({ action, phase, target: null, error_code: phase === 'failed' ? 'request_failed' : null, updated_at: AT, following, installed_from: null });
  const requests: InstanceUpdateState['request'][] = [
    null,
    install('sending', true), install('accepted', true), install('accepted', false), install('failed', false),
    install('accepted', true, { installed_from: null }), install('accepted', false, { installed_from: null }),
    other('check', 'sending', true), other('check', 'accepted', true), other('check', 'accepted', false),
    other('prereleases', 'failed', false),
  ];
  const cases = snapshots.flatMap((snapshot) => requests.flatMap((request) =>
    (['ready', 'unreachable'] as const).flatMap((status) => [true, false].flatMap((can_manage) =>
      [1, 31].map((minutes) => ({ snapshot, request, status, can_manage, minutes }))))));
  const running = (snapshot: Snapshot) => Boolean(snapshot.install)
    || Boolean(snapshot.remote_request && !['completed', 'failed', 'declined', 'expired'].includes(snapshot.remote_request.phase))
    || (snapshot.remote_request?.phase === 'completed' && snapshot.current !== snapshot.remote_request.target);

  it(`holds for all ${cases.length}`, () => {
    for (const c of cases) {
      const shown = instanceSection(state({ ...c }), Date.parse(AT) + c.minutes * 60_000);
      const where = JSON.stringify({ ...c, snapshot: { ...c.snapshot, host_actions: undefined } });
      const following = c.request?.following === true;
      // While the shell follows something it refuses every other action: none is offered, and the rest waits.
      const nothing = { canUpdate: false, canCheck: false, canSet: { prereleases: false, autoUpdate: false } };
      if (following) expect(shown, where).toMatchObject({ ...nothing, busy: true });
      // A button is offered only to an account that may use it, on an offer, and never beside a running install.
      if (shown.canUpdate) {
        expect(shown.busy, where).toBe(false);
        expect(c.can_manage, where).toBe(true);
        expect(['available', 'update-failed'], where).toContain(shown.model?.status?.kind);
      }
      if (shown.canCheck) expect(c.status === 'ready' && c.can_manage, where).toBe(true);
      // An instance that cannot be read offers nothing, and waits only for what the shell still follows.
      if (!shown.model) expect(shown, where).toMatchObject({ ...nothing, busy: following });
      // What an instance that answers reports as running is never offered again, and nothing else is offered
      // for it meanwhile, whoever started it.
      if (c.status === 'ready' && running(c.snapshot)) expect(shown, where).toMatchObject({ ...nothing, busy: true });
      // Nothing waits for ever: with nothing followed and nothing reported as running, the section is free,
      // and an instance that does not answer is a lost connection.
      if (!following && !running(c.snapshot)) {
        expect(shown.busy, where).toBe(false);
        if (c.status === 'unreachable') expect(shown.model, where).toBeNull();
      }
      // Once the shell has stopped following an install, an account that may install is never left with an
      // offer and no way to take it.
      if (!following && c.status === 'ready' && c.can_manage && !running(c.snapshot) && c.snapshot.has_update && c.snapshot.latest) {
        expect(shown.canUpdate, where).toBe(true);
      }
      // A member never gets a switch.
      if (!c.can_manage && shown.model) expect(shown.model, where).toMatchObject({ prereleases: null });
    }
  });
});
