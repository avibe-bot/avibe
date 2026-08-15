// Where a style write starts, and where its value ends.
//
// Two questions the scan asks of JavaScript, both of which it used to answer by
// reading the bytes around a match, and both of which produced a false positive
// in the same review round: a name ending in `Shadow` was read as a style
// property when it was a variable, and a value written on the line below its key
// was read as absent when it was a token. This module is the pair of answers,
// extracted for the reason `cssLength.mjs` was: `validate-theme.mjs` runs its
// whole validation at import time, so nothing inside it can be called from a
// test, and an assertion about a regex's SOURCE is not an assertion about its
// behaviour. What lives here can be reverse-verified by introducing the defect
// it names and watching a test fail.
//
// The boundary is deliberately not "everything about style writes". It is the
// two spans -- the key and the value -- that the scan cannot locate correctly by
// eye, kept next to each other because a change to one is nearly always a change
// to the other.

// A style write's TARGET, not its name. `box-shadow`, `text-shadow` and
// `drop-shadow` all end in the word, so requiring the word at the tail of an
// identifier looked like it separated properties from variables -- and it does
// not: `const cardShadow = 'compact'` ends in the word too, and was read as a
// style property, classified as a shadow layer, and failed `validate:theme` for
// a variable that never reaches a browser.
//
// The difference is not in the name at all. A property in an object literal is a
// candidate style key, because `style={{ boxShadow: … }}` is the whole reason
// the channel exists; an ASSIGNMENT reaches the browser through exactly one
// door, `element.style`, spelled `.style.boxShadow =` or `.style['boxShadow'] =`.
// An assignment to a bare identifier is a variable, and a variable is not a
// declaration however it is named.
//
// The gap this admits -- `const s = el.style; s.boxShadow = glow` -- is a MISS,
// and that direction is settled: a miss leaves the tree where it already was,
// while a false positive fails a pull request that was correct.
const STYLE_ASSIGNMENT = String.raw`(?<=\.style\s*[.[]\s*['"\`]?)`;

// The JS spelling of the key, up to but not including the punctuation that hands
// it a value. The colon and the equals sign take different prefixes now, so the
// name they share has to be one string rather than two that agree today.
const JS_SHADOW_KEY = String.raw`(?<![\w-])[A-Za-z]*[Ss]hadow(?![A-Za-z])['"]?\s*\]?\s*`;

// The two spellings, composed. `{ boxShadow: … }` and `el.style.boxShadow = …`
// are one channel to everything downstream, and this is the only place that
// knows they are written differently.
const SHADOW_KEY = `${JS_SHADOW_KEY}:|${STYLE_ASSIGNMENT}${JS_SHADOW_KEY}=`;

// The expression beginning at `start`, ending at the first terminator that is
// not nested inside a call, a bracket or a string. Null when it never
// terminates, so an unreadable expression stays loud instead of quietly yielding
// nothing.
//
// A terminator only ends an expression that has BEGUN. `\n` is a terminator for
// a property because a statement can end without a semicolon, but a value is
// free to start on the line after its key -- `boxShadow:\n  'var(--shadow-glow-
// md-mint)'` is what a formatter produces once the line is long enough -- and
// reading that newline as the end returned an empty expression, no string
// literals, and "this scan cannot read it" for a site spelling the required
// token correctly. Leading whitespace is skipped rather than terminating, which
// costs the call-argument caller nothing: whitespace is not in its set at all.
function expressionUpTo(source, start, terminators) {
  let depth = 0;
  let quote = null;
  let begun = false;
  for (let index = start; index < source.length; index += 1) {
    const char = source[index];
    if (!begun && !quote) {
      if (/\s/.test(char)) continue;
      begun = true;
    }
    if (quote) {
      if (char === '\\') index += 1;
      else if (char === quote) quote = null;
      continue;
    }
    if (char === "'" || char === '"' || char === '`') quote = char;
    else if (char === '(' || char === '[' || char === '{') depth += 1;
    else if (depth === 0 && terminators.includes(char)) return source.slice(start, index);
    else if (char === ')' || char === ']' || char === '}') depth -= 1;
  }
  return null;
}

// A call argument ends at the comma before the next one, or at the paren that
// closes the call. `setProperty` takes a third argument, `priority`, and reading
// "every string literal after the property name" swept it up as a second layer
// -- so the entirely valid `setProperty('box-shadow', 'none', 'important')`
// accepted `none` and then failed on `important`.
const valueArgument = (source, start) => expressionUpTo(source, start, ',)');

// A style property's own expression ends where the next property begins, or
// where the object or statement does. Reading `[^;\n]*` instead -- everything up
// to the end of the line -- was the same mistake the `priority` argument had
// already taught this scan once: `style={{ boxShadow: 'none', color: 'red' }}`
// swept the NEXT property up as a second shadow layer, accepted `none`, and then
// failed `validate:theme` on `red`. Valid, glow-free UI code, blocked by the
// glow guard.
const propertyExpression = (source, start) => expressionUpTo(source, start, ',;}\n');

export { JS_SHADOW_KEY, SHADOW_KEY, STYLE_ASSIGNMENT, propertyExpression, valueArgument };
