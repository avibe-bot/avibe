// The instance's section of the update panel, from what the shell reports for the selected instance.
// ui/design: update/instance-and-app
import { describe, expect, it } from 'vitest';
import type { InstanceUpdateState } from './bridge';
import { instanceSection } from './instanceUpdateModel';

const AT = '2026-11-20T07:03:17Z';
// A minute and a half after the action below was sent.
const NOW = Date.parse(AT) + 90_000;
// A read the shell made after that action was accepted.
const READ = '2026-11-20T07:03:30Z';
const state = (extra: Partial<InstanceUpdateState> = {}): InstanceUpdateState => ({
  instance_id: 'alex', status: 'ready', can_manage: true, request: null, fetched_at: AT,
  snapshot: { current: '3.2.8rc2', latest: '3.2.8rc4', has_update: true, error: null, prereleases: true, auto_update: false },
  ...extra,
});
const install = (phase: 'sending' | 'accepted' | 'failed', error_code: string | null = null) =>
  ({ action: 'install' as const, phase, target: '3.2.8rc4', error_code, updated_at: AT });

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

  it('follows an install the shell sent: installing, restarting while the old version still answers or none does, then the version now running', () => {
    expect(instanceSection(state({ request: install('sending') }), NOW).model?.status).toEqual({ kind: 'installing' });
    expect(instanceSection(state({ request: install('sending') }), NOW)).toMatchObject({ canUpdate: false, busy: true });
    // Accepted, and the instance still offers what was installed: it has not restarted yet.
    expect(instanceSection(state({ request: install('accepted') }), NOW).model?.status)
      .toEqual({ kind: 'updated', version: '3.2.8rc4', restarting: true });
    // Until it ends the shell takes no other action for this instance: the check and the switches wait.
    expect(instanceSection(state({ request: install('accepted') }), NOW)).toMatchObject({ canUpdate: false, busy: true });
    // It stops answering while it restarts: still that restart, not a lost connection.
    expect(instanceSection(state({ status: 'unreachable', request: install('accepted') }), NOW).model?.status)
      .toMatchObject({ kind: 'updated', restarting: true });
    // Back on another version: the result names the version it runs, which a standalone install does not pin.
    const done = instanceSection(state({
      request: install('accepted'),
      snapshot: { current: '3.2.8rc5', latest: '3.2.8rc5', has_update: false, error: null },
    }), NOW);
    expect(done.model?.status).toEqual({ kind: 'updated', version: '3.2.8rc5' });
    expect(done.busy).toBe(false);
  });

  it('is a lost connection, not that restart again, when it stops answering after the install has settled', () => {
    const later = instanceSection(state({
      status: 'unreachable',
      request: install('accepted'),
      snapshot: { current: '3.2.8rc4', latest: '3.2.8rc4', has_update: false, error: null },
    }), NOW);
    expect(later).toMatchObject({ model: null, busy: false });
  });

  it('says the install did not finish when the Runtime keeps answering on the old version and offer', () => {
    // Accepted, and it never restarted (it could not schedule one, or it predates its own install report).
    const still = (minutes: number) => instanceSection(state({ request: install('accepted') }), Date.parse(AT) + minutes * 60_000);
    expect(still(4).model?.status).toMatchObject({ kind: 'updated', restarting: true });
    expect(still(6).model?.status).toEqual({ kind: 'update-failed', reason: 'install', retry: true });
    expect(still(6)).toMatchObject({ canUpdate: true, busy: false });
  });

  it('waits for a restart it cannot see only as long as the shell follows the install', () => {
    const silent = (minutes: number) => instanceSection(state({ status: 'unreachable', request: install('accepted') }), Date.parse(AT) + minutes * 60_000);
    expect(silent(29).model?.status).toMatchObject({ kind: 'updated', restarting: true });
    expect(silent(31)).toMatchObject({ model: null, busy: false });
  });

  it("takes the Runtime's own word for its restart when it reports one", () => {
    const reported = instanceSection(state({
      request: install('accepted'),
      snapshot: { current: '3.2.8rc2', latest: '3.2.8rc4', has_update: true, error: null, install: { phase: 'restarting', target: '3.2.8rc4', started_at: AT } },
    }), Date.parse(AT) + 10 * 60_000);
    expect(reported.model?.status).toEqual({ kind: 'updated', version: '3.2.8rc4', restarting: true });
    expect(reported.busy).toBe(true);
  });

  it('keeps showing the restart the Runtime reported while it does not answer, whoever started the install', () => {
    const restartingNow = { current: '3.2.8rc2', latest: '3.2.8rc4', has_update: true, error: null, install: { phase: 'restarting' as const, target: '3.2.8rc4', started_at: AT } };
    // The main flow: Update was clicked here, the Runtime answered, reported its restart, and went quiet.
    const ours = instanceSection(state({ status: 'unreachable', request: install('accepted'), snapshot: restartingNow }), NOW);
    expect(ours.model?.status).toEqual({ kind: 'updated', version: '3.2.8rc4', restarting: true });
    expect(ours).toMatchObject({ canUpdate: false, busy: true });
    // Started from another surface: the same restart, from the Runtime's own report.
    const theirs = instanceSection(state({ status: 'unreachable', snapshot: restartingNow }), NOW);
    expect(theirs.model?.status).toMatchObject({ kind: 'updated', restarting: true });
    // It never came back: a lost connection once the install is no longer followed.
    expect(instanceSection(state({ status: 'unreachable', snapshot: restartingNow }), Date.parse(AT) + 31 * 60_000)).toMatchObject({ model: null, busy: false });
  });

  it('offers a release that appeared after the install it sent, instead of repeating that result', () => {
    const later = instanceSection(state({
      request: install('accepted'),
      snapshot: { current: '3.2.8rc4', latest: '3.2.8rc5', has_update: true, error: null },
    }), NOW);
    expect(later.model?.status).toEqual({ kind: 'available', version: '3.2.8rc5' });
    expect(later).toMatchObject({ target: '3.2.8rc5', canUpdate: true, busy: false });
  });

  it('keeps an install the instance reports itself, started from anywhere', () => {
    const shown = instanceSection(state({
      snapshot: { current: '3.2.8rc2', latest: '3.2.8rc4', has_update: true, error: null, install: { phase: 'installing', target: '3.2.8rc4', started_at: AT } },
    }), NOW);
    expect(shown.model?.status).toEqual({ kind: 'installing' });
    expect(shown).toMatchObject({ canUpdate: false, busy: true });
  });

  it('says a failed install failed, and can be tried again', () => {
    const shown = instanceSection(state({ request: install('failed', 'request_failed') }), NOW);
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

  it('offers only what its desktop app takes from here', () => {
    const old = instanceSection(state({ snapshot: managed }), NOW);
    expect(old.model?.subject).toBe('host-desktop');
    expect(old).toMatchObject({ canUpdate: false, canCheck: false, canSet: { prereleases: false, autoUpdate: false } });
    const current = instanceSection(state({ snapshot: { ...managed, host_actions: ['check', 'prereleases', 'auto_update', 'install'] } }), NOW);
    expect(current).toMatchObject({ target: '3.2.7', canUpdate: true, canCheck: true, canSet: { prereleases: true, autoUpdate: true } });
  });

  it('shows a member its state and no switches', () => {
    const member = instanceSection(state({ can_manage: false, snapshot: { ...managed, host_actions: ['check', 'prereleases', 'auto_update', 'install'] } }), NOW);
    expect(member.model?.status?.kind).toBe('available');
    expect(member.model).toMatchObject({ prereleases: null, autoUpdate: null });
    expect(member).toMatchObject({ canUpdate: false, canCheck: false });
  });

  it('shows the request on its way, then the progress its own receipt relays', () => {
    const snapshot = { ...managed, host_actions: ['check' as const, 'prereleases' as const, 'auto_update' as const, 'install' as const] };
    expect(instanceSection(state({ snapshot, request: { ...install('sending'), target: '3.2.7' } }), NOW).model?.status).toEqual({ kind: 'requesting' });
    const relayed = instanceSection(state({ fetched_at: READ, snapshot: { ...snapshot, remote_request: { request_id: 'r1', target: '3.2.7', phase: 'downloading', progress: 42, error_code: null, updated_at: AT } }, request: { ...install('accepted'), target: '3.2.7' } }), NOW);
    expect(relayed.model?.status).toEqual({ kind: 'downloading', progress: 42 });
    expect(relayed).toMatchObject({ canUpdate: false, busy: true });
  });

  it('keeps the request on its way until a read made after it was accepted shows what the host did with it', () => {
    const snapshot = { ...managed, host_actions: ['check' as const, 'prereleases' as const, 'auto_update' as const, 'install' as const] };
    // The answer on screen was read before the request: it cannot show the host's receipt yet.
    const before = instanceSection(state({ snapshot, fetched_at: AT, request: { ...install('accepted'), target: '3.2.7' } }), NOW);
    expect(before.model?.status).toEqual({ kind: 'requesting' });
    expect(before).toMatchObject({ canUpdate: false, busy: true });
  });

  it.each(['checking', 'awaiting_host'] as const)("is still running while the host's receipt is %s", (phase) => {
    const snapshot = {
      ...managed, host_actions: ['check' as const, 'prereleases' as const, 'auto_update' as const, 'install' as const],
      remote_request: { request_id: 'r1', target: '3.2.7', phase, progress: null, error_code: null, updated_at: AT },
    };
    expect(instanceSection(state({ snapshot, fetched_at: READ, request: { ...install('accepted'), target: '3.2.7' } }), NOW)).toMatchObject({ canUpdate: false, busy: true });
  });

  it.each(['failed', 'declined', 'expired'] as const)("is free to retry once the host's own receipt ends %s, though the shell's stays accepted", (phase) => {
    const snapshot = {
      ...managed, host_actions: ['check' as const, 'prereleases' as const, 'auto_update' as const, 'install' as const],
      remote_request: { request_id: 'r1', target: '3.2.7', phase, progress: null, error_code: null, updated_at: AT },
    };
    const ended = instanceSection(state({ snapshot, fetched_at: READ, request: { ...install('accepted'), target: '3.2.7' } }), NOW);
    expect(ended.model?.status).toMatchObject({ kind: 'update-failed', retry: true });
    expect(ended).toMatchObject({ target: '3.2.7', canUpdate: true, busy: false });
  });
});

