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
// a link, and which a component's own table carries to hand to `t()` later.
//
// Neither site reliably hands over a bare literal, though, so what is taken from
// one is the SET of values it can be statically KNOWN to name: both branches of a
// ternary, either side of a `??` fallback, through any number of wrappers that
// preserve the value, with every dynamic leaf staying invisible by design.
// Recognising shapes one at a time instead is what left `remoteAccess.flowStep1`
// and the 94 keys named through a conditional or a fallback deletable while this
// guard was green.
//
// That is total over statically-known EXPRESSIONS, and it is only half of what
// this file needs, because it says nothing about the POSITIONS a key is written
// in. Calling the class closed on the expression axis alone is what let the next
// round land: `steps/TelegramConfig.tsx` hands `shared/ProxyUrlField` a literal
// `labelKey`, `lib/agentGraph.ts` keeps a whole table of them, and 75 keys reach
// `t()` only that way, through eighteen differently-named props. Adding those
// names here would be the same enumeration a fourth time.
//
// So this file states TWO properties, and the second needs no positions at all.
//
//   EXISTENCE — every key named literally at a `t()` or an `i18nKey` position has
//   copy in both locales. Position-bound of necessity: to demand that a key
//   exist, something has to know it is a key.
//
//   PARITY — every dotted string literal anywhere in `src/` that resolves in
//   EITHER locale resolves in BOTH. No positions, no prop names, no dataflow: a
//   literal becomes a candidate because of how it READS, and the bundles decide
//   whether it is a key. `labelKey="telegramConfig.proxyUrl"` is covered not
//   because this file learned the name `labelKey` but because that string is in
//   the bundles — so a prop, a data table, or a carrier nobody has invented yet
//   is covered without editing this file. "Resolves" is i18next's own answer for
//   that locale, bare or against any plural category the locale selects, so a
//   family kept in one locale and dropped from the other is a gap here rather
//   than a stem that quietly falls out.
//
// What PARITY cannot do is find a key missing from BOTH locales: a literal absent
// from both is indistinguishable from a Monaco theme token, an event-channel
// name, or a storage key, and 62 of those sit in `src/` today. That case is
// exactly what EXISTENCE covers, which is why both properties are here and
// neither replaces the other.
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

/** The operators that pick one of their operands, so both can name a key. */
const CHOICE_OPERATORS = new Set<ts.SyntaxKind>([
  ts.SyntaxKind.AmpersandAmpersandToken,
  ts.SyntaxKind.BarBarToken,
  ts.SyntaxKind.QuestionQuestionToken,
]);

/**
 * Every key an expression can be statically KNOWN to name — the one thing this
 * file asks of a key expression, and the whole of its boundary.
 *
 * A key argument is often not a bare literal. It is a ternary picking a title
 * (`t(adding ? '…addTitle' : '…editTitle')`), a `??` naming the fallback
 * (`t(item?.labelKey ?? 'nav.settings')`), or either wrapped in something that
 * does not change the value. So an expression yields a SET: a choice
 * contributes every branch, a wrapper is transparent, and a literal is itself.
 *
 * Every other leaf yields nothing and stays invisible by design — a variable, a
 * property, a call result, an index into a lookup table, or a template with a
 * substitution. Those are the app's dynamic keys, which is also where its
 * `defaultValue` fallbacks live; this guard cannot know what they resolve to and
 * does not guess. A choice with one dynamic branch still protects the branch it
 * can read, rather than giving up on the whole call.
 *
 * Being total over the static forms rather than a list of recognised ones is the
 * point: a shape nobody enumerated was a key this guard silently stopped
 * protecting, and shapes were exactly what kept getting missed.
 */
