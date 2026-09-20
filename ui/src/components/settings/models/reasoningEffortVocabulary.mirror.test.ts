import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

import { REASONING_EFFORTS } from '@/lib/effortOptions';

import en from '../../../i18n/en.json';
import zh from '../../../i18n/zh.json';
import { TIER_SUGGESTIONS } from './tierSuggestions';

const HERE = dirname(fileURLToPath(import.meta.url));
const CATALOG_PY = resolve(HERE, '../../../../..', 'vibe/backend_model_catalog.py');

/**
 * Frozen spec pin (2026-09-03). The backend PR owns `REASONING_EFFORT_VOCABULARY`
 * and `PROTOCOL_REASONING_EFFORT_DEFAULTS`; this file holds the UI tables equal
 * to those exports when the names exist, and to this pin while they do not —
 * never a silent skip.
 */
const FROZEN_VOCABULARY = [
  'minimal',
  'low',
  'medium',
  'high',
  'xhigh',
  'max',
  'ultra',
] as const;

const FROZEN_PROTOCOL_DEFAULTS: Readonly<Record<string, readonly string[]>> = {
  anthropic: ['low', 'medium', 'high', 'xhigh', 'max'],
  openai_responses: ['minimal', 'low', 'medium', 'high', 'xhigh'],
  openai_chat: ['minimal', 'low', 'medium', 'high', 'xhigh'],
};

const quotedStrings = (block: string): string[] =>
  [...block.matchAll(/['"]([^'"]+)['"]/g)].map((match) => match[1]);

const parseNamedTuple = (source: string, name: string): string[] | null => {
  const match = source.match(new RegExp(`${name}\\s*(?::[^=]+)?=\\s*\\(([^)]*)\\)`));
  return match ? quotedStrings(match[1]) : null;
};

const parseNamedStringTupleDict = (source: string, name: string): Record<string, string[]> | null => {
  const match = source.match(new RegExp(`${name}\\s*(?::[^=]+)?=\\s*\\{([\\s\\S]*?)\\n\\}`));
  if (!match) return null;
  const entries: Record<string, string[]> = {};
  for (const entry of match[1].matchAll(/['"]([^'"]+)['"]\s*:\s*\(([^)]*)\)/g)) {
    entries[entry[1]] = quotedStrings(entry[2]);
  }
  return Object.keys(entries).length > 0 ? entries : null;
};

const catalogSource = readFileSync(CATALOG_PY, 'utf8');
const namesVocabulary = /\bREASONING_EFFORT_VOCABULARY\b/.test(catalogSource);
const namesDefaults = /\bPROTOCOL_REASONING_EFFORT_DEFAULTS\b/.test(catalogSource);
const exportedVocabulary = parseNamedTuple(catalogSource, 'REASONING_EFFORT_VOCABULARY');
const exportedDefaults = parseNamedStringTupleDict(catalogSource, 'PROTOCOL_REASONING_EFFORT_DEFAULTS');

describe('reasoning-effort vocabulary UI ↔ backend mirror', () => {
  it('parses the backend export when the name is on the branch, rather than falling through to the frozen pin', () => {
    // A comment or a renamed binding that the regex cannot read must fail here.
    // Falling through to the frozen list would keep CI green while the tables drift.
    if (namesVocabulary) expect(exportedVocabulary).not.toBeNull();
    if (namesDefaults) expect(exportedDefaults).not.toBeNull();
    expect(namesVocabulary).toBe(namesDefaults);
  });

  it('holds REASONING_EFFORTS equal to the backend export, or the frozen 7-list while that name is absent', () => {
    expect([...REASONING_EFFORTS]).toEqual(exportedVocabulary ?? [...FROZEN_VOCABULARY]);
  });

  it('holds TIER_SUGGESTIONS equal to PROTOCOL_REASONING_EFFORT_DEFAULTS, or the frozen family lists while that name is absent', () => {
    expect(TIER_SUGGESTIONS).toEqual(exportedDefaults ?? FROZEN_PROTOCOL_DEFAULTS);
  });

  it.each(['en', 'zh'] as const)('gives %s picker copy for every vocabulary value, in vocabulary order', (lng) => {
    const options = (lng === 'en' ? en : zh).chat.picker.effortOptions;
    expect(Object.keys(options)).toEqual([...REASONING_EFFORTS]);
    for (const [key, value] of Object.entries(options)) {
      expect(value.trim().length, `${lng}.${key}`).toBeGreaterThan(0);
      expect(value, `${lng}.${key}`).not.toBe(key);
    }
  });
});
