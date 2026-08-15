// Text a file contains but never renders as CSS.
//
// This scan reads whole source files with regular expressions, so every byte is
// a candidate declaration until something says otherwise. Most bytes are not:
// a comment is removed before the file is a program, JSX text becomes copy on
// the page, and a CSS string is a font name or a `content:` payload. None of
// them can draw light, and reading them as if they could is how a guard starts
// failing correct files.
//
// That failure mode is worse than the one this file exists to prevent. A missed
// glow leaves the tree exactly where it already was; a false positive fails CI
// for a pull request that was never wrong, in a workflow that runs
// `validate:theme` on every change. So the boundary is drawn here, once, for
// every language the scan reads -- it lived inline and applied only to `.css`,
// which made a rule about rendering read as a rule about file suffixes.
//
// Every function preserves length and line breaks exactly, blanking bodies to
// spaces rather than deleting them, so offsets and line numbers computed by the
// caller still point where they did.

import ts from 'typescript';

// A comment renders nothing, so a shadow spelled inside one is prose ABOUT a
// glow, not a glow. Blanking comment bodies -- preserving length and line breaks
// so every offset and line number below still points where it did -- lets a file
// explain itself in the vocabulary it is explaining. index.css earns this
// immediately: the note on the `wire` role has to name `drop-shadow(…)` to say
// why the sized roles cannot be spelled into one.
//
// Widening the scan is what made this necessary and it is worth being clear that
// it is not a hole. A commented-out declaration does not reach the page, so
// nothing can be smuggled through here that could have drawn light. The quote
// tracking is the part that matters: `/*` inside a string starts no comment, and
// blanking from one would hide the real declarations after it.
//
// A CSS string body is blanked for the same reason and on a stronger guarantee:
// no shadow value is ever quoted, because `box-shadow: "0 0 8px red"` is not
// valid CSS at all. So a string can hold a font name, a `url(…)`, or a
// `content:` that spells a declaration out as visible copy -- `content: "/*
// box-shadow: 0 0 93px red */"` renders that text into a pseudo-element and
// draws nothing -- and none of them can hide light. Keeping the quotes
// themselves means the parse below still sees where the string was. This one was
// not in the review; it was found reverse-verifying the comment fix, and it is
// the same class, so it is closed in the same commit rather than in round twelve.
function blankCssComments(source) {
  let out = '';
  let quote = null;
  let index = 0;
  while (index < source.length) {
    const char = source[index];
    if (quote) {
      if (char === '\\') {
        // A line continuation must keep its newline, or every line number after
        // this string shifts by one and the offences point at the wrong place.
        out += ' ';
        if (index + 1 < source.length) out += source[index + 1] === '\n' ? '\n' : ' ';
        index += 2;
        continue;
      }
      if (char === quote) { quote = null; out += char; index += 1; continue; }
      out += char === '\n' ? '\n' : ' ';
      index += 1;
    } else if (char === '"' || char === "'") {
      quote = char;
      out += char;
      index += 1;
    } else if (char === '/' && source[index + 1] === '*') {
      const closed = source.indexOf('*/', index + 2);
      const stop = closed === -1 ? source.length : closed + 2;
      out += source.slice(index, stop).replace(/[^\n]/g, ' ');
      index = stop;
    } else {
      out += char;
      index += 1;
    }
  }
  return out;
}

// The same rule, for the other language this scan reads. Blanking comments in
// stylesheets and then handing `.ts`/`.tsx` to the channels as raw bytes was the
// half-applied inversion of round five wearing a different hat: `// box-shadow:
// 0 0 8px red` in a TypeScript file is prose by exactly the argument the CSS
// branch already accepted, and the declaration channel claimed it, so a source
// comment failed `validate:theme` and -- because the lint workflow runs this --
// would have failed CI for an unrelated PR. A guard that fails on text which
// never renders is worse than one that misses: a miss leaves the tree where it
// was, a false positive blocks work that was never wrong.
//
// TypeScript's own parser draws the boundary rather than a second hand-rolled
// approximation of its grammar. That distinction is the whole lesson of this
// file: `//` inside `'https://x'`, `/*` inside a template literal, a regex
// literal holding either -- each is a spelling a hand-written scanner gets wrong
// on some later round, and each is already decided correctly by the compiler
// that actually reads these files. `typescript` is a direct devDependency here,
// so this is the cheaper implementation as well as the exact one.
//
// JSX text goes with the comments. `<div>box-shadow: 0 0 8px red</div>` renders
// as copy on the page and is never parsed as CSS, so it belongs to the same
// class -- text in a position that cannot draw light -- and is closed here
// rather than left for the round that would have found it next.
function blankTypeScriptComments(source, file) {
  const tree = ts.createSourceFile(file, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const blanks = [];
  const collect = (ranges) => {
    for (const range of ranges ?? []) blanks.push([range.pos, range.end]);
  };

  const visit = (node) => {
    collect(ts.getLeadingCommentRanges(source, node.getFullStart()));
    collect(ts.getTrailingCommentRanges(source, node.getEnd()));
    if (node.kind === ts.SyntaxKind.JsxText) blanks.push([node.getStart(tree), node.getEnd()]);
    node.getChildren(tree).forEach(visit);
  };
  visit(tree);

  // `split('')`, not `[...source]`: the spread iterates code POINTS, while every
  // offset TypeScript hands back counts UTF-16 code UNITS. An emoji anywhere
  // earlier in the file desynchronises the two, and blanking then lands on the
  // wrong characters. Three files in this tree already have one.
  const out = source.split('');
  for (const [start, end] of blanks) {
    for (let index = start; index < end; index += 1) {
      if (out[index] !== '\n') out[index] = ' ';
    }
  }
  return out.join('');
}

// One rule, every extension the scan reads. The conditional this replaces named
// `.css` and let everything else through, which is how a rule about rendering
// became a rule about file suffixes.
function withoutNonRenderingText(source, file) {
  return file.endsWith('.css') ? blankCssComments(source) : blankTypeScriptComments(source, file);
}

export { blankCssComments, blankTypeScriptComments, withoutNonRenderingText };
