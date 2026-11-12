// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { createInstance } from 'i18next';
import { I18nextProvider } from 'react-i18next';
import { afterEach, expect, it, vi } from 'vitest';
import en from '../i18n/en.json';
import { AccountApp } from './AccountApp';
import type { DesktopClientRequest } from './bridge';
import states from './__fixtures__/instance-client-v1/states.json';
import replies from './__fixtures__/instance-client-v1/replies.json';
import requests from './__fixtures__/instance-client-v1/requests.json';

// The prior peer tests construct U1-shaped state. These exact S1 samples catch
// consumer field drift by entering the real account hook and rendered actions.
const i18n = createInstance();
await i18n.init({ lng: 'en', resources: { en: { translation: en } } });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
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
  expect(screen.getByText('This Mac · 小明的 Mac')).toBeTruthy();
  await waitFor(() => expect(invoke).toHaveBeenCalledOnce());
  fireEvent.click(screen.getByRole('button', { name: /工作室 Studio/ }));
  await waitFor(() => expect(invoke).toHaveBeenCalledTimes(2));
  expect(invoke.mock.calls[1][1].request.action).toEqual(requests.find((entry) => entry.action.type === 'select_instance')!.action);
});

it('AUTH-SETUP-177 consumes native consent before switching and sends explicit approval', async () => {
  const invoke = mount(states.consent, (request) => ({ ...replies.success, request_id: request.request_id, state: states.consent }));
  expect(await screen.findByRole('heading', { name: 'Connect to 工作室 Studio?' })).toBeTruthy();
  expect(screen.getByText('openid profile email')).toBeTruthy();
  await waitFor(() => expect(invoke).toHaveBeenCalledOnce());
  fireEvent.click(screen.getByRole('button', { name: 'Allow and connect' }));
  await waitFor(() => expect(invoke).toHaveBeenCalledTimes(2));
  expect(invoke.mock.calls[1][1].request.action).toEqual(requests.find((entry) => entry.action.type === 'decide_consent' && entry.action.approve)!.action);
});

it('AUTH-SETUP-171 renders native direct TOTP and recovery results without projecting secrets', async () => {
  const invoke = mount(states.enroll, (request) => {
    const sample = request.action.type === 'begin_totp_enrollment' ? replies.totp_enrollment
      : request.action.type === 'finish_totp_enrollment' ? replies.totp_enrolled
      : { ...replies.success, state: states.enroll };
    return { ...sample, request_id: request.request_id };
  });
  await waitFor(() => expect(invoke).toHaveBeenCalledOnce());
  fireEvent.click(screen.getByRole('button', { name: 'Add authenticator' }));
  expect(await screen.findByText(replies.totp_enrollment.result.secret)).toBeTruthy();
  fireEvent.change(screen.getByLabelText('Authenticator code'), { target: { value: '123456' } });
  fireEvent.click(screen.getByRole('button', { name: 'Verify and enable' }));
  expect(await screen.findByText(replies.totp_enrolled.result.recoveryCodes[0])).toBeTruthy();
  expect(invoke.mock.calls.at(-1)![1].request.action).toEqual(requests.find((entry) => entry.action.type === 'finish_totp_enrollment')!.action);
  fireEvent.click(screen.getByRole('button', { name: 'I have saved these codes' }));
  expect(screen.queryByText(replies.totp_enrolled.result.recoveryCodes[0])).toBeNull();
  expect(JSON.stringify(window.__AVIBE_DESKTOP_CLIENT_STATE__)).not.toContain(replies.totp_enrollment.result.secret);
  expect(JSON.stringify({ ...localStorage, ...sessionStorage })).not.toContain(replies.totp_enrolled.result.recoveryCodes[0]);
});
