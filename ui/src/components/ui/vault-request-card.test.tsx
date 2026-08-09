// @vitest-environment jsdom

import { createInstance } from 'i18next';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { afterEach, describe, expect, it, vi } from 'vitest';

import en from '@/i18n/en.json';
import type { VaultRequest } from '@/context/ApiContext';
import { VaultProvisionDialogProvider, VaultRequestCard } from './vault-request-card';

vi.mock('./vault-secret-dialog', () => ({
  VaultSecretDialog: ({
    open,
    onOpenChange,
    onCancel,
  }: {
    open: boolean;
    onOpenChange: (open: boolean) => void;
    onCancel?: () => void;
  }) => open ? (
    <div role="dialog">
      <button type="button" aria-label="close dialog" onClick={() => onOpenChange(false)}>Close</button>
      <button type="button" onClick={onCancel}>Ignore</button>
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

const renderProvisionCard = (onProvisionRequestHidden = vi.fn()) => render(
  <I18nextProvider i18n={i18n}>
    <VaultProvisionDialogProvider
      requests={[request]}
      onResolved={vi.fn()}
      onProvisionRequestHidden={onProvisionRequestHidden}
    >
      <VaultRequestCard request={request} onResolved={vi.fn()} />
    </VaultProvisionDialogProvider>
  </I18nextProvider>,
);

describe('VaultRequestCard provision dialog dismissal', () => {
  afterEach(() => cleanup());

  it('keeps the card after close and hides it after Ignore', () => {
    const onProvisionRequestHidden = vi.fn();
    renderProvisionCard(onProvisionRequestHidden);

    fireEvent.click(screen.getByRole('button', { name: /provide/i }));
    fireEvent.click(screen.getByRole('button', { name: 'close dialog' }));
    expect(screen.getByText(request.secret_name)).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: /provide/i }));
    fireEvent.click(screen.getByRole('button', { name: 'Ignore' }));
    expect(screen.queryByText(request.secret_name)).toBeNull();
    expect(onProvisionRequestHidden).toHaveBeenCalledWith(request.id);
  });
});