// The section mirrors the shell's own rule for when an install is still under way (instance_update.rs,
// `unsettled`), from less than the shell knows. Three reviewed heads each missed a combination, so the
// combinations are walked here and held to what must be true of every one of them.
describe('every combination of what the shell can report', () => {
  const MINUTES = [0, 1, 4, 6, 29, 31];
  const requests: InstanceUpdateState['request'][] = [
    null, install('sending'), install('accepted'), install('failed', 'install_failed'),
    { action: 'check', phase: 'sending', target: null, error_code: null, updated_at: AT },
    { action: 'prereleases', phase: 'failed', target: null, error_code: 'request_failed', updated_at: AT },
  ];
  const standalone: NonNullable<InstanceUpdateState['snapshot']>[] = [
    { current: '3.2.8rc2', latest: '3.2.8rc4', has_update: true, error: null },
    { current: '3.2.8rc2', latest: '3.2.8rc4', has_update: true, error: null, install: { phase: 'installing', target: '3.2.8rc4', started_at: AT } },
    { current: '3.2.8rc2', latest: '3.2.8rc4', has_update: true, error: null, install: { phase: 'restarting', target: '3.2.8rc4', started_at: AT } },
    { current: '3.2.8rc4', latest: '3.2.8rc4', has_update: false, error: null },
    { current: '3.2.8rc4', latest: '3.2.8rc5', has_update: true, error: null },
  ];
  const host = { current: '3.2.8rc2', latest: '3.2.8rc4', has_update: true, error: null, managed_by: 'desktop' as const, phase: 'available' as const, desktop_version: '3.2.8rc2', host_actions: ['check' as const, 'prereleases' as const, 'auto_update' as const, 'install' as const] };
  const receipt = (phase: 'checking' | 'awaiting_host' | 'downloading' | 'restarting' | 'completed' | 'failed' | 'declined' | 'expired') =>
    ({ request_id: 'r1', target: '3.2.8rc4', phase, progress: null, error_code: null, updated_at: AT });
  const managedHosts: NonNullable<InstanceUpdateState['snapshot']>[] = [
    host,
    ...(['checking', 'awaiting_host', 'downloading', 'restarting', 'completed', 'failed', 'declined', 'expired'] as const)
      .map((phase) => ({ ...host, remote_request: receipt(phase) })),
    { ...host, current: '3.2.8rc4', desktop_version: '3.2.8rc4', has_update: false, phase: 'current' as const, remote_request: receipt('completed') },
  ];
  const cases = [...standalone, ...managedHosts].flatMap((snapshot) => requests.flatMap((request) =>
    (['ready', 'unreachable'] as const).flatMap((status) => [AT, READ].flatMap((fetched_at) => [true, false].flatMap((can_manage) =>
      MINUTES.map((minutes) => ({ snapshot, request, status, fetched_at, can_manage, minutes })))))));
  const running = (snapshot: NonNullable<InstanceUpdateState['snapshot']>) => Boolean(snapshot.install)
    || Boolean(snapshot.remote_request && !['completed', 'failed', 'declined', 'expired'].includes(snapshot.remote_request.phase))
    || (snapshot.remote_request?.phase === 'completed' && snapshot.current !== snapshot.remote_request.target);

  it(`holds for all ${cases.length}`, () => {
    for (const c of cases) {
      const shown = instanceSection(state({ ...c }), Date.parse(AT) + c.minutes * 60_000);
      const where = JSON.stringify({ ...c, snapshot: { ...c.snapshot, host_actions: undefined } });
      // A button is offered only where the shell would take the click, and never beside a running install.
      if (shown.canUpdate) {
        expect(shown.busy, where).toBe(false);
        expect(c.can_manage, where).toBe(true);
        expect(['available', 'update-failed'], where).toContain(shown.model?.status?.kind);
      }
      if (shown.canCheck) expect(c.status === 'ready' && c.can_manage, where).toBe(true);
      // An instance that cannot be read offers nothing, and waits only for what is on its way to it.
      if (!shown.model) expect(shown, where).toMatchObject({ canUpdate: false, canCheck: false, busy: c.request?.phase === 'sending' });
      // Something on its way, or reported as running by an instance that answers, is never offered again.
      if (c.request?.phase === 'sending') expect(shown.busy, where).toBe(true);
      if (c.status === 'ready' && running(c.snapshot)) expect(shown.busy, where).toBe(true);
      // Nothing waits for ever: past the shell's own limit, with nothing reported as running, the section is
      // free again when the instance answers, and a lost connection when it does not.
      if (c.minutes > 30 && c.request?.phase !== 'sending' && !running(c.snapshot)) {
        expect(shown.busy, where).toBe(false);
        if (c.status === 'unreachable') expect(shown.model, where).toBeNull();
      }
      // A member never gets a switch.
      if (!c.can_manage && shown.model) expect(shown.model, where).toMatchObject({ prereleases: null });
    }
  });
});

