// @vitest-environment jsdom
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { createMemoryRouter, RouterProvider } from 'react-router-dom';
import { UnsavedChangesProvider } from '../context/UnsavedChangesProvider';
import { useUnsavedChanges } from '../context/useUnsavedChanges';
import { DesktopClientPresentation } from './useDesktopClient';
import { useAccountClient } from './useAccountClient';
import { DESKTOP_CLIENT_STATE_EVENT, DESKTOP_PRESENT_EVENT, isNewerClientState, presentDesktopClient, type DesktopClientRequest, type DesktopClientState } from './bridge';

function state(generation = 2, revision = 2): DesktopClientState {
  return { version: 1, generation, revision,
    account: { status: 'signed_out', identity: null, email: null, gate: null, methods: [] },
    connection: { status: 'local', committed: { kind: 'local', name: '小麦的 Mac', instance_id: null }, pending: null },
    enrollment: { status: 'unpaired', instance_id: null, error_code: null },
    catalog: { status: 'idle', instances: [] }, local: { name: '小麦的 Mac', remote_access: 'off' },
    desktop: { version: '3.2.6', update: { phase: 'idle', latest: null, channel: 'stable', skippedVersion: null, interactive: false } },
    consent: null, remote_auxiliary: 'local' };
}
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });
it('AUTH-SETUP-177 ignores unsupported native presentation and consumes a declined unsaved-changes guard', () => {
  vi.stubGlobal('__AVIBE_DESKTOP_CLIENT_VERSION__', 1);
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
  function Content() { useUnsavedChanges('Unsaved fixture'); return <DesktopClientPresentation />; }
  const router = createMemoryRouter([{ path: '/', element: <UnsavedChangesProvider><Content /></UnsavedChangesProvider> }]);
  render(<RouterProvider router={router} />);
  const bad = new CustomEvent(DESKTOP_PRESENT_EVENT, { cancelable: true, detail: { view: 'https://example.test' } });
  window.dispatchEvent(bad); expect(bad.defaultPrevented).toBe(false); expect(confirm).not.toHaveBeenCalled();
  const open = new CustomEvent(DESKTOP_PRESENT_EVENT, { cancelable: true, detail: { view: 'instances' } });
  window.dispatchEvent(open); expect(open.defaultPrevented).toBe(true); expect(confirm).toHaveBeenCalledWith('Unsaved fixture');
});
it('AUTH-SETUP-176 rejects an older native reply after a logout generation, and correlates get_state', async () => {
  vi.stubGlobal('__AVIBE_DESKTOP_CLIENT_VERSION__', 1);
  vi.stubGlobal('__AVIBE_DESKTOP_CLIENT_STATE__', state());
  let resolve!: (value: unknown) => void;
  let request!: DesktopClientRequest;
  vi.stubGlobal('__TAURI_INTERNALS__', { invoke: vi.fn((_command, args) => { request = args.request; return new Promise((done) => { resolve = done; }); }) });
  function Probe() { const { state: value } = useAccountClient(); return <p>{value?.generation}</p>; }
  render(<Probe />);
  await waitFor(() => expect(request.action.type).toBe('get_state'));
  expect(request.expected_generation).toBe(2);
  await act(async () => { window.dispatchEvent(new CustomEvent(DESKTOP_CLIENT_STATE_EVENT, { detail: state(3, 3) })); });
  await act(async () => { resolve({ version: 1, request_id: request.request_id, ok: true, state: state(2, 100) }); });
  expect(screen.getByText('3')).toBeTruthy();
  expect(isNewerClientState(state(2, 100), state(3, 3))).toBe(false);
});

