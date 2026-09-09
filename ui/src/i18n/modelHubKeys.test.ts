// The Model Hub draws its copy through `t('settings.models…')` literals, and a
// key that never reached the bundles renders as the raw key: removing a provider
// showed `settings.models.sourceDetail.gone` where a sentence belonged.
//
// So the guard states the property — every literal key those components ask for
// has copy in BOTH locales — and EXTRACTS the key list from the components
// themselves. A key or a whole new component added later is covered without
// editing this file, which a hand-written list could never promise.
//
// Resolution runs through a real i18next instance instead of walking the JSON,
// because the bundles use two shapes a plain path walk reports as missing and
// the page depends on: plural families (`usageSummary_one` with no plain key)
// and flat dotted keys (`"vendor.hint"` sitting inside `field`, found by
// `ignoreJSONStructure`). The audit that opened this lane walked paths, and
// called 12 keys missing that the runtime resolves.
//
// A key alone does not say which copy it needs, though, which is why the sweep
// reads the syntax tree rather than the bytes: `count` is what selects a plural
// family, and each plural CATEGORY of the locale is a separate string. So a call
// site's arguments are part of what is collected, and every piece of copy the
// call can reach has to be present — never merely one of them. Requiring only
// one let a half-translated family pass: a locale that keeps `_one` and loses
// `_other` renders the raw key the moment a real call passes 2, and a call
// passing no count renders the raw key against a family that has no plain key.
import { readdirSync, readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { createInstance, type i18n as I18n } from 'i18next';
import * as ts from 'typescript';
import { describe, expect, it } from 'vitest';

import en from './en.json';
import zh from './zh.json';

const MODEL_HUB = resolve(dirname(fileURLToPath(import.meta.url)), '../components/settings/models');

/** A `t()` call site: the key it names, and whether it hands i18next a `count`. */
type Reference = { key: string; counted: boolean };

/** How a reference reads in a failure report: the key, and how it was called. */
const label = (reference: Reference): string =>
  (reference.counted ? `${reference.key} (count)` : reference.key);

const passesCount = (options: ts.Expression | undefined): boolean =>
  options !== undefined
  && ts.isObjectLiteralExpression(options)
  && options.properties.some((property) => {
    const name = property.name;
    return name !== undefined
      && (ts.isIdentifier(name) || ts.isStringLiteralLike(name))
      && name.text === 'count';
  });

/**
 * Every key a source file names as a literal first argument to `t()`, with the
 * argument shape that decides which copy that key needs.
 *
 * Read from the syntax tree, so the callee itself is the whole test of what a
 * translation call is: the page's own `jsonInit('POST')` and a `request.t(…)`
 * member call are not one, and a key built from a variable or an interpolated
 * template is invisible here by construction. The ones a frozen contract
 * declares are covered by the models page's own `contractLocaleKeys.test.ts`.
 *
 * `counted` is what the call can be SEEN to pass — a `count` property, written
 * long or shorthand. An options object that hides one behind a spread reads as
 * uncounted and is probed without one, which against a plural-only key fails
 * this guard rather than passing it: the residual risk stays a loud failure
 * someone looks at, never a raw key shipped past a green CI.
 */
export const collectReferences = (source: string, fileName = 'fixture.tsx'): Reference[] => {
  const file = ts.createSourceFile(
    fileName,
    source,
    ts.ScriptTarget.Latest,
    true,
    fileName.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
  );
  const found = new Map<string, Reference>();
  const visit = (node: ts.Node) => {
    if (ts.isCallExpression(node) && ts.isIdentifier(node.expression) && node.expression.text === 't') {
      const [key, options] = node.arguments;
      if (key && ts.isStringLiteralLike(key)) {
        // Keyed by the shape too, because one key named by a counted and by an
        // uncounted call site needs both kinds of copy to be there.
        const reference: Reference = { key: key.text, counted: passesCount(options) };
        found.set(label(reference), reference);
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(file);
  return [...found.values()].sort((a, b) => label(a).localeCompare(label(b)));
};

const sourceFiles = (dir: string): string[] =>
  readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) return sourceFiles(path);
    if (!/\.tsx?$/.test(entry.name) || /\.test\.tsx?$/.test(entry.name)) return [];
    return [path];
  });

const referencedCallSites = (): Reference[] => {
  const found = new Map<string, Reference>();
  for (const file of sourceFiles(MODEL_HUB)) {
    for (const reference of collectReferences(readFileSync(file, 'utf8'), file)) {
      found.set(label(reference), reference);
    }
  }
  return [...found.values()].sort((a, b) => label(a).localeCompare(label(b)));
};

/** One locale, resolved with no fallback: a zh gap must not be filled by en. */
const localeInstance = (lng: 'en' | 'zh'): I18n => {
  const instance = createInstance();
  void instance.init({
    lng,
    fallbackLng: false,
    resources: { [lng]: { translation: lng === 'en' ? en : zh } },
  });
  return instance;
};

/**
 * One count per plural category the locale can select, taken from
 * `Intl.PluralRules` — the same source i18next resolves its `_one` / `_other`
 * suffixes from. So en asks for both halves of a family, and zh, which selects
 * only `other`, asks for the half it can actually reach; a locale added later
 * brings its own categories instead of a number this file guessed. A category
 * no count in range selects would drop out of the requirement silently, so it
 * throws instead.
 */
const representativeCounts = (lng: string): number[] => {
  const rules = new Intl.PluralRules(lng);
  const categories = rules.resolvedOptions().pluralCategories;
  const reached = new Map<string, number>();
  for (let count = 0; count <= 200 && reached.size < categories.length; count += 1) {
    const category = rules.select(count);
    if (!reached.has(category)) reached.set(category, count);
  }
  const unreached = categories.filter((category) => !reached.has(category));
  if (unreached.length > 0) throw new Error(`no probe count selects ${lng} plural ${unreached.join(', ')}`);
  return [...reached.values()];
};

/** Every options object the call site can reach, and so must have copy for. */
const probes = (lng: string, reference: Reference): Record<string, unknown>[] =>
  (reference.counted ? representativeCounts(lng).map((count) => ({ count })) : [{}]);

/** The symptom this guard exists for, stated exactly: no copy renders as the key. */
const missingCopy = (instance: I18n, lng: string, reference: Reference): boolean =>
  probes(lng, reference).some((options) => {
    const rendered: unknown = instance.t(reference.key, options);
    return typeof rendered !== 'string' || rendered === '' || rendered === reference.key;
  });

describe('Model Hub i18n key coverage', () => {
  const referenced = referencedCallSites();

  it('collects the literal keys and how they are called, and only those, from a source file', () => {
    // The forward guard on the collector itself: a coverage test whose collector
    // silently stops matching reports coverage it never had. The call shape is
    // part of that now — a sweep that stopped seeing `count` would quietly stop
    // asking for the plural copy, which is the hole this case also closes.
    const fixture = `
      const label = t('settings.models.fixture.literal');
      const quoted = t("settings.models.fixture.double");
      const wrapped = t(
        'settings.models.fixture.wrapped',
        { count: 2 },
      );
      const shorthand = t('settings.models.fixture.shorthand', { count });
      const alongside = t('settings.models.fixture.alongside', { count: total, name: 'x' });
      const interpolated = t('settings.models.fixture.interpolated', { name: 'x' });
      const spread = t('settings.models.fixture.spread', { ...options });
      const nested = t('settings.models.fixture.outer', { hint: t('settings.models.fixture.inner', { count: 3 }) });
      void jsonInit('POST');
      void request.t('settings.models.fixture.member');
      void t(\`settings.models.fixture.\${dynamic}\`);
    `;
    expect(collectReferences(fixture)).toEqual([
      { key: 'settings.models.fixture.alongside', counted: true },
      { key: 'settings.models.fixture.double', counted: false },
      { key: 'settings.models.fixture.inner', counted: true },
      { key: 'settings.models.fixture.interpolated', counted: false },
      { key: 'settings.models.fixture.literal', counted: false },
      { key: 'settings.models.fixture.outer', counted: false },
      { key: 'settings.models.fixture.shorthand', counted: true },
      { key: 'settings.models.fixture.spread', counted: false },
      { key: 'settings.models.fixture.wrapped', counted: true },
    ]);
  });

  it('reads its key list out of the live components', () => {
    // Not a floor on the count, which would only say the scan found something:
    // the scan must have walked the tree, and produced both the key whose
    // absence put a raw `settings.models.sourceDetail.gone` on the remove flow
    // and a counted call site, which needs copy no bare probe asks for.
    expect(sourceFiles(MODEL_HUB).length).toBeGreaterThan(1);
    expect(referenced).toContainEqual({ key: 'settings.models.sourceDetail.gone', counted: false });
    expect(referenced.some((reference) => reference.counted)).toBe(true);
  });

  it.each(['en', 'zh'] as const)('translates every referenced call site in %s', (lng) => {
    const instance = localeInstance(lng);
    expect(referenced.filter((reference) => missingCopy(instance, lng, reference)).map(label)).toEqual([]);
  });
});
