import { describe, expect, it } from 'vitest';

import {
  adminLandingPath,
  isAdvancedSettingsPath,
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
    expect(isLocalSystemPath('/admin/settings/service')).toBe(true);
    expect(isLocalSystemPath('/admin/settings/platforms')).toBe(true);
    expect(isLocalSystemPath('/admin/settings/backends')).toBe(true);
    expect(isLocalSystemPath('/harness')).toBe(true);
    expect(isLocalSystemPath('/apps/library')).toBe(true);
  });

  it('matches nested paths under a gated destination', () => {
    expect(isLocalSystemPath('/admin/settings/platforms/slack')).toBe(true);
    expect(isLocalSystemPath('/harness/')).toBe(true);
  });

  it('leaves remotely usable destinations open', () => {
    expect(isLocalSystemPath('/admin/settings/messaging')).toBe(false);
    expect(isLocalSystemPath('/admin/settings/models')).toBe(false);
    expect(isLocalSystemPath('/admin/organization/overview')).toBe(false);
    expect(isLocalSystemPath('/')).toBe(false);
  });

  it('does not match a route that only shares a gated prefix', () => {
    expect(isLocalSystemPath('/admin/dashboards')).toBe(false);
    expect(isLocalSystemPath('/harness-status')).toBe(false);
    expect(isLocalSystemPath('/apps/library-picker')).toBe(false);
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
