import { describe, expect, it } from 'vitest';

import en from '../../../i18n/en.json';
import zh from '../../../i18n/zh.json';
import {
  assessSourceEdit,
  canEditSourceEndpoint,
  manageActions,
  MANAGE_DESTINATION,
  MANAGE_LABEL_KEY,
} from './manage';
import { SOURCE_STATUSES } from './types';
import type { Source } from './types';

const source = (over: Partial<Source> = {}): Source => ({
  id: 'src_manage',
  kind: 'api_key',
  vendor: 'anthropic',
  display_name: 'Production key',
  protocol: 'anthropic',
  base_url: 'https://relay.example/v1',
  supply_channel: 'hub',
  billing: 'metered',
  credential_ref: 'cred_manage',
  state: { status: 'active' },
  last_discovered_at: null,
  models: [],
  ...over,
});

const translated = (bundle: unknown, key: string): unknown =>
  key.split('.').reduce<unknown>((node, part) => {
    if (!node || typeof node !== 'object') return undefined;
    return (node as Record<string, unknown>)[part];
  }, bundle);

describe('source management capabilities', () => {
  it('keeps management available independently of repair state', () => {
    const capabilitySet = Object.keys(MANAGE_DESTINATION);
    for (const status of SOURCE_STATUSES) {
      expect(manageActions(source({ state: { status } }))).toEqual(capabilitySet);
    }
  });

  it('limits endpoint ownership to Avibe-held API-key sources', () => {
    expect(canEditSourceEndpoint(source())).toBe(true);
    expect(canEditSourceEndpoint(source({ kind: 'subscription' }))).toBe(false);
    expect(canEditSourceEndpoint(source({ supply_channel: 'native_cli' }))).toBe(false);
    expect(canEditSourceEndpoint(source({ credential_ref: null }))).toBe(false);
  });

  it('normalizes changed metadata and rejects credential-bearing drafts', () => {
    expect(assessSourceEdit(source(), {
      displayName: '  Relay key  ',
      baseUrl: 'HTTPS://relay.example/v2/',
    })).toEqual({
      valid: true,
      patch: { display_name: 'Relay key', base_url: 'https://relay.example/v2' },
    });
    expect(assessSourceEdit(source(), {
      displayName: 'Production key',
      baseUrl: 'https://relay.example/v1?api_key=secret',
    })).toEqual({ valid: false, patch: null });
  });

  it.each(Object.entries(MANAGE_LABEL_KEY))('translates the %s action in both locales', (_kind, key) => {
    for (const bundle of [en, zh]) expect(typeof translated(bundle, key)).toBe('string');
  });
});