const staticKeys = (node: ts.Expression | undefined): string[] => {
  if (node === undefined) return [];
  if (ts.isStringLiteralLike(node)) return [node.text];
  // Wrappers that preserve the value: `('k')`, `'k' as const`, `'k' satisfies T`,
  // `<T>'k'`, `k!`.
  if (
    ts.isParenthesizedExpression(node)
    || ts.isAsExpression(node)
    || ts.isSatisfiesExpression(node)
    || ts.isTypeAssertionExpression(node)
    || ts.isNonNullExpression(node)
  ) {
    return staticKeys(node.expression);
  }
  if (ts.isConditionalExpression(node)) return [...staticKeys(node.whenTrue), ...staticKeys(node.whenFalse)];
  if (ts.isBinaryExpression(node) && CHOICE_OPERATORS.has(node.operatorToken.kind)) {
    return [...staticKeys(node.left), ...staticKeys(node.right)];
  }
  return [];
};

/** `t(…)`, whose options say whether the call selects a plural. */
const translationCall = (node: ts.Node): Reference[] => {
  if (!ts.isCallExpression(node) || !ts.isIdentifier(node.expression) || node.expression.text !== 't') {
    return [];
  }
  const [key, options] = node.arguments;
  const counted = passesCount(options);
  return staticKeys(key).map((found) => ({ key: found, counted }));
};

/**
 * An `i18nKey` naming a key, in either place the app writes one: the prop
 * `<Trans>` takes, and a property in a component's own table of keys.
 *
 * `<Trans count={…}>` selects a plural exactly as `t()` does, so a sibling
 * `count` attribute counts. A table property has no call of its own to read —
 * the `t(row.i18nKey)` that consumes it is a dynamic key — so it is probed
 * uncounted, which against a plural-only family fails rather than passes.
 *
 * A `PropertySignature` in a type (`i18nKey: string`) declares no value at all
 * and so is not one of these; a value that is there but dynamic yields nothing,
 * exactly as it does at a `t()` call.
 */
const i18nKeyReference = (node: ts.Node): Reference[] => {
  if (ts.isJsxAttribute(node) && nameOf(node.name) === 'i18nKey') {
    const value = node.initializer;
    const counted = ts.isJsxAttributes(node.parent)
      && node.parent.properties.some((property) => ts.isJsxAttribute(property) && nameOf(property.name) === 'count');
    const keys = value !== undefined && ts.isJsxExpression(value) ? staticKeys(value.expression) : staticKeys(value);
    return keys.map((found) => ({ key: found, counted }));
  }
  if (ts.isPropertyAssignment(node) && nameOf(node.name) === 'i18nKey') {
    return staticKeys(node.initializer).map((found) => ({ key: found, counted: false }));
  }
  return [];
};

/**
 * Every key a source file can be statically known to name — at a `t()` call or
 * an `i18nKey` — with the argument shape that decides which copy that key needs.
 *
 * Read from the syntax tree, so the callee itself is the whole test of what a
 * translation call is: a page's own `jsonInit('POST')` and a `request.t(…)`
 * member call are not one. What each site's key expression yields is
 * `staticKeys`, and nothing here widens or narrows that. The keys a frozen
 * contract declares are covered by the models page's own
 * `contractLocaleKeys.test.ts`.
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
    // Keyed by the shape too, because one key named by a counted and by an
    // uncounted call site needs both kinds of copy to be there.
    for (const reference of [...translationCall(node), ...i18nKeyReference(node)]) {
      found.set(label(reference), reference);
    }
    ts.forEachChild(node, visit);
  };
  visit(file);
  return [...found.values()].sort((a, b) => label(a).localeCompare(label(b)));
};

/** A string that READS like a key: dotted segments, nothing else assumed. */
const DOTTED = /^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+$/;

/**
 * Every dotted string literal a source file writes, wherever it sits — the
 * PARITY property's whole input, and the reason that property needs no positions.
 *
 * This asks nothing about how the literal is used. It is not an argument to
 * anything, not an attribute of anything; it is a string in the file that reads
 * like a key. So a literal handed over as `labelKey`, parked in a lookup table,
 * held in a `const`, or written into a carrier that does not exist yet is picked
 * up identically, and no name has to be enumerated for any of them.
 *
 * A literal that is not a key comes along too — a Monaco theme token, an
 * event-channel name, a storage key. That is deliberate: the bundles decide.
 * Absent from both locales, it is not a key and PARITY has nothing to say about
 * it; present in one, it is a key one locale lost.
 *
 * Read from the syntax tree rather than the bytes, so a dotted string inside a
 * comment or a longer sentence is not mistaken for a literal the app writes.
 */
