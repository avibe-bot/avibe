// Every piece of user-visible copy is drawn through a `t('…')` literal, and a key
// that never reached the bundles renders as the raw key: removing a provider
// showed `settings.models.sourceDetail.gone` where a sentence belonged.
//
// So the guard states the property — every key the app can be known to ask for
// has copy in BOTH locales — and EXTRACTS the key list from the components
// themselves. A key, or a whole new surface added later, is covered without
// editing this file, which a hand-written list could never promise.
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
// `count` is one of the options the call site hands over, and the same reading
// answers the other one that changes what "has copy" means. `returnObjects` says
// the caller consumes a LIST — `MemorySettingsPanel` maps over its result — so a
// value turned into an object or a plain string is renderable by every other
// measure and still throws on `.map`. Both are read off the call rather than
// listed as keys here, so a second site asking for a list is covered the day it
// is written.
//
// That axis is closed by a MODEL rather than by a list of options, which is what
// it finally became: one question — how much copy does this call DEMAND — with
// three answers. `exact`, the options were a readable object literal, so `count`
// and `returnObjects` say precisely which shape the call consumes. `any`, the
// options arrived as an identifier, a conditional, or a spread (6 sites), so
// nothing can be claimed about the shape and the honest demand is that the name
// resolve at all. `none`, the call carries its own `defaultValue`, so it renders
// that when the bundles have nothing and can never show a raw key.
//
// An earlier round called `defaultValue` disjoint from literal keys "by
// construction": all 42 sites named a dynamic key, so none could hide a literal.
// Expanding templates (below) broke that — 18 of those sites now name a key this
// file knows, 7 distinct — and the disjointness was never the point. A demand of
// `none` narrows what EXISTENCE asks and narrows nothing else: the name still
// enters the candidate pool, so copy kept in one locale and dropped from the
// other is still a PARITY gap, still one locale rendering an English fallback
// beside a translated surface. There is no `context` anywhere in `src/`. A
// fourth option would arrive with its own meaning of "has copy" and belongs in
// this question the same way these three do.
//
// Reading the tree is also what lets the sweep follow the app's second way of
// naming a key: an `i18nKey`, which `<Trans>` takes as a prop when the copy wraps
// a link, and which a component's own table carries to hand to `t()` later.
//
// Neither site reliably hands over a bare literal, though, so what is taken from
// one is the SET of values it can be statically KNOWN to name: both branches of a
// ternary, either side of a `??` fallback, through any number of wrappers that
// preserve the value, and — because the sweep runs against a TYPED program — the
// product of a template whose substitutions each have a finite string-literal
// type. A leaf the compiler cannot narrow that far stays invisible. Recognising
// shapes one at a time instead is what left `remoteAccess.flowStep1` and the 94
// keys named through a conditional or a fallback deletable while this guard was
// green.
//
// That template case is the round a reviewer proved with a deletion: `App.tsx`
// writes t(`remoteAuthorization.${state}.title`), and `state` is a four-member
// union, so four real keys were nameable only there and deletable in silence.
// Reading the parts cannot answer it — TypeScript types the whole expression as
// plain `string` — so the sweep asks the CHECKER what each substitution can be
// and expands only when every member of that type is a string literal. 113 of
// the app's 164 template sites answer; 51 stay genuinely dynamic. The cost is a
// `ts.Program` over `src/`: 513 root files, ~1.5s, pinned here so a later
// blow-up to ten seconds is noticed rather than absorbed.
//
// A union is an UPPER BOUND on what reaches a call site, though, never the exact
// set. Two spans of one template can be correlated by dataflow that expanding
// each span independently cannot see, and a runtime guard can narrow a value in
// ways the compiler does not track. So expansion also names a few combinations
// the app can never ask for — six today — and those are classified in the
// residue pin below, exactly like a Monaco token or a storage key. That is what
// an upper bound costs, and it fails in the safe direction: a name too many gets
// classified by hand, a name too few goes unnoticed forever.
//
// All of that is the EXPRESSION axis, and it is only half of what this file
// needs, because it says nothing about the POSITIONS a key is written in.
// Calling the class closed on the expression axis alone is what let an earlier
// round land: `steps/TelegramConfig.tsx` hands `shared/ProxyUrlField` a literal
// `labelKey`, `lib/agentGraph.ts` keeps a whole table of them, and 75 keys reach
// `t()` only that way, through eighteen differently-named props. Adding those
// names here would be the same enumeration a fourth time.
//
// So this file states THREE properties, and only the first needs positions.
//
//   EXISTENCE — every key a `t()` or an `i18nKey` position can be KNOWN to name,
//   written as a literal or assembled by a template the checker can expand, has
//   the copy that call demands in both locales. Position-bound of necessity: to
//   demand that a key exist, something has to know it is a key. It asks nothing
//   of a name NO locale resolves, because nothing at a call site can tell a key
//   deleted from both bundles apart from a name that was never a key — that is
//   RESIDUE's question, so the two partition rather than overlap, and such a
//   name still fails one property over, as an unclassified newcomer in the pin.
//
//   PARITY — every name the app can ask for — every dotted string literal
//   anywhere in `src/`, plus every key expanded out of a template — that
//   resolves in EITHER locale resolves in BOTH. No positions, no prop names, no
//   dataflow beyond that expansion: a name becomes a candidate because of how it
//   READS or how it was assembled, and the bundles decide
//   whether it is a key. `labelKey="telegramConfig.proxyUrl"` is covered not
//   because this file learned the name `labelKey` but because that string is in
//   the bundles — so a prop, a data table, or a carrier nobody has invented yet
//   is covered without editing this file. "Resolves" means the locale RENDERS
//   copy for it, bare or against any plural category the locale selects, so a
//   family kept in one locale and dropped from the other is a gap here rather
//   than a stem that quietly falls out.
//
// What PARITY cannot do is find a key missing from BOTH locales: a name absent
// from both is indistinguishable from a Monaco theme token, an event-channel
// name, or a storage key, and 67 of those sit in `src/` today. EXISTENCE cannot
// cover that case either, for the same reason and by the same deliberate
// deferral — delete `telegramConfig.proxyUrl` from both bundles and neither
// property notices, while `ProxyUrlField` renders the raw key.
//
//   RESIDUE — the names that resolve in NEITHER locale are pinned, by exact set
//   equality, to the classified list below. Not a count: a count lets one name
//   leave as another enters. A key deleted from both bundles joins that set, the
//   set stops matching, and the suite fails. This is the only property that can
//   catch that, which is why the other two hand it the case rather than each
//   guessing at it.
//
// The pin is an enumeration, which is what the three rounds above kept getting
// wrong — but it enumerates the RESIDUE, not the positions, and that inverts the
// failure direction. A position this file has not heard of silently falls out of
// coverage; a literal this list has not heard of fails loudly and has to be
// classified. Same shape as `scripts/lint-baseline.mjs`, which pins this repo's
// known lint violations for the same reason.
//
// So the cost of the pin is a line whenever a genuinely new non-key dotted
// literal appears, and what it buys is exactly this, no more: no LITERAL-NAMED
// key, and no key assembled by a template whose substitutions are a finite
// string-literal union, can leave the bundles unnoticed — whatever position it
// was written in.
//
// Not more than that, and the residual is worth stating precisely rather than
// rounding away. 51 template sites stay genuinely dynamic, so the keys only they
// name remain invisible here. And 852 of the bundles' 4264 leaves are reached by
// no name in `src/` at all: pinning THOSE would be a maintenance contract over
// copy that churns with every feature, not over 67 stable non-keys, so it is a
// separate decision rather than something this change makes on the way past —
// tracked in #1968.
//
// All three ask about COPY rather than about a key existing, and that is the last
// thing worth saying here. Asking whether a key EXISTS is a weaker question that
// looks identical until a value goes empty, and it cost two rounds of one root
// cause: a `returnObjects` array emptied in one locale passed, then a key blanked
// to `''` in BOTH locales passed as well, resolving everywhere and so never
// reaching RESIDUE while the surface rendered a blank label.
//
// Copy is a LEAF — `rendersAsCopy`, one definition, one site: a nonblank string
// that is not the key echoed back. Whitespace-only counts as blank, because a
// value that renders as nothing is empty of copy whichever character made it so.
// Nothing else is copy; nobody renders a subtree.
//
// What differs is the CONTAINER it has to arrive in, and that is decided by who
// is asking — which is the distinction three review rounds were spent finding:
//
//   A CALL SITE consumes an exact shape. A plain call renders the value, so the
//   value must be copy. A `returnObjects` call renders each entry, so the value
//   must be a nonempty list of copy. An object passes neither: i18next hands a
//   plain call its diagnostic instead of a string, and `MemorySettingsPanel`
//   hands React an object child. `consumable` is total over the shapes i18next
//   can return, under both call shapes, and the fixture states that table.
//
//   A BARE LITERAL has no call site to read, and it may be a key PREFIX rather
//   than a key: `harness.runStatus` resolves to an object node that
//   `HarnessPage` completes at runtime. So PARITY and RESIDUE ask the subtree
//   question — `isRenderable`, is there copy under this name, nothing empty
//   under it — because dropping that whole subtree from one locale is a real
//   gap, and calling the prefix a non-key would hide it.
//
// One leaf, two containers, each owned by the property that can know it. Reusing
// the subtree reading at a call site is what let an object value pass as copy;
// using the call reading on a prefix would drop two real keys into RESIDUE.
//
// That whole claim is about copy being THERE. Whether the copy is RIGHT —
// accurate, idiomatic, actually translated rather than English pasted into
// `zh.json` — no scanner decides, and this one does not pretend to. Nor does it
// read further than the call: the guard reads the call site's OPTIONS, it does
// not guess the consumer's TypeScript assertion. `as string[]` on a `t()` result
// is a claim the compiler accepts and nobody checks; that is a product-code
// question, tracked in #1967, not something this file can see.
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

