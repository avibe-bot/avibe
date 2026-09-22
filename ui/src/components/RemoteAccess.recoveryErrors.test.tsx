/* @vitest-environment jsdom */

import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { createInstance } from 'i18next';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiProvider } from '../context/ApiContext';
import { InstanceAuthorizationProvider } from '../context/InstanceAuthorizationProvider';
import { normalizeSessionInfo, OWNER_INSTANCE_CAPABILITIES } from '../lib/sessionInfo';
import en from '../i18n/en.json';
import zh from '../i18n/zh.json';
import { RemoteAccess } from './RemoteAccess';

const apiFetch = vi.hoisted(() => vi.fn());
const showToast = vi.hoisted(() => vi.fn());
vi.mock('../lib/apiFetch', () => ({ apiFetch }));
vi.mock('../context/ToastContext', () => ({ useToast: () => ({ showToast }) }));

// Derive the bounded inventory from the producer: parity alone cannot detect
// a backend code omitted from BOTH locales. The actual API mapper and page
// below must consume every emitted pairing recovery error without a raw code.
const backendSource = readFileSync(
  join(__dirname, '../../../vibe/remote_access.py'), 'utf8',
);
const recoveryCodes = [...new Set(
  [...backendSource.matchAll(/"error":\s*"(pairing_[a-z_]+)"/g)].map((match) => match[1]),
)].sort();

const owner = normalizeSessionInfo({
  remote: false, authenticated: true, authorization_state: 'current',
  instance_role: 'owner', instance_kind: 'personal',
  capabilities: OWNER_INSTANCE_CAPABILITIES,
});
const jsonResponse = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status, headers: { 'Content-Type': 'application/json' },
});
const mountPage = async (language: 'en' | 'zh', errorCode: string) => {
  apiFetch.mockImplementation(async (path: string) => {
    if (path === '/api/remote-access/status') {
      return jsonResponse({ ok: true, paired: false, enabled: false, running: false, pending_pairing: null });
    }
    if (path === '/api/remote-access/vibe-cloud/pair') {
      // This is the real route's bare error shape, not an already-localized
      // mocked Error object. Do not bypass postJson/handleApiError.
      return jsonResponse({ ok: false, error: errorCode }, 400);
    }
    return jsonResponse({});
  });
  const i18n = createInstance();
  await i18n.use(initReactI18next).init({
    lng: language, fallbackLng: false,
    resources: { en: { translation: en }, zh: { translation: zh } },
    interpolation: { escapeValue: false },
  });
  render(
    <I18nextProvider i18n={i18n}>
      <ApiProvider>
        <InstanceAuthorizationProvider session={owner}>
          <RemoteAccess />
        </InstanceAuthorizationProvider>
      </ApiProvider>
    </I18nextProvider>,
  );
  const bundle = language === 'en' ? en : zh;
  const input = await screen.findByLabelText(bundle.remoteAccess.pairingKey);
  await waitFor(() => expect((input as HTMLInputElement).disabled).toBe(false));
  fireEvent.change(input, { target: { value: 'synthetic-one-time-key' } });
  fireEvent.click(screen.getByRole('button', { name: bundle.remoteAccess.pair }));
};

beforeEach(() => {
  apiFetch.mockReset();
  showToast.mockReset();
  vi.spyOn(console, 'error').mockImplementation(() => {});
  vi.stubGlobal('EventSource', class extends EventTarget {
    static OPEN = 1;
    readyState = 0;
    close() { this.readyState = 2; }
  });
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('pairing recovery through the real Web error and translation consumers', () => {
  it('discovers the producer error surface rather than a hand-maintained example', () => {
    expect(recoveryCodes).toContain('pairing_save_failed_after_redeem');
    expect(recoveryCodes).toContain('pairing_retirement_failed');
    expect(recoveryCodes).toContain('pairing_redeem_indeterminate');
    expect(recoveryCodes.length).toBeGreaterThan(3);
  });

  for (const language of ['en', 'zh'] as const) {
    it.each(recoveryCodes)(`${language}: localizes %s in both the page and global toast`, async (code) => {
      const errors: Record<string, string> = (language === 'en' ? en : zh).errors;
      const expected = errors[code];
      await mountPage(language, code);
      await waitFor(() => expect(showToast).toHaveBeenCalled());
      expect(expected, `missing errors.${code} in ${language}`).toBeTruthy();
      expect(expected).not.toBe(code);
      expect(await screen.findByText(expected)).toBeTruthy();
      expect(showToast).toHaveBeenCalledWith(expected, 'error');
      expect(screen.queryByText(code)).toBeNull();
      expect(apiFetch).toHaveBeenCalledWith('/api/remote-access/vibe-cloud/pair', expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          backend_url: 'https://avibe.bot', pairing_key: 'synthetic-one-time-key', device_name: 'avibe',
        }),
      }));
    });

    it(`${language}: preserves the shared unknown-code fallback`, async () => {
      await mountPage(language, 'future_pairing_failure');
      expect(await screen.findByText('future_pairing_failure')).toBeTruthy();
      expect(showToast).toHaveBeenCalledWith('future_pairing_failure', 'error');
    });
  }
});
