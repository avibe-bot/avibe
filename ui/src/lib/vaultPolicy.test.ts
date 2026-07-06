import { describe, expect, it } from 'vitest';

import { buildMetadataPatch } from './vaultPolicy';

const base = {
  description: '',
  tags: [] as string[],
  allowHosts: [] as string[],
  fetchAuthMode: 'bearer' as const,
  fetchAuthName: '',
};

describe('buildMetadataPatch', () => {
  it('trims description and clears to null when blank', () => {
    expect(buildMetadataPatch({ ...base, description: '  hi  ' }).description).toBe('hi');
    expect(buildMetadataPatch({ ...base, description: '   ' }).description).toBeNull();
    expect(buildMetadataPatch({ ...base, description: '' }).description).toBeNull();
  });

  it('includes allowed_hosts only when present', () => {
    expect(buildMetadataPatch({ ...base, allowHosts: ['api.x.com', '.x.com'] }).policy).toEqual({
      allowed_hosts: ['api.x.com', '.x.com'],
    });
    expect(buildMetadataPatch({ ...base }).policy).toEqual({});
  });

  it('bearer emits no auth; header/query emit typed auth with a trimmed name', () => {
    expect(buildMetadataPatch({ ...base, fetchAuthMode: 'bearer', fetchAuthName: 'ignored' }).policy.auth).toBeUndefined();
    expect(buildMetadataPatch({ ...base, fetchAuthMode: 'header', fetchAuthName: ' X-Api-Key ' }).policy.auth).toEqual({
      type: 'header',
      name: 'X-Api-Key',
    });
    expect(buildMetadataPatch({ ...base, fetchAuthMode: 'query', fetchAuthName: 'api_key' }).policy.auth).toEqual({
      type: 'query',
      name: 'api_key',
    });
  });

  it('never emits always_ask or other internal policy keys', () => {
    const patch = buildMetadataPatch({ ...base, allowHosts: ['api.x.com'], fetchAuthMode: 'header', fetchAuthName: 'X-Api-Key' });
    expect(patch.policy).not.toHaveProperty('always_ask');
    expect(Object.keys(patch.policy).sort()).toEqual(['allowed_hosts', 'auth']);
  });

  it('keeps skill: tags inline in tags and never emits a links field (PATCH rejects extra fields)', () => {
    const patch = buildMetadataPatch({ ...base, tags: ['prod', 'skill:deploy', 'ci'] });
    expect(patch.tags).toContain('prod');
    expect(patch.tags).toContain('skill:deploy');
    expect(patch.tags).toContain('ci');
    expect(patch).not.toHaveProperty('links');
  });
});
