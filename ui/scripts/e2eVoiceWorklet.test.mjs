// Every browser suite whose pages reach the chat composer runs on
// e2e/support/browserTest, which makes the composer's voice worklet load
// nothing. On Linux WebKit that load can freeze the page for good, so a suite
// that misses the stub times out in CI on an element that is already there.
// Two suites had copied the stub by hand; the next one had not. What the base
// does is checked in the browsers (e2e/chat-interactions/browserTest.spec.ts);
// whether a suite runs on it has no symptom a run can be sure to show, since
// the freeze is intermittent, so it is read here: the module each spec imports.
//
// Which suites reach the composer is read from the files, not listed: the
// pages each suite opens (page.goto paths, fixture .html literals; any other
// route is the whole app), then each page's module script and its imports,
// static or dynamic. A suite whose pages cannot be read fails, rather than
// passing unjudged.
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

const UI = fileURLToPath(new URL('..', import.meta.url));
const E2E = path.join(UI, 'e2e');
const SRC = path.join(UI, 'src');
const COMPOSER = path.join(SRC, 'components/workbench/Composer.tsx');
const BASE = path.join(E2E, 'support/browserTest.ts');
/** An import of the base module, by its path. */
const IMPORTS_BASE = /from\s*['"]\.\.\/support\/browserTest(?:\.ts)?['"]/;
const IMPORTS_PLAYWRIGHT = /from\s*['"]@playwright\/test['"]/;

const IMPORT = /(?:import|export)\s[^'"]*?from\s*['"]([^'"]+)['"]|import\s*\(\s*['"]([^'"]+)['"]\s*\)|import\s+['"]([^'"]+)['"]/g;
const EXTENSIONS = ['', '.ts', '.tsx', '.js', '.jsx', '/index.ts', '/index.tsx'];

const read = (file) => fs.readFileSync(file, 'utf8');

function resolveImport(from, specifier) {
  let target;
  if (specifier.startsWith('@/')) target = path.join(SRC, specifier.slice(2));
  else if (specifier.startsWith('.')) target = path.resolve(path.dirname(from), specifier);
  else if (specifier.startsWith('/')) target = path.join(UI, specifier);
  else return null;
  target = target.split('?')[0];
  for (const extension of EXTENSIONS) {
    const file = target + extension;
    if (/\.[jt]sx?$/.test(file) && fs.existsSync(file) && fs.statSync(file).isFile()) return file;
  }
  return null;
}

function reachesComposer(entry) {
  const seen = new Set();
  const stack = [entry];
  while (stack.length) {
    const file = stack.pop();
    if (seen.has(file)) continue;
    seen.add(file);
    if (file === COMPOSER) return true;
    for (const match of read(file).matchAll(IMPORT)) {
      const next = resolveImport(file, match[1] ?? match[2] ?? match[3]);
      if (next) stack.push(next);
    }
  }
  return false;
}

/** The module script a page runs. */
function pageEntry(html) {
  const src = read(html).match(/<script[^>]*type="module"[^>]*src="([^"]+)"/)?.[1];
  if (!src) return null;
  return resolveImport(html, src.startsWith('/') || src.startsWith('.') ? src : `./${src}`);
}

/** The pages a suite opens: its fixtures, and the app itself for any other route. */
function suitePages(directory) {
  const pages = new Set();
  for (const name of fs.readdirSync(directory)) {
    if (!/\.tsx?$/.test(name)) continue;
    const source = read(path.join(directory, name));
    for (const match of source.matchAll(/goto\(\s*[`'"]([^`'"?#$]+)/g)) pages.add(match[1]);
    for (const match of source.matchAll(/[`'"](\/e2e\/[^`'"?#$]+\.html)/g)) pages.add(match[1]);
  }
  return [...pages].map((page) => path.join(UI, page.endsWith('.html') ? page : 'index.html'));
}

const suites = fs.readdirSync(E2E, { withFileTypes: true })
  .filter((entry) => entry.isDirectory() && !entry.name.startsWith('.') && entry.name !== 'support')
  .map((entry) => {
    const directory = path.join(E2E, entry.name);
    const files = fs.readdirSync(directory).filter((name) => /\.tsx?$/.test(name));
    const specs = files.filter((name) => name.endsWith('.spec.ts'));
    const pages = suitePages(directory);
    const entries = pages.filter((html) => fs.existsSync(html)).map(pageEntry).filter(Boolean);
    return { name: entry.name, directory, files, specs, pages, entries, composer: entries.some(reachesComposer) };
  })
  .filter((suite) => suite.specs.length > 0);

const composerSuites = suites.filter((suite) => suite.composer);

describe('browser suites that reach the composer', () => {
  it('are found where the composer is known to be, and nowhere it is not', () => {
    expect(fs.existsSync(COMPOSER)).toBe(true);
    expect(fs.existsSync(BASE)).toBe(true);
    const unread = suites.filter((suite) => suite.entries.length === 0).map((suite) => suite.name);
    expect(unread, `No page found for ${unread.join(', ')}: say where it opens, or this guard cannot judge it.`).toEqual([]);
    const names = composerSuites.map((suite) => suite.name);
    // The chat's own suites, and the whole app.
    for (const known of ['chat-interactions', 'chat-scroll', 'voice-shortcut', 'workbench-general']) expect(names).toContain(known);
    // A fixture that never shows a chat.
    expect(names).not.toContain('project-order');
  });

  it.each(composerSuites.map((suite) => [suite.name, suite]))('%s runs every spec on support/browserTest', (_name, suite) => {
    const offences = suite.specs.flatMap((spec) => {
      const source = read(path.join(suite.directory, spec));
      return [
        ...(IMPORTS_BASE.test(source) ? [] : [`${spec} does not import its test from ../support/browserTest`]),
        ...(IMPORTS_PLAYWRIGHT.test(source) ? [`${spec} imports from '@playwright/test' beside it`] : []),
      ];
    });
    expect(offences, offences.join('\n')).toEqual([]);
  });
});