/**
 * A call site: the key it names, and how much copy the call demands of it.
 *
 * `demand` is the options axis asked as ONE question rather than as a list of
 * options, so it stays total over that axis:
 *
 *   exact — the options were readable, so the shape the call consumes is known
 *   exactly, and `counted` and `listed` say what it is.
 *   any — the options could not be read: an identifier, a conditional, or an
 *   object hiding a spread. Weaker than `counted` and `listed` both being
 *   false, which is a claim about what the call passes.
 *   none — the call carries its own `defaultValue`, so it renders that when the
 *   bundles have nothing and can never show a raw key.
 *
 * A name still has to agree across locales under every demand, including
 * `none`: copy kept in one locale and dropped from the other leaves the other
 * rendering an English fallback. So `demand` narrows what EXISTENCE asks, never
 * whether PARITY and RESIDUE see the name.
 */
type Demand = 'exact' | 'any' | 'none';
type Reference = { key: string; counted: boolean; listed: boolean; demand: Demand };

/** How a reference reads in a failure report: the key, and how it was called. */
const label = (reference: Reference): string => {
  if (reference.demand === 'any') return `${reference.key} (opaque options)`;
  const called = [reference.counted ? 'count' : '', reference.listed ? 'returnObjects' : ''].filter(Boolean);
  return called.length > 0 ? `${reference.key} (${called.join(', ')})` : reference.key;
};

/**
 * What makes two references the SAME reference — and it is not how one reads.
 *
 * `label` is free to leave a field out, because a report annotated with every
 * `defaultValue` call would only ever be noise. Deduplicating by it therefore
 * merged what it chose not to distinguish: `t('x')` and
 * `t('x', { defaultValue })` were one entry, the later file won, and the exact
 * call's demand for copy vanished into a call that demands none. So identity is
 * read off the reference itself rather than written out — a field added to
 * `Reference` is part of it the moment it exists, which is the only version of
 * this that cannot drift again.
 */
const identity = (reference: Reference): string =>
  JSON.stringify(Object.entries(reference).sort(([left], [right]) => left.localeCompare(right)));

/** The written name of an object-literal property or a JSX attribute. */
const nameOf = (node: ts.Node | undefined): string | undefined =>
  (node !== undefined && (ts.isIdentifier(node) || ts.isStringLiteralLike(node)) ? node.text : undefined);

const passesOption = (options: ts.Expression | undefined, name: string): boolean =>
  options !== undefined
  && ts.isObjectLiteralExpression(options)
  && options.properties.some((property) => nameOf(property.name) === name);

/**
 * How much copy the call DEMANDS of its key — the options axis as one question.
 *
 * A spread or a computed name inside the object is as unreadable as an
 * identifier standing where the object should be: `{ ...options }` may carry
 * `count`, so claiming the call passes none of it would demand a bare key of a
 * plural family. `defaultValue` is decided first because seeing it is enough —
 * whatever else the object hides, that call renders its own fallback.
 */
const demandOf = (options: ts.Expression | undefined): Demand => {
  if (options === undefined) return 'exact';
  if (!ts.isObjectLiteralExpression(options)) return 'any';
  if (passesOption(options, 'defaultValue')) return 'none';
  return options.properties.every((property) => nameOf(property.name) !== undefined) ? 'exact' : 'any';
};

/** The operators that pick one of their operands, so both can name a key. */
const CHOICE_OPERATORS = new Set<ts.SyntaxKind>([
  ts.SyntaxKind.AmpersandAmpersandToken,
  ts.SyntaxKind.BarBarToken,
  ts.SyntaxKind.QuestionQuestionToken,
]);

/**
 * The strings a TYPE can be known to be — finite, or nothing at all.
 *
 * A union answers only if every member is a string literal: one `string` member
 * makes the whole set open, and an open set is not something this file may
 * expand. `undefined` therefore means "unknowable", never "empty".
 */
