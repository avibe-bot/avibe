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

/**
 * Every `models.*` i18n key the given schema documents declare.
 *
 * Both ways a schema can close a vocabulary count: `enum` for a set, and `const`
 * for the degenerate one-member set. JSON Schema treats `const: X` as exactly
 * `enum: [X]`, so reading only `enum` silently skips whole branches — today
 * `source.schema.json` pins the `error` status's `detail_key` with a `const`.
 *
 * Takes its input rather than reading the directory itself, so the collector can
 * be checked against a fixture: a coverage test whose own collector is wrong
 * reports coverage it never had.
 */
export function collectKeys(schemas: unknown[]): string[] {
  const keys = new Set<string>();
  const add = (member: unknown) => {
    if (typeof member === 'string' && member.startsWith('models.')) keys.add(member);
  };
  const walk = (node: unknown) => {
    if (Array.isArray(node)) {
      node.forEach(walk);
      return;
    }
    if (!node || typeof node !== 'object') return;
    for (const [prop, value] of Object.entries(node as Record<string, unknown>)) {
      if (prop === 'enum' && Array.isArray(value)) value.forEach(add);
      if (prop === 'const') add(value);
      walk(value);
    }
  };
  schemas.forEach(walk);
  return [...keys].sort();
}

function contractKeys(): string[] {
  return collectKeys(
    readdirSync(CONTRACTS)
      .filter((f) => f.endsWith('.schema.json'))
      .map((f) => JSON.parse(readFileSync(join(CONTRACTS, f), 'utf8'))),
  );
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
    // Declared by a `const` in source.schema.json — today ALSO by probe-result's
    // enum, which is why the enum-only collector still covered it. Asserting it
    // here keeps the coverage from silently depending on that coincidence.
    expect(keys).toContain('models.source.error.unclassified');
    expect(keys.length).toBeGreaterThanOrEqual(13);
  });

  it('collects a key a schema closes with `const`, not only with `enum`', () => {
    // The forward guard, on a fixture rather than on today's schemas: a single
    // status branch pinned with `const` is a complete one-member vocabulary, and
    // the enum-only collector reported such a key as "not declared" — i.e. it
    // passed while the bundles had no copy for it.
    const fixture = {
      properties: {
        state: {
          allOf: [
            { then: { properties: { detail_key: { const: 'models.source.error.fixture_only' } } } },
            { then: { properties: { detail_key: { enum: ['models.source.cooldown.fixture_enum'] } } } },
          ],
        },
      },
      // Non-`models.` constants stay out; this collects contract vocabularies,
      // not every string literal in a schema.
      examples: [{ notes_key: { const: 'settings.models.source.nativeSupply' } }],
    };
    expect(collectKeys([fixture])).toEqual([
      'models.source.cooldown.fixture_enum',
      'models.source.error.fixture_only',
    ]);
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
