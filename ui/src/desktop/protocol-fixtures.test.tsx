// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { createInstance } from 'i18next';
import { I18nextProvider } from 'react-i18next';
import { afterEach, expect, expectTypeOf, it, vi } from 'vitest';
import en from '../i18n/en.json';
import zh from '../i18n/zh.json';
import { AccountApp } from './AccountApp';
import { DESKTOP_CLIENT_STATE_EVENT, DESKTOP_PRESENT_EVENT, DESKTOP_CLIENT_VIEWS, withLocalIdentity, type DesktopClientAction, type DesktopClientProjection, type DesktopClientRequest, type DesktopClientState } from './bridge';
import states from './__fixtures__/instance-client-v1/states.json';
import replies from './__fixtures__/instance-client-v1/replies.json';
import requests from './__fixtures__/instance-client-v1/requests.json';

// The prior peer tests construct U1-shaped state. These exact S1 samples catch
// consumer field drift by entering the real account hook and rendered actions.
const i18n = createInstance();
await i18n.init({ lng: 'en', resources: { en: { translation: en }, zh: { translation: zh } }, interpolation: { escapeValue: false } });
afterEach(async () => { cleanup(); window.history.replaceState(null, '', '/'); vi.unstubAllGlobals(); vi.restoreAllMocks(); await i18n.changeLanguage('en'); });
function mount(initial: unknown, response: (request: DesktopClientRequest) => unknown) {
  if (!window.location.search) window.history.replaceState(null, '', '/account/index.html?view=instances');
  vi.stubGlobal('__AVIBE_DESKTOP_CLIENT_VERSION__', 1);
  vi.stubGlobal('__AVIBE_DESKTOP_CLIENT_STATE__', initial);
  const invoke = vi.fn(async (command: string, { request }: { request: DesktopClientRequest }) => {
    expect(command).toBe('desktop_client_request');
    return response(request);
  });
  vi.stubGlobal('__TAURI_INTERNALS__', { invoke });
  render(<I18nextProvider i18n={i18n}><AccountApp /></I18nextProvider>);
  return invoke;
}

// Remote restrictions remain native-owned; the retired static notice must not
// consume space in either complete account route. View-only frames miss it.
it.each([['en', 'instances'], ['en', 'account'], ['zh', 'instances'], ['zh', 'account']] as const)('remote account routes omit the retired auxiliary notice (%s/%s)', async (lng, view) => {
  window.history.replaceState(null, '', `/account/index.html?view=${view}`);
  await i18n.changeLanguage(lng);
  const item = states.signed_in.catalog.instances[0];
  const state = { ...states.signed_in, remote_auxiliary: 'disabled_incomplete',
    connection: { status: 'remote_connected', pending: null,
      committed: { kind: 'remote', name: item.name, instance_id: item.id, canonical_origin: item.url } },
    catalog: { ...states.signed_in.catalog, instances: [...states.signed_in.catalog.instances,
      { ...item, id: 'offline-control', name: 'Offline control', available: false }] } };
  const invoke = mount(state, (request) => ({ ...replies.success, state, request_id: request.request_id }));
  await waitFor(() => expect(invoke).toHaveBeenCalledOnce());
  const notice = /Mini and native notifications are unavailable|第一阶段暂不支持远程实例/;
  expect(screen.queryByText(notice)).toBeNull();
  if (view === 'account') {
    fireEvent.click(screen.getByRole('button', { name: lng === 'en' ? 'Instances' : '实例', exact: true }));
    await waitFor(() => expect(invoke).toHaveBeenCalledTimes(2));
  }
  const beforeNavigation = invoke.mock.calls.length;
  expect(document.querySelector('[data-instance-id="offline-control"] button')?.hasAttribute('disabled')).toBe(true);
  fireEvent.click(screen.getByRole('button', { name: state.account.identity!.email, exact: true }));
  expect(document.querySelector('[data-account-details]')).toBeTruthy();
  expect(screen.queryByText(notice)).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: lng === 'en' ? 'Instances' : '实例', exact: true }));
  await waitFor(() => expect(invoke).toHaveBeenCalledTimes(beforeNavigation + 1));
  expect(screen.queryByText(notice)).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: lng === 'en' ? 'Back to Workbench' : '返回工作台' }));
  await waitFor(() => expect(invoke).toHaveBeenCalledTimes(beforeNavigation + 2));
  expect(invoke.mock.calls.map((call) => call[1].request.action.type)).toEqual(
    view === 'account' ? ['get_state', 'refresh_instances', 'refresh_instances', 'cancel'] : ['get_state', 'refresh_instances', 'cancel']);
  expect(state.remote_auxiliary).toBe('disabled_incomplete');
  expect(state.connection.committed.kind).toBe('remote');
});

it('AUTH-SETUP-177 consumes the native UTF-8 catalog and sends its opaque instance id', async () => {
  const invoke = mount(states.signed_in, (request) => ({ ...replies.success, request_id: request.request_id }));
  expect(await screen.findByText('工作室 Studio')).toBeTruthy();
  expect(screen.getByText('This computer')).toBeTruthy();
  await waitFor(() => expect(invoke).toHaveBeenCalledOnce());
  fireEvent.click(screen.getByRole('button', { name: /工作室 Studio/ }));
  await waitFor(() => expect(invoke).toHaveBeenCalledTimes(2));
  expect(invoke.mock.calls[1][1].request.action).toEqual(requests.find((entry) => entry.action.type === 'select_instance')!.action);
});

it('AUTH-SETUP-177 consumes native consent before switching and sends explicit approval', async () => {
  const invoke = mount(states.consent, (request) => ({ ...replies.success, request_id: request.request_id, state: states.consent }));
  expect(await screen.findByRole('heading', { name: 'Connect to 工作室 Studio?' })).toBeTruthy();
  expect(screen.getByText("工作室 Studio isn't one of your instances. If you continue, it will receive your account email to sign you in.")).toBeTruthy();
  expect(screen.getByText('It will also receive: your profile name')).toBeTruthy();
  expect(screen.queryByText(/openid/)).toBeNull();
  await waitFor(() => expect(invoke).toHaveBeenCalledOnce());
  fireEvent.click(screen.getByRole('button', { name: 'Allow and connect' }));
  await waitFor(() => expect(invoke).toHaveBeenCalledTimes(2));
  expect(invoke.mock.calls[1][1].request.action).toEqual(requests.find((entry) => entry.action.type === 'decide_consent' && entry.action.approve)!.action);
});

// Raw OAuth scope is protocol vocabulary; the consent view states what the
// instance receives in plain words and still names anything beyond the email.
it.each([
  ['en', 'openid email', [], ['It will also receive', 'And also']],
  ['en', 'openid email profile', ['It will also receive: your profile name'], ['And also']],
  ['en', 'openid email offline_access', ['And also: offline_access'], ['It will also receive']],
  ['zh', 'openid email', [], ['它还会获得', '另外还有']],
  ['zh', 'openid profile email', ['它还会获得：你的个人资料名称'], ['另外还有']],
  ['zh', 'openid email offline_access', ['另外还有：offline_access'], ['它还会获得']],
])('AUTH-SETUP-177 consent copy (%s, %s) names received data in plain words', async (lng, scope, present, absent) => {
  await i18n.changeLanguage(lng);
  const state = { ...states.consent, consent: { ...states.consent.consent, scope } };
  mount(state, (request) => ({ ...replies.success, request_id: request.request_id, state }));
  const body = lng === 'en'
    ? "工作室 Studio isn't one of your instances. If you continue, it will receive your account email to sign you in."
    : '工作室 Studio 不是你的实例。继续后，它会获得你的账号邮箱，用来让你登录。';
  expect(await screen.findByText(body)).toBeTruthy();
  for (const text of present) expect(screen.getByText(text)).toBeTruthy();
  for (const text of absent) expect(screen.queryByText(new RegExp(text))).toBeNull();
  expect(screen.queryByText(/openid/)).toBeNull();
});

