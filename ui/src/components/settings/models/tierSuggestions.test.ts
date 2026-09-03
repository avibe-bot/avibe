import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

import { EFFORT_BY_BACKEND, REASONING_EFFORTS } from '@/lib/effortOptions';

import { TIER_SUGGESTIONS } from './tierSuggestions';
import { SOURCE_PROTOCOLS } from './types';

const HERE = dirname(fileURLToPath(import.meta.url));
const BACKEND_MODELS = resolve(HERE, '../../../../..', 'vibe/data/backend_models.json');
const E2E_PROTOCOL_TIERS = resolve(HERE, '../../../../e2e/support/protocol-tiers.json');

/**
 * Tiers the builtin catalog names that the unified vocabulary does not.
 *
 * Empty on purpose: the vocabulary is the ordered SUPERSET of every value a
 * catalog row legitimately declares (`ultra` included, because gpt-5.6-sol/terra
 * name it). A NEW catalog-only tier fails this equality rather than reaching
 * the UI unnamed; a future exemption is an explicit pin here, never a loosened
 * assertion.
 */
const KNOWN_CATALOG_DIVERGENCE: readonly string[] = [];

const vocabulary = REASONING_EFFORTS as readonly string[];
const rank = (effort: string) => vocabulary.indexOf(effort);

const catalogTiers = (): Map<string, string[]> => {
  const catalog = JSON.parse(readFileSync(BACKEND_MODELS, 'utf8'));
  return new Map(
    Object.entries(catalog.backends as Record<string, { models?: { reasoning_efforts?: string[] }[] }>)
      .map(([backend, entry]) => [
        backend,
        (entry.models ?? []).flatMap((model) => model.reasoning_efforts ?? []),
      ]),
  );
};

describe('unified reasoning-effort vocabulary', () => {
  it('is the spec\'s ordered superset, including catalog-only ultra', () => {
    // The UI-side pin of the frozen vocabulary. The backend lane exports the
    // same list; when that export lands, a later contract test holds the two
    // equal. Until then this is what stops the tables below from quietly
    // shrinking the set the spec named.
    expect([...REASONING_EFFORTS]).toEqual([
      'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra',
    ]);
  });

  it('offers one suggestion list per source protocol, drawn from the vocabulary in its order', () => {
    expect(new Set(Object.keys(TIER_SUGGESTIONS))).toEqual(new Set(SOURCE_PROTOCOLS));

    for (const [protocol, tiers] of Object.entries(TIER_SUGGESTIONS)) {
      expect(tiers.length, protocol).toBeGreaterThan(0);
      expect(new Set(tiers).size, protocol).toBe(tiers.length);
      expect(tiers.filter((tier) => vocabulary.includes(tier)), protocol).toEqual([...tiers]);
      expect([...tiers].sort((a, b) => rank(a) - rank(b)), protocol).toEqual([...tiers]);
      // Family defaults are rung-1 over-claim protection, not the catalog
      // superset: an unknown relay must not be handed `ultra`.
      expect(tiers, protocol).not.toContain('ultra');
    }
  });

  it('keeps the agent-backend effort lists inside the same vocabulary and order', () => {
    for (const [backend, efforts] of Object.entries(EFFORT_BY_BACKEND)) {
      expect(efforts.filter((effort) => vocabulary.includes(effort)), backend).toEqual(efforts);
      expect([...efforts].sort((a, b) => rank(a) - rank(b)), backend).toEqual(efforts);
    }
  });

  it('says everything the builtin model catalog says, apart from the pinned divergence', () => {
    const declared = new Set([...catalogTiers().values()].flat());
    const divergent = [...declared].filter((tier) => !vocabulary.includes(tier));

    expect(new Set(divergent)).toEqual(new Set(KNOWN_CATALOG_DIVERGENCE));
  });

  it('suggests for the Anthropic protocol exactly what the claude catalog declares', () => {
    // The claim in tierSuggestions.ts is about THIS family only. The codex rows
    // answer a different question — which levels the Codex CLI accepts, not
    // which the OpenAI wire protocols name — so they are checked against the
    // vocabulary above and not against `TIER_SUGGESTIONS.openai_*`.
    const claude = catalogTiers().get('claude') ?? [];

    expect(claude.length).toBeGreaterThan(0);
    expect(new Set(claude)).toEqual(new Set(TIER_SUGGESTIONS.anthropic));
  });

  it('is the same table the Playwright suite asserts against the live editor', () => {
    // The e2e tsconfig cannot import `@/` aliases, so the suite reads this JSON
    // rather than `TIER_SUGGESTIONS` itself. Equality here is what keeps that
    // copy from silently describing a vocabulary the product no longer offers.
    const e2e = JSON.parse(readFileSync(E2E_PROTOCOL_TIERS, 'utf8')) as Record<string, string[]>;
    expect(e2e).toEqual(TIER_SUGGESTIONS);
  });
});
