/* @vitest-environment jsdom */

// The shared approval dialog reads a proof-free summary in full before deciding anything
// (protected Bitcoin contract rev0.4 §4.3): protection, the passkey and the browser hand-off come
// from the full request; a failed or mismatched read shows no approval controls.
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { createInstance } from 'i18next';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { MemoryRouter } from 'react-router-dom';

import en from '@/i18n/en.json';
import type { VaultRequest } from '@/context/ApiContext';

import { VaultApprovalDialog } from './vault-approval-dialog';

const api = vi.hoisted(() => ({ getVaultRequest: vi.fn() }));
const shell = vi.hoisted(() => ({ desktop: false, opened: [] as unknown[] }));
vi.mock('@/context/ApiContext', async (original) => ({ ...await original<typeof import('@/context/ApiContext')>(), useApi: () => api }));
vi.mock('@/lib/vaultBrowserHandoff', () => ({
  vaultPasskeyNeedsBrowser: () => shell.desktop,
  openVaultsInBrowser: (step: unknown) => { shell.opened.push(step); },
}));
vi.mock('./vault-approval-card', () => ({
  DetailRow: ({ label, children }: { label: string; children: React.ReactNode }) => <div>{label}{children}</div>,
  VaultApprovalCard: ({ request }: { request: VaultRequest }) => <div data-testid="card">{`card:${request.id}:${request.secret_name}`}</div>,
}));

const i18n = createInstance();
void i18n.use(initReactI18next).init({ lng: 'en', fallbackLng: 'en', resources: { en: { translation: en } }, interpolation: { escapeValue: false } });

const summary: VaultRequest = {
  id: 'vrq_btc', request_type: 'sign', status: 'pending', created_at: '2026-11-15T00:00:00Z', decided_at: null, expires_at: null,
  secret_name: null, requester: null, delivery: null, message_id: null, card: null, session: null,
};
const full = (over: Partial<VaultRequest> = {}): VaultRequest => ({
  ...summary,
  secret_name: 'PROTECTED_BTC',
  delivery: { scheme: 'ecdsa-secp256k1-der', digest: '00'.repeat(32) },
  card: { request_type: 'sign', secret_name: 'PROTECTED_BTC', kind: 'keypair', protection: 'protected' },
  ...over,
});
const show = (request: VaultRequest | null) => render(
  <I18nextProvider i18n={i18n}><VaultApprovalDialog request={request} onResolved={vi.fn()} onClose={vi.fn()} /></I18nextProvider>,
  { wrapper: MemoryRouter },
);

beforeEach(() => { shell.desktop = false; shell.opened = []; });
afterEach(() => { cleanup(); vi.clearAllMocks(); });

it('reads a summary in full by its id, then mounts the card on the full request', async () => {
  api.getVaultRequest.mockResolvedValue({ ok: true, request: full() });
  show(summary);
  expect(screen.getByRole('status').textContent).toContain(en.vaults.requests.loadingFull);
  expect(screen.queryByTestId('card')).toBeNull();
  expect(await screen.findByText('card:vrq_btc:PROTECTED_BTC')).toBeTruthy();
  expect(api.getVaultRequest).toHaveBeenCalledExactlyOnceWith('vrq_btc', expect.objectContaining({ signal: expect.any(AbortSignal) }));
});

it('does not read again for a request that is already full', () => {
  show(full());
  expect(screen.getByText('card:vrq_btc:PROTECTED_BTC')).toBeTruthy();
  expect(api.getVaultRequest).not.toHaveBeenCalled();
});

it('decides the browser hand-off from the full request, after the read', async () => {
  shell.desktop = true;
  api.getVaultRequest.mockResolvedValue({ ok: true, request: full() });
  show(summary);
  const button = await screen.findByRole('button', { name: new RegExp(en.vaults.requests.reviewInBrowser) });
  expect(screen.queryByTestId('card')).toBeNull();
  fireEvent.click(button);
  expect(shell.opened).toEqual([{ kind: 'request', requestId: 'vrq_btc' }]);
});

// The desktop app sends a Protected approval to the browser; the Session that asked still opens here.
it('names the asking Session as a link on the browser hand-off', async () => {
  shell.desktop = true;
  api.getVaultRequest.mockResolvedValue({
    ok: true,
    request: full({ session: { id: 'ses_promote', title: 'Promote the build', label: null, platform: 'avibe', scope_kind: 'project', is_workbench: true } }),
  });
  show(summary);
  const link = await screen.findByRole('link', { name: /Promote the build/ });
  expect(link.getAttribute('href')).toBe('/chat/ses_promote');
  expect(screen.getByText(en.vaults.approval.session)).toBeTruthy();
});

it('a standard request read in full is approved here even in the desktop app', async () => {
  shell.desktop = true;
  api.getVaultRequest.mockResolvedValue({ ok: true, request: full({ card: { request_type: 'sign', secret_name: 'PROTECTED_BTC', kind: 'keypair', protection: 'standard' } }) });
  show(summary);
  expect(await screen.findByTestId('card')).toBeTruthy();
});

it.each([
  ['another id', { ok: true, request: full({ id: 'vrq_other' }) }, en.vaults.requests.loadFullFailed, true],
  ['a request no longer pending', { ok: true, request: full({ status: 'approved' }) }, en.vaults.approval.errors.loadFailed, false],
  ['not found', { ok: false, code: 'request_not_found' }, en.vaults.approval.errors.loadFailed, false],
  ['a server error', { ok: false, code: 'internal' }, en.vaults.requests.loadFullFailed, true],
])('shows no approval controls for %s', async (_label, reply, message, retry) => {
  api.getVaultRequest.mockResolvedValue(reply);
  show(summary);
  expect(await screen.findByText(message)).toBeTruthy();
  expect(screen.queryByTestId('card')).toBeNull();
  expect(Boolean(screen.queryByRole('button', { name: new RegExp(en.vaults.requests.retry) }))).toBe(retry);
});

it('retries a failed read and drops an answer for a request no longer selected', async () => {
  api.getVaultRequest.mockRejectedValueOnce(new TypeError('Failed to fetch'));
  const { rerender } = show(summary);
  const retry = await screen.findByRole('button', { name: new RegExp(en.vaults.requests.retry) });
  let answer: (value: unknown) => void = () => undefined;
  api.getVaultRequest.mockImplementationOnce(() => new Promise((resolve) => { answer = resolve; }));
  fireEvent.click(retry);
  // Closed before the answer: nothing is mounted when it arrives.
  rerender(<I18nextProvider i18n={i18n}><VaultApprovalDialog request={null} onResolved={vi.fn()} onClose={vi.fn()} /></I18nextProvider>);
  await act(async () => { answer({ ok: true, request: full() }); });
  expect(screen.queryByTestId('card')).toBeNull();
});
