// Where a stylesheet DECLARES something, as offsets into the text it was read
// from.
//
// The scan this serves reads bytes, so it asked "is this a declaration?" with a
// regex: a shadow property name followed by a colon. A property name followed by
// a colon is also a pseudo-class, and `.box-shadow:hover { color: red }` is a
// perfectly ordinary selector -- so the scan handed `hover { color: red` to the
// shadow classifier and failed a stylesheet that draws no shadow at all. That is
// the direction that costs a pull request rather than a glow.
//
// A tighter regex is not the fix, because the question is not about spelling. A
// declaration is a POSITION in a grammar, and the parser that already reads
// these files decides it for free -- a selector is a Rule, a declaration is a
// Decl, and no amount of punctuation can make one look like the other. The same
// argument `nonRenderingText.mjs` makes for TypeScript's parser, one language
// over.
//
// Offsets rather than values, because the caller is a byte scan: it has already
// matched, and what it needs to know is whether the place it matched is a place
// CSS declares anything.

import postcss from 'postcss';

// `offset` and `into` let a caller fold several stretches of CSS into one list
// while keeping the coordinates of the file they came from -- a `<style>` child
// is CSS whose offsets are its position in a `.tsx` file, not in itself.
//
// Unparseable text keeps its whole range. That is the deny-by-default direction:
// this module exists to prove a match is NOT in declaration position, and text
// no parser can read proves nothing, so it stays exactly as scannable as it was
// before this file existed.
function declarationSpansIn(css, offset = 0, into = []) {
  let root;
  try {
    root = postcss.parse(css);
  } catch {
    if (css.length > 0) into.push([offset, offset + css.length]);
    return into;
  }

  root.walkDecls((decl) => {
    into.push([offset + decl.source.start.offset, offset + decl.source.end.offset]);
  });
  return into;
}

const declaresAt = (spans, index) => spans.some(([start, end]) => index >= start && index < end);

export { declarationSpansIn, declaresAt };
