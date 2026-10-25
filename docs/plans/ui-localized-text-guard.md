# Localized user-text lint guard (#2618)

## Change contract

Production `ui/src/**/*.tsx` JSX children and `aria-label`, `title`,
`placeholder`, and `alt` must not contain literal copy or literal sentence/list
composition. Locale templates, `Trans` component interpolation, and locale-aware
formatters own composition. No UI copy or locale resource is changed in this
rollout.

Use one dependency-free custom ESLint rule with the installed TypeScript AST and
ESLint lexical scopes. Follow immutable local bindings, TypeScript wrappers,
conditional display branches, arrays, and inline map/filter result paths into
these sinks. Calls to translation/formatting functions remain value boundaries;
this is not whole-program taint analysis. In particular, the original accessible
label used component props, not inline `t()` calls: literal joins at a sink must
be diagnosed even when the dynamic values' provenance is unknown.

Keep the rule pinned at error in the shared policy for *every* TS/TSX file; decide
its narrower production-TSX applicability inside the rule. Preserve domain,
policy, inline-override and suppression verification without exemptions there.

## Allow/deny boundaries

- Deny ordinary literal JSX copy and punctuation, string templates/`+`/`.join`
  with literal glue reaching the listed sinks, including local const aliases.
- Permit dynamic data, translation/formatter results, locale-owned separators,
  and genuine `react-i18next` `Trans` interpolation. Do not examine class names,
  protocol props, logging, translation keys, or arbitrary call arguments as copy.
- Decorative exemptions must be explicit, direct, and glyph-only. Separators
  such as the original `aria-hidden` middle dot are **not** decorative opt-outs.
- Technical native markup may display narrowly recognizable machine tokens,
  structured JSON objects/arrays, or keyboard notation, never arbitrary prose.
  Complete URL/path literals are data in any sink (not surrounding prose or a
  separator). Decorative/machine/JSON child exceptions do not apply to
  accessible-label/title/placeholder/alt attributes or to neighboring prose.
  No filename/component-name or suppression allowlist.

## Validation plan

1. Behavioral fixtures reproduce the four reviewed #2559 shapes (including the
   original local `reason`, spread/map cost array, props-based accessible name,
   and hidden JSX middle dot). Run them against the pre-change config first.
2. Table-driven positives/negatives exercise every sink, local aliases and scope
   shadowing, valid localization/formatting/data, and exception containment.
3. Execute the real gate in a test-owned miniature UI: new file/count fails,
   localized replacement requires baseline shrink, re-recorded debt passes, and
   suppression/policy changes cannot bypass enforcement. Retain the existing
   count-ratchet limitation: same-file/same-rule replacement at equal count is
   not site-identity enforcement.
4. Record only measured new-rule debt on unchanged `ui/src`; compare every
   unrelated violation and suppression allowance to the base byte-for-byte.
5. Focused lint suites, `npm run lint`, test typechecking, and UI build locally;
   remaining repository checks in CI. Exact-head Codex review, passing applicable
   CI, and zero unresolved threads are required. Do not merge or deploy.

## Implemented policy and analysis limits

`avibe/localized-user-text` is an error in `ui/eslint.config.js` and pinned in
`lintPolicy.mjs`. Applicability is relative to the UI ESLint working directory:
`src/**/*.tsx`, excluding `*.test.tsx`, `*.spec.tsx`, and `__tests__` directories.
The rule also treats the explicit `children` prop as a child sink. It reports
one diagnostic per offending expression/attribute sink or literal JSX text node;
using the same bad binding in both a title and a child yields two diagnostics.

The analysis follows unmodified, identifier-bound `const` initializers in their
actual lexical scope (including aliases), TS casts/non-null/satisfies wrappers,
conditionals, logical output branches, arrays/spreads, final sequence values,
`join` separators and receivers, `filter` receivers, and `map`/`flatMap` inline
or const callback return expressions. It ignores conditions and logging inside
callbacks, and stops at nested functions. Cycles terminate. Literal glue is
checked even when inputs are component props with unknown provenance.

