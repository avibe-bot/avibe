import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';

import en from '../../../i18n/en.json';
import zh from '../../../i18n/zh.json';
import {
  assessSourceEdit,
  canEditSourceEndpoint,
  manageActions,
  MANAGE_DESTINATION,
  MANAGE_LABEL_KEY,
  MANAGE_STAGE_CANCEL,
  MANAGE_STAGE_FAILURE_SURFACE,
  MANAGE_STAGE_KINDS,
  MANAGE_STAGE_RETRY,
  SOURCE_EDIT_REASON_KEY,
} from './manage';
import { SOURCE_PROTOCOLS } from './types';
import type { Source, SourceProtocol } from './types';

type ValidationCase = {
  id: string;
  value: string;
  valid: boolean;
  normalized?: string;
  reason?: string;
};
type EmptyTargetCase = {
  id: string;
  vendor: string;
  protocol: SourceProtocol;
  server_valid: boolean;
};

const validationFixture = JSON.parse(readFileSync(join(
  process.cwd(),
  '../tests/fixtures/model_hub_source_edit_validation.json',
), 'utf8')) as {
  display_names: ValidationCase[];
  base_urls: ValidationCase[];
  empty_targets: EmptyTargetCase[];
};

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
    expect(manageActions()).toEqual(Object.keys(MANAGE_DESTINATION));
  });

  it('limits endpoint ownership to Avibe-held API-key sources', () => {
    expect(canEditSourceEndpoint(source())).toBe(true);
    expect(canEditSourceEndpoint(source({ kind: 'subscription' }))).toBe(false);
    expect(canEditSourceEndpoint(source({ supply_channel: 'native_cli' }))).toBe(false);
    expect(canEditSourceEndpoint(source({ credential_ref: null }))).toBe(false);
  });

  it('defines every stage cancel, retry, and visible-failure destination', () => {
    for (const record of [MANAGE_STAGE_CANCEL, MANAGE_STAGE_RETRY, MANAGE_STAGE_FAILURE_SURFACE]) {
      expect(new Set(Object.keys(record))).toEqual(new Set(MANAGE_STAGE_KINDS));
      for (const kind of MANAGE_STAGE_KINDS) expect(record[kind]).toEqual(expect.any(String));
    }
  });

  it('trims metadata but leaves endpoint normalization to the server', () => {
    expect(assessSourceEdit(source(), {
      displayName: '  Relay key  ',
      baseUrl: 'HTTPS://relay.example/v2/',
    })).toEqual({
      valid: true,
      patch: { display_name: 'Relay key', base_url: 'HTTPS://relay.example/v2/' },
      reason: null,
    });
  });

  it('holds the same display-name and Base-URL contract fixture as the server', () => {
    for (const item of validationFixture.display_names) {
      const assessment = assessSourceEdit(source(), {
        displayName: item.value,
        baseUrl: source().base_url ?? '',
      });
      expect(assessment.valid, item.id).toBe(item.valid);
      expect(assessment.reason, item.id).toBe(item.reason ?? null);
    }
    for (const item of validationFixture.base_urls) {
      const assessment = assessSourceEdit(source(), {
        displayName: source().display_name,
        baseUrl: item.value,
      });
      expect(assessment.valid, item.id).toBe(item.valid);
      expect(assessment.reason, item.id).toBe(item.reason ?? null);
      if (item.valid) expect(assessment.patch?.base_url, item.id).toBe(item.value.trim());
    }
  });

  it('never adds an endpoint patch to a display-name-only edit', () => {
    for (const item of validationFixture.base_urls.filter((candidate) => candidate.valid)) {
      const stored = item.normalized ?? item.value.trim();
      const assessment = assessSourceEdit(source({ base_url: stored }), {
        displayName: 'Renamed source',
        baseUrl: stored,
      });
      expect(assessment.valid, item.id).toBe(true);
      expect(assessment.patch, item.id).toEqual({ display_name: 'Renamed source' });
      expect(assessment.patch, item.id).not.toHaveProperty('base_url');
    }
  });

  it('holds no vendor or protocol policy for an emptied endpoint', () => {
    const vendors = new Set(validationFixture.empty_targets.map((item) => item.vendor));
    const pairs = new Set(validationFixture.empty_targets.map((item) => `${item.vendor}:${item.protocol}`));
    expect(pairs.size).toBe(vendors.size * SOURCE_PROTOCOLS.length);
    for (const vendor of vendors) {
      for (const protocol of SOURCE_PROTOCOLS) expect(pairs.has(`${vendor}:${protocol}`)).toBe(true);
    }

    for (const item of validationFixture.empty_targets) {
      const assessment = assessSourceEdit(source({ vendor: item.vendor, protocol: item.protocol }), {
        displayName: source().display_name,
        baseUrl: '',
      });
      expect(assessment, item.id).toEqual({
        valid: true,
        patch: { base_url: null },
        reason: null,
      });
    }
  });

  it.each(Object.entries(MANAGE_LABEL_KEY))('translates the %s action in both locales', (_kind, key) => {
    for (const bundle of [en, zh]) expect(typeof translated(bundle, key)).toBe('string');
  });

  it.each(Object.entries(SOURCE_EDIT_REASON_KEY))('translates the %s validation reason in both locales', (_reason, key) => {
    for (const bundle of [en, zh]) expect(typeof translated(bundle, key)).toBe('string');
  });
});
