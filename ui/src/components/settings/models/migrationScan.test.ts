import { describe, expect, it, vi } from 'vitest';

import { importableKeys, isImportableKey, scanMigrationWhenEnabled } from './migrationScan';
import type { MigrationItem } from './types';

describe('scanMigrationWhenEnabled', () => {
  it('does not issue a migration scan while Model Hub is disabled', async () => {
    const scan = vi.fn(async () => ({ items: [] }));

    await expect(scanMigrationWhenEnabled(false, scan)).resolves.toBeNull();
    expect(scan).not.toHaveBeenCalled();
  });

  it('issues one scan after the backend capability is enabled', async () => {
    const result = { items: [] };
    const scan = vi.fn(async () => result);

    await expect(scanMigrationWhenEnabled(true, scan)).resolves.toBe(result);
    expect(scan).toHaveBeenCalledTimes(1);
  });
});

// The predicate the setup import entry is defined by. It is stated over the real
// shapes `migration.py` produces, because the mistake it guards against is a
// plausible-looking `kind === 'api_key'` filter: every OpenCode key-backed import
// is `opencode_provider`, so that filter silently offers zero OpenCode keys.
const row = (over: Partial<MigrationItem>): MigrationItem => ({
  id: 'mig_1',
  backend: 'opencode',
  kind: 'opencode_provider',
  masked_detail: 'zhipuai · sk-…3456',
  proposed_action: 'import',
  selected: true,
  notes_key: null,
  ...over,
});

describe('isImportableKey', () => {
  it('counts an OpenCode key, which never arrives as kind api_key', () => {
    expect(isImportableKey(row({ backend: 'opencode', kind: 'opencode_provider' }))).toBe(true);
  });

  it('counts Claude and Codex API keys', () => {
    expect(isImportableKey(row({ backend: 'claude', kind: 'api_key' }))).toBe(true);
    expect(isImportableKey(row({ backend: 'codex', kind: 'api_key' }))).toBe(true);
  });

  it('leaves native subscriptions out — they are kept, not imported', () => {
    expect(isImportableKey(row({ kind: 'oauth_native', proposed_action: 'keep_native' }))).toBe(false);
  });

  it('leaves keep_native out even on a key-shaped row', () => {
    // keep_native is a legitimate applied action in the broader settings
    // migration; this entry is about keys the gateway takes over, so it is out
    // here without being disabled there.
    expect(isImportableKey(row({ kind: 'api_key', proposed_action: 'keep_native' }))).toBe(false);
  });

  it('leaves reauth out — it needs the interactive flow, not a batch', () => {
    expect(isImportableKey(row({ kind: 'api_key', proposed_action: 'reauth' }))).toBe(false);
  });

  it('leaves the reserved controlled_import out', () => {
    expect(isImportableKey(row({ proposed_action: 'controlled_import' }))).toBe(false);
  });
});

describe('importableKeys', () => {
  it('keeps scan order and drops everything the predicate rejects', () => {
    const items = [
      row({ id: 'a', backend: 'claude', kind: 'api_key' }),
      row({ id: 'b', kind: 'oauth_native', proposed_action: 'keep_native' }),
      row({ id: 'c', backend: 'codex', kind: 'api_key', proposed_action: 'reauth' }),
      row({ id: 'd' }),
    ];

    expect(importableKeys(items).map((i) => i.id)).toEqual(['a', 'd']);
  });

  it('counts a row the scan pre-selected and one it did not, alike', () => {
    // The notice reports what CAN be imported; what is ticked is the dialog's
    // business. A count that silently followed `selected` would disagree with
    // the rows the dialog then shows.
    const items = [row({ id: 'a', selected: true }), row({ id: 'b', selected: false })];

    expect(importableKeys(items)).toHaveLength(2);
  });
});
