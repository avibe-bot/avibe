// @vitest-environment jsdom

import { createInstance } from 'i18next';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';

import en from '@/i18n/en.json';
import type { VaultRequest } from '@/context/ApiContext';
import { VaultProvisionDialogProvider, VaultRequestCard } from './vault-request-card';
import { LazyVaultSecretDialog } from './vault-secret-dialog-lazy';

vi.mock('./vault-secret-dialog', () => ({
  VaultSecretDialog: ({
    open,
    onOpenChange,
    onCancel,
    onDeny,
  }: {
    open: boolean;
    onOpenChange: (open: boolean) => void;
    onCancel?: () => void;
    onDeny?: () => void;
  }) => open ? (
    <div role="dialog">
      <button type="button" aria-label="close dialog" onClick={() => onOpenChange(false)}>Close</button>
      <button type="button" onClick={onCancel}>Ignore</button>
      {onDeny ? <button type="button" onClick={onDeny}>Deny</button> : null}
    </div>
  ) : null,
}));

const i18n = createInstance();
void i18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: en } },
  interpolation: { escapeValue: false },
});

const request = {
  id: 'vrq_test',
  request_type: 'provision',
  secret_name: 'CHAINBOT_CLOUDFLARE_API_TOKEN',
  requester: {},
  delivery: {},
  status: 'pending',
  message_id: null,
  created_at: '2026-08-08T00:00:00Z',
  decided_at: null,
  expires_at: null,
  card: { request_type: 'provision' },
} satisfies VaultRequest;

const renderProvisionCard = (onProvisionRequestHidden = vi.fn(), onProvisionRequestDenied = vi.fn()) => render(
  <I18nextProvider i18n={i18n}>
    <VaultProvisionDialogProvider
      requests={[request]}
      onResolved={vi.fn()}
      onProvisionRequestHidden={onProvisionRequestHidden}
      onProvisionRequestDenied={onProvisionRequestDenied}
    >
      <VaultRequestCard request={request} onResolved={vi.fn()} />
    </VaultProvisionDialogProvider>
  </I18nextProvider>,
);

describe('VaultRequestCard provision dialog dismissal', () => {
  afterEach(() => cleanup());
  // The dialog is fetched on first use; load it once so these tests are about dismissal.
  beforeAll(async () => { await LazyVaultSecretDialog.preload(); });

  it('keeps the card after close and hides it after Ignore', async () => {
    const onProvisionRequestHidden = vi.fn();
    renderProvisionCard(onProvisionRequestHidden);

    fireEvent.click(screen.getByRole('button', { name: /provide/i }));
    fireEvent.click(await screen.findByRole('button', { name: 'close dialog' }));
    expect(screen.getByText(request.secret_name)).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: /provide/i }));
    fireEvent.click(await screen.findByRole('button', { name: 'Ignore' }));
    expect(screen.queryByText(request.secret_name)).toBeNull();
    expect(onProvisionRequestHidden).toHaveBeenCalledWith(request.id);
  });

  it('offers a terminal Deny action alongside the chat-only dismissal', async () => {
    const onProvisionRequestDenied = vi.fn(() => true);
    renderProvisionCard(vi.fn(), onProvisionRequestDenied);

    fireEvent.click(screen.getByRole('button', { name: /provide/i }));
    fireEvent.click(await screen.findByRole('button', { name: 'Deny' }));

    expect(onProvisionRequestDenied).toHaveBeenCalledWith(request.id);
  });
});

// The chat card for a proof-free Bitcoin summary: its key, Protected and Signing chips as the server
// names them, or the generic line when the name is withheld.
describe('VaultRequestCard proof-free Bitcoin summary', () => {
  afterEach(() => cleanup());
  const summary = (secret_name: string | null, protection: 'protected' | null) => ({
    ...request, id: 'vrq_btc', request_type: 'sign', secret_name, protection, requester: null, delivery: null, card: null,
  }) as unknown as VaultRequest;

  it.each([
    ['R9_PROT_KEY', 'protected', 'R9_PROT_KEY', true],
    [null, 'protected', en.vaults.requests.detailsOnReview, true],
    ['STD_KEY', null, 'STD_KEY', false],
  ] as const)('names %s (%s)', (name, protection, shown, isProtected) => {
    render(<I18nextProvider i18n={i18n}><VaultRequestCard request={summary(name, protection)} onResolved={vi.fn()} /></I18nextProvider>);
    expect(screen.getByText(shown)).toBeTruthy();
    expect(screen.getByText(en.vaults.signing)).toBeTruthy();
    expect(Boolean(screen.queryByText(en.vaults.protected))).toBe(isProtected);
  });
});
