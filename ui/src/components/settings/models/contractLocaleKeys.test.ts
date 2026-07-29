// AC-19, mechanically: the frozen contracts publish closed vocabularies of i18n
// keys (eligibility reasons, source detail keys), and this page renders them
// as-is. So the locale files must hold a string for EVERY member of every such
// enum — checked against the schemas themselves, not against a hand-copied list,
// because a hand-copied list is the drift it is meant to prevent.
//
// The extension rule this enforces: a new cause ships its enum member (L1's
// schema) and its locale copy (L4's `ui/src/i18n/*.json`) in the same change.
import { readdirSync, readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

import en from '../../../i18n/en.json';
import zh from '../../../i18n/zh.json';

const CONTRACTS = resolve(dirname(fileURLToPath(import.meta.url)), '../../../../..', 'docs/plans/model-hub-contracts');

/** Every `models.*` i18n key any frozen schema declares, from every enum in it. */
function contractKeys(): string[] {
  const keys = new Set<string>();
  const walk = (node: unknown) => {
    if (Array.isArray(node)) {
      node.forEach(walk);
      return;
    }
    if (!node || typeof node !== 'object') return;
    for (const [prop, value] of Object.entries(node as Record<string, unknown>)) {
      if (prop === 'enum' && Array.isArray(value)) {
        for (const member of value) if (typeof member === 'string' && member.startsWith('models.')) keys.add(member);
      }
      walk(value);
    }
  };
  for (const file of readdirSync(CONTRACTS).filter((f) => f.endsWith('.schema.json'))) {
    walk(JSON.parse(readFileSync(join(CONTRACTS, file), 'utf8')));
  }
  return [...keys].sort();
}

const lookup = (bundle: unknown, key: string): unknown =>
  key.split('.').reduce<unknown>((node, part) => {
    if (!node || typeof node !== 'object') return undefined;
    return (node as Record<string, unknown>)[part];
  }, bundle);

describe('contract i18n key coverage (AC-19)', () => {
  const keys = contractKeys();

  it('reads a non-trivial vocabulary out of the frozen schemas', () => {
    // Guards the check itself: a broken path or a renamed contract directory
    // would otherwise make every assertion below pass over an empty list.
    expect(keys).toContain('models.eligibility.subscription_wrong_client');
    expect(keys).toContain('models.eligibility.opencode_api_key_only');
    expect(keys).toContain('models.eligibility.consent_required');
    expect(keys.length).toBeGreaterThanOrEqual(13);
  });

  it.each(['zh', 'en'] as const)('translates every declared key in %s', (lng) => {
    const bundle = lng === 'zh' ? zh : en;
    const missing = keys.filter((k) => typeof lookup(bundle, k) !== 'string' || lookup(bundle, k) === '');
    expect(missing).toEqual([]);
  });

  it('keeps the two locales on the same key set', () => {
    const enOnly = keys.filter((k) => typeof lookup(en, k) === 'string' && typeof lookup(zh, k) !== 'string');
    const zhOnly = keys.filter((k) => typeof lookup(zh, k) === 'string' && typeof lookup(en, k) !== 'string');
    expect({ enOnly, zhOnly }).toEqual({ enOnly: [], zhOnly: [] });
  });
});
