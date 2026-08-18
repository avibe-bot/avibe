/** @vitest-environment jsdom */
import { createInstance } from 'i18next';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { afterEach, describe, expect, it, vi } from 'vitest';

import en from '../../i18n/en.json';
import type { ShowAccess, ShowAccessApplyResult } from '../../lib/showPageAccess';
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

const settings = (sessionId = 'ses-1', showCustomLink = true) => (
  <I18nextProvider i18n={i18n}>
    <ShowPageSharingSettings
      active
      canManage
      sessionId={sessionId}
      showCustomLink={showCustomLink}
    />
  </I18nextProvider>
);
const renderSettings = (sessionId = 'ses-1', showCustomLink = true) => (
  render(settings(sessionId, showCustomLink))
);

const chooseMode = async (name: 'Private' | 'Limited' | 'Fully public') => {
  fireEvent.click(await screen.findByRole('button', { name: /Access:/ }));
  fireEvent.click(screen.getByRole('option', { name: new RegExp(name) }));
};

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('ShowPageSharingSettings', () => {
  it('uses semantic icons instead of radio controls for the three access modes', async () => {
    api.getShowAccessSettings.mockResolvedValue({ show_access: showAccess() });
    renderSettings();

    const trigger = await screen.findByRole('button', { name: 'Access: Private' });
    expect(trigger.className).toContain('w-40');
    expect(trigger.className).not.toContain('w-full');
    expect(screen.queryAllByRole('radio')).toHaveLength(0);
    fireEvent.click(screen.getByRole('button', { name: 'Access: Private' }));

    expect(screen.getByRole('option', { name: /Private/ })).toBeTruthy();
    expect(screen.getByRole('option', { name: /Limited/ })).toBeTruthy();
    expect(screen.getByRole('option', { name: /Fully public/ })).toBeTruthy();
    expect(document.querySelector('[data-access-icon="private"]')).toBeTruthy();
    expect(document.querySelector('[data-access-icon="limited"]')).toBeTruthy();
    expect(document.querySelector('[data-access-icon="public"]')).toBeTruthy();
  });

  it('saves a Limited audience as soon as an email is added', async () => {
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

    await chooseMode('Limited');
    expect(screen.queryByRole('button', { name: 'Apply' })).toBeNull();
    fireEvent.change(screen.getByRole('textbox', { name: 'People with access' }), {
      target: { value: ' Guest@Example.COM ' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Add email' }));

    await waitFor(() => expect(api.applyShowAccess).toHaveBeenCalledTimes(1));
    expect(api.applyShowAccess).toHaveBeenCalledWith('ses-1', {
      expected_revision: 0,
      target_access_mode: 'limited',
      target_share_id: 'stable-link',
      target_emails: ['guest@example.com'],
    });
  });

  it('saves a direct mode change without an Apply button', async () => {
    api.getShowAccessSettings.mockResolvedValue({ show_access: showAccess() });
    api.applyShowAccess.mockResolvedValue({
      status: 'applied',
      show_access: showAccess({ access_mode: 'public', revision: 1 }),
    });
    renderSettings();

    await chooseMode('Limited');
    fireEvent.change(screen.getByRole('textbox', { name: 'People with access' }), {
      target: { value: 'unfinished@example.com' },
    });
    await chooseMode('Fully public');

    await waitFor(() => expect(api.applyShowAccess).toHaveBeenCalledTimes(1));
    expect(api.applyShowAccess).toHaveBeenCalledWith('ses-1', {
      expected_revision: 0,
      target_access_mode: 'public',
      target_share_id: 'stable-link',
      target_emails: [],
    });
    expect(screen.queryByRole('button', { name: 'Apply' })).toBeNull();
  });

  it('disables new Limited email input at the audience limit', async () => {
    api.getShowAccessSettings.mockResolvedValue({
      show_access: showAccess({
        access_mode: 'limited',
        normalized_emails: Array.from({ length: 64 }, (_, index) => `guest-${index}@example.com`),
      }),
    });
    renderSettings();

    const input = await screen.findByRole('textbox', { name: 'People with access' });
    expect((input as HTMLInputElement).disabled).toBe(true);
    expect(screen.getByText('Enter an email and press Enter to add · up to 64')).toBeTruthy();
  });

  it('does not allow removing the last email while Limited is selected', async () => {
    api.getShowAccessSettings.mockResolvedValue({
      show_access: showAccess({
        access_mode: 'limited',
        normalized_emails: ['guest@example.com'],
      }),
    });
    renderSettings();

    const remove = await screen.findByRole('button', { name: 'Remove guest@example.com' });
    expect((remove as HTMLButtonElement).disabled).toBe(true);
    expect(remove.getAttribute('title')).toBe('Switch to Private to remove the last email');
  });

  it('reloads the latest snapshot after an automatic CAS conflict', async () => {
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

    await chooseMode('Fully public');

    await waitFor(() => expect(api.getShowAccessSettings).toHaveBeenCalledTimes(2));
    expect(screen.getByRole('button', { name: 'Access: Fully public' })).toBeTruthy();
    expect(screen.getByText(/changed elsewhere/)).toBeTruthy();
  });

  it('keeps the custom-link draft visible after an automatic collision response', async () => {
    api.getShowAccessSettings.mockResolvedValue({
      show_access: showAccess({ access_mode: 'public' }),
    });
    api.applyShowAccess.mockResolvedValue({
      status: 'share_id_taken',
      show_access: showAccess({ access_mode: 'public' }),
    });
    renderSettings();
    const input = await screen.findByRole('textbox', { name: 'Custom link' });

    fireEvent.change(input, { target: { value: 'taken-link' } });
    fireEvent.blur(input);

    expect(await screen.findByText('That custom link is already taken. Pick another.')).toBeTruthy();
    expect((screen.getByRole('textbox', { name: 'Custom link' }) as HTMLInputElement).value).toBe(
      'taken-link',
    );
  });

  it('can hide custom-link editing when embedded in the share popover', async () => {
    api.getShowAccessSettings.mockResolvedValue({
      show_access: showAccess({ access_mode: 'public' }),
    });
    renderSettings('ses-1', false);

    expect(await screen.findByRole('button', { name: 'Access: Fully public' })).toBeTruthy();
    expect(screen.queryByRole('textbox', { name: 'Custom link' })).toBeNull();
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

    await chooseMode('Fully public');

    expect(await screen.findByText('Link access could not be loaded.')).toBeTruthy();
    expect(screen.queryByText('secret@example.com')).toBeNull();
  });

  it('discards an in-flight autosave result after moving to another session', async () => {
    api.getShowAccessSettings.mockImplementation((sessionId: string) => Promise.resolve({
      show_access: showAccess({
        page_id: sessionId,
        share_id: sessionId === 'ses-1' ? 'first-link' : 'second-link',
      }),
    }));
    let resolveApply = (_result: ShowAccessApplyResult) => undefined;
    api.applyShowAccess.mockReturnValue(new Promise<ShowAccessApplyResult>((resolve) => {
      resolveApply = resolve;
    }));
    const view = renderSettings();

    await chooseMode('Fully public');
    await waitFor(() => expect(api.applyShowAccess).toHaveBeenCalledTimes(1));

    view.rerender(settings('ses-2'));
    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Access: Private' })).toBeTruthy();
    });
    await act(async () => {
      resolveApply({
        status: 'applied',
        show_access: showAccess({
          access_mode: 'limited',
          revision: 1,
          normalized_emails: ['first-secret@example.com'],
        }),
      });
      await Promise.resolve();
    });

    expect(screen.getByRole('button', { name: 'Access: Private' })).toBeTruthy();
    expect(screen.queryByText('first-secret@example.com')).toBeNull();
    expect(screen.queryByText('Link access could not be loaded.')).toBeNull();
  });
});
