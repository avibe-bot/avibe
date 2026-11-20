// The instance's section of the update panel, from what the shell reports for the selected instance.
// ui/design: update/instance-and-app
import { describe, expect, it } from 'vitest';
import type { InstanceUpdateState } from './bridge';
import { instanceSection } from './instanceUpdateModel';

const AT = '2026-11-20T07:03:17Z';
// A minute and a half after the action below was sent.
const NOW = Date.parse(AT) + 90_000;
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
    expect(shown).toMatchObject({ target: '3.2.8rc4', canUpdate: true, canSet: { prereleases: true, autoUpdate: true } });
  });

  it('shows its state to a member and offers nothing to change', () => {
    const shown = instanceSection(state({ can_manage: false }), NOW);
    expect(shown.model?.status?.kind).toBe('available');
    expect(shown).toMatchObject({ canUpdate: false, canSet: { prereleases: false, autoUpdate: false } });
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

  it('says the install did not take once the shell has stopped following it and nothing changed', () => {
    const late = instanceSection(state({ request: install('accepted') }), Date.parse(AT) + 31 * 60_000);
    expect(late.model?.status).toEqual({ kind: 'update-failed', reason: 'install', retry: true });
    expect(late).toMatchObject({ canUpdate: true, busy: false });
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
    expect(instanceSection(state({ status: 'unreachable' }), NOW)).toMatchObject({ model: null, canUpdate: false });
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
    expect(old).toMatchObject({ canUpdate: false, canSet: { prereleases: false, autoUpdate: false } });
    const current = instanceSection(state({ snapshot: { ...managed, host_actions: ['check', 'prereleases', 'auto_update', 'install'] } }), NOW);
    expect(current).toMatchObject({ target: '3.2.7', canUpdate: true, canSet: { prereleases: true, autoUpdate: true } });
  });

  it('shows the request on its way, then the progress its own receipt relays', () => {
    const snapshot = { ...managed, host_actions: ['check' as const, 'prereleases' as const, 'auto_update' as const, 'install' as const] };
    expect(instanceSection(state({ snapshot, request: { ...install('sending'), target: '3.2.7' } }), NOW).model?.status).toEqual({ kind: 'requesting' });
    const relayed = instanceSection(state({ snapshot: { ...snapshot, remote_request: { request_id: 'r1', target: '3.2.7', phase: 'downloading', progress: 42, error_code: null, updated_at: AT } }, request: { ...install('accepted'), target: '3.2.7' } }), NOW);
    expect(relayed.model?.status).toEqual({ kind: 'downloading', progress: 42 });
    expect(relayed).toMatchObject({ canUpdate: false, busy: true });
  });

  it.each(['checking', 'awaiting_host'] as const)("is still running while the host's receipt is %s", (phase) => {
    const snapshot = {
      ...managed, host_actions: ['check' as const, 'prereleases' as const, 'auto_update' as const, 'install' as const],
      remote_request: { request_id: 'r1', target: '3.2.7', phase, progress: null, error_code: null, updated_at: AT },
    };
    expect(instanceSection(state({ snapshot, request: { ...install('accepted'), target: '3.2.7' } }), NOW)).toMatchObject({ canUpdate: false, busy: true });
  });

  it.each(['failed', 'declined', 'expired'] as const)("is free to retry once the host's own receipt ends %s, though the shell's stays accepted", (phase) => {
    const snapshot = {
      ...managed, host_actions: ['check' as const, 'prereleases' as const, 'auto_update' as const, 'install' as const],
      remote_request: { request_id: 'r1', target: '3.2.7', phase, progress: null, error_code: null, updated_at: AT },
    };
    const ended = instanceSection(state({ snapshot, request: { ...install('accepted'), target: '3.2.7' } }), NOW);
    expect(ended.model?.status).toMatchObject({ kind: 'update-failed', retry: true });
    expect(ended).toMatchObject({ target: '3.2.7', canUpdate: true, busy: false });
  });
});
