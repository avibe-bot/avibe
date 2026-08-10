/* @vitest-environment jsdom */

import type { ReactNode } from 'react';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { createInstance } from 'i18next';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { I18nextProvider, initReactI18next } from 'react-i18next';

import en from '@/i18n/en.json';
import type { VaultRequest } from '@/context/ApiContext';
import { VaultSecretDialog } from './vault-secret-dialog';

vi.mock('./dialog', () => ({
  Dialog: ({ children, open = true }: { children: ReactNode; open?: boolean }) => open ? <div>{children}</div> : null,
  DialogContent: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  DialogDescription: ({ children }: { children: ReactNode }) => <p>{children}</p>,
  DialogFooter: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  DialogHeader: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  DialogTitle: ({ children }: { children: ReactNode }) => <h1>{children}</h1>,
}));

vi.mock('./vault-secret-form', () => ({
  VaultSecretForm: ({ onDeny }: { onDeny?: () => void }) => (
    <button type="button" onClick={onDeny}>deny-in-form</button>
  ),
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
  secret_name: 'CHAT_SECRET',
  status: 'pending',
  card: { request_type: 'provision' },
} as VaultRequest;

const renderDialog = (onDeny: () => boolean) => render(
  <I18nextProvider i18n={i18n}>
    <VaultSecretDialog
      open
      onOpenChange={vi.fn()}
      request={request}
      onDeny={onDeny}
      onCreated={vi.fn()}
    />
  </I18nextProvider>,
);

describe('VaultSecretDialog provision denial', () => {
  afterEach(() => cleanup());

  it('confirms before invoking the terminal denial callback', async () => {
    const onDeny = vi.fn(() => true);
    const onOpenChange = vi.fn();
    render(
      <I18nextProvider i18n={i18n}>
        <VaultSecretDialog
          open
          onOpenChange={onOpenChange}
          request={request}
          onDeny={onDeny}
          onCreated={vi.fn()}
        />
      </I18nextProvider>,
    );

    fireEvent.click(screen.getByRole('button', { name: 'deny-in-form' }));
    expect(onDeny).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Deny' }));

    await waitFor(() => expect(onDeny).toHaveBeenCalledTimes(1));
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it('keeps the confirmation open when denial fails', async () => {
    const onDeny = vi.fn(() => false);
    renderDialog(onDeny);

    fireEvent.click(screen.getByRole('button', { name: 'deny-in-form' }));
    fireEvent.click(screen.getByRole('button', { name: 'Deny' }));

    await waitFor(() => expect(onDeny).toHaveBeenCalledTimes(1));
    expect(screen.getByRole('button', { name: 'Deny' })).toBeTruthy();
  });
});
