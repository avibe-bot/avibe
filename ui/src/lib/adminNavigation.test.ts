import { describe, expect, it } from 'vitest';

import {
  adminLandingPath,
  filterOwnerOnlyNavItems,
  isAdvancedSettingsPath,
  isLocalOnlyMessagingField,
  isOwnerOnlyPath,
  isMemorySettingsPath,
  visibleAdminNavItems,
} from './adminNavigation';

describe('isAdvancedSettingsPath', () => {
  it('defers to the standalone Memory item when that item is visible', () => {
    expect(isAdvancedSettingsPath('/admin/settings/memory', true)).toBe(false);
    expect(isAdvancedSettingsPath('/admin/settings/memory/', true)).toBe(false);
  });

  it('keeps Memory setup under Advanced Settings when the standalone item is hidden', () => {
    expect(isAdvancedSettingsPath('/admin/settings/memory', false)).toBe(true);
    expect(isAdvancedSettingsPath('/admin/settings/memory/', false)).toBe(true);
  });

  it('keeps the remaining settings pages grouped under Advanced Settings', () => {
    expect(isAdvancedSettingsPath('/admin/settings/messaging', true)).toBe(true);
    expect(isAdvancedSettingsPath('/admin/settings/service', true)).toBe(true);
    expect(isAdvancedSettingsPath('/admin/settings/dependencies', true)).toBe(true);
    expect(isAdvancedSettingsPath('/admin/settings/diagnostics', true)).toBe(true);
  });

  it('leaves other standalone settings destinations inactive', () => {
    expect(isAdvancedSettingsPath('/admin/settings/platforms', true)).toBe(false);
    expect(isAdvancedSettingsPath('/admin/settings/backends', true)).toBe(false);
    expect(isAdvancedSettingsPath('/admin/settings/models', true)).toBe(false);
  });
});

describe('isOwnerOnlyPath', () => {
  it('covers every destination with an Owner management gate', () => {
    expect(isOwnerOnlyPath('/admin/dashboard')).toBe(true);
    expect(isOwnerOnlyPath('/admin/remote-access')).toBe(true);
    expect(isOwnerOnlyPath('/admin/groups')).toBe(true);
    expect(isOwnerOnlyPath('/admin/users')).toBe(true);
    expect(isOwnerOnlyPath('/admin/logs')).toBe(true);
    expect(isOwnerOnlyPath('/admin/settings/service')).toBe(true);
    expect(isOwnerOnlyPath('/admin/settings/platforms')).toBe(true);
    expect(isOwnerOnlyPath('/admin/settings/backends')).toBe(true);
    expect(isOwnerOnlyPath('/admin/settings/models')).toBe(true);
    expect(isOwnerOnlyPath('/admin/settings/dependencies')).toBe(true);
    expect(isOwnerOnlyPath('/admin/settings/diagnostics')).toBe(true);
    expect(isOwnerOnlyPath('/admin/settings/logs')).toBe(true);
    expect(isOwnerOnlyPath('/harness')).toBe(false);
  });

  it('matches nested paths under a gated destination', () => {
    expect(isOwnerOnlyPath('/admin/settings/platforms/slack')).toBe(true);
    expect(isOwnerOnlyPath('/admin/groups/engineering')).toBe(true);
    expect(isOwnerOnlyPath('/harness/')).toBe(false);
  });

  it('leaves remotely usable destinations open', () => {
    expect(isOwnerOnlyPath('/admin/settings/messaging')).toBe(false);
    expect(isOwnerOnlyPath('/admin/permissions')).toBe(false);
    expect(isOwnerOnlyPath('/apps/files')).toBe(false);
    expect(isOwnerOnlyPath('/apps/editor')).toBe(false);
    expect(isOwnerOnlyPath('/apps/terminal')).toBe(false);
    expect(isOwnerOnlyPath('/apps/library')).toBe(false);
    expect(isOwnerOnlyPath('/apps/show/session-1')).toBe(false);
    expect(isOwnerOnlyPath('/')).toBe(false);
  });

  it('does not match a route that only shares a gated prefix', () => {
    expect(isOwnerOnlyPath('/admin/dashboards')).toBe(false);
    expect(isOwnerOnlyPath('/harness-status')).toBe(false);
    expect(isOwnerOnlyPath('/apps/library-picker')).toBe(false);
  });
});

describe('filterOwnerOnlyNavItems', () => {
  it('removes Owner-only destinations from nested admin navigation trees', () => {
    const visible = filterOwnerOnlyNavItems([
      {
        children: [
          { to: '/admin/settings/platforms' },
          { to: '/admin/groups' },
          { to: '/admin/settings/messaging' },
        ],
      },
      { onClick: () => undefined },
      { to: '/admin/settings/messaging' },
    ]);

    expect(visible).toEqual([
      {
        children: [{ to: '/admin/settings/messaging' }],
      },
      { onClick: expect.any(Function) },
      { to: '/admin/settings/messaging' },
    ]);
  });
});

describe('visibleAdminNavItems', () => {
  const adminItems = [
    { to: '/admin/dashboard' },
    { to: '/admin/remote-access' },
    { to: '/admin/settings/messaging' },
  ];

  it('keeps Owner destinations when the caller can manage the instance', () => {
    expect(visibleAdminNavItems(adminItems, true)).toEqual(adminItems);
  });

  it('hides Owner destinations only when the caller cannot manage the instance', () => {
    expect(visibleAdminNavItems(adminItems, false)).toEqual([
      { to: '/admin/settings/messaging' },
    ]);
  });
});

describe('adminLandingPath', () => {
  it('opens the Dashboard for an owner', () => {
    expect(adminLandingPath(true)).toBe('/admin/dashboard');
  });

  it('sends a remote owner to an admin page they can actually use', () => {
    const destination = adminLandingPath(false);
    expect(destination).toBe('/admin/settings/messaging');
    expect(isOwnerOnlyPath(destination)).toBe(false);
  });

  it('keeps non-owners on messaging settings', () => {
    expect(adminLandingPath(false)).toBe('/admin/settings/messaging');
  });
});

describe('isLocalOnlyMessagingField', () => {
  it('marks messaging controls governed by the Owner capability', () => {
    expect(isLocalOnlyMessagingField('agents.opencode.error_retry_limit')).toBe(true);
    expect(isLocalOnlyMessagingField('agents.opencode.active_turn_timeout_seconds')).toBe(true);
    expect(isLocalOnlyMessagingField('show_pages_prompt')).toBe(true);
  });

  it('leaves remote-safe messaging preferences available remotely', () => {
    expect(isLocalOnlyMessagingField('ack_mode')).toBe(false);
    expect(isLocalOnlyMessagingField('show_duration')).toBe(false);
    expect(isLocalOnlyMessagingField('reply_enhancements')).toBe(false);
  });
});

describe('isMemorySettingsPath', () => {
  it('matches the Memory route and nested path boundaries', () => {
    expect(isMemorySettingsPath('/admin/settings/memory')).toBe(true);
    expect(isMemorySettingsPath('/admin/settings/memory/')).toBe(true);
    expect(isMemorySettingsPath('/admin/settings/memory/details')).toBe(true);
  });

  it('does not match a route that only shares the Memory prefix', () => {
    expect(isMemorySettingsPath('/admin/settings/memory-tools')).toBe(false);
  });
});
