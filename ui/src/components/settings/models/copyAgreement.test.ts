// A redline over English grammatical number, wherever copy interpolates a QUANTITY.
//
// Round 7 reported 「{{names}} has no supply」: `formatNameList` joins any number of
// Agents into one placeholder, so two of them render 「agent-a, agent-b has no
// supply」. Chinese needs no agreement, which is why the zh bundle these strings were
// translated from reads correctly and the English silently did not.
//
// The reported string was not the class. The same page also wrote 「{{count}} agents
// have no supply」, which breaks at the MOST common count — one. The final UI copy
// register resolves that class with i18next plural families. The rule below is TOTAL
// rather than a list of known-bad keys; that is the only way it catches copy that does
// not exist yet. Two shapes, two scopes:
//
//   1. a list placeholder in front of a singular verb — checked over the WHOLE
//      bundle, because no string anywhere passes a joined list to a singular verb.
//   2. every `{{count}}` key under `settings.models` belongs to a complete
//      `_one`/`_other` family. Both locale files carry both leaves; zh deliberately
//      repeats the value so locale parity remains a plain set equality.
import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

import en from '../../../i18n/en.json';
import zh from '../../../i18n/zh.json';
import { BACKEND_ADOPTION_VENDOR_KEY } from './vendorMeta';

/** Placeholders a join can fill with more than one item. */
const LIST_PLACEHOLDER = /\{\{(?:names|models|agents|sources|backends|list|skipped)\}\}/;

/**
 * A list placeholder used as the subject of a verb that only agrees with ONE thing.
 * Auxiliaries and copulas only: a general third-person `-s` rule would also match
 * plural nouns, and a check that cries wolf gets deleted.
 */
const SINGULAR_VERB =
  /\{\{(?:names|models|agents|sources|backends|list|skipped)\}\}\s+(?:has|is|was|does|hasn't|isn't|wasn't|doesn't)\b/i;

const COUNT = /\{\{count\}\}/;
const PARENTHESIZED_PLURAL = /\b[a-z]+\(s\)/i;
const PLURAL_SUFFIX = /_(one|other)$/;

type Leaf = { key: string; text: string };

const leaves = (node: unknown, path: string[] = []): Leaf[] =>
  typeof node === 'string'
    ? [{ key: path.join('.'), text: node }]
    : node && typeof node === 'object'
      ? Object.entries(node as Record<string, unknown>).flatMap(([k, v]) => leaves(v, [...path, k]))
      : [];

describe('English copy agrees with the number of things it interpolates', () => {
  const all = leaves(en);
  const models = leaves(en.settings.models);

  it('walks a non-trivial bundle, so the rules have something to check', () => {
    // Guards the walks themselves: a renamed namespace would otherwise make every
    // assertion below pass over an empty list.
    expect(all.some((leaf) => LIST_PLACEHOLDER.test(leaf.text))).toBe(true);
    expect(models.some((leaf) => COUNT.test(leaf.text))).toBe(true);
    expect(models.some((leaf) => PLURAL_SUFFIX.test(leaf.key))).toBe(true);
  });

  it('never gives a joined list a singular verb, anywhere in the bundle', () => {
    const broken = all.filter((l) => SINGULAR_VERB.test(l.text));
    expect(broken.map((l) => `${l.key}: ${l.text}`)).toEqual([]);
  });

  it.each([['en', en], ['zh', zh]] as const)('gives every Models count a complete plural family in %s', (_lng, bundle) => {
    const localized = leaves(bundle.settings.models);
    const byKey = new Map(localized.map((leaf) => [leaf.key, leaf.text]));
    const bareCounts = localized.filter((leaf) => COUNT.test(leaf.text) && !PLURAL_SUFFIX.test(leaf.key));
    const orphaned = localized.filter((leaf) => {
      const match = leaf.key.match(PLURAL_SUFFIX);
      if (!match) return false;
      const base = leaf.key.replace(PLURAL_SUFFIX, '');
      return !byKey.has(`${base}_one`) || !byKey.has(`${base}_other`);
    });
    expect(bareCounts.map((leaf) => leaf.key)).toEqual([]);
    expect(orphaned.map((leaf) => leaf.key)).toEqual([]);
  });

  it('uses real plural families instead of parenthesized English plurals', () => {
    const broken = models.filter((leaf) => PARENTHESIZED_PLURAL.test(leaf.text));
    expect(broken.map((leaf) => `${leaf.key}: ${leaf.text}`)).toEqual([]);
  });

  it('resolves gateway-adoption vendor interpolation through locale keys', () => {
    const component = readFileSync(new URL('./EnableGatewayDialog.tsx', import.meta.url), 'utf8');
    const vendorKeys = new Set(Object.values(BACKEND_ADOPTION_VENDOR_KEY));
    expect(component).toContain('BACKEND_ADOPTION_VENDOR_KEY[agent.backend]');
    expect(new Set(Object.keys(en.settings.models.adopt.vendor))).toEqual(vendorKeys);
    expect(new Set(Object.keys(zh.settings.models.adopt.vendor))).toEqual(vendorKeys);
    for (const key of vendorKeys) {
      expect(en.settings.models.adopt.vendor[key]).toBeTruthy();
      expect(zh.settings.models.adopt.vendor[key]).toBeTruthy();
    }
  });
});
