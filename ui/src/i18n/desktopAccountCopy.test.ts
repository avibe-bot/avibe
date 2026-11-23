import { existsSync, readFileSync } from 'node:fs';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

import i18n from './index';
import { desktopAccountCopy } from './desktop';

// The Desktop Account page's copy (`desktopAccount.*`, ./desktop) is not in the Web UI's locale
// chunks, which every first screen downloads (scripts/check-first-load-budget.mjs). A Web UI file
// asking for it would draw the raw key, so nothing the Web UI entry can load may name it.
const SRC = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const SPECIFIER = /(?:import|export)\s[^'"]*?from\s*['"]([^'"]+)['"]|import\(\s*['"]([^'"]+)['"]\s*\)|import\s*['"]([^'"]+)['"]/g;

function resolveImport(from: string, specifier: string): string | null {
  const base = specifier.startsWith('@/') ? join(SRC, specifier.slice(2))
    : specifier.startsWith('.') ? resolve(dirname(from), specifier) : null;
  if (!base) return null;
  for (const candidate of [base, `${base}.ts`, `${base}.tsx`, join(base, 'index.ts'), join(base, 'index.tsx')]) {
    if (candidate.startsWith(SRC) && existsSync(candidate) && /\.(tsx?|json)$/.test(candidate)) return candidate;
  }
  return null;
}

/** Every source file the entry can load, statically or on demand. */
function reachable(entry: string): Set<string> {
  const seen = new Set<string>();
  const queue = [entry];
  while (queue.length) {
    const file = queue.pop()!;
    if (seen.has(file)) continue;
    seen.add(file);
    if (file.endsWith('.json')) continue;
    for (const match of readFileSync(file, 'utf8').matchAll(SPECIFIER)) {
      // A type-only import is erased from the build.
      if (/^(?:import|export)\s+type\s/.test(match[0])) continue;
      const next = resolveImport(file, match[1] ?? match[2] ?? match[3]);
      if (next) queue.push(next);
    }
  }
  return seen;
}

describe('the Desktop Account page copy', () => {
  const web = reachable(join(SRC, 'main.tsx'));

  it('is never asked for or loaded by the Web UI', () => {
    expect(web.size).toBeGreaterThan(100);
    const offending = [...web]
      .filter((file) => file.startsWith(join(SRC, 'i18n', 'desktop')) || (!file.endsWith('.json') && readFileSync(file, 'utf8').includes('desktopAccount.')))
      .map((file) => relative(SRC, file));
    expect(offending).toEqual([]);
  });

  it('reaches the account document, which loads ./index', () => {
    expect(reachable(join(SRC, 'desktop', 'mount.tsx')).has(join(SRC, 'i18n', 'desktop', 'en.json'))).toBe(true);
    expect(i18n.t('desktopAccount.accountTitle', { lng: 'en' })).toBe(desktopAccountCopy.en.desktopAccount.accountTitle);
    expect(i18n.t('desktopAccount.accountTitle', { lng: 'zh' })).toBe(desktopAccountCopy.zh.desktopAccount.accountTitle);
  });
});
