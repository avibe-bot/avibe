// @vitest-environment jsdom

import { createInstance } from 'i18next';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { AvaultAuthorizationStatus } from '@/context/ApiContext';
import en from '@/i18n/en.json';
import zh from '@/i18n/zh.json';
import { isAvaultAuthorizationRequired } from '@/lib/useAvaultAuthorization';
import { AvaultAuthorizationNotice } from './avault-authorization-notice';

const api = vi.hoisted(() => ({
  getAvaultAuthorization: vi.fn(),
  authorizeAvault: vi.fn(),
}));
const authorization = vi.hoisted(() => ({ capabilities: { can_manage_instance: true } }));

vi.mock('@/context/ApiContext', () => ({ useApi: () => api }));
vi.mock('@/context/InstanceAuthorizationContext', () => ({
  useInstanceAuthorization: () => authorization,
}));

const i18n = createInstance();
void i18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: en } },
  interpolation: { escapeValue: false },
});

const status = (overrides: Partial<AvaultAuthorizationStatus> = {}): AvaultAuthorizationStatus => ({
  ok: true,
  required: true,
  detected_at: '2026-10-26T09:00:00Z',
  attempt: null,
  ...overrides,
});
const attempt = (state: 'running' | 'succeeded' | 'failed', extra: Record<string, string> = {}) => ({
  id: 'att_1',
  state,
  started_at: '2026-10-26T09:01:00Z',
  ...extra,
});

const renderNotice = (props: { forceVisible?: boolean } = {}) =>
  render(
    <I18nextProvider i18n={i18n}>
      <AvaultAuthorizationNotice {...props} />
    </I18nextProvider>,
  );

// Let the mocked API promises and the state updates they trigger settle.
const settle = () => act(async () => {
  await Promise.resolve();
  await Promise.resolve();
});

describe('AvaultAuthorizationNotice', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    api.getAvaultAuthorization.mockReset();
    api.authorizeAvault.mockReset();
    authorization.capabilities.can_manage_instance = true;
  });
  afterEach(() => {
    cleanup();
    vi.useRealTimers();
  });

  it('stays hidden while avault needs no authorization', async () => {
    api.getAvaultAuthorization.mockResolvedValue(status({ required: false }));
    renderNotice();
    await settle();

    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('says where the dialog appears and runs one authorization per click until it finishes', async () => {
    api.getAvaultAuthorization.mockResolvedValueOnce(status());
    api.authorizeAvault.mockResolvedValue(status({ attempt: attempt('running') }));
    renderNotice();
    await settle();

    expect(screen.getByText(en.vaults.avaultAuthorization.steps)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: en.vaults.avaultAuthorization.action }));
    await settle();
    expect(screen.getByText(en.vaults.avaultAuthorization.waiting)).toBeTruthy();
    const button = screen.getByRole('button', { name: en.vaults.avaultAuthorization.action }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    fireEvent.click(button);
    expect(api.authorizeAvault).toHaveBeenCalledTimes(1);

    // Only the status is polled while the person at the Mac answers.
    api.getAvaultAuthorization.mockResolvedValueOnce(status({ required: false, attempt: attempt('succeeded') }));
    await act(async () => {
      vi.advanceTimersByTime(1600);
    });
    await settle();

    expect(screen.getByText(en.vaults.avaultAuthorization.succeeded)).toBeTruthy();
    expect(api.authorizeAvault).toHaveBeenCalledTimes(1);
  });

  it('explains a failed attempt by its reason and shows avault detail', async () => {
    api.getAvaultAuthorization.mockResolvedValue(status());
    api.authorizeAvault.mockResolvedValue(
      status({ attempt: attempt('failed', { reason: 'no_gui_session', detail: 'avault: interaction not allowed' }) }),
    );
    renderNotice({ forceVisible: true });
    await settle();

    fireEvent.click(screen.getByRole('button', { name: en.vaults.avaultAuthorization.action }));
    await settle();

    expect(screen.getByText(en.vaults.avaultAuthorization.failed.no_gui_session)).toBeTruthy();
    expect(screen.getByText('avault: interaction not allowed')).toBeTruthy();
  });

  it('asks an admin when this user cannot manage the instance', async () => {
    authorization.capabilities.can_manage_instance = false;
    api.getAvaultAuthorization.mockResolvedValue(status());
    renderNotice();
    await settle();

    expect(screen.getByText(en.vaults.avaultAuthorization.askAdmin)).toBeTruthy();
    expect(screen.queryByRole('button')).toBeNull();
  });
});

describe('avault authorization copy', () => {
  it('names the code the Vault API sends', () => {
    expect(isAvaultAuthorizationRequired({ code: 'avault_authorization_required' })).toBe(true);
    expect(isAvaultAuthorizationRequired({ code: 'avault_failed' })).toBe(false);
    expect(isAvaultAuthorizationRequired(new Error('avault_authorization_required'))).toBe(false);
  });

  it('localizes every failure reason the backend reports in both languages', () => {
    const reasons = ['not_completed', 'no_gui_session', 'timed_out', 'avault_missing', 'avault_upgrade_required', 'failed'];
    for (const catalog of [en, zh]) {
      expect(Object.keys(catalog.vaults.avaultAuthorization.failed).sort()).toEqual([...reasons].sort());
      expect(catalog.errors.avault_authorization_required).toBeTruthy();
    }
  });
});
