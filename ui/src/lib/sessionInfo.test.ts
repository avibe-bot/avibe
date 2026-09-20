import { describe, expect, it } from 'vitest';

import {
  canCreateLocalProject,
  DENIED_INSTANCE_CAPABILITIES,
  normalizeSessionInfo,
  OWNER_INSTANCE_CAPABILITIES,
} from './sessionInfo';

describe('canCreateLocalProject', () => {
  it('requires project management and local file capabilities', () => {
    expect(canCreateLocalProject(OWNER_INSTANCE_CAPABILITIES)).toBe(true);
    expect(
      canCreateLocalProject({
        ...OWNER_INSTANCE_CAPABILITIES,
        can_use_files: false,
      }),
    ).toBe(false);
  });
});

describe('normalizeSessionInfo', () => {
  it('preserves legacy authenticated remote sessions as owners', () => {
    expect(
      normalizeSessionInfo({
        remote: true,
        authenticated: true,
        email: 'owner@example.com',
        sub: 'owner-1',
      }),
    ).toEqual({
      remote: true,
      authenticated: true,
      email: 'owner@example.com',
      sub: 'owner-1',
      instance_kind: null,
      instance_role: 'owner',
      capabilities: OWNER_INSTANCE_CAPABILITIES,
      authorization_state: 'current',
    });
  });

  it('preserves explicit current capabilities without granting missing fields', () => {
    const session = normalizeSessionInfo({
      remote: true,
      authenticated: true,
      email: 'viewer@example.com',
      instance_kind: null,
      instance_role: 'viewer',
      capabilities: {
        can_read_instance: true,
        can_use_show_pages: true,
      },
    });

    expect(session).toEqual({
      remote: true,
      authenticated: true,
      email: 'viewer@example.com',
      instance_kind: null,
      instance_role: 'viewer',
      capabilities: {
        ...DENIED_INSTANCE_CAPABILITIES,
        can_read_instance: true,
        can_use_show_pages: true,
      },
      authorization_state: 'current',
    });
  });

  it.each(['revoked', 'unavailable'] as const)(
    'keeps an authenticated %s session distinct from login expiry',
    (authorizationState) => {
      expect(normalizeSessionInfo({
        remote: true,
        authenticated: true,
        email: 'member@example.com',
        sub: 'member-1',
        instance_kind: 'organization',
        authorization_state: authorizationState,
      })).toEqual({
        remote: true,
        authenticated: true,
        email: 'member@example.com',
        sub: 'member-1',
        instance_kind: 'organization',
        authorization_state: authorizationState,
      });
    },
  );

  it('keeps local sessions owner-compatible when an older server omits capabilities', () => {
    expect(normalizeSessionInfo({ remote: false })).toEqual({
      remote: false,
      instance_kind: null,
      instance_role: 'owner',
      capabilities: OWNER_INSTANCE_CAPABILITIES,
    });
  });

  it('fails closed for malformed session payloads', () => {
    expect(normalizeSessionInfo(null)).toEqual({ remote: true, authenticated: false });
  });

  it('preserves an explicit member role and capability', () => {
    const session = normalizeSessionInfo({
      remote: true,
      authenticated: true,
      email: 'member@example.com',
      instance_kind: 'organization',
      instance_role: 'member',
      capabilities: {
        ...OWNER_INSTANCE_CAPABILITIES,
        can_manage_access_members: false,
        is_instance_owner: false,
      },
    });

    expect(session).toMatchObject({
      instance_role: 'member',
      capabilities: {
        ...OWNER_INSTANCE_CAPABILITIES,
        can_manage_access_members: false,
        is_instance_owner: false,
      },
    });
  });

  it('does not grant can_manage_access_members from a pre-member payload', () => {
    const session = normalizeSessionInfo({
      remote: true,
      authenticated: true,
      email: 'owner@example.com',
      instance_role: 'owner',
      capabilities: {
        is_instance_owner: true,
        can_manage_instance: true,
      },
    });

    expect(session).toMatchObject({
      instance_role: 'owner',
      capabilities: {
        ...DENIED_INSTANCE_CAPABILITIES,
        is_instance_owner: true,
        can_manage_instance: true,
        can_manage_access_members: false,
      },
    });
  });

  it.each([
    ['personal', 'personal'],
    ['organization', 'organization'],
    ['enterprise', null],
    ['', null],
    [null, null],
  ])('normalizes instance kind %j to %j', (rawKind, expectedKind) => {
    expect(normalizeSessionInfo({ remote: false, instance_kind: rawKind })).toMatchObject({
      instance_kind: expectedKind,
    });
  });
});
