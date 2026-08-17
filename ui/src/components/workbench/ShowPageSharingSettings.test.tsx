/** @vitest-environment jsdom */
import { createInstance } from 'i18next';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { afterEach, describe, expect, it, vi } from 'vitest';

import en from '../../i18n/en.json';
import type { ShowAccess } from '../../lib/showPageAccess';
import { ShowPageSharingSettings } from './ShowPageSharingSettings';

const api = {
  getShowAccessSettings: vi.fn(),
  applyShowAccess: vi.fn(),
};

vi.mock('../../context/ApiContext', () => ({
  useApi: () => api,
}));

vi.mock('@/context/ApiContext', () => ({
  useApi: () => api,
}));

const i18n = createInstance();
void i18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: en } },
  interpolation: { escapeValue: false },
});

const showAccess = (overrides: Partial<ShowAccess> = {}): ShowAccess => ({
  page_id: 'ses-1',
  access_mode: 'private',
  share_id: 'stable-link',
  revision: 0,
  normalized_emails: [],
  ...overrides,
});

const renderSettings = () => render(
  <I18nextProvider i18n={i18n}>
    <ShowPageSharingSettings active canManage sessionId="ses-1" />
  </I18nextProvider>,
);

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('ShowPageSharingSettings', () => {
  it('offers the three closed modes and shows Limited-only fields', async () => {
    api.getShowAccessSettings.mockResolvedValue({ show_access: showAccess() });
    renderSettings();

    expect(await screen.findByRole('radio', { name: 'Private' })).toBeTruthy();
    expect(screen.getByRole('radio', { name: 'Limited' })).toBeTruthy();
    expect(screen.getByRole('radio', { name: 'Fully public' })).toBeTruthy();
    expect(screen.queryByRole('textbox', { name: 'Limited access emails' })).toBeNull();

    fireEvent.click(screen.getByRole('radio', { name: 'Limited' }));

    expect(screen.getByRole('textbox', { name: 'Limited access emails' })).toBeTruthy();
    expect((screen.getByRole('textbox', { name: 'Custom link' }) as HTMLInputElement).value).toBe(
      'stable-link',
    );
    expect(screen.getByText('Add at least one email address.')).toBeTruthy();
  });

  it('submits mode, stable link, and normalized emails in one Apply', async () => {
    api.getShowAccessSettings.mockResolvedValue({ show_access: showAccess() });
    api.applyShowAccess.mockResolvedValue({
      status: 'applied',
      show_access: showAccess({
        access_mode: 'limited',
        revision: 1,
        normalized_emails: ['guest@example.com'],
      }),
    });
    renderSettings();
    await screen.findByRole('radio', { name: 'Private' });

    fireEvent.click(screen.getByRole('radio', { name: 'Limited' }));
    fireEvent.change(screen.getByRole('textbox', { name: 'Limited access emails' }), {
      target: { value: ' Guest@Example.COM ' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Add email' }));
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }));

    await waitFor(() => {
      expect(api.applyShowAccess).toHaveBeenCalledTimes(1);
    });
    expect(api.applyShowAccess).toHaveBeenCalledWith('ses-1', {
      expected_revision: 0,
      target_access_mode: 'limited',
      target_share_id: 'stable-link',
      target_emails: ['guest@example.com'],
    });
  });

  it('reloads the latest snapshot after a CAS conflict', async () => {
    api.getShowAccessSettings
      .mockResolvedValueOnce({ show_access: showAccess() })
      .mockResolvedValueOnce({
        show_access: showAccess({ access_mode: 'public', revision: 3 }),
      });
    api.applyShowAccess.mockResolvedValue({
      status: 'conflict',
      show_access: showAccess({ access_mode: 'limited', revision: 2 }),
    });
    renderSettings();
    await screen.findByRole('radio', { name: 'Private' });

    fireEvent.click(screen.getByRole('radio', { name: 'Fully public' }));
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }));

    await waitFor(() => {
      expect(api.getShowAccessSettings).toHaveBeenCalledTimes(2);
    });
    expect(screen.getByRole('radio', { name: 'Fully public' }).getAttribute('aria-checked')).toBe('true');
    expect(screen.getByText(/changed elsewhere/)).toBeTruthy();
  });

  it('keeps the slug draft visible after a collision', async () => {
    api.getShowAccessSettings.mockResolvedValue({ show_access: showAccess() });
    api.applyShowAccess.mockResolvedValue({
      status: 'share_id_taken',
      show_access: showAccess(),
    });
    renderSettings();
    await screen.findByRole('radio', { name: 'Private' });

    fireEvent.click(screen.getByRole('radio', { name: 'Fully public' }));
    fireEvent.change(screen.getByRole('textbox', { name: 'Custom link' }), {
      target: { value: 'taken-link' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }));

    expect(await screen.findByText('That custom link is already taken. Pick another.')).toBeTruthy();
    expect((screen.getByRole('textbox', { name: 'Custom link' }) as HTMLInputElement).value).toBe(
      'taken-link',
    );
  });

  it('rejects a mismatched result identity without adopting its email list', async () => {
    api.getShowAccessSettings.mockResolvedValue({ show_access: showAccess() });
    api.applyShowAccess.mockResolvedValue({
      status: 'applied',
      show_access: showAccess({
        page_id: 'ses-other',
        access_mode: 'limited',
        revision: 1,
        normalized_emails: ['secret@example.com'],
      }),
    });
    renderSettings();
    await screen.findByRole('radio', { name: 'Private' });

    fireEvent.click(screen.getByRole('radio', { name: 'Fully public' }));
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }));

    expect(await screen.findByText('Link access could not be loaded.')).toBeTruthy();
    expect(screen.queryByText('secret@example.com')).toBeNull();
  });
});
