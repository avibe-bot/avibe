/* @vitest-environment jsdom */

import { beforeEach, describe, expect, it } from 'vitest';

import {
  isLocalOnlyMessagingField,
  isMemorySettingsPath,
  isOwnerOnlyPath,
  rememberSettingsPath,
  SETTINGS_LAST_PATH_KEY,
  settingsLandingPath,
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
      '/settings/memory',
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

describe('settings landing memory', () => {
  it('remembers the most recent canonical section or detail', () => {
    rememberSettingsPath('/settings/backends/claude');
    expect(window.localStorage.getItem(SETTINGS_LAST_PATH_KEY)).toBe('/settings/backends/claude');
    expect(settingsLandingPath(true)).toBe('/settings/backends/claude');
  });

  it('falls back to Replies for members when an owner-only section was remembered', () => {
    window.localStorage.setItem(SETTINGS_LAST_PATH_KEY, '/settings/service');
    expect(settingsLandingPath(false)).toBe('/settings/replies');
  });

  it('does not remember transitional platform scope pages as the Settings landing page', () => {
    rememberSettingsPath('/settings/platforms/groups');
    expect(settingsLandingPath(true)).toBe('/settings/replies');
  });

  it.each(['/settings/appearance', '/settings/account'])('retires a remembered preference destination: %s', (path) => {
    window.localStorage.setItem(SETTINGS_LAST_PATH_KEY, path);
    expect(settingsLandingPath(true)).toBe('/settings/replies');
  });
});

describe('isLocalOnlyMessagingField', () => {
  it('keeps machine-global controls owner-only without gating Replies', () => {
    expect(isLocalOnlyMessagingField('agents.opencode.error_retry_limit')).toBe(true);
    expect(isLocalOnlyMessagingField('agents.opencode.active_turn_timeout_seconds')).toBe(true);
    expect(isLocalOnlyMessagingField('reply_enhancements')).toBe(false);
  });
});

describe('isMemorySettingsPath', () => {
  it('matches the canonical Memory route at a path boundary', () => {
    expect(isMemorySettingsPath('/settings/memory')).toBe(true);
    expect(isMemorySettingsPath('/settings/memory/details')).toBe(true);
    expect(isMemorySettingsPath('/settings/memory-tools')).toBe(false);
  });
});