it('AUTH-SETUP-177 only enumerated presentation URLs leave the Workbench', () => {
  const assign = vi.fn();
  const nativeWindow = { top: null as unknown, __AVIBE_DESKTOP_CLIENT_VERSION__: 1, location: { assign } };
  nativeWindow.top = nativeWindow;
  vi.stubGlobal('window', nativeWindow);
  expect(presentDesktopClient('updates')).toBe(true);
  expect(assign).toHaveBeenCalledExactlyOnceWith('avibe://updates');
  assign.mockClear();
  expect(presentDesktopClient('updates', 'native/req 1')).toBe(true);
  expect(assign).toHaveBeenCalledExactlyOnceWith('avibe://desktop/updates?native_request=native%2Freq%201');
  assign.mockClear();
  expect(presentDesktopClient('sign-out')).toBe(true);
  expect(assign).toHaveBeenCalledExactlyOnceWith('avibe://desktop/sign-out');
  assign.mockClear();
  expect(presentDesktopClient('https://example.test' as never)).toBe(false);
  expect(assign).not.toHaveBeenCalled();
  nativeWindow.__AVIBE_DESKTOP_CLIENT_VERSION__ = 2;
  expect(presentDesktopClient('account')).toBe(false);
});

// A4: a query alone cannot authorize automatic logout; only a correlated
// get_state result may reach the account component, and a newer epoch retires it.
it('exposes only a current direct sign-out receipt and clears it on escape', async () => {
  vi.stubGlobal('__AVIBE_DESKTOP_CLIENT_VERSION__', 1);
  vi.stubGlobal('__AVIBE_DESKTOP_CLIENT_STATE__', state());
  const invoke = vi.fn(async (_command, { request }: { request: DesktopClientRequest }) => ({
    version: 1, request_id: request.request_id, ok: true, state: state(),
    ...(request.action.type === 'get_state' ? { result: { type: 'sign_out_intent', intent_id: 'one-use', generation: 2 } } : {}),
  }));
  vi.stubGlobal('__TAURI_INTERNALS__', { invoke });
  function Probe() {
    const client = useAccountClient();
    const receipt = (client as typeof client & { signOutIntent?: { intent_id: string } | null }).signOutIntent;
    return <><p>{receipt?.intent_id ?? 'none'}</p><button onClick={() => void client.send({ type: 'cancel' })}>Escape</button></>;
  }
  render(<Probe />);
  await screen.findByText('one-use');
  await act(async () => { screen.getByText('Escape').click(); });
  expect(screen.getByText('none')).toBeTruthy();
  await act(async () => { window.dispatchEvent(new CustomEvent(DESKTOP_CLIENT_STATE_EVENT, { detail: state(3, 3) })); });
  expect(screen.getByText('none')).toBeTruthy();
});

// A5: older producers omit the new display metadata. The actual account hook
// must normalize absence, then accept the native snapshot without inventing
// a relationship from the committed target or an OS hostname.
it('normalizes missing display proof and accepts the current native instance snapshot', async () => {
  vi.stubGlobal('__AVIBE_DESKTOP_CLIENT_VERSION__', 1);
  vi.stubGlobal('__AVIBE_DESKTOP_CLIENT_STATE__', state());
  vi.stubGlobal('__TAURI_INTERNALS__', { invoke: vi.fn(async (_command, { request }) => ({
    version: 1, request_id: request.request_id, ok: true, state: state(),
  })) });
  function Probe() {
    const { state: value } = useAccountClient();
    return <p>{String(value?.local.slug)} / {String(value?.current_instance?.relationship ?? value?.current_instance)}</p>;
  }
  render(<Probe />);
  await screen.findByText('null / null');
  const next = {
    ...state(2, 3),
    account: { ...state().account, status: 'signed_in', identity: { id: 'user', email: 'user@example.test' } },
    connection: {
      status: 'remote_connected',
      committed: { kind: 'remote', instance_id: 'shared-id', name: 'shared-slug', canonical_origin: 'https://shared.avibe.bot' },
      pending: null,
    },
    local: { ...state().local, slug: 'registered-slug' },
    current_instance: {
      id: 'shared-id', name: 'shared-slug', host: 'shared.avibe.bot', url: 'https://shared.avibe.bot',
      relationship: 'shared', owner_email: 'owner@example.test', available: true,
    },
  };
  await act(async () => { window.dispatchEvent(new CustomEvent(DESKTOP_CLIENT_STATE_EVENT, { detail: next })); });
  expect(screen.getByText('registered-slug / shared')).toBeTruthy();
});
