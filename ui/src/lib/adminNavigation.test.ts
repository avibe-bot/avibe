/* @vitest-environment jsdom */

import { beforeEach, describe, expect, it } from 'vitest';

import {
  isOwnerOnlyPath,
  SETTINGS_LANDING_PATH,
} from './adminNavigation';

beforeEach(() => {
  window.localStorage.clear();
});
describe('isOwnerOnlyPath', () => {
  it('covers canonical machine-management destinations and their details', () => {
    const ownerOnly = [
      '/settings/service',
      '/settings/platforms/slack',
      '/settings/remote-access/connection',
      '/settings/remote-access/connection/address',
      '/settings/remote-access/activity',
      '/settings/backends/claude',
      '/settings/models',
      '/settings/dependencies',
      '/settings/diagnostics/logs',
    ];
    expect(ownerOnly.every(isOwnerOnlyPath)).toBe(true);
  });

  it('keeps personal preferences, messages, and access readable', () => {
    expect(isOwnerOnlyPath('/settings/messages')).toBe(false);
    expect(isOwnerOnlyPath('/settings/replies')).toBe(false);
    expect(isOwnerOnlyPath('/settings/access')).toBe(false);
    // 「设备」 is every signed-in person's page: a member sees their own devices there.
    expect(isOwnerOnlyPath('/settings/remote-access')).toBe(false);
  });

  it('keeps retired owner routes gated before their redirect runs', () => {
    expect(isOwnerOnlyPath('/admin/dashboard')).toBe(true);
    expect(isOwnerOnlyPath('/admin/settings/backends/codex')).toBe(true);
    expect(isOwnerOnlyPath('/admin/settings/messaging')).toBe(false);
    expect(isOwnerOnlyPath('/admin/permissions')).toBe(false);
  });
});
describe('settings landing', () => {
  it('opens General, which every role can read', () => {
    expect(SETTINGS_LANDING_PATH).toBe('/settings/general');
    expect(isOwnerOnlyPath(SETTINGS_LANDING_PATH)).toBe(false);
  });
});