export const collectDottedLiterals = (source: string, fileName = 'fixture.tsx'): string[] => {
  const file = ts.createSourceFile(
    fileName,
    source,
    ts.ScriptTarget.Latest,
    true,
    fileName.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
  );
  const found = new Set<string>();
  const visit = (node: ts.Node) => {
    if (ts.isStringLiteralLike(node) && DOTTED.test(node.text)) found.add(node.text);
    ts.forEachChild(node, visit);
  };
  visit(file);
  return [...found].sort((a, b) => a.localeCompare(b));
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

/** The app's locales, and the bundle each one resolves against. */
const BUNDLES: { lng: string; bundle: unknown }[] = [
  { lng: 'en', bundle: en },
  { lng: 'zh', bundle: zh },
];

/** One locale, resolved with no fallback: a zh gap must not be filled by en. */
const localeInstance = (lng: string, bundle: unknown): I18n => {
  const instance = createInstance();
  void instance.init({
    lng,
    fallbackLng: false,
    resources: { [lng]: { translation: bundle as Record<string, unknown> } },
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
const countsByLocale = new Map<string, number[]>();
const representativeCounts = (lng: string): number[] => {
  const memoised = countsByLocale.get(lng);
  if (memoised !== undefined) return memoised;
  const computed = computeRepresentativeCounts(lng);
  countsByLocale.set(lng, computed);
  return computed;
};

const computeRepresentativeCounts = (lng: string): number[] => {
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

/**
 * Whether a locale resolves a literal at all — i18next's own answer, bare or
 * against any plural category the locale selects.
 *
 * The plural half matters: a family with no plain key (`usageSummary_one` and
 * `_other`, no `usageSummary`) does not exist bare, so asking only that way
 * would drop every family out of PARITY and miss the locale that kept the family
 * while the other lost it.
 */
const resolvesIn = (instance: I18n, lng: string, key: string): boolean =>
  instance.exists(key) || representativeCounts(lng).some((count) => instance.exists(key, { count }));

/** A literal one locale resolves and another does not: a key someone lost. */
type ParityGap = { key: string; present: string[]; absent: string[] };

/**
 * PARITY over whatever literals it is handed, against whatever bundles it is
 * handed — so the fixture below can pin its boundaries on locales it controls.
 *
 * A literal no locale resolves is not a key and yields nothing. One every locale
 * resolves is fine. Anything in between is the gap this reports.
 */
export const parityGaps = (
  literals: string[],
  bundles: { lng: string; bundle: unknown }[],
): ParityGap[] => {
  const locales = bundles.map(({ lng, bundle }) => ({ lng, instance: localeInstance(lng, bundle) }));
  return literals.flatMap((key) => {
    const present = locales.filter(({ lng, instance }) => resolvesIn(instance, lng, key)).map(({ lng }) => lng);
    if (present.length === 0 || present.length === locales.length) return [];
    return [{ key, present, absent: locales.map(({ lng }) => lng).filter((lng) => !present.includes(lng)) }];
  });
};

/**
 * Whether a bundle VALUE carries something to render — the whole of what
 * EXISTENCE asks, and asked of the value rather than of a `t()` projection.
 *
 * Going through the projection is what made this wrong twice over. It needed the
 * call's position to find the key, and it needed the call's OPTIONS to see the
 * value: `memory/MemorySettingsPanel.tsx` reads two keys with `returnObjects`
 * and maps them as arrays, and a probe without that option gets i18next's
 * "returned an object instead of string" diagnostic — a nonempty string, which
 * passed, while the surface rendered no rows. Asking the value directly needs
 * neither, so no call shape has to be replicated here.
 *
 * A collection has to be nonempty AND have something renderable in every slot,
 * because a locale that keeps the array and empties an entry renders a blank row
 * rather than a raw key, and a blank row is still missing copy.
 */
const isRenderable = (value: unknown, key: string): boolean => {
  if (typeof value === 'string') return value !== '' && value !== key;
  if (Array.isArray(value)) return value.length > 0 && value.every((entry) => isRenderable(entry, key));
  if (typeof value === 'object' && value !== null) {
    const entries = Object.values(value);
    return entries.length > 0 && entries.every((entry) => isRenderable(entry, key));
  }
  return false;
};

/** The symptom this guard exists for, stated exactly: no copy renders as the key. */
const missingCopy = (instance: I18n, lng: string, reference: Reference): boolean =>
  probes(lng, reference).some(
    (options) => !isRenderable(instance.t(reference.key, { ...options, returnObjects: true }), reference.key),
  );

describe('app i18n key coverage', () => {
  const referenced = referencedCallSites();

  it('collects the literal keys and how they are called, and only those, from a source file', () => {
    // The forward guard on the collector itself: a coverage test whose collector
    // silently stops matching reports coverage it never had. So this pins the
    // whole boundary rather than a sample of it — every static form the collector
    // must see through, every dynamic leaf it must decline, the two places a key
    // is named, and `count`, whose loss would quietly stop asking for plural
    // copy. A form added to `staticKeys` later fails here until it is stated.
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

      // Choices: every branch can be the key that renders.
      const ternary = t(adding ? 'fixture.yes' : 'fixture.no');
      const chained = t(a ? 'fixture.a' : b ? 'fixture.b' : 'fixture.c');
      const fallback = t(item?.labelKey ?? 'fixture.fallback');
      const orElse = t(override || 'fixture.orElse');
      const andThen = t(ready && 'fixture.andThen');
      const eitherPlural = t(many ? 'fixture.oneWay' : 'fixture.otherWay', { count: total });

      // Wrappers that preserve the value.
      const parenthesised = t(('fixture.paren'));
      const asserted = t('fixture.asserted' as const);
      const insisted = t('fixture.bang'!);

      // A choice with one dynamic branch still protects the branch it can read.
      const partly = t(known ? 'fixture.knownBranch' : row.key);

      // Both places a key is named carry a choice too.
      const wrapping = <Trans i18nKey="fixture.trans" components={{ link: <a /> }} />;
      const braced = <Trans i18nKey={'fixture.braced'} />;
      const plural = <Trans i18nKey="fixture.transCount" count={total} />;
      const transChoice = <Trans i18nKey={outgoing ? 'fixture.transYes' : 'fixture.transNo'} />;
      const rows = [{ id: 'x', i18nKey: 'fixture.table' }];
      const tableChoice = [{ i18nKey: outgoing ? 'fixture.tableYes' : 'fixture.tableNo' }];

      // Dynamic leaves: nothing statically known, so nothing collected.
      type Row = { i18nKey: string };
      const computed = <Trans i18nKey={dynamic} />;
      const carried = { i18nKey: pickKey(row) };
      const looked = t(LABEL_KEY[kind]);
      const held = t(messageKey);
      const reached = t(def.titleKey);
      const eitherDynamic = t(row.key ?? LABEL_KEY[kind]);
      void jsonInit('POST');
      void request.t('fixture.member');
      void t(\`fixture.\${dynamic}\`);
    `;
    expect(collectReferences(fixture)).toEqual([
      { key: 'fixture.a', counted: false },
      { key: 'fixture.alongside', counted: true },
      { key: 'fixture.andThen', counted: false },
      { key: 'fixture.asserted', counted: false },
      { key: 'fixture.b', counted: false },
      { key: 'fixture.bang', counted: false },
      { key: 'fixture.braced', counted: false },
      { key: 'fixture.c', counted: false },
      { key: 'fixture.double', counted: false },
      { key: 'fixture.fallback', counted: false },
      { key: 'fixture.inner', counted: true },
      { key: 'fixture.interpolated', counted: false },
      { key: 'fixture.knownBranch', counted: false },
      { key: 'fixture.literal', counted: false },
      { key: 'fixture.no', counted: false },
      { key: 'fixture.oneWay', counted: true },
      { key: 'fixture.orElse', counted: false },
      { key: 'fixture.otherWay', counted: true },
      { key: 'fixture.outer', counted: false },
      { key: 'fixture.paren', counted: false },
      { key: 'fixture.shorthand', counted: true },
      { key: 'fixture.spread', counted: false },
      { key: 'fixture.table', counted: false },
      { key: 'fixture.tableNo', counted: false },
      { key: 'fixture.tableYes', counted: false },
      { key: 'fixture.trans', counted: false },
      { key: 'fixture.transCount', counted: true },
      { key: 'fixture.transNo', counted: false },
      { key: 'fixture.transYes', counted: false },
      { key: 'fixture.wrapped', counted: true },
      { key: 'fixture.yes', counted: false },
    ]);
  });

  it('reads its key list out of the whole app, not one directory', () => {
    // Not a floor on the count, which would only say the scan found something.
    // The scan must have walked past the directory this guard started in — the
    // narrower root passed while eight keys on other surfaces had no copy — and
    // it must reach every way the app's own code names a key, each anchored on a
    // real site that was demonstrably escaping before it did.
    const files = sourceFiles(APP_SOURCE).map((file) => relative(APP_SOURCE, file));
    expect(files.some((file) => file.startsWith(`${MODEL_HUB}${sep}`))).toBe(true);
    expect(files.some((file) => !file.startsWith(`${MODEL_HUB}${sep}`))).toBe(true);
    // The raw key that opened the lane, on the source remove flow.
    expect(referenced).toContainEqual({ key: 'settings.models.sourceDetail.gone', counted: false });
    // The `<Trans>` prop carrying the Avibe Cloud link.
    expect(referenced).toContainEqual({ key: 'remoteAccess.flowStep1', counted: false });
    // Both branches of the permissions dialog title, and the inner branch of its
    // nested conflict ternary.
    expect(referenced).toContainEqual({ key: 'permissions.access.addTitle', counted: false });
    expect(referenced).toContainEqual({ key: 'permissions.access.editTitle', counted: false });
    expect(referenced).toContainEqual({ key: 'permissions.states.pairingChangedBody', counted: false });
    // The `??` fallbacks: the settings breadcrumb and the OAuth success toast.
    expect(referenced).toContainEqual({ key: 'nav.settings', counted: false });
    expect(referenced).toContainEqual({ key: 'settings.models.oauth.status.success', counted: false });
    // A counted site, which needs copy no bare probe asks for.
    expect(referenced.some((reference) => reference.counted)).toBe(true);
  });

  it.each(BUNDLES)('translates every referenced call site in $lng', ({ lng, bundle }) => {
    const instance = localeInstance(lng, bundle);
    expect(referenced.filter((reference) => missingCopy(instance, lng, reference)).map(label)).toEqual([]);
  });

  it('reports a bundle value as copy only when the value itself renders', () => {
    // EXISTENCE asks the VALUE, not a `t()` projection, so this pins what a value
    // has to carry. The array cases are the ones that were passing wrongly: the
    // probe used to re-call `t()` without the caller's `returnObjects`, take
    // i18next's object-instead-of-string diagnostic as copy, and stay green while
    // the surface rendered nothing.
    expect(isRenderable('Disclosure', 'k')).toBe(true);
    expect(isRenderable('', 'k')).toBe(false);
    expect(isRenderable('k', 'k')).toBe(false); // i18next echoes the key when it has none
    expect(isRenderable(['one', 'two'], 'k')).toBe(true);
    expect(isRenderable([], 'k')).toBe(false); // the disclosure list, emptied
    expect(isRenderable(['one', ''], 'k')).toBe(false); // a blank row is missing copy too
    expect(isRenderable({ a: 'one' }, 'k')).toBe(true);
    expect(isRenderable({}, 'k')).toBe(false);
    expect(isRenderable(undefined, 'k')).toBe(false);
    expect(isRenderable(42, 'k')).toBe(false);
  });

  it('collects dotted literals wherever they sit, with no position enumerated', () => {
    // PARITY's input, and the reason it needs no positions: every one of these is
    // collected by the same rule, and none of the carriers is named anywhere in
    // this file. The key-prop forms are the ones that were escaping — a literal
    // `labelKey` on a component that later calls `t(labelKey)`, and the data
    // tables in `lib/agentGraph.ts` and `SettingsLayout.tsx` that hold keys as
    // values. A carrier invented later is picked up without editing this file.
    const fixture = `
      const called = t('fixture.called');
      const attr = <ProxyUrlField labelKey="fixture.attrLabel" hintKey="fixture.attrHint" />;
      const braced = <Panel titleKey={'fixture.braced'} />;
      const table = [{ labelKey: 'fixture.tableLabel', bodyKey: 'fixture.tableBody' }];
      const held = 'fixture.held';
      const invented = <Thing someKeyNobodyEnumerated="fixture.invented" />;
      const nested = { deep: { deeper: ['fixture.inArray'] } };
      const notAKey = 'editor.background';
      const alsoNot = 'avibe.editor.fontSize.v1';
      // fixture.inAComment must not be collected
      const sentence = 'Read the fixture.docs page for more';
      const single = 'undotted';
      const templated = \`fixture.\${dynamic}\`;
    `;
    expect(collectDottedLiterals(fixture)).toEqual([
      'avibe.editor.fontSize.v1',
      'editor.background',
      'fixture.attrHint',
      'fixture.attrLabel',
      'fixture.braced',
      'fixture.called',
      'fixture.held',
      'fixture.inArray',
      'fixture.invented',
      'fixture.tableBody',
      'fixture.tableLabel',
    ]);
  });

  it('reports a literal one locale lost, and stays silent on what is not a key', () => {
    // PARITY's boundaries, pinned on bundles this test owns, so a scanner
    // regression fails here rather than in production. The classes are the ones
    // measured in `src/`: a key one locale dropped, a plural family one locale
    // dropped, and the 62 literals that are not keys at all.
    const gaps = parityGaps(
      [
        'card.title',
        'card.subtitle',
        'card.count',
        'card.total',
        'editor.background',
        'projects.changed',
        'agents.opencode.error_retry_limit',
        'avibe.editor.fontSize.v1',
      ],
      [
        {
          lng: 'en',
          bundle: {
            card: {
              title: 'Title',
              subtitle: 'Subtitle',
              count_one: 'one model',
              count_other: '{{count}} models',
              total_one: 'one total',
              total_other: '{{count}} total',
            },
          },
        },
        {
          lng: 'zh',
          // `subtitle` dropped outright; `total` dropped as a whole family.
          bundle: { card: { title: '标题', count_other: '{{count}} 个模型' } },
        },
      ],
    );
    expect(gaps).toEqual([
      { key: 'card.subtitle', present: ['en'], absent: ['zh'] },
      // A family kept in en and dropped in zh is a gap, not a stem that falls out:
      // neither locale resolves `card.total` bare, so only the plural probe sees it.
      { key: 'card.total', present: ['en'], absent: ['zh'] },
    ]);
    // `card.count` resolves in both — zh selects only `other`, and asking for the
    // category zh cannot reach would make every plural family a false gap.
    // The rest resolve in neither locale, so they are not keys and say nothing:
    // a Monaco theme token, an event-channel name, a config field path, a storage
    // key. None of them needed an allowlist to be quiet.
  });

  it('keeps every dotted literal in the app resolving in both locales or neither', () => {
    const literals = new Set<string>();
    for (const file of sourceFiles(APP_SOURCE)) {
      for (const found of collectDottedLiterals(readFileSync(file, 'utf8'), file)) literals.add(found);
    }
    expect(parityGaps([...literals].sort((a, b) => a.localeCompare(b)), BUNDLES)).toEqual([]);
  });
});