it.each([1, 2])('AUTH-SETUP-171 preserves direct recovery material only in the current generation (%s), independently of snapshot revision', async (generation) => {
  let finish!: (reply: unknown) => void;
  let finishRequest!: DesktopClientRequest;
  const invoke = mount(states.enroll, (request) => {
    if (request.action.type === 'finish_totp_enrollment') {
      finishRequest = request;
      return new Promise((resolve) => { finish = resolve; });
    }
    const sample = request.action.type === 'begin_totp_enrollment' ? replies.totp_enrollment
      : { ...replies.success, state: states.enroll };
    return { ...sample, request_id: request.request_id };
  });
  await waitFor(() => expect(invoke).toHaveBeenCalledOnce());
  fireEvent.click(screen.getByRole('button', { name: 'Add authenticator' }));
  expect(await screen.findByText(replies.totp_enrollment.result.secret)).toBeTruthy();
  fireEvent.change(screen.getByLabelText('Authenticator code'), { target: { value: '123456' } });
  fireEvent.click(screen.getByRole('button', { name: 'Verify and enable' }));
  await waitFor(() => expect(finishRequest).toBeDefined());
  // A later nonsecret broadcast can overtake a direct result. It must win state
  // ordering without losing one-time recovery codes from the still-current epoch.
  const newer = { ...states.signed_in, generation, revision: 12, local: { ...states.signed_in.local, name: '最新的 Mac', slug: 'latest-local-slug' } };
  fireEvent(window, new CustomEvent(DESKTOP_CLIENT_STATE_EVENT, { detail: newer }));
  await act(async () => finish({ ...replies.totp_enrolled, request_id: finishRequest.request_id, state: { ...states.signed_in, revision: 11 } }));
  expect(invoke.mock.calls.at(-1)![1].request.action).toEqual(requests.find((entry) => entry.action.type === 'finish_totp_enrollment')!.action);
  if (generation === 1) {
    expect(await screen.findByText(replies.totp_enrolled.result.recoveryCodes[0])).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'I have saved these codes' }));
    // Enrollment is still unknown: acknowledgement reveals account state.
    fireEvent.click(screen.getByRole('button', { name: 'Instances', exact: true }));
  }
  expect(screen.queryByText(replies.totp_enrolled.result.recoveryCodes[0])).toBeNull();
  expect(screen.getByText('latest-local-slug')).toBeTruthy();
  expect(JSON.stringify(window.__AVIBE_DESKTOP_CLIENT_STATE__)).not.toContain(replies.totp_enrollment.result.secret);
  expect(JSON.stringify({ ...localStorage, ...sessionStorage })).not.toContain(replies.totp_enrolled.result.recoveryCodes[0]);
});

// Feedback.2 merges ownership sections but must preserve native recency/server order.
it('shows the current shared instance once, followed by native-ordered other instances and the local escape', async () => {
  const instance = states.signed_in.catalog.instances[0];
  const row = (id: string, relationship: 'owner' | 'shared') => ({ ...instance, id, name: id.toUpperCase(), relationship, host: `${id}.avibe.bot`, url: `https://${id}.avibe.bot` });
  const state = { ...states.signed_in, current_instance: row('zeta', 'shared'),
    connection: { status: 'remote_connected', pending: null, committed: { kind: 'remote', name: 'ZETA', instance_id: 'zeta', canonical_origin: 'https://zeta.avibe.bot' } },
    catalog: { status: 'ready', instances: [row('zeta', 'shared'), row('beta', 'owner'), row('gamma', 'shared'), row('alpha', 'owner')] },
  };
  mount(state, (request) => ({ ...replies.success, request_id: request.request_id, state }));
  const current = await screen.findByRole('region', { name: 'Currently using' });
  expect(within(current).getByText('ZETA')).toBeTruthy();
  expect(current.querySelector('[data-instance-icon="shared"]')).toBeTruthy();
  expect(screen.getAllByText('ZETA')).toHaveLength(1);
  const other = screen.getByRole('region', { name: 'Other instances' });
  expect([...other.querySelectorAll('[data-instance-id]')].map((node) => node.getAttribute('data-instance-id'))).toEqual(['beta', 'gamma', 'alpha', 'local']);
  expect(within(other).getByText('This computer')).toBeTruthy();
});

// The old full-page transition and bottom error never kept the selected row visible.
// Exercise AccountApp + the real bridge hook with a deferred native reply.
it('keeps opening, failure and retry on the selected instance without replacing the list', async () => {
  let finish!: (value: unknown) => void;
  let pendingRequest!: DesktopClientRequest;
  const invoke = mount(states.signed_in, (request) => {
    if (request.action.type === 'select_instance') {
      pendingRequest = request;
      return new Promise((resolve) => { finish = resolve; });
    }
    return { ...replies.success, request_id: request.request_id };
  });
  await waitFor(() => expect(invoke).toHaveBeenCalledOnce());
  fireEvent.click(screen.getByRole('button', { name: /工作室 Studio/ }));
  const row = document.querySelector('[data-instance-id="instance-owned-fixture"]') ?? screen.getByText('工作室 Studio').closest('[data-instance-id]');
  expect(row).toBeTruthy();
  expect(within(row as HTMLElement).getByText('Opening…')).toBeTruthy();
  expect(document.querySelector('[data-instance-mask]')).toBeTruthy();
  for (const button of document.querySelectorAll('[data-instance-list] button')) expect((button as HTMLButtonElement).disabled).toBe(true);
  await act(async () => finish({ ...replies.success, request_id: pendingRequest.request_id, ok: false, error: { code: 'instance_upgrade_required' } }));
  expect(within(row as HTMLElement).getByRole('alert').textContent).toContain(en.desktopClient.errors.upgrade);
  expect(document.querySelector('[data-instance-mask]')).toBeNull();
  fireEvent.click(within(row as HTMLElement).getByRole('button', { name: 'Retry' }));
  await waitFor(() => expect(invoke).toHaveBeenCalledTimes(3));
  await act(async () => finish({ ...replies.success, request_id: pendingRequest.request_id }));
});

it('disables offline instances without expansion or a retry bypass and keeps page refresh usable', async () => {
  const initial = structuredClone(states.signed_in);
  initial.catalog.instances[0].available = false;
  const invoke = mount(initial, (request) => ({ ...replies.success, state: initial, request_id: request.request_id }));
  await waitFor(() => expect(invoke).toHaveBeenCalledOnce());
  const row = screen.getByText('工作室 Studio').closest('[data-instance-id]') as HTMLElement;
  const button = within(row).getByRole('button');
  expect((button as HTMLButtonElement).disabled).toBe(true);
  expect(button.hasAttribute('aria-expanded')).toBe(false);
  fireEvent.click(button); fireEvent.keyDown(button, { key: 'Enter' });
  expect(invoke).toHaveBeenCalledOnce();
  expect(within(row).queryByText(en.desktopClient.offlineInstructions)).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: 'Refresh instances' }));
  await waitFor(() => expect(invoke).toHaveBeenCalledTimes(2));
  expect(invoke.mock.calls[1][1].request.action).toEqual({ type: 'refresh_instances' });
});

