import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import type { ShowPageAccess } from '@/lib/showPageAccess';
import { ShowPageWorkspaceAccessControl } from './ShowPageWorkspaceAccessControl';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

const access = (overrides: Partial<ShowPageAccess> = {}): ShowPageAccess => ({
  ok: true,
  mode: 'organization',
  instance_id: 'inst-1',
  organization_id: 'org-1',
  access_level: 'private',
  group_ids: [],
  policy_revision: 4,
  last_applied_control_plane_revision: 4,
  can_manage: true,
  can_publish_public: true,
  public_link_enabled: false,
  ...overrides,
});

const renderControl = (value: ShowPageAccess) => renderToStaticMarkup(
  <ShowPageWorkspaceAccessControl access={value} active={false} sessionId="session-1" />,
);

describe('ShowPageWorkspaceAccessControl', () => {
  it('renders Personal as a fixed Private audience without Organization choices', () => {
    const html = renderControl(access({
      mode: 'personal',
      instance_id: null,
      organization_id: null,
      policy_revision: null,
      last_applied_control_plane_revision: null,
    }));

    expect(html).toContain('chat.showPage.workspaceLevels.private');
    expect(html).not.toContain('chat.showPage.workspaceLevels.public');
    expect(html).not.toContain('chat.showPage.workspaceLevels.scope');
    expect(html).not.toContain('<select');
  });

  it('renders all Organization audiences and keeps the public wire value labeled as Organization', () => {
    const html = renderControl(access({ access_level: 'public' }));

    expect(html).toContain('value="private"');
    expect(html).toContain('value="public" selected=""');
    expect(html).toContain('value="scope"');
    expect(html).toContain('chat.showPage.workspaceLevels.public');
  });

  it('keeps the Organization audience read-only for a non-owner viewer', () => {
    const html = renderControl(access({ can_manage: false, can_publish_public: false }));

    expect(html).toContain('<select disabled=""');
    expect(html).toContain('chat.showPage.workspaceReadOnly');
  });
});
