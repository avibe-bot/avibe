/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { createInstance } from 'i18next';
import { afterEach, expect, it, vi } from 'vitest';
import { I18nextProvider, initReactI18next } from 'react-i18next';

import en from '@/i18n/en.json';
import { pressOutside } from '@/lib/testing/pressOutside';
import { VaultSecretDialog } from './vault-secret-dialog';

// The real Radix dialog; only the form inside is a stand-in for something typed into.
vi.mock('./vault-secret-form', () => ({
  VaultSecretForm: () => <input aria-label="typed value" defaultValue="half-typed secret" />,
}));

const i18n = createInstance();
void i18n.use(initReactI18next).init({
  lng: 'en', fallbackLng: 'en', resources: { en: { translation: en } }, interpolation: { escapeValue: false },
});

afterEach(() => cleanup());

// Owner, 2026-11-12: a dialog the user types or pastes into keeps it on a click outside; the X,
// Cancel and Escape still close it.
it('keeps the Add key dialog open on a click outside, and closes it on Escape', async () => {
  const onOpenChange = vi.fn();
  render(
    <I18nextProvider i18n={i18n}>
      <VaultSecretDialog open onOpenChange={onOpenChange} onCreated={vi.fn()} />
    </I18nextProvider>,
  );
  expect(screen.getByRole('textbox', { name: 'typed value' })).toBeTruthy();
  await pressOutside();
  expect(onOpenChange).not.toHaveBeenCalled();
  fireEvent.keyDown(document.activeElement ?? document.body, { key: 'Escape' });
  expect(onOpenChange).toHaveBeenCalledWith(false);
});
