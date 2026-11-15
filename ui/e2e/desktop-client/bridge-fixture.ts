import type { Page } from '@playwright/test';
import type { DesktopClientRequest, DesktopClientState, DesktopClientReply } from '../../src/desktop/bridge';

/** A native peer, not proof of native/backend implementation. All values are test-owned. */
export function clientState(): DesktopClientState {
  return {
    version: 1, revision: 1, generation: 1,
    account: { status: 'signed_out', identity: null, email: null, gate: null, methods: [] },
    connection: { status: 'local', committed: { kind: 'local', name: '小麦的 Mac', instance_id: null }, pending: null },
    enrollment: { status: 'unpaired', instance_id: null, error_code: null },
    catalog: { status: 'idle', instances: [
      { id: 'studio', name: '工作室 Studio', host: 'studio-app.avibe.bot', url: 'https://studio-app.avibe.bot', relationship: 'owner', owner_email: 'max@example.test', available: true },
      { id: 'team', name: '共享团队', host: 'team-app.avibe.bot', url: 'https://team-app.avibe.bot', relationship: 'shared', owner_email: '周@example.test', available: false },
    ] },
    local: { name: '小麦的 Mac', remote_access: 'off' },
    desktop: { version: '3.2.6rc9', update: {
      phase: 'available', latest: '3.2.7', channel: 'test', prereleases: true, autoUpdate: false, checkedAt: '2026-11-13T07:00:00Z',
      hostActions: ['check', 'prereleases', 'auto_update'], skippedVersion: null, interactive: false,
    } },
    consent: null, remote_auxiliary: 'local',
  };
}
export function signIn(state: DesktopClientState) {
  state.account = { status: 'signed_in', identity: { id: '账号-fixture', email: 'max@example.test' }, email: 'max@example.test', methods: ['totp'], gate: null };
  state.enrollment = { status: 'paired_this_account', instance_id: 'local-fixture', error_code: null };
  state.catalog.status = 'ready';
}
export async function installBridge(page: Page, state: DesktopClientState, options: { projection?: boolean; factor?: 'challenge' | 'enroll' } = {}) {
  const requests: DesktopClientRequest[] = [];
  await page.exposeFunction('__fixtureDesktopRequest', async (request: DesktopClientRequest): Promise<DesktopClientReply> => {
    requests.push(request);
    if (request.expected_generation !== state.generation && !['get_state', 'cancel', 'return_local'].includes(request.action.type)) {
      return { version: 1, request_id: request.request_id, ok: false, state, error: { code: 'stale_generation' } };
    }
    const action = request.action;
    let result: Extract<DesktopClientReply, { ok: true }>['result'];
    if (action.type === 'request_otp') state.account = { ...state.account, status: 'verifying', email: action.email };
    if (action.type === 'verify_otp') {
      if (options.factor) state.account = { ...state.account, status: 'second_factor_required', gate: options.factor, methods: ['totp', 'passkey'] };
      else signIn(state);
    }
    if (action.type === 'verify_second_factor' || action.type === 'verify_passkey') signIn(state);
    if (action.type === 'begin_totp_enrollment') result = { type: 'totp_enrollment', uri: 'otpauth://totp/fixture?secret=JBSWY3DPEHPK3PXP', qrSvg: '<svg/>', secret: 'JBSWY3DPEHPK3PXP' };
    if (action.type === 'finish_totp_enrollment') {
      signIn(state);
      result = { type: 'totp_enrolled', method: { id: 'totp-fixture', kind: 'totp', name: 'Fixture', createdAt: '2026-11-12', lastUsedAt: null }, recoveryCodes: ['fixture-recovery-one', 'fixture-recovery-two'] };
    }
    if (action.type === 'refresh_instances') state.catalog.status = 'ready';
    if (action.type === 'select_instance') {
      const instance = state.catalog.instances.find((item) => item.id === action.instance_id)!;
      state.consent = { instance_id: instance.id, instance_name: instance.name, host: instance.host, scope: 'openid email profile' };
      state.connection.pending = { kind: 'remote', instance_id: instance.id, name: instance.name, canonical_origin: instance.url };
    }
    if (action.type === 'decide_consent') {
      state.consent = null;
      if (action.approve) { state.connection.status = 'switching'; }
      else state.connection.pending = null;
    }
    if (['cancel', 'return_local', 'sign_out'].includes(action.type)) {
      state.generation += 1;
      state.connection.pending = null;
      state.consent = null;
      if (action.type !== 'cancel') state.connection = { status: 'local', committed: { kind: 'local', name: state.local.name, instance_id: 'local-fixture' }, pending: null };
      if (action.type === 'sign_out') state.account = { status: 'signed_out', identity: null, email: null, gate: null, methods: [] };
    }
    state.revision += 1;
    return { version: 1, request_id: request.request_id, ok: true, state: structuredClone(state), ...(result ? { result } : {}) };
  });
  const initial = options.projection ? {
    version: state.version, revision: state.revision, generation: state.generation,
    account: { status: state.account.status, identity: state.account.identity },
    connection: state.connection, local: state.local, desktop: state.desktop, remote_auxiliary: state.remote_auxiliary,
  } : state;
  await page.addInitScript(({ initial, projection }) => {
    Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { value: true });
    Object.defineProperty(window, '__AVIBE_DESKTOP_VERSION__', { value: initial.desktop.version });
    Object.defineProperty(window, '__AVIBE_DESKTOP_CLIENT_VERSION__', { value: 1 });
    Object.defineProperty(window, '__AVIBE_DESKTOP_CLIENT_STATE__', { value: initial });
    document.addEventListener('DOMContentLoaded', () => document.documentElement.style.setProperty('--shell-titlebar-inset', '28px'));
    if (projection && initial.connection.committed.kind === 'local') Object.defineProperty(window, '__TAURI_INTERNALS__', { value: {
      invoke: async (command: string) => {
        if (command === 'desktop_update_state') return initial.desktop.update;
        if (command === 'desktop_update_check') return;
        throw new Error(`Unexpected updater IPC: ${command}`);
      },
    } });
    if (!projection) Object.defineProperty(window, '__TAURI_INTERNALS__', { value: {
      invoke: (command: string, args: { request: DesktopClientRequest }) => {
        if (command !== 'desktop_client_request') throw new Error(`Unexpected IPC: ${command}`);
        return (window as unknown as { __fixtureDesktopRequest: (request: DesktopClientRequest) => Promise<DesktopClientReply> }).__fixtureDesktopRequest(args.request);
      },
    } });
  }, { initial, projection: options.projection ?? false });
  return requests;
}
