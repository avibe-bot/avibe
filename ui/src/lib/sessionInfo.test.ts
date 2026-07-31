import { describe, expect, it } from 'vitest';

import {
  DENIED_INSTANCE_CAPABILITIES,
  normalizeSessionInfo,
  OWNER_INSTANCE_CAPABILITIES,
} from './sessionInfo';

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
      instance_role: 'owner',
      capabilities: OWNER_INSTANCE_CAPABILITIES,
    });
  });

  it('preserves explicit current capabilities without granting missing fields', () => {
    const session = normalizeSessionInfo({
      remote: true,
      authenticated: true,
      email: 'viewer@example.com',
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
      instance_role: 'viewer',
      capabilities: {
        ...DENIED_INSTANCE_CAPABILITIES,
        can_read_instance: true,
        can_use_show_pages: true,
      },
    });
  });

  it('keeps local sessions owner-compatible when an older server omits capabilities', () => {
    expect(normalizeSessionInfo({ remote: false })).toEqual({
      remote: false,
      instance_role: 'owner',
      capabilities: OWNER_INSTANCE_CAPABILITIES,
    });
  });

  it('fails closed for malformed session payloads', () => {
    expect(normalizeSessionInfo(null)).toEqual({ remote: true, authenticated: false });
  });
});