it('keeps the list under consent and Escape declines only that consent', async () => {
  const invoke = mount(states.consent, (request) => ({ ...replies.success, request_id: request.request_id, state: states.consent }));
  await waitFor(() => expect(invoke).toHaveBeenCalledOnce());
  const dialog = screen.getByRole('dialog');
  expect(document.querySelector('[data-instance-list]')).toBeTruthy();
  fireEvent.keyDown(dialog, { key: 'Escape' });
  await waitFor(() => expect(invoke).toHaveBeenCalledTimes(2));
  expect(invoke.mock.calls[1][1].request.action).toEqual({ type: 'decide_consent', approve: false });
});

it('Escape during pending consent approval still cancels the native operation', async () => {
  let finish!: (reply: unknown) => void;
  let approval!: DesktopClientRequest;
  const invoke = mount(states.consent, (request) => {
    if (request.action.type === 'decide_consent') { approval = request; return new Promise((resolve) => { finish = resolve; }); }
    return { ...replies.success, request_id: request.request_id, state: states.consent };
  });
  await waitFor(() => expect(invoke).toHaveBeenCalledOnce());
  fireEvent.click(screen.getByRole('button', { name: 'Allow and connect' }));
  await waitFor(() => expect(approval).toBeDefined());
  fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' });
  await waitFor(() => expect(invoke).toHaveBeenCalledTimes(3));
  expect(invoke.mock.calls[2][1].request.action).toEqual({ type: 'cancel' });
  await act(async () => finish({ ...replies.success, request_id: approval.request_id }));
});


it.each([
  { currentKind: 'local', known: true },
  { currentKind: 'local', known: false },
  { currentKind: 'remote', known: true },
  { currentKind: 'remote', known: false },
])('deduplicates only a known authoritative local ID (current=$currentKind, known=$known), independently of enrollment and names', async ({ currentKind, known }) => {
  const item = states.signed_in.catalog.instances[0];
  const state = { ...states.signed_in,
    local: { ...states.signed_in.local, name: item.name, instance_id: known ? item.id : null, remote_access_url: null },
    enrollment: { ...states.signed_in.enrollment, instance_id: item.id },
    // A failed local refresh clears proof but retains the old committed ID.
    connection: { status: currentKind === 'local' ? 'local' : 'remote_connected', pending: null, committed: { kind: currentKind, name: 'Another instance', instance_id: currentKind === 'local' ? item.id : 'another', canonical_origin: 'https://another-app.avibe.bot' } },
  };
  const invoke = mount(state, (request) => ({ ...replies.success, state, request_id: request.request_id }));
  await waitFor(() => expect(invoke).toHaveBeenCalledOnce());
  const catalogRows = [...document.querySelectorAll('[data-instance-id]')];
  expect(catalogRows.some((node) => node.getAttribute('data-instance-id') === item.id)).toBe(!known);
  expect(catalogRows.filter((node) => node.getAttribute('data-instance-id') === 'local')).toHaveLength(1);
  if (currentKind === 'remote') expect(screen.getByRole('region', { name: 'Currently using' }).querySelector('[data-instance-icon="unknown"]')).toBeTruthy();
});

// Native advances generation when it starts selection; this is not a logout.
// Use its accepted generation, never predict it from the submitted request.
it.each([false, true])('selection generation advancement retains only a current row error (superseded=%s)', async (superseded) => {
  let finish!: (reply: unknown) => void;
  let request!: DesktopClientRequest;
  const initial = structuredClone(states.signed_in);
  const target = initial.catalog.instances[0];
  const advanced = { ...initial, generation: initial.generation + 7, revision: initial.revision + 1,
    connection: { ...initial.connection, status: 'switching', pending: { kind: 'remote', instance_id: target.id, name: target.name, canonical_origin: target.url } } };
  const invoke = mount(initial, (input) => {
    if (input.action.type === 'select_instance') { request = input; return new Promise((resolve) => { finish = resolve; }); }
    return { ...replies.success, state: initial, request_id: input.request_id };
  });
  await waitFor(() => expect(invoke).toHaveBeenCalledOnce());
  fireEvent.click(screen.getByRole('button', { name: /工作室 Studio/ }));
  await waitFor(() => expect(request).toBeDefined());
  fireEvent(window, new CustomEvent(DESKTOP_CLIENT_STATE_EVENT, { detail: advanced }));
  expect(screen.getByText('Opening…')).toBeTruthy();
  if (superseded) fireEvent(window, new CustomEvent(DESKTOP_CLIENT_STATE_EVENT, { detail: {
    ...initial, generation: advanced.generation + 9, account: { ...initial.account, status: 'signed_out', identity: null },
  } }));
  await act(async () => finish({ version: 1, request_id: request.request_id, ok: false,
    state: { ...advanced, connection: initial.connection }, error: { code: 'instance_upgrade_required' } }));
  if (superseded) expect(screen.queryByText(en.desktopClient.errors.upgrade)).toBeNull();
  else {
    const row = screen.getByText(target.name).closest('[data-instance-id]') as HTMLElement;
    expect(within(row).getByRole('alert').textContent).toContain(en.desktopClient.errors.upgrade);
    expect(within(row).getByRole('button', { name: 'Retry' })).toBeTruthy();
  }
});

// Admission remains in native. The product must not turn a non-get_state result,
// or a get_state reply overtaken by Escape, into a new automatic action.
it.each(['non-get-state', 'canceled-get-state'] as const)('does not auto-consume an unadmitted receipt (%s)', async (mode) => {
  let finish!: (reply: unknown) => void;
  let initialRequest!: DesktopClientRequest;
  const state = structuredClone(states.signed_in);
  const invoke = mount(state, (request) => {
    if (request.action.type === 'get_state' && mode === 'canceled-get-state') {
      initialRequest = request; return new Promise((resolve) => { finish = resolve; });
    }
    return { ...replies.success, request_id: request.request_id, state,
      ...(request.action.type === 'refresh_instances' ? { result: { type: 'sign_out_intent', intent_id: `unadmitted-${mode}`, generation: state.generation } } : {}) };
  });
  await waitFor(() => expect(invoke).toHaveBeenCalledOnce());
  if (mode === 'non-get-state') fireEvent.click(screen.getByRole('button', { name: 'Refresh instances' }));
  else {
    fireEvent.keyDown(window, { key: 'Escape' });
    await act(async () => finish({ ...replies.success, request_id: initialRequest.request_id, state,
      result: { type: 'sign_out_intent', intent_id: `unadmitted-${mode}`, generation: state.generation } }));
  }
  await waitFor(() => expect(invoke).toHaveBeenCalledTimes(2));
  expect(invoke.mock.calls.map((call) => call[1].request.action.type)).not.toContain('sign_out');
});