const literalTypes = (type: ts.Type): string[] | undefined => {
  const parts = type.isUnion() ? type.types : [type];
  const strings: string[] = [];
  for (const part of parts) {
    if (!part.isStringLiteral()) return undefined;
    strings.push(part.value);
  }
  return strings.length > 0 ? strings : undefined;
};

/**
 * Every key an expression can be statically KNOWN to name — the one thing this
 * file asks of a key expression, and the whole of its boundary.
 *
 * A key argument is often not a bare literal. It is a ternary picking a title
 * (`t(adding ? '…addTitle' : '…editTitle')`), a `??` naming the fallback
 * (`t(item?.labelKey ?? 'nav.settings')`), a template assembling a family
 * member (`` t(`remoteAuthorization.${state}.title`) ``), or any of those
 * wrapped in something that does not change the value. So an expression yields
 * a SET: a choice contributes every branch, a wrapper is transparent, a
 * template contributes every string its substitutions can produce, and a
 * literal is itself.
 *
 * A substitution is read from the TYPE when a checker is available, which is
 * the only thing here that needs one. `state: 'revoked' | 'unavailable'` is a
 * finite set of strings, so the template names a finite set of keys, and
 * knowing that costs a `ts.Program`. Nothing else in this file does — the
 * syntax tree answers every other shape, and a template of literals
 * (`` `${adding ? 'add' : 'edit'}.title` ``) expands with no checker at all.
 *
 * Every remaining leaf yields nothing and stays invisible by design — a
 * variable, property, or call result whose type is `string`, and an index into
 * a lookup table. Those are the app's genuinely dynamic keys, which is also
 * where its `defaultValue` fallbacks live; this guard cannot know what they
 * resolve to and does not guess. A choice or a template with one unknowable
 * part still protects the parts it can read, rather than giving up on the whole
 * call.
 *
 * Being total over the knowable forms rather than a list of recognised ones is
 * the point: a shape nobody enumerated was a key this guard silently stopped
 * protecting, and shapes were exactly what kept getting missed.
 */
const staticKeys = (node: ts.Expression | undefined, checker?: ts.TypeChecker): string[] => {
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
    return staticKeys(node.expression, checker);
  }
  if (ts.isConditionalExpression(node)) {
    return [...staticKeys(node.whenTrue, checker), ...staticKeys(node.whenFalse, checker)];
  }
  if (ts.isBinaryExpression(node) && CHOICE_OPERATORS.has(node.operatorToken.kind)) {
    return [...staticKeys(node.left, checker), ...staticKeys(node.right, checker)];
  }
  if (ts.isTemplateExpression(node)) {
    // The product of the parts, in order. TypeScript itself types this
    // expression as plain `string` outside a const context, so the spans are
    // expanded here rather than asked of the whole template.
    let assembled = [node.head.text];
    for (const span of node.templateSpans) {
      const parts = staticKeys(span.expression, checker);
      if (parts.length === 0) return [];
      assembled = assembled.flatMap((prefix) => parts.map((part) => `${prefix}${part}${span.literal.text}`));
    }
    return assembled;
  }
  if (checker !== undefined) {
    const known = literalTypes(checker.getTypeAtLocation(node));
    if (known !== undefined) return known;
  }
  return [];
};

/**
 * `t(…)`, whose options say what copy the call needs.
 *
 * The options argument is READABLE when it is an object literal whose every
 * property NAMES itself: then every option the call passes is written there, so
 * the demand is exact. `count` selects a
 * plural family, `returnObjects` says the caller maps a list, and
 * `defaultValue` says the call renders its own fallback, which demands no copy
 * at all. That is the whole options axis the app uses; there is no `context`
 * in `src/`.
 *
 * Anything else — an identifier, a conditional, an object hiding a spread —
 * demands `any`: not "no options", which is what this file used to read it as,
 * but "unknown options". Those are different claims, and conflating them made
 * `` t(`settings.models.shell.${key}`, allDirect ? { count } : undefined) ``
 * look like an uncounted call against a plural-only family.
 */
