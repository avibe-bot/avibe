/** @vitest-environment jsdom */
import { createInstance } from 'i18next';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { afterEach, describe, expect, it, vi } from 'vitest';

import en from '../../i18n/en.json';
import zh from '../../i18n/zh.json';
import type { PermissionsResponse } from '../../features/permissions/types';
import { ShowPageSharingSettings } from './ShowPageSharingSettings';

const api = {
  getShowAccessSettings: vi.fn(),
  applyShowAccess: vi.fn(),
};

const getPermissions = vi.fn();

vi.mock('../../context/ApiContext', () => ({ useApi: () => api }));
vi.mock('@/context/ApiContext', () => ({ useApi: () => api }));
vi.mock('@/features/permissions/api', () => ({
  getPermissions: (...args: unknown[]) => getPermissions(...args),
}));

const ORGANIZATION = {
  ok: true,
  source: 'live',
  offline: false,
  cached_at: null,
  projection: {
    schema_version: 1,
    instance: {
      id: 'inst-1',
      organization: { id: 'org-1', name: 'Acme' },
      access_mode: 'allowlist',
      permission_authority: 'cloud',
      local_mutation_allowed: false,
      authorization_revision: 1,
    },
    capabilities: [],
    access: { owner: { email: null, role: 'owner' }, entries: [] },
    directory: { members: [], groups: [] },
    projects: [],
    policy_sync: {
      status: 'in_sync',
      projects: { active: 0, error: 0, offline: 0, applying: 0, in_sync: 0 },
      resources: { active: 0, error: 0, offline: 0, applying: 0, in_sync: 0 },
    },
  },
} as unknown as PermissionsResponse;

const keyPaths = (value: unknown, prefix = ''): string[] => {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return [prefix];
  return Object.entries(value as Record<string, unknown>)
    .flatMap(([key, child]) => keyPaths(child, prefix ? `${prefix}.${key}` : key));
};

const renderSharing = (language: 'en' | 'zh') => {
  const i18n = createInstance();
  void i18n.use(initReactI18next).init({
    lng: language,
    fallbackLng: 'en',
    resources: { en: { translation: en }, zh: { translation: zh } },
    interpolation: { escapeValue: false },
  });
  render(
    <I18nextProvider i18n={i18n}>
      <ShowPageSharingSettings active canManage sessionId="ses-1" />
    </I18nextProvider>,
  );
};

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('Show Page sharing copy', () => {
  it('keeps the Show Page locale trees at parity', () => {
    const enKeys = keyPaths(en.chat.showPage);
    const zhKeys = keyPaths(zh.chat.showPage);

    expect(zhKeys).toEqual(enKeys);
    // The retired Organization axis takes its Resource sync/ACK copy with it.
    expect(enKeys.filter((key) => key.startsWith('workspace'))).toEqual([]);
    expect(zhKeys.filter((key) => key.startsWith('workspace'))).toEqual([]);
    expect(JSON.stringify(zh.chat.showPage)).not.toContain('尚未确认最新策略');
    expect(JSON.stringify(en.chat.showPage)).not.toContain('acknowledged the latest policy');
    // The audience is no longer email-shaped, so its email-only copy is gone.
    for (const retired of ['limitedEmails', 'limitedEmailHint', 'keepOneLimitedEmail']) {
      expect(enKeys).not.toContain(retired);
      expect(zhKeys).not.toContain(retired);
    }
    // `limit` is an interpolation slot, not i18next's reserved `count` plural
    // selector — using `count` can drop the string in CI even when local tests pass.
    expect(en.chat.showPage.shareAudienceHint.organization).toContain('{{limit}}');
    expect(en.chat.showPage.shareAudienceHint.email).toContain('{{limit}}');
    expect(JSON.stringify(en.chat.showPage.shareAudienceHint)).not.toContain('{{count}}');
    expect(JSON.stringify(zh.chat.showPage.shareAudienceHint)).not.toContain('{{count}}');
  });

  it.each([
    ['en', ['Private', 'Limited', 'Fully public'], 'This Organization'],
    ['zh', ['私有', '有限访问', '完全公开'], '本组织'],
  ] as const)('renders the three tiers and the Organization row in %s', async (
    language,
    tiers,
    organizationLabel,
  ) => {
    api.getShowAccessSettings.mockResolvedValue({
      show_access: {
        page_id: 'ses-1',
        access_mode: 'limited',
        share_id: 'stable-link',
        revision: 0,
        normalized_emails: ['guest@example.com'],
      },
    });
    getPermissions.mockResolvedValue(ORGANIZATION);
    renderSharing(language);

    const trigger = await screen.findByRole('button', { name: new RegExp(tiers[1]) });
    fireEvent.click(trigger);
    expect(screen.getAllByRole('option').map((option) => (
      option.querySelector('span > span')?.textContent
    ))).toEqual([...tiers]);
    expect(await screen.findByRole('switch', { name: organizationLabel })).toBeTruthy();
  });
});
