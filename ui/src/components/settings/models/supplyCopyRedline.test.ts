// A redline over the page's COPY, not over one component.
//
// Round 1 fixed two strings that promised the Hub falls back to Direct when supply
// runs out. Round 2 found three more saying the same thing elsewhere, because the
// fix had been applied to the strings that were reported rather than to the class.
// `model_hub.resolve()` returns a Direct launch only when the backend's own mode is
// direct; past a switch to Hub an empty or blocked order raises
// `mapping_target_unavailable` and the turn FAILS. So any copy telling a Hub user
// that Direct will cover them is false, wherever it lives.
//
// The check is deliberately TOTAL rather than a list of known-bad keys: it walks
// every leaf under `settings.models` in both locales, and any string that pairs
// Direct with a fallback verb must be classified below with a reason. An
// unclassified match fails — which is the only way a rule catches copy that does
// not exist yet.
import { describe, expect, it } from 'vitest';

import en from '../../../i18n/en.json';
import zh from '../../../i18n/zh.json';
import { SUPPLY_WARNINGS } from './sufficiency';

const BUNDLES = { zh, en } as const;

const DIRECT = /直连|direct/i;
/** Verbs that turn a mention of Direct into a claim about falling back to it. */
const FALLBACK = /回退|切回|退回|改用|回到|继续用|fall ?back|revert|switch(?:es|ed)? back|stays? on/i;

/**
 * Leaves allowed to pair the two, each because it is NOT the false promise.
 *
 * Keys, not substrings: an allowance is granted to a sentence someone has read,
 * and rewriting that sentence into a promise should re-trip the check.
 */
const CLASSIFIED: Record<string, string> = {
  'order.enabledEmpty': 'States the opposite — 「中枢不会替它回退到直连」 is the correction itself.',
  'migration.nonDestructive':
    'Describes a manual switch the user may make on the Backends page, not something the Hub does on their behalf.',
  'consent.points.2':
    'ToS advice: recommends the user choose Direct mode for subscriptions themselves. The subject is the user, not the Hub, and it is offered before enabling rather than after supply runs out.',
};

type Leaf = { key: string; text: string };

function leaves(bundle: unknown): Leaf[] {
  const out: Leaf[] = [];
  const walk = (node: unknown, path: string[]) => {
    if (typeof node === 'string') {
      out.push({ key: path.join('.'), text: node });
      return;
    }
    if (!node || typeof node !== 'object') return;
    for (const [k, v] of Object.entries(node as Record<string, unknown>)) walk(v, [...path, k]);
  };
  walk((bundle as any).settings?.models, []);
  return out;
}

const lookup = (bundle: unknown, key: string): unknown =>
  key.split('.').reduce<unknown>((node, part) => {
    if (!node || typeof node !== 'object') return undefined;
    return (node as Record<string, unknown>)[part];
  }, (bundle as any).settings?.models);

describe('Direct-fallback redline over settings.models copy', () => {
  it.each(['zh', 'en'] as const)('walks a non-trivial %s subtree', (lng) => {
    // Guards the walk itself: a renamed namespace would otherwise make every
    // assertion below pass over an empty list.
    const all = leaves(BUNDLES[lng]);
    expect(all.length).toBeGreaterThan(100);
    expect(all.map((l) => l.key)).toContain('supply.interrupted');
  });

  it.each(['zh', 'en'] as const)('classifies every Direct-fallback sentence in %s', (lng) => {
    const suspects = leaves(BUNDLES[lng]).filter((l) => DIRECT.test(l.text) && FALLBACK.test(l.text));
    const unclassified = suspects.filter((l) => !(l.key in CLASSIFIED));
    expect(unclassified.map((l) => `${l.key}: ${l.text}`)).toEqual([]);
  });

  // The family that replaced the false promise. Every non-ok outcome of a switch to
  // Hub owns exactly one string here, and none of them may mention Direct at all —
  // there is nothing true to say about Direct on any of these paths.
  //
  // Imported, never re-listed: a hand-kept copy of this list would have gone stale
  // the moment `nothingRunnable` joined it, and a coverage rule that misses the newest
  // member is exactly the drift it was written to catch.
  it.each(['zh', 'en'] as const)('gives every non-ok outcome one %s string that never names Direct', (lng) => {
    for (const outcome of SUPPLY_WARNINGS) {
      const text = lookup(BUNDLES[lng], `supply.${outcome}`);
      expect(typeof text, `supply.${outcome} missing in ${lng}`).toBe('string');
      expect(DIRECT.test(text as string), `supply.${outcome} names Direct in ${lng}`).toBe(false);
    }
  });

  // The three strings the class was fixed by DELETING. Re-adding any of them means
  // some surface has gone back to keeping its own paraphrase.
  it.each(['zh', 'en'] as const)('keeps the retired %s keys retired', (lng) => {
    for (const key of ['toast.connectedNoSupply', 'supplyMode.switchedHubNoSupply', 'supplyMode.hubNoSupply']) {
      expect(lookup(BUNDLES[lng], key), `${key} is back in ${lng}`).toBeUndefined();
    }
  });

  it('keeps zh and en in exact key correspondence under settings.models', () => {
    expect(leaves(zh).map((l) => l.key).sort()).toEqual(leaves(en).map((l) => l.key).sort());
  });
});
