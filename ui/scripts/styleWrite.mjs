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

// The properties a shadow can be written to, spelled every way JavaScript
// spells them. This is a CLOSED set and closed for a reason that is not a
// judgement call: a style object and `element.style` both address real CSS
// properties, so a key that is not one of these draws nothing whatever it is
// called. `const opts = { cardShadow: 'compact' }` is a variable-shaped object
// that happens to end in the word, and reading it as a declaration failed
// `validate:theme` on `compact` -- a false positive, in CI, on code with no CSS
// in it at all.
//
// The rule it replaces was `[A-Za-z]*[Ss]hadow`: "the name ends in the word".
// That is a guess about intent, and every round of this review has found another
// name that satisfies it without being a property. The CSS spec's list does not
// have that problem -- it is finite, published, and changes on a timescale where
// a missed addition is a MISS rather than a false positive.
//
// `filter` and `backdrop-filter` are deliberately NOT here, even though
// `drop-shadow()` reaches the page through them. Adding them would widen this
// channel onto every ordinary `{ filter: 'blur(2px)' }` in the tree and ask it
// to classify a value that is not a shadow at all -- trading the false positive
// below for a larger one. `drop-shadow(` is already counted and read where it is
// unambiguous: by the function name, one channel over.
const SHADOW_PROPERTIES = ['box-shadow', 'text-shadow', '-webkit-box-shadow', '-moz-box-shadow'];

// Every spelling of each, because the same property is written three ways
// depending on where it is: `box-shadow` in a stylesheet or in
// `element.style['box-shadow']`, `boxShadow` in a style object, and
// `WebkitBoxShadow` for the vendor-prefixed pair, whose JS name capitalises the
// prefix rather than dropping it. Sorted longest-first so `box-shadow` cannot
// match as the tail of `-webkit-box-shadow` and leave the prefix outside the
// match.
const camel = (property) => property.replace(/^-/, '').replace(/-(\w)/g, (_, next) => next.toUpperCase());
const capitalised = (name) => name[0].toUpperCase() + name.slice(1);

const IDENTIFIER_NAMES = [
  ...new Set(SHADOW_PROPERTIES.map((property) => {
    const js = camel(property);
    return property.startsWith('-') ? capitalised(js) : js;
  })),
];

const SHADOW_PROPERTY_NAMES = [...new Set([...SHADOW_PROPERTIES, ...IDENTIFIER_NAMES])]
  .sort((a, b) => b.length - a.length);

// The JS spelling of the key, up to but not including the punctuation that hands
// it a value. The colon and the equals sign take different prefixes now, so the
// name they share has to be one string rather than two that agree today.
//
// Two spellings, and the quote is what separates them rather than decorating
// them. A hyphenated name is not a JavaScript identifier, so `box-shadow` can
// only ever be a key QUOTED -- `el.style['box-shadow']`, `{'box-shadow': …}` --
// and a bare one is CSS. Accepting the hyphenated names bare read every
// `box-shadow:` declaration in every stylesheet as a JS style write, handed it to
// a channel that reads JavaScript expressions, and got nothing back: four
// correct declarations reported as values the scan cannot read. The quote is not
// a detail of how the key is written; it is the whole difference between the two
// languages this scan reads.
//
// The delimiter accepts all three of JavaScript's string quotes. That is an
// enumeration, and it is a safe one for the same reason the property list is: the
// language has exactly three, so a backtick-quoted computed key is not the next
// spelling nobody imagined, it is the last one. Missing it let a hardcoded glow
// ship with every guard green.
const QUOTE = String.raw`['"\x60]`;

const JS_SHADOW_KEY = String.raw`(?<![\w-])(?:(?:${IDENTIFIER_NAMES.join('|')})(?![\w-])${QUOTE}?`
  + String.raw`|(?<=${QUOTE})(?:${SHADOW_PROPERTY_NAMES.join('|')})(?![\w-])${QUOTE})\s*\]?\s*`;

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

// A style property's own expression, ended by the compiler.
//
// Every previous answer here was a rule about punctuation, and each one was
// right about the case that prompted it and wrong about the next. `[^;\n]*` swept
// the following property up as a second layer, so `style={{ boxShadow: 'none',
// color: 'red' }}` failed on `red`. Adding `,` and `}` fixed that and left `\n`
// behind to stop a runaway, so a value a formatter wrapped onto the next line
// read as absent. Removing `\n` fixes the wrap and reopens the runaway: a
// semicolon-less assignment swallows the statements after it.
//
// That last pair does not have a punctuation answer. Whether a newline ends a
// statement is JavaScript's automatic semicolon insertion, which is defined in
// terms of the parse -- `x = a\n ? b : c` continues because `?` cannot begin a
// statement, and `x = a\n const y = 1` does not. Any scanner that decides it by
// looking at characters is re-implementing the grammar, badly, one review round
// at a time. Six rounds of this file's history are that re-implementation.
//
// So the question goes to the parser that has already read the file. The tree is
// the enumeration: it knows where the expression that starts here ends, for
// every spelling at once, including the ones nobody has written yet. The
// outermost node starting at the value is the whole value -- at `a ? b : c` both
// `a` and the conditional start on the same character, and it is the conditional
// that was assigned.
//
// The tree and the text are deliberately two arguments rather than one parse of
// one string. The tree is the file AS WRITTEN, because blanking a regex literal
// to spaces leaves `const re =      ;`, which is no longer a program and parses
// into error nodes whose boundaries answer nothing. The text is the file with
// its non-rendering spans removed, because a value is allowed to carry a comment
// -- `boxShadow: /* red */ 'none'` -- and reading the comment back would hand a
// colour to the caller that never renders. Offsets agree between the two by
// construction: every blank in `nonRenderingText.mjs` preserves length.
function propertyExpression(source, start, tree) {
  if (!tree) return null;
  const begin = source.slice(start).search(/\S/);
  if (begin < 0) return null;
  const at = start + begin;

  let end = -1;
  const visit = (node) => {
    const nodeStart = node.getStart(tree);
    if (nodeStart > at || node.getEnd() <= at) {
      if (nodeStart <= at) node.getChildren(tree).forEach(visit);
      return;
    }
    if (nodeStart === at) end = Math.max(end, node.getEnd());
    node.getChildren(tree).forEach(visit);
  };
  visit(tree);

  return end < 0 ? null : source.slice(start, end);
}

export {
  JS_SHADOW_KEY,
  SHADOW_KEY,
  SHADOW_PROPERTIES,
  SHADOW_PROPERTY_NAMES,
  STYLE_ASSIGNMENT,
  propertyExpression,
  valueArgument,
};
