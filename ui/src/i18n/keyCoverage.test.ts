// Every piece of user-visible copy is drawn through a `t('…')` literal, and a key
// that never reached the bundles renders as the raw key: removing a provider
// showed `settings.models.sourceDetail.gone` where a sentence belonged.
//
// So the guard states the property — every literal key the app asks for has copy
// in BOTH locales — and EXTRACTS the key list from the components themselves. A
// key, or a whole new surface added later, is covered without editing this file,
// which a hand-written list could never promise.
//
// The root is `src/`, so the property belongs to the app rather than to one
// directory. It was scoped to the Model Hub for exactly as long as it had to be:
// eight keys on three other surfaces had copy in neither locale, four of them
// rendering raw keys, and the root widened in the change that wrote them.
//
// Resolution runs through a real i18next instance instead of walking the JSON,
// because the bundles use two shapes a plain path walk reports as missing and
// the app depends on: plural families (`usageSummary_one` with no plain key)
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
//
// Reading the tree is also what lets the sweep follow the app's second way of
// naming a key: an `i18nKey`, which `<Trans>` takes as a prop when the copy wraps
// a link, and which a component's own table carries to hand to `t()` later. Those
// are the same literal keys with the same copy requirement, and a sweep that knew
// only `t('…')` stayed green while `remoteAccess.flowStep1` was deletable.
import { readdirSync, readFileSync } from 'node:fs';
import { dirname, join, relative, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

import { createInstance, type i18n as I18n } from 'i18next';
import * as ts from 'typescript';
import { describe, expect, it } from 'vitest';

import en from './en.json';
import zh from './zh.json';

const APP_SOURCE = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const MODEL_HUB = join('components', 'settings', 'models');

/** A call site: the key it names, and whether it hands i18next a `count`. */
type Reference = { key: string; counted: boolean };

/** How a reference reads in a failure report: the key, and how it was called. */
const label = (reference: Reference): string =>
  (reference.counted ? `${reference.key} (count)` : reference.key);

/** The written name of an object-literal property or a JSX attribute. */
const nameOf = (node: ts.Node | undefined): string | undefined =>
  (node !== undefined && (ts.isIdentifier(node) || ts.isStringLiteralLike(node)) ? node.text : undefined);

const passesCount = (options: ts.Expression | undefined): boolean =>
  options !== undefined
  && ts.isObjectLiteralExpression(options)
  && options.properties.some((property) => nameOf(property.name) === 'count');

/** `t('literal', …)`, whose options say whether the call selects a plural. */
const translationCall = (node: ts.Node): Reference | undefined => {
  if (!ts.isCallExpression(node) || !ts.isIdentifier(node.expression) || node.expression.text !== 't') {
    return undefined;
  }
  const [key, options] = node.arguments;
  return key && ts.isStringLiteralLike(key) ? { key: key.text, counted: passesCount(options) } : undefined;
};

/** A JSX attribute's value when it is written as a literal, quoted or braced. */
const attributeLiteral = (value: ts.JsxAttributeValue | undefined): string | undefined => {
  if (value === undefined) return undefined;
  if (ts.isStringLiteralLike(value)) return value.text;
  if (ts.isJsxExpression(value) && value.expression && ts.isStringLiteralLike(value.expression)) {
    return value.expression.text;
  }
  return undefined;
};

/**
 * An `i18nKey` naming a literal key, in either place the app writes one: the
 * prop `<Trans>` takes, and a property in a component's own table of keys.
 *
 * `<Trans count={…}>` selects a plural exactly as `t()` does, so a sibling
 * `count` attribute counts. A table property has no call of its own to read —
 * the `t(row.i18nKey)` that consumes it is a dynamic key — so it is probed
 * uncounted, which against a plural-only family fails rather than passes.
 *
 * A `PropertySignature` in a type (`i18nKey: string`) has no literal value and
 * so is not one of these, and an `i18nKey` given a variable or a call result is
 * dynamic like any other computed key.
 */
const i18nKeyReference = (node: ts.Node): Reference | undefined => {
  if (ts.isJsxAttribute(node) && nameOf(node.name) === 'i18nKey') {
    const key = attributeLiteral(node.initializer);
    if (key === undefined) return undefined;
    const counted = ts.isJsxAttributes(node.parent)
      && node.parent.properties.some((property) => ts.isJsxAttribute(property) && nameOf(property.name) === 'count');
    return { key, counted };
  }
  if (ts.isPropertyAssignment(node) && nameOf(node.name) === 'i18nKey' && ts.isStringLiteralLike(node.initializer)) {
    return { key: node.initializer.text, counted: false };
  }
  return undefined;
};

/**
 * Every key a source file names as a literal — as the first argument to `t()`,
 * or as an `i18nKey` — with the argument shape that decides which copy that key
 * needs.
 *
 * Read from the syntax tree, so the callee itself is the whole test of what a
 * translation call is: a page's own `jsonInit('POST')` and a `request.t(…)`
 * member call are not one, and a key built from a variable or an interpolated
 * template is invisible here by construction — that dynamic form is exactly
 * where the app's `defaultValue` fallbacks live. The keys a frozen contract
 * declares are covered by the models page's own `contractLocaleKeys.test.ts`.
 *
 * `counted` is what the call can be SEEN to pass — a `count` property, written
 * long or shorthand. An options object that hides one behind a spread reads as
 * uncounted and is probed without one, which against a plural-only key fails
 * this guard rather than passing it. A bare `t(…)` that is not i18next at all
 * would likewise be asked for copy it does not need. Both residuals stay a loud
 * failure someone looks at, never a raw key shipped past a green CI.
 *
 * A key's namespace is not part of this: every `useTranslation()` in the app
 * takes the default one, so a key resolves against `translation` exactly as the
 * probes below resolve it.
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
    const reference = translationCall(node) ?? i18nKeyReference(node);
    // Keyed by the shape too, because one key named by a counted and by an
    // uncounted call site needs both kinds of copy to be there.
    if (reference) found.set(label(reference), reference);
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
  for (const file of sourceFiles(APP_SOURCE)) {
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

describe('app i18n key coverage', () => {
  const referenced = referencedCallSites();

  it('collects the literal keys and how they are called, and only those, from a source file', () => {
    // The forward guard on the collector itself: a coverage test whose collector
    // silently stops matching reports coverage it never had. The call shape is
    // part of that now — a sweep that stopped seeing `count` would quietly stop
    // asking for the plural copy, which is the hole this case also closes — and
    // so is `i18nKey`, in both places the app writes one.
    const fixture = `
      const label = t('fixture.literal');
      const quoted = t("fixture.double");
      const wrapped = t(
        'fixture.wrapped',
        { count: 2 },
      );
      const shorthand = t('fixture.shorthand', { count });
      const alongside = t('fixture.alongside', { count: total, name: 'x' });
      const interpolated = t('fixture.interpolated', { name: 'x' });
      const spread = t('fixture.spread', { ...options });
      const nested = t('fixture.outer', { hint: t('fixture.inner', { count: 3 }) });
      const wrapping = <Trans i18nKey="fixture.trans" components={{ link: <a /> }} />;
      const braced = <Trans i18nKey={'fixture.braced'} />;
      const plural = <Trans i18nKey="fixture.transCount" count={total} />;
      const rows = [{ id: 'x', i18nKey: 'fixture.table' }];
      type Row = { i18nKey: string };
      const computed = <Trans i18nKey={dynamic} />;
      const carried = { i18nKey: pickKey(row) };
      void jsonInit('POST');
      void request.t('fixture.member');
      void t(\`fixture.\${dynamic}\`);
    `;
    expect(collectReferences(fixture)).toEqual([
      { key: 'fixture.alongside', counted: true },
      { key: 'fixture.braced', counted: false },
      { key: 'fixture.double', counted: false },
      { key: 'fixture.inner', counted: true },
      { key: 'fixture.interpolated', counted: false },
      { key: 'fixture.literal', counted: false },
      { key: 'fixture.outer', counted: false },
      { key: 'fixture.shorthand', counted: true },
      { key: 'fixture.spread', counted: false },
      { key: 'fixture.table', counted: false },
      { key: 'fixture.trans', counted: false },
      { key: 'fixture.transCount', counted: true },
      { key: 'fixture.wrapped', counted: true },
    ]);
  });

  it('reads its key list out of the whole app, not one directory', () => {
    // Not a floor on the count, which would only say the scan found something.
    // The scan must have walked past the directory this guard started in — the
    // narrower root passed while eight keys on other surfaces had no copy — and
    // produced each kind of reference the app's own code contains: the key whose
    // absence put a raw `settings.models.sourceDetail.gone` on the remove flow,
    // the `<Trans>` key that carries the Avibe Cloud link, and a counted call
    // site, which needs copy no bare probe asks for.
    const files = sourceFiles(APP_SOURCE).map((file) => relative(APP_SOURCE, file));
    expect(files.some((file) => file.startsWith(`${MODEL_HUB}${sep}`))).toBe(true);
    expect(files.some((file) => !file.startsWith(`${MODEL_HUB}${sep}`))).toBe(true);
    expect(referenced).toContainEqual({ key: 'settings.models.sourceDetail.gone', counted: false });
    expect(referenced).toContainEqual({ key: 'remoteAccess.flowStep1', counted: false });
    expect(referenced.some((reference) => reference.counted)).toBe(true);
  });

  it.each(['en', 'zh'] as const)('translates every referenced call site in %s', (lng) => {
    const instance = localeInstance(lng);
    expect(referenced.filter((reference) => missingCopy(instance, lng, reference)).map(label)).toEqual([]);
  });
});
