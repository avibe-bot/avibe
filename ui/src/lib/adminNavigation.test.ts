/* @vitest-environment jsdom */

import { beforeEach, describe, expect, it } from 'vitest';

import {
  isLocalOnlyMessagingField,
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
      '/settings/remote-access',
      '/settings/backends/claude',
      '/settings/models',
      '/settings/dependencies',
      '/settings/diagnostics/logs',
    ];
    expect(ownerOnly.every(isOwnerOnlyPath)).toBe(true);
  });

  it('keeps personal preferences, replies, and access readable', () => {
    expect(isOwnerOnlyPath('/settings/replies')).toBe(false);
    expect(isOwnerOnlyPath('/settings/access')).toBe(false);
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

describe('isLocalOnlyMessagingField', () => {
  it('keeps machine-global controls owner-only without gating Replies', () => {
    expect(isLocalOnlyMessagingField('agents.opencode.error_retry_limit')).toBe(true);
    expect(isLocalOnlyMessagingField('agents.opencode.active_turn_timeout_seconds')).toBe(true);
    expect(isLocalOnlyMessagingField('reply_enhancements')).toBe(false);
  });
});
