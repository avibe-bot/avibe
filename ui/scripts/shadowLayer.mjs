import { isLength, isZeroLength } from './cssLength.mjs';

// Whether a shadow value is a glow drawn by hand.
//
// A glow is a shadow layer drawn at the element's own position: both offsets
// zero, a blur, and an accent colour. Written as a literal it is invisible to
// every assertion in `validate-theme.mjs` -- which is how 72 of them accumulated
// 52 distinct spellings of the same four shapes, and how the eight card washes
// came to retype `--shadow-mint-card`'s value and so keep the dark frame's neon
// on a white page. A token cannot drift that way: it is one value, it is read
// against design.pen, and light re-anchors it in one place. So a glow names a
// token, and this asserts the property rather than listing the spellings,
// because the next literal will be a spelling nobody listed.
//
// Only the glow layer is held to it. An offset shadow is directional light, not
// a glow, and `0 0 0 2px` is a ring -- no blur, nothing to colour-manage.
//
// This lives in its own module for the reason the other four extractions did:
// it is the rule the whole gate is about, and inside `validate-theme.mjs` --
// a script that reads the tree the moment it is imported -- there was no way to
// ask it a question. Ten documented rounds of holes in it were found by a
// reviewer rather than by a test, which is the cost of a rule that cannot be
// called.

// Top-level split, blind to anything inside parentheses. Layers are separated
// by commas in every syntax the scan reads; the parts of one layer are
// separated by spaces in CSS and by `_` in a Tailwind arbitrary value, so one
// splitter reads either and no channel needs its own normalisation step.
function splitTopLevel(value, isSeparator) {
  const parts = [];
  let depth = 0;
  let start = 0;
  for (let i = 0; i < value.length; i += 1) {
    if (value[i] === '(') depth += 1;
    else if (value[i] === ')') depth -= 1;
    else if (depth === 0 && isSeparator(value[i])) {
      parts.push(value.slice(start, i));
      start = i + 1;
    }
  }
  parts.push(value.slice(start));
  return parts.filter((part) => part !== '');
}

const shadowLayers = (value) => splitTopLevel(value, (char) => char === ',');
const layerParts = (layer) => splitTopLevel(layer, (char) => char === '_' || /\s/.test(char));