const translationCall = (node: ts.Node, checker?: ts.TypeChecker): Reference[] => {
  if (!ts.isCallExpression(node) || !ts.isIdentifier(node.expression) || node.expression.text !== 't') {
    return [];
  }
  const [key, options] = node.arguments;
  const demand = demandOf(options);
  const counted = passesOption(options, 'count');
  const listed = passesOption(options, 'returnObjects');
  return staticKeys(key, checker).map((found) => ({ key: found, counted, listed, demand }));
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
const i18nKeyReference = (node: ts.Node, checker?: ts.TypeChecker): Reference[] => {
  if (ts.isJsxAttribute(node) && nameOf(node.name) === 'i18nKey') {
    const value = node.initializer;
    const counted = ts.isJsxAttributes(node.parent)
      && node.parent.properties.some((property) => ts.isJsxAttribute(property) && nameOf(property.name) === 'count');
    const keys = value !== undefined && ts.isJsxExpression(value)
      ? staticKeys(value.expression, checker)
      : staticKeys(value, checker);
    return keys.map((found) => ({ key: found, counted, listed: false, demand: 'exact' }));
  }
  if (ts.isPropertyAssignment(node) && nameOf(node.name) === 'i18nKey') {
    return staticKeys(node.initializer, checker).map((found) => ({ key: found, counted: false, listed: false, demand: 'exact' }));
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
 * `counted` and `listed` are what the call can be SEEN to pass, and they mean
 * something only under a demand of `exact`; anything unreadable answers `any`
 * instead of claiming the call passes nothing. What stays a residual is a bare
 * `t(…)` that is not i18next at all: it would be asked for copy it does not
 * need, and that is a loud failure someone looks at, never a raw key shipped
 * past a green CI.
 *
 * A key's namespace is not part of this: every `useTranslation()` in the app
 * takes the default one, so a key resolves against `translation` exactly as the
 * probes below resolve it.
 *
 * The checker is optional because only one shape needs it — a template whose
 * substitution is a typed variable. Without one the sweep is narrower, never
 * wrong.
 */
const referencesIn = (file: ts.SourceFile, checker?: ts.TypeChecker): Reference[] => {
  const found = new Map<string, Reference>();
  const visit = (node: ts.Node) => {
    // Keyed by the whole shape, because one key named by a counted and by an
    // uncounted call site needs both kinds of copy to be there — and one named
    // by an exact call and by a call carrying its own fallback still has to
    // answer the exact one.
    for (const reference of [...translationCall(node, checker), ...i18nKeyReference(node, checker)]) {
      found.set(identity(reference), reference);
    }
    ts.forEachChild(node, visit);
  };
  visit(file);
  return [...found.values()].sort((a, b) => label(a).localeCompare(label(b)));
};

export const collectReferences = (source: string, fileName = 'fixture.tsx'): Reference[] =>
  referencesIn(
    ts.createSourceFile(
      fileName,
      source,
      ts.ScriptTarget.Latest,
      true,
      fileName.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
    ),
  );

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

/**
 * The whole app as one typed program, which is what makes a template key
 * knowable.
 *
 * Built from an explicit walk of `src/` rather than from a tsconfig, because
 * `tsconfig.app.json` does not include `App.tsx` — and `App.tsx` is where
 * `` t(`remoteAuthorization.${state}.title`) `` is written. A program that
 * silently omitted it would expand nothing there and stay green, which is the
 * failure this whole file exists to refuse.
 *
 * The options are the app's, inline: a tsconfig that stops covering a file is
 * exactly the input this must not depend on. `paths` matters because a source
 * file imports `@/…`, and an unresolved import types its values as `any`, which
 * is not a finite union and so expands to nothing.
 *
 * Cost, measured: ~2s over 513 files, once for the suite. That is the price of
 * the type checker and it is pinned here so a later blow-up — a 10s program —
 * is noticed as a regression rather than absorbed.
 */
const typedApp = (files: string[]): ts.Program =>
  ts.createProgram(files, {
    target: ts.ScriptTarget.ES2022,
    module: ts.ModuleKind.ESNext,
    moduleResolution: ts.ModuleResolutionKind.Bundler,
    jsx: ts.JsxEmit.ReactJSX,
    strict: true,
    noEmit: true,
    skipLibCheck: true,
    esModuleInterop: true,
    resolveJsonModule: true,
    baseUrl: resolve(APP_SOURCE, '..'),
    paths: { '@/*': ['src/*'] },
  });

const referencedCallSites = (): Reference[] => {
  const files = sourceFiles(APP_SOURCE);
  const program = typedApp(files);
  const checker = program.getTypeChecker();
  const found = new Map<string, Reference>();
  for (const file of files) {
    const source = program.getSourceFile(file);
    // A file the program could not load would silently contribute no keys.
    expect(source, `${relative(APP_SOURCE, file)} is missing from the program`).toBeDefined();
    for (const reference of referencesIn(source as ts.SourceFile, checker)) {
      found.set(identity(reference), reference);
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

/** Every options object a readable call site can reach, and so must have copy for. */
const probes = (lng: string, reference: Reference): Record<string, unknown>[] =>
  (reference.counted ? representativeCounts(lng).map((count) => ({ count })) : [{}]);

/**
 * Whether a bundle VALUE carries something to render. This is the file's only
 * notion of a key being there, and every property below is stated through it.
 *
 * Asking the value rather than a `t()` projection is what makes it position-free
 * and option-free. The projection needed the call's position to find the key AND
 * the call's options to see the value: `memory/MemorySettingsPanel.tsx` reads two
 * keys with `returnObjects` and maps them as arrays, and a probe without that
 * option gets i18next's "returned an object instead of string" diagnostic — a
 * nonempty string, which passed, while the surface rendered no rows.
 *
 * A collection has to be nonempty AND renderable in every slot, because a locale
 * that keeps the array and empties an entry renders a blank row rather than a raw
 * key, and a blank row is still missing copy.
 *
 * The boundary is exactly this and no wider: a value is renderable when it is a
 * nonblank string that is not the key itself, or a nonempty collection whose
 * every entry is renderable. Whitespace-only counts as blank, because a value
 * that renders as nothing is empty of copy whichever character made it so.
 * Whether the copy is CORRECT — right register, right meaning, right locale —
 * is not decidable here and stays human territory.
 */
const rendersAsCopy = (value: unknown, key: string): boolean =>
  typeof value === 'string' && value.trim() !== '' && value !== key;

const isRenderable = (value: unknown, key: string): boolean => {
  if (typeof value === 'string') return rendersAsCopy(value, key);
  if (Array.isArray(value)) return value.length > 0 && value.every((entry) => isRenderable(entry, key));
  if (typeof value === 'object' && value !== null) {
    const entries = Object.values(value);
    return entries.length > 0 && entries.every((entry) => isRenderable(entry, key));
  }
  return false;
};

/**
 * Whether a locale resolves a literal — meaning it renders copy for it, bare or
 * against any plural category the locale selects.
 *
 * Renders, not merely exists. `instance.exists()` was the earlier answer and it
 * is presence only, which cost two review rounds of the same root cause: an
 * emptied `returnObjects` array passed, and then a key blanked to `''` in BOTH
 * locales passed as well, resolving everywhere and so never reaching RESIDUE
 * while `ProxyUrlField` rendered a blank label. Both are the same mistake, asking
 * whether a key is there instead of whether copy is. So there is one predicate
 * and "resolves" means one thing file-wide: EXISTENCE, PARITY and RESIDUE all
 * inherit it, and no value-shaped hole can be open in one and closed in another.
 *
 * The plural half matters: a family with no plain key (`usageSummary_one` and
 * `_other`, no `usageSummary`) renders nothing bare, so asking only that way
 * would drop every family out of PARITY and miss the locale that kept the family
 * while the other lost it.
 */
const resolvesIn = (instance: I18n, lng: string, key: string): boolean =>
  isRenderable(instance.t(key, { returnObjects: true }), key) ||
  representativeCounts(lng).some((count) =>
    isRenderable(instance.t(key, { count, returnObjects: true }), key),
  );

/**
 * Every dotted literal in `src/` that resolves in NEITHER locale, pinned exactly.
 *
 * These read like keys and are not keys. Each one is classified, because the
 * whole value of the pin is that an unclassified literal cannot sit here quietly:
 * adding a line is a deliberate claim that the string is not copy, and deleting
 * copy that something still references shows up as an unexplained new entry.
 *
 * There are no plural stems in this list. Families fold into PARITY through
 * `resolvesIn`, so a stem whose family exists resolves and never reaches here.
 */
const NON_KEY_LITERALS = [
  // Monaco theme tokens and editor actions. Read by the editor, never by i18next.
  'editor.action.selectAll',
  'editor.background',
  'editor.lineHighlightBackground',
  'editor.selectionBackground',
  'editorGutter.background',
  'editorIndentGuide.background1',
  'editorLineNumber.activeForeground',
  'editorLineNumber.foreground',
  'editorWidget.background',
  'minimap.background',

  // Event-channel names. `ApiContext.tsx` publishes and subscribes by these.
  'authorization.changed',
  'inbox.session.updated',
  'inbox.unread.changed',
  'message.new',
  'message.updated',
  'projects.changed',
  'queue.updated',
  'remote.authorization',
  'remote_access.quality.changed',
  'runs.updated',
  'session.activity',
  'session.status',
  'turn.end',
  'turn.start',
  'vaults.updated',
  'workbench.events.bridge.status',
  // Event-channel names on the vault sandbox bridge and the permissions surface.
  'confirm.surface',
  'instance.permissions.mutate',
  'ui.hide',
  'ui.show',
  'vault.state',

  // Server error codes. `serverCopy.ts` renders these through a `defaultValue`,
  // so the server's own message shows when the client has no copy for the code.
  'modelHub.errors.backend_model_catalog_invalid',
  'modelHub.errors.backend_model_conflict',
  'modelHub.errors.backend_model_duplicate',
  'modelHub.errors.backend_model_id_invalid',
  'modelHub.errors.backend_model_id_prefix',
  'modelHub.errors.backend_model_in_route',
  'modelHub.errors.backend_model_locked',
  'modelHub.errors.candidate_suppliers_changed',
  'modelHub.errors.engine_down',
  'modelHub.errors.models_dev_unavailable',
  'modelHub.errors.native_login_in_progress',
  'modelHub.errors.native_subscription_exists',

  // Storage keys. Namespaced on purpose, which is also why they read like keys.
  'avibe.agents.tab.v1',
  'avibe.editor.fontSize.v1',
  'avibe.editor.recents.v1',
  'avibe.terminal.fontSize.v1',
  'avibe.terminal.sessionId',
  'avibe.vault.crypto',
  'avibe.workbench.windows.v1',
  'vibe.webPush.deviceId',
  'vibe.webPush.endpoints',

  // Config field paths, addressed against the V2 config rather than a bundle.
  'agents.opencode.active_turn_timeout_seconds',
  'agents.opencode.error_retry_limit',
  'platforms.enabled',

  // Not keys in any sense: a package name, a host, a filename, a sample domain.
  'Thumbs.db',
  'opencode.ai',
  'qrcode.react',
  'relay.example',

  // Dead literals, removal tracked. `CreateViaChatDialog.tsx` builds these and
  // feeds them through a `defaultValue` chain into `harness.createDialog.*`,
  // which does exist in both locales, so the rendered copy is correct. The
  // comment there claims they are kept for a translation linter, which is not
  // true of this guard. Deleting product code is out of scope for this PR.
  'workbench.createDialog.kindTask',
  'workbench.createDialog.kindWatch',

  // Combinations a template's types allow and the app cannot reach. A union is
  // an upper bound on what arrives at a call site, never the exact set, so
  // expanding one names keys alongside the real ones — and the two are told
  // apart the same way every other name here is: by the bundles. These have
  // copy in neither locale because nothing ever asks for them.
  //
  // `OAuthConnectDialog` picks the prefix and the leaf from the SAME condition
  // (`disabled ? 'opt' : 'badge'` beside `badgeKey = disabled ? 'added' : …`),
  // so only 4 of the 8 products exist; the other 4 are these. Correlation
  // between spans is real dataflow, and no expansion of independent parts can
  // see it.
  'settings.models.addSub.badge.added',
  'settings.models.addSub.opt.recommended',
  'settings.models.addSub.opt.secondary',
  'settings.models.addSub.opt.supportedNotRecommended',
  // `ShowPageSharingSettings` renders this behind
  // `['conflict', 'invalid', 'error'].includes(gate)`, which narrows nothing
  // for the compiler, so the other two members of `Gate` expand too.
  'chat.showPage.sharingErrors.ready',
  'chat.showPage.sharingErrors.share_id_taken',
];

/** Every dotted literal the app carries, wherever it sits, sorted. */
const appDottedLiterals = (): string[] => {
  const literals = new Set<string>();
  for (const file of sourceFiles(APP_SOURCE)) {
    for (const found of collectDottedLiterals(readFileSync(file, 'utf8'), file)) literals.add(found);
  }
  return [...literals].sort((a, b) => a.localeCompare(b));
};

/**
 * Every name the app can ask a bundle for: the dotted literals it writes, plus
 * the keys its call sites are known to name.
 *
 * Those two nearly coincide, and the difference is the whole point — a key no
 * literal spells. `remoteAuthorization.revoked.title` is assembled from a
 * template, so it appears nowhere in `src/` as a string, and deleting it from
 * BOTH locales was invisible to every property in this file until the
 * expansion above put the assembled name into this pool.
 */
const appCandidateKeys = (referenced: Reference[]): string[] =>
  [...new Set([...appDottedLiterals(), ...referenced.map((reference) => reference.key)])]
    .sort((a, b) => a.localeCompare(b));

/** The literals no locale resolves, sorted for comparison against the pin. */
export const residueOf = (
  literals: string[],
  bundles: { lng: string; bundle: unknown }[],
): string[] => {
  const locales = bundles.map(({ lng, bundle }) => ({ lng, instance: localeInstance(lng, bundle) }));
  return literals
    .filter((key) => !locales.some(({ lng, instance }) => resolvesIn(instance, lng, key)))
    .sort((a, b) => a.localeCompare(b));
};

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
 * The symptom this guard exists for, stated exactly: nothing the call site can
 * render comes back.
 *
 * A call site names an exact shape, and only copy in THAT shape reaches a screen
 * through it. A plain call renders the value itself, so the value has to be a
 * string; turn it into an object and i18next hands back its diagnostic instead
 * of copy. A call reading `returnObjects` maps the value, so it has to be a list
 * of strings; turn it into an object, a plain string, or a list of objects and
 * `MemorySettingsPanel` throws on `.map` or hands React an object child.
 *
 * That shape is read off the call, exactly as `count` is, so a second site
 * asking for a list is covered without naming its keys here.
 *
 * The subtree reading (`isRenderable`) is deliberately NOT used here. It answers
 * a different question — does this NAME have copy under it — which is the right
 * question for a bare literal that may be a key prefix, and the wrong one for a
 * call site, because no consumer renders a subtree.
 */
const consumable = (value: unknown, reference: Reference): boolean =>
  reference.listed
    ? Array.isArray(value)
      && value.length > 0
      && value.every((entry) => rendersAsCopy(entry, reference.key))
    : rendersAsCopy(value, reference.key);

/**
 * What a locale owes a call site, which is exactly what the call demands.
 *
 * An `exact` call names its own shape, so every option set it can reach has to
 * come back consumable. An `any` call names none of it: the options could hide
 * a `count`, a `returnObjects`, or a `defaultValue`, and picking one shape
 * would be inventing a fact. So it is asked the same question a bare literal is
 * asked — does this name have copy under it — the weakest demand that still
 * fails when copy is deleted, and one this file already has a predicate for
 * rather than a third notion. A `none` call renders its own fallback and is
 * filtered out before this, since there is no copy it can fail to find.
 *
 * The residual is stated rather than hidden: an opaque options object that
 * hides a `defaultValue` would be asked for copy the call does not need. That
 * is a loud failure someone reads, never a raw key shipped past a green CI, and
 * of the 6 opaque sites in `src/` today none is one.
 */
const missingCopy = (instance: I18n, lng: string, reference: Reference): boolean =>
  (reference.demand === 'any'
    ? !resolvesIn(instance, lng, reference.key)
    : probes(lng, reference).some(
      (options) => !consumable(instance.t(reference.key, { ...options, returnObjects: true }), reference),
    ));

describe('app i18n key coverage', () => {
  const referenced = referencedCallSites();

  it('collects the literal keys and how they are called, and only those, from a source file', () => {
    // The forward guard on the collector itself: a coverage test whose collector
    // silently stops matching reports coverage it never had. So this pins the
    // whole boundary rather than a sample of it — every static form the collector
    // must see through, every dynamic leaf it must decline, the two places a key
    // is named, all three demands and one name carrying two of them, and
    // `count`, whose loss would quietly stop asking for plural copy. A form
    // added to `staticKeys` later, or an options shape read differently, fails
    // here until it is stated.
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
      const nested = t('fixture.outer', { hint: t('fixture.inner', { count: 3 }) });

      // How much copy the call DEMANDS. Options nobody can read are unknown, not
      // absent — an object hiding a spread or a computed name included, since
      // either may be the \`count\` that picks a plural family. A call carrying its
      // own fallback demands nothing, and is still a name the bundles must agree
      // on, so it is collected rather than dropped.
      const spread = t('fixture.spread', { ...options });
      const computedOption = t('fixture.computed', { [dynamicOption]: 2 });
      const carriedOptions = t('fixture.carried', options);
      const chosenOptions = t('fixture.chosen', many ? { count } : undefined);
      const defaulted = t('fixture.defaulted', { defaultValue: 'Background work' });
      const defaultedHiding = t('fixture.defaultedSpread', { defaultValue: 'x', ...options });

      // One name, two demands. The call that carries a fallback cannot answer
      // for the one that does not, so neither absorbs the other.
      const both = t('fixture.both');
      const bothDefaulted = t('fixture.both', { defaultValue: 'x' });

      // A template is the product of its parts, and needs no checker when every
      // part is written out.
      const assembled = t(\`fixture.\${'left'}.\${'tail'}\`);
      const branched = t(\`fixture.\${adding ? 'on' : 'off'}.label\`, { count: total });

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
      { key: 'fixture.a', counted: false, listed: false, demand: 'exact' },
      { key: 'fixture.alongside', counted: true, listed: false, demand: 'exact' },
      { key: 'fixture.andThen', counted: false, listed: false, demand: 'exact' },
      { key: 'fixture.asserted', counted: false, listed: false, demand: 'exact' },
      { key: 'fixture.b', counted: false, listed: false, demand: 'exact' },
      { key: 'fixture.bang', counted: false, listed: false, demand: 'exact' },
      { key: 'fixture.both', counted: false, listed: false, demand: 'exact' },
      { key: 'fixture.both', counted: false, listed: false, demand: 'none' },
      { key: 'fixture.braced', counted: false, listed: false, demand: 'exact' },
      { key: 'fixture.c', counted: false, listed: false, demand: 'exact' },
      { key: 'fixture.carried', counted: false, listed: false, demand: 'any' },
      { key: 'fixture.chosen', counted: false, listed: false, demand: 'any' },
      { key: 'fixture.computed', counted: false, listed: false, demand: 'any' },
      { key: 'fixture.defaulted', counted: false, listed: false, demand: 'none' },
      { key: 'fixture.defaultedSpread', counted: false, listed: false, demand: 'none' },
      { key: 'fixture.double', counted: false, listed: false, demand: 'exact' },
      { key: 'fixture.fallback', counted: false, listed: false, demand: 'exact' },
      { key: 'fixture.inner', counted: true, listed: false, demand: 'exact' },
      { key: 'fixture.interpolated', counted: false, listed: false, demand: 'exact' },
      { key: 'fixture.knownBranch', counted: false, listed: false, demand: 'exact' },
      { key: 'fixture.left.tail', counted: false, listed: false, demand: 'exact' },
      { key: 'fixture.literal', counted: false, listed: false, demand: 'exact' },
      { key: 'fixture.no', counted: false, listed: false, demand: 'exact' },
      { key: 'fixture.off.label', counted: true, listed: false, demand: 'exact' },
      { key: 'fixture.on.label', counted: true, listed: false, demand: 'exact' },
      { key: 'fixture.oneWay', counted: true, listed: false, demand: 'exact' },
      { key: 'fixture.orElse', counted: false, listed: false, demand: 'exact' },
      { key: 'fixture.otherWay', counted: true, listed: false, demand: 'exact' },
      { key: 'fixture.outer', counted: false, listed: false, demand: 'exact' },
      { key: 'fixture.paren', counted: false, listed: false, demand: 'exact' },
      { key: 'fixture.shorthand', counted: true, listed: false, demand: 'exact' },
      { key: 'fixture.spread', counted: false, listed: false, demand: 'any' },
      { key: 'fixture.table', counted: false, listed: false, demand: 'exact' },
      { key: 'fixture.tableNo', counted: false, listed: false, demand: 'exact' },
      { key: 'fixture.tableYes', counted: false, listed: false, demand: 'exact' },
      { key: 'fixture.trans', counted: false, listed: false, demand: 'exact' },
      { key: 'fixture.transCount', counted: true, listed: false, demand: 'exact' },
      { key: 'fixture.transNo', counted: false, listed: false, demand: 'exact' },
      { key: 'fixture.transYes', counted: false, listed: false, demand: 'exact' },
      { key: 'fixture.wrapped', counted: true, listed: false, demand: 'exact' },
      { key: 'fixture.yes', counted: false, listed: false, demand: 'exact' },
    ]);
  });

  it('tells two references apart by every field one carries', () => {
    // Which is why the dedup key is read off the reference rather than written
    // out: a field added to `Reference` later belongs to its identity without
    // anyone remembering to say so. The length check is the same argument
    // applied to this test — the variants below are a list, and a list goes
    // stale exactly when the identity it stands for does.
    const base: Reference = { key: 'a.b', counted: false, listed: false, demand: 'exact' };
    expect(Object.keys(base)).toHaveLength(4);
    const variants: Reference[] = [
      { ...base, key: 'a.c' },
      { ...base, counted: true },
      { ...base, listed: true },
      { ...base, demand: 'any' },
      { ...base, demand: 'none' },
    ];
    expect(new Set([base, ...variants].map(identity)).size).toBe(variants.length + 1);
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
    expect(referenced).toContainEqual({ key: 'settings.models.sourceDetail.gone', counted: false, listed: false, demand: 'exact' });
    // The `<Trans>` prop carrying the Avibe Cloud link.
    expect(referenced).toContainEqual({ key: 'remoteAccess.flowStep1', counted: false, listed: false, demand: 'exact' });
    // Both branches of the permissions dialog title, and the inner branch of its
    // nested conflict ternary.
    expect(referenced).toContainEqual({ key: 'permissions.access.addTitle', counted: false, listed: false, demand: 'exact' });
    expect(referenced).toContainEqual({ key: 'permissions.access.editTitle', counted: false, listed: false, demand: 'exact' });
    expect(referenced).toContainEqual({ key: 'permissions.states.pairingChangedBody', counted: false, listed: false, demand: 'exact' });
    // The `??` fallbacks: the settings breadcrumb and the OAuth success toast.
    expect(referenced).toContainEqual({ key: 'nav.settings', counted: false, listed: false, demand: 'exact' });
    expect(referenced).toContainEqual({ key: 'settings.models.oauth.status.success', counted: false, listed: false, demand: 'exact' });
    // A counted site, which needs copy no bare probe asks for.
    expect(referenced.some((reference) => reference.counted)).toBe(true);
    // A key no literal in `src/` spells, assembled from a template whose
    // substitution the type checker pins to a finite set of strings. `App.tsx`
    // writes `` t(`remoteAuthorization.${state}.title`) `` and nothing else
    // names these; before the program above, deleting them was invisible here.
    expect(referenced.map((reference) => reference.key)).toEqual(
      expect.arrayContaining([
        'remoteAuthorization.revoked.title',
        'remoteAuthorization.revoked.body',
        'remoteAuthorization.unavailable.title',
        'remoteAuthorization.unavailable.body',
      ]),
    );
    // A name two kinds of call reach. `RouteOriginBadge` asks for the copy
    // outright; a dozen sites pass `agent.backend` as their own fallback. Both
    // belong here, and while the dedup key was the failure label the fallback
    // calls buried the outright one — so the whole family sat outside the
    // existence check, in the one place the app names its backends.
    expect(referenced).toContainEqual({ key: 'settings.models.backends.claude', counted: false, listed: false, demand: 'exact' });
    expect(referenced).toContainEqual({ key: 'settings.models.backends.claude', counted: false, listed: false, demand: 'none' });
  });

  // A key NO locale resolves is not an existence gap: nothing here can tell it
  // apart from a name the bundles never had, which is RESIDUE's question and
  // where it gets classified by hand. So the properties partition rather than
  // overlap, and a key deleted from BOTH locales still fails — as an
  // unclassified newcomer in the pin, naming itself just as loudly.
  const unresolvedEverywhere = new Set(residueOf(referenced.map((reference) => reference.key), BUNDLES));

  it.each(BUNDLES)('translates every referenced call site in $lng', ({ lng, bundle }) => {
    const instance = localeInstance(lng, bundle);
    expect(
      referenced
        .filter((reference) => reference.demand !== 'none')
        .filter((reference) => !unresolvedEverywhere.has(reference.key))
        .filter((reference) => missingCopy(instance, lng, reference))
        .map(label),
    ).toEqual([]);
  });

  it('reports a NAME as having copy under it when any leaf under it renders', () => {
    // The subtree question, which is what PARITY and RESIDUE ask. A dotted literal
    // is not always a leaf: `harness.runStatus` is a PREFIX that `HarnessPage`
    // completes at runtime, and it resolves to an object node. Dropping that whole
    // subtree from one locale is a real gap, so the object branch is load-bearing
    // here — and wrong at a call site, which is the next test.
    expect(isRenderable('Disclosure', 'k')).toBe(true);
    expect(isRenderable('', 'k')).toBe(false);
    expect(isRenderable('   ', 'k')).toBe(false);
    expect(isRenderable('k', 'k')).toBe(false); // i18next echoes the key when it has none
    expect(isRenderable(['one', 'two'], 'k')).toBe(true);
    expect(isRenderable([], 'k')).toBe(false); // the disclosure list, emptied
    expect(isRenderable(['one', ''], 'k')).toBe(false); // a blank row is missing copy too
    expect(isRenderable({ a: 'one' }, 'k')).toBe(true); // a prefix: copy lives under it
    expect(isRenderable({}, 'k')).toBe(false);
    expect(isRenderable(undefined, 'k')).toBe(false);
    expect(isRenderable(42, 'k')).toBe(false);
  });

  it('decides a CALL by the exact shape it consumes, over every shape i18next returns', () => {
    // Nobody renders a subtree. A call site renders either the value (plain) or
    // each entry of it (`returnObjects`), so what reaches a screen is always a
    // LEAF string, and the container it has to arrive in is fixed by the call.
    //
    // Stated as a total table rather than as the cases a review has reached so
    // far: every shape i18next can hand back, judged under both call shapes. The
    // call shapes are exhaustive because the options census above measured them
    // — `count` selects which copy, `returnObjects` selects the container, and no
    // third option in `src/` changes either. A value shape added to this table
    // must be classified; one omitted fails to compile the row it belongs in.
    const plain: Reference = { key: 'k', counted: false, listed: false, demand: 'exact' };
    const listed: Reference = { key: 'k', counted: false, listed: true, demand: 'exact' };

    const table: Array<{ value: unknown; plain: boolean; listed: boolean; why: string }> = [
      { value: 'Copy', plain: true, listed: false, why: 'a string is copy; `.map` throws on it' },
      { value: '', plain: false, listed: false, why: 'blank' },
      { value: '   ', plain: false, listed: false, why: 'renders as nothing' },
      { value: 'k', plain: false, listed: false, why: 'i18next echoing the key' },
      { value: ['one', 'two'], plain: false, listed: true, why: 'a list; rendered directly it is not copy' },
      { value: [], plain: false, listed: false, why: 'the disclosure list, emptied' },
      { value: ['one', ''], plain: false, listed: false, why: 'a blank row is missing copy' },
      { value: [{ text: 'one' }], plain: false, listed: false, why: 'React throws on an object child' },
      { value: [['one']], plain: false, listed: false, why: 'a nested list is not a row of copy' },
      { value: { label: 'Copy' }, plain: false, listed: false, why: 'a prefix, not copy any call renders' },
      { value: {}, plain: false, listed: false, why: 'empty' },
      { value: undefined, plain: false, listed: false, why: 'no value at all' },
      { value: null, plain: false, listed: false, why: 'no value at all' },
      { value: 42, plain: false, listed: false, why: 'not copy' },
      { value: true, plain: false, listed: false, why: 'not copy' },
    ];

    for (const row of table) {
      expect(consumable(row.value, plain), `plain: ${row.why}`).toBe(row.plain);
      expect(consumable(row.value, listed), `listed: ${row.why}`).toBe(row.listed);
    }
  });

  it('requires a list where the call site reads returnObjects, not merely copy', () => {
    // The options axis, stated as a fixture. `count` decides WHICH copy a call can
    // reach; `returnObjects` decides what SHAPE it can consume. Both are read off
    // the call, so neither is a list of keys here and a new call site inherits.
    const source = `
      const lines = t(custom ? 'fixture.listA' : 'fixture.listB', { returnObjects: true });
      const plain = t('fixture.plain');
    `;
    const references = collectReferences(source);
    expect(references).toEqual([
      { key: 'fixture.listA', counted: false, listed: true, demand: 'exact' },
      { key: 'fixture.listB', counted: false, listed: true, demand: 'exact' },
      { key: 'fixture.plain', counted: false, listed: false, demand: 'exact' },
    ]);

    const listed: Reference = { key: 'fixture.listA', counted: false, listed: true, demand: 'exact' };
    const check = (value: unknown) =>
      missingCopy(localeInstance('en', { fixture: { listA: value } }), 'en', listed);

    expect(check(['one', 'two'])).toBe(false);
    // The two shapes that read as copy and still throw on `.map`, which is what
    // `MemorySettingsPanel` does with the result. Renderable is not enough here.
    expect(check({ 0: 'one', 1: 'two' })).toBe(true);
    expect(check('one, two')).toBe(true);
    // And a plain call is unaffected: a string is all it ever needed.
    expect(missingCopy(localeInstance('en', { fixture: { plain: 'Plain' } }), 'en', {
      key: 'fixture.plain', counted: false, listed: false, demand: 'exact',
    })).toBe(false);

    // The whole options axis, measured on this head rather than assumed: the app
    // passes exactly three i18next options at a `t()` call, across 4024 of them.
    // `count` and `returnObjects` are modelled above. `defaultValue` is the third
    // (42 sites) and is answered by `demand`, not by a claim that it never meets
    // a key this guard knows — it does, at 18 of those sites, since templates
    // started expanding. There is no `context` anywhere in `src/`.
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
    // dropped, and the 61 literals that are not keys at all.
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
    // The rest resolve in neither locale, so PARITY says nothing about them:
    // a Monaco theme token, an event-channel name, a config field path, a storage
    // key. Staying quiet is what makes them RESIDUE's problem, below.
  });

  it('fails the pin when a carrier-only key is deleted from every locale', () => {
    // The mutation that produced the pin, kept as a fixture. `labelKey` is a
    // carrier: no `t()` or `i18nKey` position names this key, so EXISTENCE
    // cannot see it, and once both locales drop it PARITY cannot either.
    const source = `
      const Field = () => <ProxyUrlField labelKey="telegramConfig.proxyUrl" />;
    `;
    const literals = collectDottedLiterals(source);
    const bundles = [
      { lng: 'en', bundle: { telegramConfig: { botToken: 'Bot token' } } },
      { lng: 'zh', bundle: { telegramConfig: { botToken: '机器人令牌' } } },
    ];

    // Both locales lost it, so it reads as residue rather than as a parity gap.
    expect(parityGaps(literals, bundles)).toEqual([]);
    expect(residueOf(literals, bundles)).toEqual(['telegramConfig.proxyUrl']);

    // Which is the whole point: it is not in the pinned list, so the pin fails.
    expect(NON_KEY_LITERALS).not.toContain('telegramConfig.proxyUrl');
  });

  it('hands a call-site key no locale resolves to the pin, and keeps the name either way', () => {
    // The partition, from the call-site side. This is the key a reviewer deleted
    // from both bundles to show the guard silent — `App.tsx` names it through a
    // template the checker now expands. Even so, EXISTENCE cannot be the one to
    // answer: nothing at a call site tells a key deleted from every locale apart
    // from a name that was never a key, which is why it steps aside and the pin
    // names it instead. Two properties, one case, no overlap.
    const referenced = collectReferences(`
      const Banner = () => <p>{t('remoteAuthorization.revoked.title')}</p>;
    `);
    const names = referenced.map((reference) => reference.key);
    const gone = [
      { lng: 'en', bundle: { remoteAuthorization: { revoked: { body: 'Access was revoked.' } } } },
      { lng: 'zh', bundle: { remoteAuthorization: { revoked: { body: '访问已被撤销。' } } } },
    ];
    expect(parityGaps(names, gone)).toEqual([]);
    expect(residueOf(names, gone)).toEqual(['remoteAuthorization.revoked.title']);
    expect(NON_KEY_LITERALS).not.toContain('remoteAuthorization.revoked.title');

    // And a demand of `none` narrows only what copy is required, never whether
    // the name is watched: a call carrying its own fallback still may not have
    // copy in one locale and an English fallback in the other.
    const defaulted = collectReferences(`
      const title = t('harness.createDialog.kindTask', { defaultValue: 'Background work' });
    `);
    expect(defaulted).toEqual([
      { key: 'harness.createDialog.kindTask', counted: false, listed: false, demand: 'none' },
    ]);
    expect(
      parityGaps(defaulted.map((reference) => reference.key), [
        { lng: 'en', bundle: { harness: { createDialog: { kindTask: 'Background work' } } } },
        { lng: 'zh', bundle: { harness: { createDialog: { kindWatch: '后台监听' } } } },
      ]),
    ).toEqual([{ key: 'harness.createDialog.kindTask', present: ['en'], absent: ['zh'] }]);
  });

  it('treats a blanked value as missing copy, in one locale or in every locale', () => {
    // A key can be emptied as well as deleted, and an empty value renders a blank
    // label rather than a raw key, which is the quieter of the two failures. The
    // earlier `exists()` predicate said yes to both bundles here and the suite
    // stayed green; asking the VALUE moves each case to the property that owns it.
    const source = `
      const Field = () => <ProxyUrlField labelKey="telegramConfig.proxyUrl" />;
    `;
    const literals = collectDottedLiterals(source);
    const copy = (proxyUrl: string, botToken: string) => ({ telegramConfig: { proxyUrl, botToken } });

    // Blanked in one locale: the other still renders it, so it is a parity gap.
    const oneBlank = [
      { lng: 'en', bundle: copy('Proxy URL', 'Bot token') },
      { lng: 'zh', bundle: copy('', '机器人令牌') },
    ];
    expect(parityGaps(literals, oneBlank)).toEqual([
      { key: 'telegramConfig.proxyUrl', present: ['en'], absent: ['zh'] },
    ]);
    expect(residueOf(literals, oneBlank)).toEqual([]);

    // Blanked in every locale: nothing renders it, so it falls to the pin —
    // the same landing as deleting it outright, because it is the same loss.
    const allBlank = [
      { lng: 'en', bundle: copy('', 'Bot token') },
      { lng: 'zh', bundle: copy('   ', '机器人令牌') },
    ];
    expect(parityGaps(literals, allBlank)).toEqual([]);
    expect(residueOf(literals, allBlank)).toEqual(['telegramConfig.proxyUrl']);
    expect(NON_KEY_LITERALS).not.toContain('telegramConfig.proxyUrl');

    // Whitespace is blank: `zh` above holds three spaces, and a value that
    // renders as nothing is empty of copy whichever character made it so.
  });

  it('pins the names that are not keys, so no key can leave both locales unseen', () => {
    // Exact set equality, not a count: a count lets one literal leave as another
    // enters. Sorted on both sides so the list above can stay grouped by class.
    expect(residueOf(appCandidateKeys(referenced), BUNDLES)).toEqual(
      [...NON_KEY_LITERALS].sort((a, b) => a.localeCompare(b)),
    );
  });

  it('keeps every name the app can ask for resolving in both locales or neither', () => {
    expect(parityGaps(appCandidateKeys(referenced), BUNDLES)).toEqual([]);
  });
});
