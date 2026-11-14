// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { createInstance } from 'i18next';
import { I18nextProvider } from 'react-i18next';
import { afterEach, expect, it, vi } from 'vitest';
import en from '../i18n/en.json';
import zh from '../i18n/zh.json';
import { AccountApp } from './AccountApp';
import { DESKTOP_CLIENT_STATE_EVENT, type DesktopClientRequest } from './bridge';
import states from './__fixtures__/instance-client-v1/states.json';
import replies from './__fixtures__/instance-client-v1/replies.json';
import requests from './__fixtures__/instance-client-v1/requests.json';

// The prior peer tests construct U1-shaped state. These exact S1 samples catch
// consumer field drift by entering the real account hook and rendered actions.
const i18n = createInstance();
await i18n.init({ lng: 'en', resources: { en: { translation: en }, zh: { translation: zh } }, interpolation: { escapeValue: false } });
afterEach(async () => { cleanup(); vi.unstubAllGlobals(); await i18n.changeLanguage('en'); });
function mount(initial: unknown, response: (request: DesktopClientRequest) => unknown) {
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

it('AUTH-SETUP-177 consumes the native UTF-8 catalog and sends its opaque instance id', async () => {
  const invoke = mount(states.signed_in, (request) => ({ ...replies.success, request_id: request.request_id }));
  expect(await screen.findByText('工作室 Studio')).toBeTruthy();
  expect(screen.getByText('小明的 Mac')).toBeTruthy();
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
  const newer = { ...states.signed_in, generation, revision: 12, local: { ...states.signed_in.local, name: '最新的 Mac' } };
  fireEvent(window, new CustomEvent(DESKTOP_CLIENT_STATE_EVENT, { detail: newer }));
  await act(async () => finish({ ...replies.totp_enrolled, request_id: finishRequest.request_id, state: { ...states.signed_in, revision: 11 } }));
  expect(invoke.mock.calls.at(-1)![1].request.action).toEqual(requests.find((entry) => entry.action.type === 'finish_totp_enrollment')!.action);
  if (generation === 1) {
    expect(await screen.findByText(replies.totp_enrolled.result.recoveryCodes[0])).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'I have saved these codes' }));
  }
  expect(screen.queryByText(replies.totp_enrolled.result.recoveryCodes[0])).toBeNull();
  expect(screen.getByText('最新的 Mac')).toBeTruthy();
  expect(JSON.stringify(window.__AVIBE_DESKTOP_CLIENT_STATE__)).not.toContain(replies.totp_enrollment.result.secret);
  expect(JSON.stringify({ ...localStorage, ...sessionStorage })).not.toContain(replies.totp_enrolled.result.recoveryCodes[0]);
});

it('keeps the native instance order inside each section, with the current shared instance leading Shared', async () => {
  // Native orders the catalog: current, then opened on this device, then avibe.bot.
  // The page only splits it into Owned and Shared; it must not re-sort.
  const instance = states.signed_in.catalog.instances[0];
  const row = (id: string, relationship: 'owner' | 'shared') => ({
    ...instance, id, name: id.toUpperCase(), host: `${id}.avibe.bot`, url: `https://${id}.avibe.bot`, relationship,
  });
  const state = {
    ...states.signed_in,
    connection: {
      status: 'remote_connected', pending: null,
      committed: { kind: 'remote', name: 'ZETA', instance_id: 'zeta', canonical_origin: 'https://zeta.avibe.bot' },
    },
    catalog: { status: 'ready', instances: [row('zeta', 'shared'), row('beta', 'owner'), row('alpha', 'owner'), row('gamma', 'shared')] },
  };
  mount(state, (request) => ({ ...replies.success, request_id: request.request_id, state }));
  const names = (heading: string) => within(screen.getByRole('heading', { name: heading }).closest('section')!)
    .getAllByRole('button').map((button) => button.textContent?.match(/[A-Z]+/)?.[0]);
  await waitFor(() => expect(names('Your instances')).toEqual(['BETA', 'ALPHA']));
  expect(names('Shared with you')).toEqual(['ZETA', 'GAMMA']);
  const shared = within(screen.getByRole('heading', { name: 'Shared with you' }).closest('section')!).getAllByRole('button');
  expect(within(shared[0]).getByLabelText('Current')).toBeTruthy();
  expect(within(shared[1]).queryByLabelText('Current')).toBeNull();
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

it('expands an offline row locally and refreshes without issuing select_instance', async () => {
  const initial = structuredClone(states.signed_in);
  initial.catalog.instances[0].available = false;
  const invoke = mount(initial, (request) => ({ ...replies.success, state: initial, request_id: request.request_id }));
  await waitFor(() => expect(invoke).toHaveBeenCalledOnce());
  fireEvent.click(screen.getByRole('button', { name: /工作室 Studio/ }));
  const row = screen.getByText('工作室 Studio').closest('[data-instance-id]') as HTMLElement;
  expect(within(row).getByText(en.desktopClient.offlineInstructions)).toBeTruthy();
  expect(invoke).toHaveBeenCalledOnce();
  fireEvent.click(within(row).getByRole('button', { name: 'Refresh instances' }));
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


it.each([true, false])('deduplicates only a known authoritative local ID (%s), independently of enrollment and names', async (known) => {
  const item = states.signed_in.catalog.instances[0];
  const state = { ...states.signed_in,
    local: { ...states.signed_in.local, name: item.name, instance_id: known ? item.id : null, remote_access_url: null },
    enrollment: { ...states.signed_in.enrollment, instance_id: item.id },
    connection: { status: 'remote_connected', pending: null, committed: { kind: 'remote', name: 'Another instance', instance_id: 'another', canonical_origin: 'https://another-app.avibe.bot' } },
  };
  const invoke = mount(state, (request) => ({ ...replies.success, state, request_id: request.request_id }));
  await waitFor(() => expect(invoke).toHaveBeenCalledOnce());
  const catalogRows = [...document.querySelectorAll('[data-instance-id]')];
  expect(catalogRows.some((node) => node.getAttribute('data-instance-id') === item.id)).toBe(!known);
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