const CSS_WIDE_KEYWORDS = new Set(['none', 'inherit', 'initial', 'unset', 'revert', 'revert-layer']);
const GLOW_TOKEN = /^--shadow-glow-/;
const VAR_REFERENCE = /^var\(\s*(--[A-Za-z0-9_-]+)\s*(?:,([\s\S]*))?\)$/;
// A length this scan can read is a literal. These two spell the ways one can
// arrive without being readable: computed, or named. Neither is rejected for
// being exotic -- they are rejected because a glow is what they might be.
const MATH_FUNCTION = /\b(calc|min|max|clamp)\s*\(/;
const INDIRECT = /\bvar\s*\(/;
const MAX_INDIRECTION = 8;

// `inset` is a keyword of the shadow grammar, and both of its freedoms are
// freedoms the parser has to grant: the spec allows it before the lengths or
// after the colour, and CSS keywords are ASCII case-insensitive. Removing it
// only in first position, only lowercase, made `box-shadow:
// var(--shadow-glow-md-mint) inset` -- the managed token this whole gate exists
// to require -- read as a two-part hand-written shadow and fail, while the
// identical leading spelling passed. A guard that rejects one word order of the
// thing it is asking for is the false positive that is hardest to argue with.
const INSET = /^inset$/i;

// What a colour LOOKS like, which is a different question from what a blur does
// not look like -- and the difference is the sixth round of this file. The blur
// slot used to be tested by elimination: not a length, not a `var()`, therefore
// harmless. `0 0 ${blur}px red` is neither of those two things and is also a
// glow, so it walked out through the bottom of the test. That is the same
// implicit accept the layer-level default was inverted to close; it had simply
// survived one level further down, inside the branch that does the inverting.
//
// A bare identifier counts because a <length> always carries a digit, so a name
// with none provably is not one. The function list is an enumeration and would
// be a liability on the reject side; here it sits on the ACCEPT side, where a
// colour syntax missing from it costs a spurious failure and a one-line fix
// rather than a glow that ships. Order matters: `color-mix(…, var(--x) …)` is a
// colour that happens to contain a name, so this is asked before INDIRECT.
//
// That one-line fix has now been called in, which is the enumeration behaving as
// designed rather than failing: `box-shadow: 0 0 light-dark(red, blue)` is a
// valid blur-free shadow, and an unlisted colour function put the colour in the
// blur slot and blocked it. The names below are CSS Color's functional
// notations as they stand; the cost of the next one arriving is exactly this
// again -- a spurious failure, loudly, rather than a glow nobody sees.
export const COLOUR = new RegExp(
  '^(#[0-9A-Fa-f]{3,8}'
  + '|[A-Za-z][A-Za-z0-9-]*'
  + '|(rgba?|hsla?|hwb|lab|lch|oklab|oklch|color|color-mix|light-dark|contrast-color|device-cmyk)'
  + '\\([\\s\\S]*\\))$',
  'i',
);

// The classification is DENY BY DEFAULT, and that is the whole point of it.
// Three review rounds each found another spelling that fell past a check built
// to recognise glows and reject them -- an unlisted colour syntax, an unread
// input channel, hand-picked geometry behind a managed colour -- because
// anything the parser failed to recognise landed in an implicit "accept". A
// fourth then found the cheapest fall-through of all: `shadow-[var(--anything)]`
// is a single part, so it had no offsets to test and was waved through, which
// let `--rogue-glow: 0 0 93px var(--mint)` ship a hand-drawn glow behind a name.
// Widening the recogniser a fourth time would just relocate the gap, so the
// default is inverted instead: every layer must land in a form named here, and
// a layer this scan cannot classify FAILS asking to be made legible. A name is
// not taken at face value either -- indirection is resolved and the same test
// runs on what it resolves to, so a glow cannot hide one alias deeper.
// `!important` is a property of the DECLARATION, not of any layer in it, so it
// is dropped here rather than in the six channels that would each have to
// remember. This is the fact round ten already established -- `setProperty('box-
// shadow', 'none', 'important')` had swept the priority up as a second layer --
// taught to the branch that had not heard it: written in CSS instead of in
// CSSOM, `box-shadow: var(--shadow-glow-md-mint) !important` parsed `!important`
// as a layer part and failed correct, fully tokenized CSS. A guard that rejects
// the very spelling it is asking for is worse than one that misses.
const IMPORTANT = /\s*!\s*important\s*$/i;

// `tokens` is what the stylesheets declare, gathered once by the caller:
// `values` maps a custom property to every value declared for it, `managed` to
// the values declared inside an `@theme` block, and `colours` holds the names
// registered as `<color>` by `@property`.
export function glowOffencesInValue(value, tokens, seen = new Set(), depth = 0) {
  return shadowLayers(value.replace(IMPORTANT, '')).flatMap((layer) =>
    glowOffencesInLayer(layer, tokens, seen, depth)
  );
}

function glowOffencesInLayer(layer, tokens, seen, depth) {
  const parts = layerParts(layer).filter((part) => !INSET.test(part));
  const shown = layer.trim();

  if (parts.length === 1) {
    const only = parts[0];
    if (CSS_WIDE_KEYWORDS.has(only.toLowerCase())) return [];
    const reference = only.match(VAR_REFERENCE);
    if (!reference) return [`${shown} -- not a length triple, a keyword or a var() this scan can read`];
    const [, name, fallback] = reference;
    if (depth >= MAX_INDIRECTION) return [`${shown} -- indirection deeper than ${MAX_INDIRECTION} hops`];
    if (seen.has(name)) return [];
    const declared = tokens.values.get(name);
    if (!declared) return [`${shown} -- ${name} is declared in no scanned stylesheet, so its value cannot be checked`];
    const next = new Set(seen).add(name);
    // The sanctioned home for a shadow literal, and the only stop condition
    // here: a managed glow token is read against design.pen and re-anchored for
    // light in one place, which is exactly what a call site cannot do. It stops
    // the recursion, not the check -- the name still has to exist, and a
    // fallback beside it is a second value that renders whenever it does not,
    // so the fallback is classified even when the token it guards is managed.
    //
    // Managed is a PLACE, not a prefix. This used to stop on the name matching
    // `--shadow-glow-`, which let anything satisfy the check by naming itself
    // after the thing being checked: `--shadow-glow-rogue: 0 0 93px red`
    // declared in any component stylesheet was resolved, found, and then
    // deliberately discarded unread. Requiring the declaration to live in an
    // `@theme` block asks the question the prefix was standing in for, because
    // that block is what design.pen is read against -- and a name that only
    // looks managed now falls through to the ordinary classification, where its
    // geometry is judged like any other literal.
    //
    // The sanction attaches to the DECLARATION, not to the name: subtracting
    // the `@theme` values from everything collected for this name leaves
    // exactly the declarations made somewhere else, and those are classified.
    // A token declared only in `@theme` subtracts to nothing and stops the
    // recursion as before; a managed name redeclared in a component stylesheet
    // keeps the override in the list, which is what the cascade does too.
    const sanctioned = GLOW_TOKEN.test(name) ? tokens.managed.get(name) : undefined;
    const resolved = sanctioned ? [...declared].filter((value) => !sanctioned.has(value)) : [...declared];
    const deeper = [...resolved, ...(fallback ? [fallback] : [])]
      .flatMap((declaredValue) => glowOffencesInValue(declaredValue, tokens, next, depth + 1));
    return deeper.map((offence) => `${offence}  <- reached through ${shown}`);
  }

  // CSS lets the colour lead instead of trail, so move a leading non-length to
  // the back and let the offsets line up. This used to exempt a leading `var()`,
  // which quietly reopened the same hole from the other end: `var(--mint) 0 0
  // 93px` kept its colour in the offset slot and drew whatever geometry it liked.
  if (!isLength(parts[0])) parts.push(parts.shift());

  // A multi-part layer passes only if it can be shown NOT to be a glow. That is
  // the same inversion the single-part branch makes, and this branch was left
  // out of it -- the check still asked "is this a glow" and accepted silence as
  // a no. `calc(0px) calc(0px) 93px red` is what that costs: both offsets
  // compute to zero, neither is the literal `0` a zero-test looks for, so the
  // question answers no and a hand-drawn glow ships. Asked the other way round,
  // an offset this scan cannot evaluate has no innocent answer to give.
  if (parts.some((part) => MATH_FUNCTION.test(part))) {
    return [`${shown} -- a computed length cannot be evaluated here, so this cannot be shown not to be a glow`];
  }
  const [x, y, third] = parts;
  if (!isLength(x ?? '') || !isLength(y ?? '')) {
    return [`${shown} -- the offsets are not plain lengths, so this cannot be shown not to be a glow`];
  }
  // A non-zero offset is directional light whatever the blur does, and that is
  // provable from the offset alone.
  if (!isZeroLength(x) || !isZeroLength(y)) return [];
  // Both offsets are zero, so the third part decides, and it has to prove itself
  // the same way the layer does. Absent means there is no blur part at all; a
  // colour means the same thing, since a layer whose third part is its colour
  // skipped the blur -- that is the ring-spacer shape. A name is not an answer:
  // it could hold any radius, so it is unprovable rather than innocent. Anything
  // else is unreadable and fails, which is the point -- this branch used to end
  // in a bare `return []`, and every spelling that reached it was accepted for
  // no better reason than that the two tests above had not recognised it.
  if (third === undefined) return [];
  if (isLength(third)) return isZeroLength(third) ? [] : [shown];
  if (COLOUR.test(third)) return [];
  // A name is unprovable only while it could be a length. `@property --tint
  // { syntax: "<color>" }` removes that possibility at the source: the browser
  // rejects a length assigned to it, so the slot holds a colour and the layer
  // has no blur part at all -- the same ring-spacer shape the line above already
  // accepts, written with a registered name instead of a literal. Asking the
  // registration is what turns "could be any radius" from a fact about the
  // spelling into a fact about the value.
  const registered = third.match(VAR_REFERENCE);
  if (registered && tokens.colours.has(registered[1]) && !registered[2]) return [];
  if (INDIRECT.test(third)) {
    return [`${shown} -- a name in the blur slot could be any radius, so this cannot be shown not to be a glow`];
  }
  return [`${shown} -- the blur slot is neither a length nor a colour this scan can read, so this cannot be shown not to be a glow`];
}
