// A redline over English grammatical number, wherever copy interpolates a QUANTITY.
//
// Round 7 reported 「{{names}} has no supply」: `formatNameList` joins any number of
// Agents into one placeholder, so two of them render 「agent-a, agent-b has no
// supply」. Chinese needs no agreement, which is why the zh bundle these strings were
// translated from reads correctly and the English silently did not.
//
// The reported string was not the class. The same page also wrote 「{{count}} agents
// have no supply」, which breaks at the MOST common count — one. So the rule below is
// TOTAL rather than a list of known-bad keys; that is the only way it catches copy
// that does not exist yet. Two shapes, two scopes:
//
//   1. a list placeholder in front of a singular verb — checked over the WHOLE
//      bundle, because no string anywhere passes a joined list to a singular verb.
//   2. `{{count}}` in front of a plural noun — checked over `settings.models`, this
//      page's own copy. The wider bundle has a long-standing 「{{count}} items」
//      habit across File Browser, Vaults, Skills and Chat; rewriting forty strings
//      on other surfaces is not this lane's change to make.
//
// Neither shape is fixed with a plural key. This bundle has no `_one`/`_other` keys
// at all, and adding them would mean dead `_one` entries in zh (whose plural rule has
// only `other`) — a trap for the next translator, and a break of the zh/en key
// correspondence that `supplyCopyRedline.test.ts` guards. `attribution.unassigned`
// and `order.groupDisabled` already show the cheaper answer that this file enforces:
// word it so the subject's number stops mattering.
import { describe, expect, it } from 'vitest';

import en from '../../../i18n/en.json';

/** Placeholders a join can fill with more than one item. */
const LIST_PLACEHOLDER = /\{\{(?:names|models|agents|sources|backends)\}\}/;

/**
 * A list placeholder used as the subject of a verb that only agrees with ONE thing.
 * Auxiliaries and copulas only: a general third-person `-s` rule would also match
 * plural nouns, and a check that cries wolf gets deleted.
 */
const SINGULAR_VERB =
  /\{\{(?:names|models|agents|sources|backends)\}\}\s+(?:has|is|was|does|hasn't|isn't|wasn't|doesn't)\b/i;

/** `{{count}}` followed by a noun that has already committed to plural. */
const PLURAL_NOUN = /\{\{count\}\}\s+(?:more\s+)?[a-z]+s\b/i;

/**
 * Leaves allowed to pair `{{count}}` with a plural noun, each with a reason.
 *
 * Keys, not substrings: an allowance is granted to a sentence someone has read.
 */
const CLASSIFIED: Record<string, string> = {
  'agents.modelCount':
    'Inherited from master, not written by this lane. Same habit as the rest of the bundle; a repo-wide copy pass owns it.',
  'addKey.discovered':
    'Inherited from master, not written by this lane. Same habit as the rest of the bundle; a repo-wide copy pass owns it.',
};

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
    expect(all.filter((l) => LIST_PLACEHOLDER.test(l.text)).length).toBeGreaterThan(0);
    expect(models.length).toBeGreaterThan(100);
    expect(models.map((l) => l.key)).toContain('statusPill.interrupted');
  });

  it('never gives a joined list a singular verb, anywhere in the bundle', () => {
    const broken = all.filter((l) => SINGULAR_VERB.test(l.text));
    expect(broken.map((l) => `${l.key}: ${l.text}`)).toEqual([]);
  });

  it('never gives a count a plural noun on the Models page', () => {
    const suspects = models.filter((l) => PLURAL_NOUN.test(l.text));
    const unclassified = suspects.filter((l) => !(l.key in CLASSIFIED));
    expect(unclassified.map((l) => `${l.key}: ${l.text}`)).toEqual([]);
  });
});