// The same document may remount while native cleanup is pending. Re-reading
// the same receipt must not submit a second cleanup request.
it('deduplicates an admitted sign-out across same-document remounts while cleanup is in flight', async () => {
  let finish!: (reply: unknown) => void;
  let pending!: DesktopClientRequest;
  const respond = (request: DesktopClientRequest) => {
    if (request.action.type === 'sign_out') {
      pending = request; return new Promise((resolve) => { finish = resolve; });
    }
    return { ...replies.success, request_id: request.request_id,
      result: { type: 'sign_out_intent', intent_id: 'same-document-pending', generation: states.signed_in.generation } };
  };
  const first = mount(states.signed_in, respond);
  await waitFor(() => expect(first.mock.calls.filter((call) => call[1].request.action.type === 'sign_out')).toHaveLength(1));
  cleanup();
  const second = mount(states.signed_in, respond);
  await waitFor(() => expect(second).toHaveBeenCalledOnce());
  await act(async () => { await Promise.resolve(); });
  expect(second.mock.calls.map((call) => call[1].request.action.type)).toEqual(['get_state']);
  await act(async () => finish({ ...replies.success, request_id: pending.request_id }));
});

// A local IPC failure has no native error envelope. It must still be visible on
// its selected row, but cannot revive a request retired by Escape or a new owner.
it.each(['reject', 'malformed'] as const)('correlates selected-row %s with its active native generation', async (failure) => {
  for (const advance of [false, true]) {
    const initial = structuredClone(states.signed_in);
    const target = initial.catalog.instances[0];
    let finish!: () => void;
    const invoke = mount(initial, (request) => {
      if (request.action.type === 'select_instance') return new Promise((resolve, reject) => {
        finish = () => failure === 'reject' ? reject(new Error('Synthetic IPC failure'))
          : resolve({ ...replies.success, request_id: 'wrong-request' });
      });
      return { ...replies.success, state: initial, request_id: request.request_id };
    });
    await waitFor(() => expect(invoke).toHaveBeenCalledOnce());
    fireEvent.click(screen.getByRole('button', { name: /工作室 Studio/ }));
    await waitFor(() => expect(finish).toBeDefined());
    if (advance) fireEvent(window, new CustomEvent(DESKTOP_CLIENT_STATE_EVENT, { detail: {
      ...initial, generation: initial.generation + 7, revision: initial.revision + 1,
      connection: { ...initial.connection, status: 'switching', pending: { kind: 'remote', instance_id: target.id, name: target.name, canonical_origin: target.url } },
    } }));
    await act(async () => finish());
    const row = screen.getByText(target.name).closest('[data-instance-id]') as HTMLElement;
    expect(within(row).getByRole('alert').textContent).toContain(en.desktopClient.errors.bridge);
    expect((within(row).getByRole('button', { name: 'Retry' }) as HTMLButtonElement).disabled).toBe(false);
    expect(document.querySelector('[data-instance-list]')).toBeTruthy();
    expect(document.querySelector('[data-instance-mask]')).toBeNull();
    cleanup();
  }
});

it.each(['reject', 'malformed'] as const)('does not revive selected-row %s after Escape or a newer account generation', async (failure) => {
  for (const retirement of ['escape', 'signed-out'] as const) {
    const initial = structuredClone(states.signed_in);
    let finish!: () => void;
    const invoke = mount(initial, (request) => {
      if (request.action.type === 'select_instance') return new Promise((resolve, reject) => {
        finish = () => failure === 'reject' ? reject(new Error('Synthetic retired IPC failure'))
          : resolve({ ...replies.success, request_id: 'wrong-retired-request' });
      });
      return { ...replies.success, state: initial, request_id: request.request_id };
    });
    await waitFor(() => expect(invoke).toHaveBeenCalledOnce());
    fireEvent.click(screen.getByRole('button', { name: /工作室 Studio/ }));
    await waitFor(() => expect(finish).toBeDefined());
    if (retirement === 'escape') fireEvent.keyDown(window, { key: 'Escape' });
    else fireEvent(window, new CustomEvent(DESKTOP_CLIENT_STATE_EVENT, { detail: {
      ...initial, generation: initial.generation + 9, account: { ...initial.account, status: 'signed_out', identity: null },
    } }));
    await act(async () => finish());
    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull();
    expect(document.querySelector('[data-instance-mask]')).toBeNull();
    cleanup();
  }
});

