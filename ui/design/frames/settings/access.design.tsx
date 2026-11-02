// Settings · Access: who besides the Owner may reach this paired instance, and per-project access.
import { PermissionsPage } from '@/features/permissions/PermissionsPage';
import type { PermissionsResponse } from '@/features/permissions/types';

import { frames } from '../../frame';
import { settingsFixtures, settingsPage } from './fixtures';

const page = settingsPage('access', <PermissionsPage />);
const route = '/settings/access';

const counts = { active: 0, error: 0, offline: 0, applying: 0, in_sync: 0 };

const permissions: PermissionsResponse = {
  ok: true,
  source: 'live',
  offline: false,
  cached_at: null,
  projection: {
    schema_version: 1,
    instance: {
      id: 'inst_demo',
      name: 'demo-mac',
      public_url: 'https://demo-mac.app.avibe.bot',
      organization: null,
      access_mode: 'allowlist',
      permission_authority: 'instance',
      local_mutation_allowed: true,
      authorization_revision: 7,
    },
    capabilities: [],
    access: {
      owner: { email: 'alex@acme.dev', role: 'owner' },
      entries: [
        { kind: 'email', value: 'sara@acme.dev', role: 'editor' },
        { kind: 'email', value: 'marc@acme.dev', role: 'viewer' },
        { kind: 'email_domain', value: 'acme.dev', role: 'member' },
      ],
    },
    directory: { members: [], groups: [] },
    projects: [],
    policy_sync: { status: 'none', projects: counts, resources: counts },
  },
};

const fixtures = settingsFixtures({ '/api/permissions': permissions });

const designFrames = frames([
  { name: 'default', title: '设置 · 访问 · 已授权的人', width: 1440, height: 900, status: 'shipped', route, fixtures, render: page },
  { name: 'phone', title: '设置 · 访问 · 手机', width: 390, height: 844, status: 'shipped', route, fixtures, render: page },
]);

export default designFrames;
