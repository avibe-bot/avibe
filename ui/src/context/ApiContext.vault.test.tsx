/* @vitest-environment jsdom */
import { cleanup, renderHook } from '@testing-library/react';
import { createInstance } from 'i18next';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import zh from '@/i18n/zh.json';

const apiFetch = vi.hoisted(() => vi.fn());
const showToast = vi.hoisted(() => vi.fn());
vi.mock('../lib/apiFetch', async (original) => ({
  ...await original<typeof import('../lib/apiFetch')>(), apiFetch,
}));
vi.mock('./ToastContext', () => ({ useToast: () => ({ showToast }) }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: i18n.t.bind(i18n) }) }));
import { ApiProvider, useApi } from './ApiContext';

const i18n = createInstance();
void i18n.init({ lng: 'zh', resources: { zh: { translation: zh } }, interpolation: { escapeValue: false } });
beforeEach(() => {
  apiFetch.mockReset();
  showToast.mockReset();
  vi.spyOn(console, 'error').mockImplementation(() => {});
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

// The transport must send the version, not merely pass it to a mock API method.
it('sends the displayed field version in the DELETE request body', async () => {
  apiFetch.mockResolvedValue(new Response(JSON.stringify({ ok: true })));
  const { result } = renderHook(useApi, { wrapper: ApiProvider });
  await result.current.deleteVaultSecretField('LOGIN', 'username', { expected_updated_at: 'editor-version' });
  const [path, request] = apiFetch.mock.calls.at(-1)!;
  expect(path).toBe('/api/vault/secrets/LOGIN/fields/username');
  expect(request.method).toBe('DELETE');
  expect(request.body).toBe(JSON.stringify({ expected_updated_at: 'editor-version' }));
});

it('keeps a stale field DELETE inline instead of showing a global toast', async () => {
  apiFetch.mockResolvedValue(new Response(JSON.stringify({
    ok: false,
    code: 'secret_conflict',
    message: 'stale field version',
  }), { status: 409 }));
  const { result } = renderHook(useApi, { wrapper: ApiProvider });
  await expect(result.current.deleteVaultSecretField('LOGIN', 'username', {
    expected_updated_at: 'editor-version',
  })).resolves.toMatchObject({ ok: false, code: 'secret_conflict' });
  expect(showToast).not.toHaveBeenCalled();
});

// The Authorize avault notice polls this read; an error body taken for a status would
// drop the running attempt and end the polling before the person at the Mac answers.
it('rejects a failed avault authorization status read without a toast', async () => {
  apiFetch.mockResolvedValue(new Response(JSON.stringify({ ok: false, error: 'unavailable' }), { status: 503 }));
  const { result } = renderHook(useApi, { wrapper: ApiProvider });
  await expect(result.current.getAvaultAuthorization()).rejects.toMatchObject({ status: 503 });
  expect(showToast).not.toHaveBeenCalled();
});

it.each([
  'invalid_field_name', 'field_exists', 'field_not_found', 'too_many_fields',
  'fields_not_supported', 'record_mismatch', 'secret_conflict', 'fields_corrupt',
] as const)('localizes the Web toast for %s instead of displaying the service English', async (code) => {
  apiFetch.mockResolvedValue(new Response(JSON.stringify({ code, message: 'English service detail' }), { status: 409 }));
  const { result } = renderHook(useApi, { wrapper: ApiProvider });
  await expect(result.current.createVaultSecret({ name: 'LOGIN' })).rejects.toMatchObject({ code, message: zh.errors[code] });
  expect(showToast).toHaveBeenCalledWith(zh.errors[code], 'error');
});