it('shows a native get_state cleanup error without manufacturing a sign-out receipt', async () => {
  window.history.replaceState({}, '', '?view=sign-out');
  try {
    const invoke = mount(states.signed_in, (request) => ({ ...replies.success, request_id: request.request_id, ok: false, error: { code: 'cleanup_error' } }));
    expect(await screen.findByRole('alert')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy();
    expect(invoke.mock.calls.map((call) => call[1].request.action.type)).toEqual(['get_state']);
  } finally {
    window.history.replaceState({}, '', '/');
  }
});

// The entry and details are distinct surfaces: the previous test checked email
// only after navigation, allowing the entry to ship a fixed Account label.
it.each([
  { identityEmail: 'identity@example.test', email: 'fallback@example.test', label: 'identity@example.test' },
  { identityEmail: null, email: 'fallback@example.test', label: 'fallback@example.test' },
  { identityEmail: null, email: null, label: 'Account' },
  { identityEmail: undefined, email: undefined, label: 'Account' },
])('account entry identifies the current account before navigation ($label/$identityEmail)', async ({ identityEmail, email, label }) => {
  let state = { ...states.signed_in, account: { ...states.signed_in.account,
    identity: identityEmail ? { id: 'entry-account', email: identityEmail } : null, email } };
  let finishRefresh: (() => void) | undefined;
  const invoke = mount(state, (request) => {
    const reply = { ...replies.success, state, request_id: request.request_id };
    if (request.action.type === 'refresh_instances') return new Promise((resolve) => { finishRefresh = () => resolve(reply); });
    return reply;
  });
  await waitFor(() => expect(invoke).toHaveBeenCalledOnce());
  // Email is absent here on the pre-fix product, before details can mask the omission.
  const entry = screen.getByRole('button', { name: label, exact: true });
  expect(within(entry).getByText(label)).toBeTruthy();
  expect(screen.getByRole('heading', { name: 'Account', level: 2 })).toBeTruthy();
  fireEvent.click(entry);
  const details = document.querySelector('[data-account-details]') as HTMLElement;
  expect(within(details).getByRole('heading', { name: 'Account' })).toBeTruthy();
  if (email || identityEmail) expect(within(details).getByText(label)).toBeTruthy();
  expect(invoke).toHaveBeenCalledOnce();
  fireEvent.click(within(details).getByRole('button', { name: 'Instances', exact: true }));
  await waitFor(() => expect(finishRefresh).toBeDefined());
  expect(screen.getByRole('button', { name: label, exact: true }).hasAttribute('disabled')).toBe(true);
  fireEvent.click(screen.getByRole('button', { name: label, exact: true }));
  expect(document.querySelector('[data-account-details]')).toBeNull();
  await act(async () => finishRefresh!());
  state = { ...state, generation: state.generation + 1, revision: state.revision + 1,
    account: { ...state.account, identity: { id: 'next-account', email: 'next@example.test' }, email: 'next@example.test' } };
  fireEvent(window, new CustomEvent(DESKTOP_CLIENT_STATE_EVENT, { detail: state }));
  expect(screen.getByRole('button', { name: 'next@example.test' })).toBeTruthy();
  expect(screen.queryByRole('button', { name: label, exact: true })).toBeNull();
  fireEvent(window, new CustomEvent(DESKTOP_CLIENT_STATE_EVENT, { detail: { ...state, generation: state.generation + 1,
    account: { ...state.account, identity: null, email: null },
    connection: { ...state.connection, status: 'switching', pending: { kind: 'remote', name: 'Pending', instance_id: 'pending', canonical_origin: 'https://pending.example.test' } } } }));
  const cleared = screen.getByRole('button', { name: 'Account', exact: true });
  expect(cleared.hasAttribute('disabled')).toBe(true);
  fireEvent.click(cleared);
  expect(screen.queryByText('next@example.test')).toBeNull();
  expect(document.querySelector('[data-account-details]')).toBeNull();
  expect(invoke.mock.calls.map((call) => call[1].request.action.type)).toEqual(['get_state', 'refresh_instances']);
});

// The removed popup used to own the local link and sign-out. The real container
// must retain them on the account page without treating presentation as consent.
it.each(['on', 'off', 'starting', 'unavailable'])('account page shows only the enabled local address and signs out explicitly (%s)', async (access) => {
  const state = { ...states.signed_in, local: { ...states.signed_in.local, remote_access: access, remote_access_url: 'https://local-fixture-app.avibe.bot' } };
  const invoke = mount(state, (request) => request.action.type === 'sign_out'
    ? { ...replies.success, state, request_id: request.request_id, ok: false, error: { code: 'cleanup_error' } }
    : { ...replies.success, state, request_id: request.request_id });
  await waitFor(() => expect(invoke).toHaveBeenCalledOnce());
  fireEvent.click(screen.getByRole('button', { name: state.account.identity!.email, exact: true }));
  expect(screen.getByRole('heading', { name: 'Account' })).toBeTruthy();
  expect(screen.getByText(state.account.identity!.email)).toBeTruthy();
  const link = screen.queryByRole('link', { name: state.local.remote_access_url });
  if (access === 'on') expect(link?.getAttribute('href')).toBe(state.local.remote_access_url);
  else expect(link).toBeNull();
  expect(invoke).toHaveBeenCalledOnce();
  fireEvent.click(screen.getByRole('button', { name: 'Sign out on this device' }));
  await waitFor(() => expect(invoke).toHaveBeenCalledTimes(2));
  expect(invoke.mock.calls[1][1].request.action).toEqual({ type: 'sign_out' });
  expect(await screen.findByRole('alert')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
  await waitFor(() => expect(invoke).toHaveBeenCalledTimes(3));
  expect(invoke.mock.calls[2][1].request.action).toEqual({ type: 'sign_out' });
});

it('Add instance gives supported target-machine instructions and returns with a refresh only', async () => {
  const invoke = mount(states.signed_in, (request) => ({ ...replies.success, request_id: request.request_id }));
  await waitFor(() => expect(invoke).toHaveBeenCalledOnce());
  fireEvent.click(screen.getByRole('button', { name: 'Add instance' }));
  expect(screen.getByText('vibe remote')).toBeTruthy();
  expect(screen.getByRole('link', { name: 'Open installation guide' }).getAttribute('href')).toBe('https://docs.avibe.bot/get-started/install');
  expect(invoke).toHaveBeenCalledOnce();
  fireEvent.click(screen.getByRole('button', { name: 'Return and refresh' }));
  await waitFor(() => expect(invoke).toHaveBeenCalledTimes(2));
  expect(invoke.mock.calls[1][1].request.action).toEqual({ type: 'refresh_instances' });
  expect(screen.getByRole('heading', { name: 'Instances', exact: true })).toBeTruthy();
});

it('a failed instance becoming offline removes its retry action without losing the row error', async () => {
  const invoke = mount(states.signed_in, (request) => ({ ...replies.success, request_id: request.request_id,
    ...(request.action.type === 'select_instance' ? { ok: false, error: { code: 'instance_upgrade_required' } } : {}) }));
  await waitFor(() => expect(invoke).toHaveBeenCalledOnce());
  fireEvent.click(screen.getByRole('button', { name: /工作室 Studio/ }));
  expect(await screen.findByRole('button', { name: 'Retry' })).toBeTruthy();
  const next = structuredClone(states.signed_in); next.revision += 20; next.catalog.instances[0].available = false;
  fireEvent(window, new CustomEvent(DESKTOP_CLIENT_STATE_EVENT, { detail: next }));
  expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull();
  const row = screen.getByText('工作室 Studio').closest('[data-instance-id]') as HTMLElement;
  expect(within(row).getByRole('alert')).toBeTruthy();
  expect((within(row).getByRole('button') as HTMLButtonElement).disabled).toBe(true);
});

// A5 metadata is display-only: native sends a validated snapshot when the
// current shared target disappears from a later catalog, and clears it on
// account/target changes. It must never become a selectable catalog entry.
it.each([null, undefined, 'validated'] as const)('current attribution uses only native proof despite a conflicting catalog (%s)', async (proof) => {
  const trusted = { ...states.signed_in.catalog.instances[0], relationship: 'shared', owner_email: 'verified-owner@example.test', available: true };
  const conflicting = { ...trusted, name: 'Unvalidated name', url: 'https://wrong-origin.example.test', host: 'wrong-origin.example.test',
    relationship: 'owner', owner_email: 'wrong-owner@example.test', available: false };
  const state = { ...states.signed_in, current_instance: proof === 'validated' ? trusted : proof,
    connection: { status: 'remote_connected', pending: null,
      committed: { kind: 'remote', name: 'Committed name', instance_id: trusted.id, canonical_origin: trusted.url } },
    catalog: { status: 'ready', instances: [conflicting] } };
  const invoke = mount(state, (request) => ({ ...replies.success, state, request_id: request.request_id }));
  await waitFor(() => expect(invoke).toHaveBeenCalledOnce());
  const current = screen.getByRole('region', { name: 'Currently using' });
  expect(within(current).getByText('Committed name')).toBeTruthy();
  expect(current.textContent).not.toContain('wrong-origin');
  expect(current.textContent).not.toContain('wrong-owner');
  expect(current.querySelector(`[data-instance-icon="${proof === 'validated' ? 'shared' : 'unknown'}"]`)).toBeTruthy();
  expect(within(current).getByRole('button').hasAttribute('disabled')).toBe(false);
  if (proof === 'validated') expect(within(current).getByText(/verified-owner/)).toBeTruthy();
  else expect(within(current).getByText(trusted.url)).toBeTruthy();
  expect(screen.getByRole('region', { name: 'Other instances' }).querySelector(`[data-instance-id="${trusted.id}"]`)).toBeNull();
  const cleared = { ...state, generation: state.generation + 1, revision: state.revision + 1, current_instance: null,
    account: { ...state.account, identity: { id: 'new-account', email: 'new@example.test' } } };
  fireEvent(window, new CustomEvent(DESKTOP_CLIENT_STATE_EVENT, { detail: cleared }));
  expect(current.querySelector('[data-instance-icon="unknown"]')).toBeTruthy();
  expect(current.textContent).not.toMatch(/verified-owner|wrong-origin|wrong-owner/);
  fireEvent(window, new CustomEvent(DESKTOP_CLIENT_STATE_EVENT, { detail: { ...cleared, generation: cleared.generation + 1,
    connection: { status: 'remote_unreachable', pending: null, committed: { kind: 'remote', name: 'New target', instance_id: 'new-target', canonical_origin: 'https://new-target.example.test' } },
    catalog: { status: 'ready', instances: [{ ...conflicting, id: 'new-target' }] },
  } }));
  expect(within(current).getByText('New target')).toBeTruthy();
  expect(within(current).getByText('https://new-target.example.test')).toBeTruthy();
  expect(current.querySelector('[data-instance-icon="unknown"]')).toBeTruthy();
  expect(within(current).getByRole('button').hasAttribute('disabled')).toBe(true);
  expect(invoke).toHaveBeenCalledOnce();
});

// The current row reads offline from the connection only: native's `available: false` on a still
// connected current instance neither disables it nor greys it (Max 2026-11-19 09:26).
it.each(['available', 'offline', 'unreachable'])('keeps omitted current shared metadata display-only (%s)', async (status) => {
  const offline = status === 'unreachable';
  const item = { ...states.signed_in.catalog.instances[0], relationship: 'shared', owner_email: 'owner@example.test', available: status !== 'offline' };
  const state = { ...states.signed_in, current_instance: item,
    connection: { status: status === 'unreachable' ? 'remote_unreachable' : 'remote_connected', pending: null,
      committed: { kind: 'remote', name: item.name, instance_id: item.id, canonical_origin: item.url } },
    catalog: { status: 'ready', instances: [] },
  };
  const invoke = mount(state, (request) => ({ ...replies.success, state, request_id: request.request_id }));
  await waitFor(() => expect(invoke).toHaveBeenCalledOnce());
  const current = screen.getByRole('region', { name: 'Currently using' });
  expect(current.querySelector('[data-instance-icon="shared"]')).toBeTruthy();
  expect(within(current).getByText(/owner@example.test/)).toBeTruthy();
  expect(screen.getByRole('region', { name: 'Other instances' }).querySelector(`[data-instance-id="${item.id}"]`)).toBeNull();
  expect(within(current).getByRole('button').hasAttribute('disabled')).toBe(offline);
  if (offline) {
    const button = within(current).getByRole('button');
    expect(button.hasAttribute('disabled')).toBe(true);
    fireEvent.click(button); fireEvent.keyDown(button, { key: 'Enter' });
    expect(invoke).toHaveBeenCalledOnce();
  }
  // A different account has no retained current-entry proof, even if the target
  // itself stays committed. A later target change likewise cannot reuse it.
  const changed = { ...state, generation: state.generation + 1, revision: state.revision + 1,
    account: { ...state.account, identity: { id: 'other-account', email: 'other@example.test' } }, current_instance: null };
  fireEvent(window, new CustomEvent(DESKTOP_CLIENT_STATE_EVENT, { detail: changed }));
  expect(current.querySelector('[data-instance-icon="unknown"]')).toBeTruthy();
  expect(within(current).queryByText(/owner@example.test/)).toBeNull();
  fireEvent(window, new CustomEvent(DESKTOP_CLIENT_STATE_EVENT, { detail: { ...changed, generation: changed.generation + 1,
    connection: { status: 'remote_connected', pending: null, committed: { kind: 'remote', name: 'Next target', instance_id: 'next', canonical_origin: 'https://next.example.test' } } } }));
  expect(within(current).getByText('Next target')).toBeTruthy();
  expect(current.querySelector('[data-instance-icon="unknown"]')).toBeTruthy();
  expect(invoke).toHaveBeenCalledOnce();
});

it.each([
  { lang: 'en', slug: 'catalog-slug', expected: 'catalog-slug' },
  { lang: 'en', slug: null, expected: 'This computer' },
  { lang: 'zh', slug: undefined, expected: '本机' },
])('local current displays only the authoritative slug or localized fallback ($lang/$slug)', async ({ lang, slug, expected }) => {
  await i18n.changeLanguage(lang);
  const state = { ...states.signed_in, local: { ...states.signed_in.local, slug } };
  const invoke = mount(state, (request) => ({ ...replies.success, state, request_id: request.request_id }));
  await waitFor(() => expect(invoke).toHaveBeenCalledOnce());
  const current = screen.getByRole('region', { name: lang === 'en' ? 'Currently using' : '正在使用' });
  expect(within(current).getAllByText(expected).length).toBeGreaterThan(0);
  expect(within(current).queryByText(state.local.name)).toBeNull();
});

// Restoring a signed-in account is presentation, not a newly completed login.
// Only native's initiating operation can automatically return the Workbench.
it('does not auto-cancel a restored signed-in state or a later successful broadcast', async () => {
  window.history.replaceState(null, '', '/account/index.html?view=account');
  const state = { ...states.signed_in, enrollment: { ...states.signed_in.enrollment, status: 'paired_this_account', error_code: null } };
  const invoke = mount(state, (request) => ({ ...replies.success, state, request_id: request.request_id }));
  await waitFor(() => expect(invoke).toHaveBeenCalledOnce());
  expect(document.querySelector('[data-account-details]')).toBeTruthy();
  await act(async () => { fireEvent(window, new CustomEvent(DESKTOP_CLIENT_STATE_EVENT, { detail: { ...state, revision: state.revision + 1 } })); });
  expect(invoke.mock.calls.map((call) => call[1].request.action.type)).toEqual(['get_state']);
});

it.each(['paired_this_account', 'failed', 'conflict', 'registering', 'paired_tunnel_unavailable', 'paired_error', 'new_owner']
  .flatMap((status) => ['back', 'escape'].map((exit) => ({ status, exit }))))(
  'acknowledges recovery codes before any eligible return ($status/$exit)', async ({ status, exit }) => {
    window.history.replaceState(null, '', '/account/index.html?view=account');
    const completed = { ...states.signed_in, enrollment: { ...states.signed_in.enrollment,
      status: status === 'paired_error' || status === 'new_owner' ? 'paired_this_account' : status,
      error_code: status === 'paired_error' || status === 'failed' ? 'persistence_failed' : null } };
    const invoke = mount(states.enroll, (request) => {
      const reply = request.action.type === 'begin_totp_enrollment' ? replies.totp_enrollment
        : request.action.type === 'finish_totp_enrollment' ? { ...replies.totp_enrolled, state: completed }
        : { ...replies.success, state: request.action.type === 'cancel' ? completed : states.enroll };
      return { ...reply, request_id: request.request_id };
    });
    await waitFor(() => expect(invoke).toHaveBeenCalledOnce());
    fireEvent.click(screen.getByRole('button', { name: 'Add authenticator' }));
    await screen.findByText(replies.totp_enrollment.result.secret);
    fireEvent.change(screen.getByLabelText('Authenticator code'), { target: { value: '123456' } });
    fireEvent.click(screen.getByRole('button', { name: 'Verify and enable' }));
    expect(await screen.findByText(replies.totp_enrolled.result.recoveryCodes[0])).toBeTruthy();
    expect(invoke.mock.calls.some((call) => call[1].request.action.type === 'cancel')).toBe(false);
    if (exit === 'back') expect(screen.queryByRole('button', { name: 'Back to Workbench' })).toBeNull();
    else fireEvent.keyDown(window, { key: 'Escape' });
    expect(screen.getByText(replies.totp_enrolled.result.recoveryCodes[0])).toBeTruthy();
    expect(invoke.mock.calls.some((call) => call[1].request.action.type === 'cancel')).toBe(false);
    expect(screen.queryByRole('button', { name: 'Back to Workbench' })).toBeNull();
    if (status === 'new_owner') {
      fireEvent(window, new CustomEvent(DESKTOP_CLIENT_STATE_EVENT, { detail: {
        ...completed, generation: completed.generation + 1, revision: completed.revision + 1,
        account: { ...completed.account, status: 'signed_out', identity: null },
      } }));
      expect(screen.queryByText(replies.totp_enrolled.result.recoveryCodes[0])).toBeNull();
      expect(screen.getByRole('button', { name: 'Back to Workbench' }).hasAttribute('disabled')).toBe(false);
      expect(invoke.mock.calls.some((call) => call[1].request.action.type === 'cancel')).toBe(false);
      return;
    }
    fireEvent.click(screen.getByRole('button', { name: 'I have saved these codes' }));
    await waitFor(() => expect(screen.queryByText(replies.totp_enrolled.result.recoveryCodes[0])).toBeNull());
    const cancels = invoke.mock.calls.filter((call) => call[1].request.action.type === 'cancel');
    expect(cancels).toHaveLength(status === 'paired_this_account' ? 1 : 0);
    if (status === 'failed') expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy();
    if (status === 'paired_error') expect(screen.getByRole('alert')).toBeTruthy();
    const back = screen.getByRole('button', { name: 'Back to Workbench' });
    expect(back.hasAttribute('disabled')).toBe(false);
    if (status !== 'paired_this_account') {
      fireEvent.click(back);
      await waitFor(() => expect(invoke.mock.calls.filter((call) => call[1].request.action.type === 'cancel')).toHaveLength(1));
    }
  },
);

// The native presentation event must reach the real material owner. Older tests
// exercised only React exits and missed replacement by menus/updater/deep links.
// Capture only location.assign: the guard, bridge URL helper and hook stay real.
function capturePresentationUrls() {
  const original = window;
  const assign = vi.fn();
  const location = new Proxy({} as Location, {
    get(_target, key) {
      if (key === 'assign') return assign;
      const value = Reflect.get(original.location, key, original.location);
      return typeof value === 'function' ? value.bind(original.location) : value;
    },
  });
  const receiver: Window = new Proxy(original, {
    get(target, key) {
      if (key === 'location') return location;
      if (key === 'top') return receiver;
      const value = Reflect.get(target, key, target);
      return typeof value === 'function' && !String(key).startsWith('HTML') ? value.bind(target) : value;
    },
  });
  vi.stubGlobal('window', receiver);
  return assign;
}
function askAccountPresentation(detail: unknown, cancelable = true) {
  const event = new CustomEvent(DESKTOP_PRESENT_EVENT, { cancelable, detail });
  window.dispatchEvent(event);
  return event;
}

it.each(['en', 'zh'] as const)('account native presentation preserves all five views, exact token and current locale (%s)', async (lang) => {
  const assign = capturePresentationUrls();
  const invoke = mount(states.signed_in, (request) => ({ ...replies.success, request_id: request.request_id }));
  await waitFor(() => expect(invoke).toHaveBeenCalledOnce());
  await act(async () => { await i18n.changeLanguage(lang); });
  for (const view of DESKTOP_CLIENT_VIEWS) {
    const presentationToken = `native/${view} 空&?`;
    expect(askAccountPresentation({ view, presentationToken }).defaultPrevented).toBe(true);
    expect(assign).toHaveBeenLastCalledWith(`avibe://desktop/${view}?native_request=${encodeURIComponent(presentationToken)}&locale=${lang}`);
  }
  expect(assign).toHaveBeenCalledTimes(5);
  expect(invoke).toHaveBeenCalledOnce();
  cleanup();
  assign.mockClear();
  expect(askAccountPresentation({ view: 'account', presentationToken: 'after-unmount' }).defaultPrevented).toBe(false);
  expect(assign).not.toHaveBeenCalled();
});

it('account native presentation rejects malformed or incompatible input without uncorrelated fallback', async () => {
  const assign = capturePresentationUrls();
  const invoke = mount(states.signed_in, (request) => ({ ...replies.success, request_id: request.request_id }));
  await waitFor(() => expect(invoke).toHaveBeenCalledOnce());
  for (const detail of [null, {}, { view: 'https://example.test', presentationToken: 'token' },
    { view: 'account' }, { view: 'account', presentationToken: 7 }, { view: 'account', presentationToken: '' },
    { view: 'account', presentationToken: 'x'.repeat(129) }, { view: 'account', presentationToken: '界'.repeat(43) }]) {
    askAccountPresentation(detail);
  }
  askAccountPresentation({ view: 'account', presentationToken: 'not-cancelable' }, false);
  expect(assign).not.toHaveBeenCalled();
  expect(askAccountPresentation({ view: 'account', presentationToken: 'x'.repeat(128) }).defaultPrevented).toBe(true);
  expect(assign).toHaveBeenCalledOnce();
  assign.mockClear();
  vi.stubGlobal('__AVIBE_DESKTOP_CLIENT_VERSION__', 2);
  askAccountPresentation({ view: 'account', presentationToken: 'incompatible' });
  expect(assign).not.toHaveBeenCalled();
  vi.stubGlobal('__AVIBE_DESKTOP_CLIENT_VERSION__', 1);
  const receiver = window;
  vi.stubGlobal('window', new Proxy(receiver, { get: (target, key) => key === 'top' ? null : Reflect.get(target, key) }));
  expect(askAccountPresentation({ view: 'account', presentationToken: 'embedded' }).defaultPrevented).toBe(false);
  expect(assign).not.toHaveBeenCalled();
});

it.each(['acknowledge', 'generation', 'native-sign-out', 'failure', 'unmount', 'generation-inflight'] as const)(
  'account native presentation protects finalization and pre-commit material, then retires on %s', async (retire) => {
    const assign = capturePresentationUrls();
    let finish!: (reply: unknown) => void;
    let finishRequest!: DesktopClientRequest;
    let resultRead!: () => void;
    const accepted = new Promise<void>((resolve) => { resultRead = resolve; });
    const completed = { ...states.signed_in, enrollment: { ...states.signed_in.enrollment,
      status: 'paired_this_account', error_code: 'persistence_failed' } };
    let current = states.enroll;
    const invoke = mount(current, (request) => {
      if (request.action.type === 'finish_totp_enrollment') {
        finishRequest = request;
        return new Promise((resolve) => { finish = resolve; });
      }
      const reply = request.action.type === 'begin_totp_enrollment' ? replies.totp_enrollment
        : { ...replies.success, state: current };
      return { ...reply, request_id: request.request_id };
    });
    await waitFor(() => expect(invoke).toHaveBeenCalledOnce());
    fireEvent.click(screen.getByRole('button', { name: 'Add authenticator' }));
    await screen.findByText(replies.totp_enrollment.result.secret);
    fireEvent.change(screen.getByLabelText('Authenticator code'), { target: { value: '123456' } });
    fireEvent.click(screen.getByRole('button', { name: 'Verify and enable' }));
    expect(finishRequest).toBeDefined();
    for (const view of DESKTOP_CLIENT_VIEWS) {
      expect(askAccountPresentation({ view, presentationToken: `finishing-${view}` }).defaultPrevented).toBe(true);
    }
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(assign).not.toHaveBeenCalled();
    expect(invoke.mock.calls.map((call) => call[1].request.action.type)).toEqual(['get_state', 'begin_totp_enrollment', 'finish_totp_enrollment']);
    if (retire === 'unmount') cleanup();
    if (retire === 'generation-inflight') {
      fireEvent(window, new CustomEvent(DESKTOP_CLIENT_STATE_EVENT, { detail: { ...completed, generation: completed.generation + 1 } }));
      expect(askAccountPresentation({ view: 'account', presentationToken: 'new-generation' }).defaultPrevented).toBe(true);
      expect(assign).toHaveBeenCalledExactlyOnceWith('avibe://desktop/account?native_request=new-generation&locale=en');
      await act(async () => finish({ ...replies.totp_enrolled, state: completed, request_id: finishRequest.request_id }));
      expect(screen.queryByText(replies.totp_enrolled.result.recoveryCodes[0])).toBeNull();
      return;
    }
    current = completed;
    await act(async () => {
      if (retire === 'failure') finish({ ...replies.success, ok: false, state: completed, request_id: finishRequest.request_id, error: { code: 'persistence_failed' } });
      else {
        finish({ ...replies.totp_enrolled, state: completed, request_id: finishRequest.request_id,
          get result() { resultRead(); return replies.totp_enrolled.result; } });
        if (retire !== 'unmount') {
          await accepted;
          // All synchronous result handling has finished, but act has not let
          // React commit the new recovery view. This is not a mocked guard.
          expect(screen.queryByText(replies.totp_enrolled.result.recoveryCodes[0])).toBeNull();
          expect(askAccountPresentation({ view: 'sign-out', presentationToken: 'reply-before-commit' }).defaultPrevented).toBe(true);
          expect(assign).not.toHaveBeenCalled();
        }
      }
    });
    if (retire === 'unmount') {
      expect(askAccountPresentation({ view: 'account', presentationToken: 'after-unmount' }).defaultPrevented).toBe(false);
      expect(assign).not.toHaveBeenCalled();
      return;
    }
    if (retire !== 'failure') {
      expect(screen.getByText(replies.totp_enrolled.result.recoveryCodes[0])).toBeTruthy();
      expect(screen.getByRole('alert')).toBeTruthy();
      for (const view of DESKTOP_CLIENT_VIEWS) expect(askAccountPresentation({ view, presentationToken: `codes-${view}` }).defaultPrevented).toBe(true);
      expect(assign).not.toHaveBeenCalled();
      if (retire === 'generation') {
        current = { ...completed, generation: completed.generation + 1, revision: completed.revision + 1 };
        fireEvent(window, new CustomEvent(DESKTOP_CLIENT_STATE_EVENT, { detail: current }));
      } else if (retire === 'native-sign-out') {
        // Remount is an actual new document owner, whose direct get_state
        // intent must still execute without being treated as a menu request.
        cleanup();
        mount(current, (request) => ({ ...replies.success, state: current, request_id: request.request_id,
          ...(request.action.type === 'get_state' ? { result: { type: 'sign_out_intent', intent_id: 'presentation-native-owner', generation: current.generation } } : {}) }));
        await waitFor(() => expect((window as unknown as { __TAURI_INTERNALS__: { invoke: ReturnType<typeof vi.fn> } }).__TAURI_INTERNALS__.invoke.mock.calls.some((call) => call[1].request.action.type === 'sign_out')).toBe(true));
      } else fireEvent.click(screen.getByRole('button', { name: 'I have saved these codes' }));
    }
    expect(screen.queryByText(replies.totp_enrolled.result.recoveryCodes[0])).toBeNull();
    expect(assign).not.toHaveBeenCalled(); // No deferred replay after acknowledgement/invalidation.
    expect(askAccountPresentation({ view: 'instances', presentationToken: 'fresh' }).defaultPrevented).toBe(true);
    expect(assign).toHaveBeenCalledExactlyOnceWith('avibe://desktop/instances?native_request=fresh&locale=en');
  },
);

// The popup uses AccountApp's panel, but must never participate in the Account
// presentation handshake. Existing popup tests covered only Escape/backdrop.
it.each(['en', 'zh'] as const)('update popup ignores Account presentation and receipts (%s)', async (lang) => {
  window.history.replaceState(null, '', '/account/index.html?view=update-popover');
  const assign = capturePresentationUrls();
  await i18n.changeLanguage(lang);
  const invoke = mount(states.signed_in, (request) => ({ ...replies.success, request_id: request.request_id,
    result: { type: 'sign_out_intent', intent_id: 'popup-must-not-consume', generation: states.signed_in.generation } }));
  await waitFor(() => expect(invoke).toHaveBeenCalledOnce());
  for (const detail of [...DESKTOP_CLIENT_VIEWS.map((view) => ({ view, presentationToken: 'popup/空' })), null, {}]) {
    expect(askAccountPresentation(detail).defaultPrevented).toBe(false);
  }
  expect(assign).not.toHaveBeenCalled();
  expect(invoke.mock.calls.map((call) => call[1].request.action.type)).toEqual(['get_state']);
});

// B1 (docs/plans/2026-11-20-update-panel-instance-and-app.md): the shell's own
// read of the selected instance is full-state input for the bundled pages only,
// with exactly five enumerated actions. Native roundtrips the same samples.
it('B1 instance update state and actions are bundled-page protocol only', () => {
  const actions: DesktopClientAction[] = requests.map((entry) => entry.action as DesktopClientAction)
    .filter((action) => action.type.startsWith('instance_update_'));
  expect(actions).toEqual([
    { type: 'instance_update_refresh' },
    { type: 'instance_update_check' },
    { type: 'instance_update_set_prereleases', enabled: true },
    { type: 'instance_update_set_auto_update', enabled: false },
    { type: 'instance_update_install', target_version: '3.2.8rc4' },
  ]);
  for (const [name, sample] of Object.entries(states)) {
    expect(sample.instance_update === null, name).toBe(name !== 'remote_connected');
  }
  const remote = states.remote_connected as unknown as DesktopClientState;
  expect(remote.instance_update).toMatchObject({ instance_id: remote.connection.committed.instance_id, status: 'ready', can_manage: true,
    request: { action: 'install', phase: 'sending', target: remote.instance_update?.snapshot?.latest,
      // The shell's own facts: it refuses other actions now, and the install started from what was running.
      following: true, installed_from: remote.instance_update?.snapshot?.current } });
  expect(remote.instance_update?.snapshot?.install).toEqual({ phase: 'installing', target: '3.2.8rc4', started_at: '2026-11-20T07:03:17Z' });
  expectTypeOf<DesktopClientProjection>().not.toHaveProperty('instance_update');
  // A shell from before B1 sends no field; consumers see null, not undefined.
  const { instance_update: _omitted, ...older } = remote;
  expect(withLocalIdentity(older as DesktopClientState).instance_update).toBeNull();
});
