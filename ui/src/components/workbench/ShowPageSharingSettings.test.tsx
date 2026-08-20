/** @vitest-environment jsdom */
import { createInstance } from 'i18next';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { afterEach, describe, expect, it, vi } from 'vitest';

import en from '../../i18n/en.json';
import type { PermissionsResponse } from '../../features/permissions/types';
import type { ShowAccess, ShowAccessApplyResult } from '../../lib/showPageAccess';
import { ShowPageSharingSettings } from './ShowPageSharingSettings';

const api = {
  getShowAccessSettings: vi.fn(),
  applyShowAccess: vi.fn(),
};

const getPermissions = vi.fn();

vi.mock('../../context/ApiContext', () => ({
  useApi: () => api,
}));

vi.mock('@/context/ApiContext', () => ({
  useApi: () => api,
}));

// A1/A2 have not landed, so the Organization directory is the only permissions
// read this control makes. It is never a show_page Resource request.
vi.mock('@/features/permissions/api', () => ({
  getPermissions: (...args: unknown[]) => getPermissions(...args),
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

const permissions = (
  organization: { id: string; name: string } | null,
): PermissionsResponse => ({
  ok: true,
  source: 'live',
  offline: false,
  cached_at: null,
  projection: {
    schema_version: 1,
    instance: {
      id: 'inst-1',
      organization,
      access_mode: 'allowlist',
      permission_authority: 'cloud',
      local_mutation_allowed: false,
      authorization_revision: 3,
    },
    capabilities: [],
    access: { owner: { email: null, role: 'owner' }, entries: [] },
    directory: {
      members: [
        { id: 'u1', email: 'alice@example.com', organization_role: 'member', group_ids: [] },
        { id: 'u2', email: 'bob@example.com', organization_role: 'member', group_ids: [] },
      ],
      groups: [
        { id: 'grp-eng', name: 'Engineering', archived_at: null },
        { id: 'grp-old', name: 'Legacy', archived_at: '2026-01-01T00:00:00Z' },
      ],
    },
    projects: [],
    policy_sync: {
      status: 'in_sync',
      projects: { active: 0, error: 0, offline: 0, applying: 0, in_sync: 0 },
      resources: { active: 0, error: 0, offline: 0, applying: 0, in_sync: 0 },
    },
  },
});

const ORGANIZATION = permissions({ id: 'org-1', name: 'Acme' });
const PERSONAL = permissions(null);

const settings = (
  sessionId = 'ses-1',
  onApplied?: (showAccess: ShowAccess) => void,
) => (
  <I18nextProvider i18n={i18n}>
    <ShowPageSharingSettings
      active
      canManage
      sessionId={sessionId}
      onApplied={onApplied}
    />
  </I18nextProvider>
);
const renderSettings = (
  sessionId = 'ses-1',
  onApplied?: (showAccess: ShowAccess) => void,
) => (
  render(settings(sessionId, onApplied))
);

const chooseMode = async (name: 'Private' | 'Limited' | 'Fully public') => {
  fireEvent.click(await screen.findByRole('button', { name: /Access:/ }));
  fireEvent.click(screen.getByRole('option', { name: new RegExp(name) }));
};

const audience = async () => screen.findByRole('combobox', { name: 'People with access' });

const openAudience = async () => {
  const input = await audience();
  fireEvent.focus(input);
  return input;
};

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('ShowPageSharingSettings', () => {
  it('offers exactly the three sharing tiers in order, with no second access axis', async () => {
    api.getShowAccessSettings.mockResolvedValue({ show_access: showAccess() });
    getPermissions.mockResolvedValue(ORGANIZATION);
    renderSettings();

    const trigger = await screen.findByRole('button', { name: 'Access: Private' });
    expect(trigger.className).toContain('w-40');
    expect(screen.queryAllByRole('radio')).toHaveLength(0);
    fireEvent.click(trigger);

    expect(screen.getAllByRole('option').map((option) => option.textContent)).toEqual([
      'PrivateOnly you can access this page',
      'LimitedOnly people in the list can access',
      'Fully publicAnyone can access without signing in',
    ]);
    expect(document.querySelector('[data-access-icon="private"]')).toBeTruthy();
    expect(document.querySelector('[data-access-icon="limited"]')).toBeTruthy();
    expect(document.querySelector('[data-access-icon="public"]')).toBeTruthy();
    // The Organization axis is gone: no second access block, no sync/ACK status.
    expect(screen.queryByText('Organization access')).toBeNull();
    expect(screen.queryByText(/has not acknowledged the latest policy/)).toBeNull();
    expect(screen.queryByRole('button', { name: 'Apply' })).toBeNull();
  });

  it('starts an Organization page as Private with no audience field', async () => {
    api.getShowAccessSettings.mockResolvedValue({ show_access: showAccess() });
    getPermissions.mockResolvedValue(ORGANIZATION);
    renderSettings();

    expect(await screen.findByRole('button', { name: 'Access: Private' })).toBeTruthy();
    expect(screen.queryByRole('combobox', { name: 'People with access' })).toBeNull();
    expect(screen.queryByRole('switch')).toBeNull();
    // Private is not a shared mode, so nothing reads the directory yet.
    expect(getPermissions).not.toHaveBeenCalled();
  });

  it('starts a Personal page as Private too', async () => {
    api.getShowAccessSettings.mockResolvedValue({ show_access: showAccess() });
    getPermissions.mockResolvedValue(PERSONAL);
    renderSettings();

    expect(await screen.findByRole('button', { name: 'Access: Private' })).toBeTruthy();
    expect(screen.queryByRole('combobox', { name: 'People with access' })).toBeNull();
  });

  it('opens the directory on focus and lets a group be picked straight from it', async () => {
    api.getShowAccessSettings.mockResolvedValue({
      show_access: showAccess({
        access_mode: 'limited',
        access_entries: [{ kind: 'email', value: 'guest@example.com' }],
        normalized_emails: ['guest@example.com'],
      }),
    });
    getPermissions.mockResolvedValue(ORGANIZATION);
    api.applyShowAccess.mockResolvedValue({
      status: 'applied',
      show_access: showAccess({
        access_mode: 'limited',
        revision: 1,
        normalized_emails: ['guest@example.com'],
        access_entries: [
          { kind: 'group', value: 'grp-eng' },
          { kind: 'email', value: 'guest@example.com' },
        ],
      }),
    });
    renderSettings();
    await waitFor(() => expect(getPermissions).toHaveBeenCalledTimes(1));

    const input = await openAudience();
    expect(input.getAttribute('aria-expanded')).toBe('true');
    const options = await waitFor(() => screen.getAllByRole('option'));
    // Groups and people are searchable side by side; an archived group is not.
    expect(options.map((option) => option.textContent)).toEqual([
      'EngineeringGroup',
      'alice@example.com',
      'bob@example.com',
    ]);
    fireEvent.click(screen.getByRole('option', { name: /Engineering/ }));

    // A group-only change is local until A1/A2: the current endpoint has
    // nowhere to store it, and an email-only round-trip would wipe the row.
    await waitFor(() => expect(screen.getByText('Engineering')).toBeTruthy());
    expect(api.applyShowAccess).not.toHaveBeenCalled();
  });

  it('narrows the directory from a half-typed query and still accepts any typed email', async () => {
    api.getShowAccessSettings.mockResolvedValue({
      show_access: showAccess({ access_mode: 'limited' }),
    });
    getPermissions.mockResolvedValue(ORGANIZATION);
    api.applyShowAccess.mockResolvedValue({
      status: 'applied',
      show_access: showAccess({
        access_mode: 'limited',
        revision: 1,
        normalized_emails: ['outsider@partner.dev'],
        access_entries: [{ kind: 'email', value: 'outsider@partner.dev' }],
      }),
    });
    renderSettings();

    const input = await openAudience();
    fireEvent.change(input, { target: { value: 'eng' } });
    await waitFor(() => expect(screen.getAllByRole('option')).toHaveLength(1));
    expect(screen.getByRole('option', { name: /Engineering/ })).toBeTruthy();

    fireEvent.change(input, { target: { value: ' Outsider@Partner.DEV ' } });
    fireEvent.click(screen.getByRole('button', { name: 'Add email' }));

    await waitFor(() => expect(api.applyShowAccess).toHaveBeenCalledTimes(1));
    expect(api.applyShowAccess).toHaveBeenCalledWith('ses-1', {
      expected_revision: 0,
      target_access_mode: 'limited',
      target_share_id: 'stable-link',
      target_emails: ['outsider@partner.dev'],
    });
  });

  it('keeps the audience list open with empty-state copy when a directory query matches nothing', async () => {
    api.getShowAccessSettings.mockResolvedValue({
      show_access: showAccess({
        access_mode: 'limited',
        normalized_emails: ['guest@example.com'],
      }),
    });
    getPermissions.mockResolvedValue(ORGANIZATION);
    renderSettings();

    const input = await openAudience();
    fireEvent.change(input, { target: { value: 'zzz-no-match' } });
    expect(await screen.findByRole('listbox', { name: 'People and group suggestions' })).toBeTruthy();
    expect(screen.getByText('No matching person or group. Type a full email to add it.')).toBeTruthy();
    expect(screen.queryAllByRole('option')).toHaveLength(0);
  });

  it('closes the audience list when focus leaves the field from the add button', async () => {
    api.getShowAccessSettings.mockResolvedValue({
      show_access: showAccess({
        access_mode: 'limited',
        normalized_emails: ['guest@example.com'],
      }),
    });
    getPermissions.mockResolvedValue(ORGANIZATION);
    renderSettings();

    const input = await openAudience();
    fireEvent.change(input, { target: { value: 'alice' } });
    const add = await screen.findByRole('button', { name: 'Add email' });
    fireEvent.focus(add);
    fireEvent.blur(input, { relatedTarget: add });
    expect(screen.getByRole('option', { name: 'alice@example.com' })).toBeTruthy();

    const outside = document.createElement('button');
    document.body.appendChild(outside);
    fireEvent.blur(add, { relatedTarget: outside });
    await waitFor(() => {
      expect(screen.queryByRole('listbox', { name: 'People and group suggestions' })).toBeNull();
    });
    outside.remove();
  });

  it('keeps the Organization switch and the narrower entries as peers', async () => {
    api.getShowAccessSettings.mockResolvedValue({
      show_access: showAccess({
        access_mode: 'limited',
        normalized_emails: ['guest@example.com'],
        access_entries: [
          { kind: 'group', value: 'grp-eng' },
          { kind: 'email', value: 'guest@example.com' },
        ],
      }),
    });
    getPermissions.mockResolvedValue(ORGANIZATION);
    api.applyShowAccess.mockResolvedValue({
      status: 'applied',
      show_access: showAccess({
        access_mode: 'limited',
        revision: 1,
        normalized_emails: ['guest@example.com'],
        access_entries: [
          { kind: 'organization', value: 'org-1' },
          { kind: 'group', value: 'grp-eng' },
          { kind: 'email', value: 'guest@example.com' },
        ],
      }),
    });
    renderSettings();

    const toggle = await screen.findByRole('switch', { name: 'This Organization' });
    expect(toggle.getAttribute('aria-checked')).toBe('false');
    expect(screen.getByText('This Organization (Acme)')).toBeTruthy();
    fireEvent.click(toggle);

    await waitFor(() => {
      expect((screen.getByRole('switch', { name: 'This Organization' }))
        .getAttribute('aria-checked')).toBe('true');
    });
    // Turning the Organization on supersedes nothing: the group and the email stay.
    // Until A1/A2 land, the current endpoint cannot store those kinds, so the
    // switch is local-only and must not fire an email-only round-trip.
    expect(api.applyShowAccess).not.toHaveBeenCalled();
    expect(screen.getByText('Engineering')).toBeTruthy();
    expect(screen.getByText('guest@example.com')).toBeTruthy();

    api.applyShowAccess.mockResolvedValue({
      status: 'applied',
      show_access: showAccess({
        access_mode: 'limited',
        revision: 1,
        normalized_emails: ['guest@example.com', 'alice@example.com'],
      }),
    });
    fireEvent.change(await audience(), { target: { value: 'alice@example.com' } });
    fireEvent.click(screen.getByRole('button', { name: 'Add email' }));
    await waitFor(() => expect(api.applyShowAccess).toHaveBeenCalledTimes(1));
    expect(api.applyShowAccess).toHaveBeenCalledWith('ses-1', {
      expected_revision: 0,
      target_access_mode: 'limited',
      target_share_id: 'stable-link',
      target_emails: ['alice@example.com', 'guest@example.com'],
    });
    // The email-only round-trip must not wipe the local extras.
    expect((screen.getByRole('switch', { name: 'This Organization' }))
      .getAttribute('aria-checked')).toBe('true');
    expect(screen.getByText('Engineering')).toBeTruthy();
    expect(screen.getByText('alice@example.com')).toBeTruthy();
  });

  it('gives a Personal page an email-only audience', async () => {
    api.getShowAccessSettings.mockResolvedValue({
      show_access: showAccess({
        access_mode: 'limited',
        normalized_emails: ['guest@example.com'],
      }),
    });
    getPermissions.mockResolvedValue(PERSONAL);
    renderSettings();
    await waitFor(() => expect(getPermissions).toHaveBeenCalledTimes(1));

    const input = await openAudience();
    expect(input.getAttribute('placeholder')).toBe('name@example.com');
    expect(screen.getByText('Enter an email and press Enter to add · up to 64')).toBeTruthy();
    // No Organization to share with, so no switch and nothing to search.
    expect(screen.queryByRole('switch')).toBeNull();
    expect(screen.queryByRole('option')).toBeNull();
    expect(screen.getByText('guest@example.com')).toBeTruthy();
  });

  it('degrades to email-only entry when the directory cannot be read', async () => {
    api.getShowAccessSettings.mockResolvedValue({
      show_access: showAccess({
        access_mode: 'limited',
        normalized_emails: ['guest@example.com'],
      }),
    });
    getPermissions.mockRejectedValue(new Error('permissions offline'));
    renderSettings();
    await waitFor(() => expect(getPermissions).toHaveBeenCalledTimes(1));

    const input = await openAudience();
    expect((input as HTMLInputElement).disabled).toBe(false);
    expect(screen.queryByRole('switch')).toBeNull();
    expect(screen.getByText('guest@example.com')).toBeTruthy();
  });

  it('rejects an unusable typed audience without calling the API', async () => {
    api.getShowAccessSettings.mockResolvedValue({
      show_access: showAccess({
        access_mode: 'limited',
        normalized_emails: ['guest@example.com'],
      }),
    });
    getPermissions.mockResolvedValue(PERSONAL);
    renderSettings();

    const input = await audience();
    fireEvent.change(input, { target: { value: 'not-an-email' } });
    fireEvent.keyDown(input, { key: 'Enter' });

    expect(await screen.findByText('Enter a valid email address.')).toBeTruthy();
    expect(api.applyShowAccess).not.toHaveBeenCalled();
  });

  it('disables new audience input at the entry limit', async () => {
    api.getShowAccessSettings.mockResolvedValue({
      show_access: showAccess({
        access_mode: 'limited',
        normalized_emails: Array.from({ length: 64 }, (_, index) => `guest-${index}@example.com`),
      }),
    });
    getPermissions.mockResolvedValue(ORGANIZATION);
    renderSettings();

    const input = await audience();
    expect((input as HTMLInputElement).disabled).toBe(true);
    expect(screen.getByText(
      'Pick a person or group, or type any email · up to 64',
    )).toBeTruthy();
    // The Organization switch is also at the cap, so it cannot silently add a
    // 65th entry; an existing Organization row would still be removable.
    expect(
      (await screen.findByRole('switch', { name: 'This Organization' }) as HTMLButtonElement).disabled,
    ).toBe(true);
  });

  it('does not allow removing the last entry while Limited is selected', async () => {
    api.getShowAccessSettings.mockResolvedValue({
      show_access: showAccess({
        access_mode: 'limited',
        normalized_emails: ['guest@example.com'],
      }),
    });
    getPermissions.mockResolvedValue(ORGANIZATION);
    renderSettings();

    const remove = await screen.findByRole('button', { name: 'Remove guest@example.com' });
    expect((remove as HTMLButtonElement).disabled).toBe(true);
    expect(remove.getAttribute('title')).toBe('Switch to Private to remove the last email');
    // The same guard covers an Organization that is the only entry left.
    expect((await screen.findByRole('switch', { name: 'This Organization' })).getAttribute(
      'aria-checked',
    )).toBe('false');
  });

  it('refuses Limited with no emails and pins the last email beside extras', async () => {
    api.getShowAccessSettings.mockResolvedValue({ show_access: showAccess() });
    getPermissions.mockResolvedValue(ORGANIZATION);
    renderSettings();

    await chooseMode('Limited');
    expect(await screen.findByText(
      'Add at least one email before Limited can be saved.',
    )).toBeTruthy();
    expect(api.applyShowAccess).not.toHaveBeenCalled();

    const input = await openAudience();
    fireEvent.click(await screen.findByRole('option', { name: /Engineering/ }));
    expect(await screen.findByText('Engineering')).toBeTruthy();
    expect(screen.getByText(
      'Add at least one email before Limited can be saved.',
    )).toBeTruthy();
    expect(api.applyShowAccess).not.toHaveBeenCalled();

    api.applyShowAccess.mockResolvedValue({
      status: 'applied',
      show_access: showAccess({
        access_mode: 'limited',
        revision: 1,
        normalized_emails: ['guest@example.com'],
      }),
    });
    fireEvent.change(input, { target: { value: 'guest@example.com' } });
    fireEvent.click(screen.getByRole('button', { name: 'Add email' }));
    await waitFor(() => expect(api.applyShowAccess).toHaveBeenCalledTimes(1));
    expect(api.applyShowAccess).toHaveBeenCalledWith('ses-1', {
      expected_revision: 0,
      target_access_mode: 'limited',
      target_share_id: 'stable-link',
      target_emails: ['guest@example.com'],
    });
    expect(screen.queryByText(
      'Add at least one email before Limited can be saved.',
    )).toBeNull();

    const removeEmail = screen.getByRole('button', { name: 'Remove guest@example.com' });
    const removeGroup = screen.getByRole('button', { name: 'Remove Engineering' });
    expect((removeEmail as HTMLButtonElement).disabled).toBe(true);
    expect((removeGroup as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(removeGroup);
    expect(screen.queryByRole('button', { name: 'Remove Engineering' })).toBeNull();
    expect(screen.getByText('guest@example.com')).toBeTruthy();
    expect(api.applyShowAccess).toHaveBeenCalledTimes(1);
  });

  it('saves a direct mode change without an Apply button', async () => {
    api.getShowAccessSettings.mockResolvedValue({ show_access: showAccess() });
    getPermissions.mockResolvedValue(ORGANIZATION);
    api.applyShowAccess.mockResolvedValue({
      status: 'applied',
      show_access: showAccess({ access_mode: 'public', revision: 1 }),
    });
    renderSettings();

    await chooseMode('Limited');
    fireEvent.change(await audience(), { target: { value: 'unfinished@example.com' } });
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

  it('reloads the latest access snapshot without dropping a custom-link draft after a CAS conflict', async () => {
    api.getShowAccessSettings
      .mockResolvedValueOnce({
        show_access: showAccess({
          access_mode: 'limited',
          normalized_emails: ['guest@example.com'],
        }),
      })
      .mockResolvedValueOnce({
        show_access: showAccess({ access_mode: 'public', revision: 3 }),
      });
    getPermissions.mockResolvedValue(ORGANIZATION);
    api.applyShowAccess.mockResolvedValue({
      status: 'conflict',
      show_access: showAccess({ access_mode: 'limited', revision: 2 }),
    });
    renderSettings();

    const customLink = await screen.findByRole('textbox', { name: 'Custom link' });
    fireEvent.change(customLink, { target: { value: 'unsaved-link' } });

    await chooseMode('Fully public');

    await waitFor(() => expect(api.getShowAccessSettings).toHaveBeenCalledTimes(2));
    expect(screen.getByRole('button', { name: 'Access: Fully public' })).toBeTruthy();
    expect(screen.getByText(/changed elsewhere/)).toBeTruthy();
    expect((screen.getByRole('textbox', { name: 'Custom link' }) as HTMLInputElement).value).toBe(
      'unsaved-link',
    );
    expect(screen.getByRole('button', { name: 'Save' })).toBeTruthy();
  });

  it('keeps an explicitly submitted custom-link draft after a CAS conflict', async () => {
    api.getShowAccessSettings
      .mockResolvedValueOnce({ show_access: showAccess({ access_mode: 'public' }) })
      .mockResolvedValueOnce({
        show_access: showAccess({ access_mode: 'public', revision: 2, share_id: 'server-link' }),
      });
    getPermissions.mockResolvedValue(ORGANIZATION);
    api.applyShowAccess.mockResolvedValue({
      status: 'conflict',
      show_access: showAccess({ access_mode: 'public', revision: 1, share_id: 'other-link' }),
    });
    renderSettings();

    const customLink = await screen.findByRole('textbox', { name: 'Custom link' });
    fireEvent.change(customLink, { target: { value: 'submitted-link' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => expect(api.getShowAccessSettings).toHaveBeenCalledTimes(2));
    expect((screen.getByRole('textbox', { name: 'Custom link' }) as HTMLInputElement).value).toBe(
      'submitted-link',
    );
    expect(screen.getByRole('button', { name: 'Save' })).toBeTruthy();
  });

  it('adopts a concurrent custom link when an access autosave had no link draft', async () => {
    api.getShowAccessSettings
      .mockResolvedValueOnce({
        show_access: showAccess({
          access_mode: 'limited',
          normalized_emails: ['guest@example.com'],
        }),
      })
      .mockResolvedValueOnce({
        show_access: showAccess({ access_mode: 'public', revision: 2, share_id: 'server-link' }),
      });
    getPermissions.mockResolvedValue(ORGANIZATION);
    api.applyShowAccess.mockResolvedValue({
      status: 'conflict',
      show_access: showAccess({ access_mode: 'limited', revision: 1 }),
    });
    renderSettings();

    await chooseMode('Fully public');

    await waitFor(() => expect(api.getShowAccessSettings).toHaveBeenCalledTimes(2));
    expect((screen.getByRole('textbox', { name: 'Custom link' }) as HTMLInputElement).value).toBe(
      'server-link',
    );
    expect(screen.queryByRole('button', { name: 'Save' })).toBeNull();
  });

  it('keeps a custom-link draft when retrying a failed access autosave', async () => {
    api.getShowAccessSettings
      .mockResolvedValueOnce({
        show_access: showAccess({
          access_mode: 'limited',
          normalized_emails: ['guest@example.com'],
        }),
      })
      .mockResolvedValueOnce({
        show_access: showAccess({ access_mode: 'public', revision: 2, share_id: 'server-link' }),
      });
    getPermissions.mockResolvedValue(ORGANIZATION);
    api.applyShowAccess.mockRejectedValueOnce(new Error('network unavailable'));
    renderSettings();

    const customLink = await screen.findByRole('textbox', { name: 'Custom link' });
    fireEvent.change(customLink, { target: { value: 'retry-link' } });
    await chooseMode('Fully public');
    await waitFor(() => expect(screen.getByText('Link access could not be loaded.')).toBeTruthy());

    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));

    await waitFor(() => expect(api.getShowAccessSettings).toHaveBeenCalledTimes(2));
    expect((screen.getByRole('textbox', { name: 'Custom link' }) as HTMLInputElement).value).toBe(
      'retry-link',
    );
    expect(screen.getByRole('button', { name: 'Save' })).toBeTruthy();
  });

  it('shows an explicit custom-link save action only after editing', async () => {
    api.getShowAccessSettings.mockResolvedValue({
      show_access: showAccess({ access_mode: 'public' }),
    });
    getPermissions.mockResolvedValue(ORGANIZATION);
    api.applyShowAccess.mockResolvedValue({
      status: 'share_id_taken',
      show_access: showAccess({ access_mode: 'public' }),
    });
    renderSettings();
    const input = await screen.findByRole('textbox', { name: 'Custom link' });

    expect(screen.getByText('Custom link')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Save' })).toBeNull();
    fireEvent.change(input, { target: { value: 'taken-link' } });
    fireEvent.blur(input);
    fireEvent.keyDown(input, { key: 'Enter' });

    expect(api.applyShowAccess).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    expect(await screen.findByText('That custom link is already taken. Pick another.')).toBeTruthy();
    expect((screen.getByRole('textbox', { name: 'Custom link' }) as HTMLInputElement).value).toBe(
      'taken-link',
    );
  });

  it('adopts the canonical custom link after a successful save', async () => {
    api.getShowAccessSettings.mockResolvedValue({
      show_access: showAccess({ access_mode: 'public' }),
    });
    getPermissions.mockResolvedValue(ORGANIZATION);
    api.applyShowAccess.mockResolvedValue({
      status: 'applied',
      show_access: showAccess({ access_mode: 'public', revision: 1, share_id: 'canonical-link' }),
    });
    renderSettings();

    const input = await screen.findByRole('textbox', { name: 'Custom link' });
    fireEvent.change(input, { target: { value: ' canonical-link ' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => expect(api.applyShowAccess).toHaveBeenCalledTimes(1));
    expect((screen.getByRole('textbox', { name: 'Custom link' }) as HTMLInputElement).value).toBe(
      'canonical-link',
    );
    expect(screen.queryByRole('button', { name: 'Save' })).toBeNull();
  });

  it('reconciles a successful custom-link save after the editor unmounts', async () => {
    api.getShowAccessSettings.mockResolvedValue({
      show_access: showAccess({ access_mode: 'public' }),
    });
    getPermissions.mockResolvedValue(ORGANIZATION);
    let resolveApply = (_result: ShowAccessApplyResult) => undefined;
    api.applyShowAccess.mockReturnValue(new Promise<ShowAccessApplyResult>((resolve) => {
      resolveApply = resolve;
    }));
    const onApplied = vi.fn();
    const view = renderSettings('ses-1', onApplied);
    const input = await screen.findByRole('textbox', { name: 'Custom link' });

    fireEvent.change(input, { target: { value: 'new-link' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(api.applyShowAccess).toHaveBeenCalledTimes(1));
    view.unmount();

    await act(async () => {
      resolveApply({
        status: 'applied',
        show_access: showAccess({
          access_mode: 'public',
          revision: 1,
          share_id: 'new-link',
        }),
      });
      await Promise.resolve();
    });

    expect(onApplied).toHaveBeenCalledWith(expect.objectContaining({ share_id: 'new-link' }));
  });

  it('keeps an unsaved custom-link draft out of access autosaves', async () => {
    api.getShowAccessSettings.mockResolvedValue({
      show_access: showAccess({
        access_mode: 'limited',
        normalized_emails: ['first@example.com'],
      }),
    });
    getPermissions.mockResolvedValue(PERSONAL);
    api.applyShowAccess.mockResolvedValue({
      status: 'applied',
      show_access: showAccess({
        access_mode: 'limited',
        normalized_emails: ['first@example.com', 'second@example.com'],
        revision: 1,
      }),
    });
    renderSettings();

    const customLink = await screen.findByRole('textbox', { name: 'Custom link' });
    fireEvent.change(customLink, { target: { value: 'unsaved-link' } });
    fireEvent.change(await audience(), { target: { value: 'second@example.com' } });
    fireEvent.click(screen.getByRole('button', { name: 'Add email' }));

    await waitFor(() => expect(api.applyShowAccess).toHaveBeenCalledTimes(1));
    expect(api.applyShowAccess).toHaveBeenCalledWith('ses-1', {
      expected_revision: 0,
      target_access_mode: 'limited',
      target_share_id: 'stable-link',
      target_emails: ['first@example.com', 'second@example.com'],
    });
    expect((screen.getByRole('textbox', { name: 'Custom link' }) as HTMLInputElement).value).toBe(
      'unsaved-link',
    );
    expect(screen.getByRole('button', { name: 'Save' })).toBeTruthy();
  });

  it('keeps the Organization directory after a CAS reload overlaps the first fetch', async () => {
    api.getShowAccessSettings
      .mockResolvedValueOnce({
        show_access: showAccess({
          access_mode: 'limited',
          normalized_emails: ['guest@example.com'],
        }),
      })
      .mockResolvedValueOnce({
        show_access: showAccess({
          access_mode: 'limited',
          revision: 3,
          normalized_emails: ['guest@example.com'],
        }),
      });
    let resolvePermissions = (_value: PermissionsResponse) => undefined;
    getPermissions.mockReturnValue(new Promise<PermissionsResponse>((resolve) => {
      resolvePermissions = resolve;
    }));
    api.applyShowAccess.mockResolvedValue({
      status: 'conflict',
      show_access: showAccess({ access_mode: 'limited', revision: 2 }),
    });
    renderSettings();

    await waitFor(() => expect(getPermissions).toHaveBeenCalledTimes(1));
    const input = await openAudience();
    expect(screen.getByText('Loading Organization directory…')).toBeTruthy();

    fireEvent.change(input, { target: { value: 'alice@example.com' } });
    fireEvent.click(screen.getByRole('button', { name: 'Add email' }));
    await waitFor(() => expect(api.getShowAccessSettings).toHaveBeenCalledTimes(2));

    await act(async () => {
      resolvePermissions(ORGANIZATION);
      await Promise.resolve();
    });

    await openAudience();
    await waitFor(() => expect(screen.getByRole('option', { name: /Engineering/ })).toBeTruthy());
    expect(screen.queryByText('Loading Organization directory…')).toBeNull();
    expect(getPermissions).toHaveBeenCalledTimes(1);
  });

  it('rejects a mismatched result identity without adopting its audience', async () => {
    api.getShowAccessSettings.mockResolvedValue({ show_access: showAccess() });
    getPermissions.mockResolvedValue(ORGANIZATION);
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
    getPermissions.mockResolvedValue(ORGANIZATION);
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