This deliberately does **not** resolve imports, helper return values, arbitrary
object properties/destructuring, mutations, state, callback argument flow, or
cross-component prop flow. Ordinary calls (including `t`, `serverText`, `Intl`
and the existing `formatNameList`/`formatSentences`/number helpers) are opaque
output boundaries: their keys and arguments are not assumed to be output copy.
A helper can therefore hide unlocalized text; this is not proof that every UI
string is localized. Whitespace-only JSX formatting nodes and empty string
expressions are ignored; explicit nonempty string expressions/joiners are not.
No punctuation scan runs outside user-facing sinks.

A named `Trans` import from `react-i18next` (including an import alias) with an
`i18nKey` owns its fallback JSX text, including component children. Unverified
same-name components do not. Attributes and composed expression children still
get checked inside a `Trans` subtree.

Exceptions are content-shaped, not file/component allowlists:

- A direct native `span` with literal true `aria-hidden`, no spread attributes,
  and exactly one of the rule's small icon glyph set may display that glyph.
  The original hidden middle dot remains an error. Prose and labels remain errors.
- Complete whitespace-free HTTP(S)/WS(S) URLs with a hostname and simple explicit
  absolute/relative/home paths are data, including in titles/placeholders. A lone
  slash or text around the resource is not exempt. String glue is never exempt.
- Native `code`/`samp` may display recognizable compound identifiers/flags;
  `code`/`samp`/`pre` may display a complete parsed JSON object/array. `kbd` may
  display narrowly recognized keyboard notation. These do not exempt attributes,
  descendants, or arbitrary prose, and are not a general programming-language
  parser. Other existing technical display may conservatively remain baselined;
  do not translate a protocol command merely to silence it. A future exception
  needs a bounded content contract plus positive and escape-hatch tests.

## Initial measured baseline

Measured against unchanged `ui/src` at base
`055870279641fd86a5f501e6c67310ccde28194a`:

| Ledger | Before | After | Delta |
| --- | ---: | ---: | ---: |
| New localized-user-text diagnostics | 0 | 322 across 102 files | +322, initial rollout only |
| All unrelated error allowances | 471 | 471 | 0 |
| All inline-suppression allowances | 44 | 44 | 0 |
| Measured TS/TSX domain | 1111 | 1111 | 0 |

Every per-file new-rule count is in `ui/eslint-baseline.json`; it is the only
ledger, not duplicated here. All unrelated per-file/rule counts, suppressions,
and rationales compare equal to the base. No source or locale files changed.
The initial diagnostics include real sentence/accessible-name composition,
ordinary copy, and conservative technical/glyph display; this is not a claim
that every diagnostic should become translated prose.

The gate still freezes counts, not site identities. Replacing one violation with
another of this same rule in this same file at equal count can pass. New pairs,
increased counts, newly suppressed messages, and stale allowances cannot. This
accepted residual is tested explicitly rather than obscured by a second ledger.

## Validation evidence

- Before enabling the rule, all four historical fixture tests failed because the
  real config returned zero localized-text errors; afterward all four pass.
- `localizedUserText.test.mjs` executes the real ESLint config for the historical
  forms, all sinks, local indirection, valid locale/formatter/data paths and
  narrow exception containment. Existing convention/policy/baseline tests remain.
- `localizedUserTextGate.test.mjs` runs real `npm run lint` in test-owned miniature
  UI trees: new files/counts fail; localized replacements force shrink; re-recorded
  shrink passes; equal-count replacement retains the documented residual;
  suppressions, inline overrides and severity demotion cannot bypass the gate.
- `npm run lint`, `npm run typecheck:tests`, and `npm run build` pass locally.
  Build retains its pre-existing browser-crypto/chunk-size warnings. No Incus
  update, service change, runtime deployment or manual UI-copy rewrite is needed
  for this lint-only change.

