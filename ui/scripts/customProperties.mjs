// Every custom property a stylesheet declares, and what it is worth.
//
// Extracted for the reason `cssLength.mjs` and `styleWrite.mjs` were:
// `validate-theme.mjs` runs its whole validation at import time, so nothing
// inside it can be called from a test. This is the piece of that file which had
// to answer a question about CSS grammar, and a question about grammar is
// exactly the kind that wants cases rather than a careful reading.

import postcss from 'postcss';

// A name is declared by two different constructs, and only one of them is a
// declaration. `@property --elevation { syntax: "*"; initial-value: none }`
// registers the name in its PARAMS, so the only declarations inside it are
// called `syntax`, `inherits` and `initial-value` -- none of which start with
// `--`. Walking declarations alone therefore collected nothing from it, and a
// later `box-shadow: var(--elevation)` was reported as resolving to a name no
// stylesheet declares: the property is registered, the browser resolves it to a
// glow-free initial value, and the guard failed correct CSS.
//
// A registration with no `initial-value` still registers the name. With
// `syntax: "*"` that is legal, and `var()` on it is invalid at computed-value
// time, so the declaration draws nothing at all. Recording the name against an
// empty value set says exactly that -- declared, and resolving to nothing that
// could carry a glow -- where omitting it would report the same false positive
// one spelling further along.
//
// Values are accumulated per name rather than last-write-wins, because a
// property is routinely declared several times -- dark, `prefers-color-scheme:
// light`, `[data-theme="light"]` -- and a glow smuggled into just one of those
// blocks is still a glow that ships. `into` lets a caller fold many stylesheets
// into one map without the caller knowing how a name gets recorded.
function customPropertiesIn(css, into = new Map()) {
  const record = (name, value) => {
    if (!name.startsWith('--')) return;
    if (!into.has(name)) into.set(name, new Set());
    if (value !== undefined) into.get(name).add(value);
  };

  const root = typeof css === 'string' ? postcss.parse(css) : css;
  root.walkDecls((decl) => record(decl.prop, decl.value));
  root.walkAtRules('property', (rule) => {
    let initial;
    rule.walkDecls('initial-value', (decl) => { initial = decl.value; });
    record(rule.params.trim(), initial);
  });

  return into;
}

// A registration says more than what a name is worth: it says what a name is
// ALLOWED to be worth. `@property --tint { syntax: "<color>" }` is a promise the
// browser enforces -- a length assigned to that name is invalid at computed-value
// time and never reaches the property -- so `box-shadow: 0 0 var(--tint)` has no
// blur part at all, and reading its `var()` as "a name that could be any radius"
// failed CSS whose radius is provably absent.
//
// Only a syntax whose every alternative is `<color>` earns it. That is a closed
// rule rather than an enumeration of the ones that happen to be lengths: the
// component types CSS can grow are open, so proving "no alternative here is a
// length" by listing lengths would be wrong on the next spec release, while
// proving "every alternative is a colour" stays true. The multipliers are
// stripped because `<color>#` is a comma-separated list of colours and `<color>+`
// a space-separated one -- neither introduces a component that is not a colour.
//
// Anything else -- `*`, `<length>`, `<color> | <length>` -- is simply not proven
// here, and falls through to the classification it had before.
const COLOUR_ONLY_SYNTAX = /^<color>[#+]?$/;

function colourRegistrationsIn(css, into = new Set()) {
  const root = typeof css === 'string' ? postcss.parse(css) : css;
  root.walkAtRules('property', (rule) => {
    let syntax;
    rule.walkDecls('syntax', (decl) => { syntax = decl.value; });
    if (syntax === undefined) return;

    const alternatives = syntax.trim().replace(/^(['"])(.*)\1$/s, '$2').split('|');
    if (alternatives.every((one) => COLOUR_ONLY_SYNTAX.test(one.trim()))) {
      into.add(rule.params.trim());
    }
  });

  return into;
}

export { colourRegistrationsIn, customPropertiesIn };
