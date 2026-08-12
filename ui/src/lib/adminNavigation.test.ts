import { describe, expect, it } from 'vitest';

import {
  adminLandingPath,
  filterLocalSystemNavItems,
  isAdvancedSettingsPath,
  isLocalOnlyMessagingField,
  isLocalSystemPath,
  isMemorySettingsPath,
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

describe('isLocalSystemPath', () => {
  it('covers every destination whose page runs entirely on local-only routes', () => {
    expect(isLocalSystemPath('/admin/dashboard')).toBe(true);
    expect(isLocalSystemPath('/admin/remote-access')).toBe(true);
    expect(isLocalSystemPath('/admin/groups')).toBe(true);
    expect(isLocalSystemPath('/admin/users')).toBe(true);
    expect(isLocalSystemPath('/admin/logs')).toBe(true);
    expect(isLocalSystemPath('/admin/settings/service')).toBe(true);
    expect(isLocalSystemPath('/admin/settings/platforms')).toBe(true);
    expect(isLocalSystemPath('/admin/settings/backends')).toBe(true);
    expect(isLocalSystemPath('/admin/settings/models')).toBe(true);
    expect(isLocalSystemPath('/admin/settings/dependencies')).toBe(true);
    expect(isLocalSystemPath('/admin/settings/diagnostics')).toBe(true);
    expect(isLocalSystemPath('/admin/settings/logs')).toBe(true);
    expect(isLocalSystemPath('/harness')).toBe(true);
    expect(isLocalSystemPath('/apps/library')).toBe(true);
  });

  it('matches nested paths under a gated destination', () => {
    expect(isLocalSystemPath('/admin/settings/platforms/slack')).toBe(true);
    expect(isLocalSystemPath('/admin/groups/engineering')).toBe(true);
    expect(isLocalSystemPath('/harness/')).toBe(true);
  });

  it('leaves remotely usable destinations open', () => {
    expect(isLocalSystemPath('/admin/settings/messaging')).toBe(false);
    expect(isLocalSystemPath('/admin/organization/overview')).toBe(false);
    expect(isLocalSystemPath('/')).toBe(false);
  });

  it('does not match a route that only shares a gated prefix', () => {
    expect(isLocalSystemPath('/admin/dashboards')).toBe(false);
    expect(isLocalSystemPath('/harness-status')).toBe(false);
    expect(isLocalSystemPath('/apps/library-picker')).toBe(false);
  });
});

describe('filterLocalSystemNavItems', () => {
  it('removes local-only destinations from nested admin navigation trees', () => {
    const visible = filterLocalSystemNavItems([
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

describe('adminLandingPath', () => {
  it('opens the Dashboard for a trusted-local caller', () => {
    expect(adminLandingPath(true)).toBe('/admin/dashboard');
  });

  it('sends a remote owner to an admin page they can actually use', () => {
    const destination = adminLandingPath(false);
    expect(destination).toBe('/admin/settings/messaging');
    expect(isLocalSystemPath(destination)).toBe(false);
  });
});

describe('isLocalOnlyMessagingField', () => {
  it('marks only the protected messaging controls as trusted-local', () => {
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
