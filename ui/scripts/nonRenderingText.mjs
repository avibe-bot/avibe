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

import postcss from 'postcss';
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
// A CSS string body is blanked for the same reason, but on a narrower guarantee
// than the one first written here. "No shadow value is ever quoted, because
// `box-shadow: "0 0 8px red"` is not valid CSS at all" is true of a DECLARATION
// and false of an at-rule prelude: Tailwind's `@source inline("shadow-[0_0_93px_red]")`
// exists precisely to generate a utility from a string, so blanking it hides a
// glow that really does reach the page. In a declaration a string still cannot
// draw light -- it holds a font name, a `url(…)`, or a `content:` spelling a
// declaration out as visible copy, and `content: "/* box-shadow: 0 0 93px red */"`
// renders that text into a pseudo-element and draws nothing.
//
// So the boundary is where the string sits, not that it is quoted. A prelude runs
// from `@` to the `{` or `;` that ends it. Keeping the quotes themselves means the
// parse below still sees where the string was.
//
// Which prelude, though. Treating every at-rule alike was right about `@source`
// and wrong about the rest, because a prelude does one of two opposite things:
// it either GENERATES CSS from its contents, as `@source inline(…)` does, or it
// TESTS a value and draws nothing, as `@supports (box-shadow: 0 0 93px red)`
// does. The second was left intact and read as a declaration, so a feature query
// -- the one construct in CSS whose entire purpose is to name a value without
// applying it -- failed `validate:theme`.
//
// A condition is therefore blanked whole, not merely stripped of its strings.
// Which polarity, though. Listing the GENERATORS and blanking everything else
// was the first answer, and its cost arrived on schedule: `@apply
// shadow-[0_0_93px_red]` generates a declaration from its prelude exactly as
// `@source inline(…)` does, was not on the list, and was blanked through the
// semicolon -- an untokenized glow into the built CSS, past a guard reporting
// all-clear.
//
// The list was the wrong shape, not one entry short. Generators are an OPEN set:
// Tailwind alone has `@source`, `@apply`, `@utility` and `@variant`, and a
// framework can add another next release. Conditions are CLOSED -- the CSS
// conditional rules are `@supports`, `@media` and `@container`, defined by a
// spec rather than by a build tool -- so naming THEM is an enumeration that can
// actually be finished, and an at-rule nobody here has thought of is kept rather
// than blanked. That inverts the residual risk from a silent miss to a loud
// failure, which is the direction this scan has been wrong in every time it was
// wrong.
//
// The name comes from PostCSS rather than from the bytes after the `@`. The
// prefix test it replaces matched `@sourcemap` as `@source`, which is the same
// class of error one level down: a rule about structure answered by comparing
// characters. `{` and `;` survive so the block structure the scan reads
// afterwards is unchanged.
const CONDITION_AT_RULES = new Set(['supports', 'media', 'container']);

// Which `@` characters actually open an at-rule -- asked of a CSS parser rather
// than of the bytes, for the same reason the TypeScript branch below asks the
// compiler. `@` is not a reserved character in a declaration value, so
// `filter: url(logo@2x.png) drop-shadow(0 0 93px red)` carries one that opens
// nothing; reading it as an at-rule blanked the rest of the declaration and took
// a rendered glow with it. `@2x` in a retina asset name is the ordinary way to
// write that, so this was a hole waiting on a filename.
//
// The decomposition is deliberate: PostCSS answers the STRUCTURAL question --
// where an at-rule begins and therefore where a prelude runs -- and the scanner
// below keeps the LEXICAL one, which spans are comments and strings. That
// boundary is where CSS is genuinely simple: `/*` and a quote mean one thing
// each, with none of the ambiguity that makes `/` un-scannable in JavaScript.
//
// PostCSS is not a new dependency, and this is not a new argument. It is a
// direct devDependency, `validate-theme.mjs` already parses every one of these
// files with it to collect custom properties, and this scan therefore read each
// stylesheet twice -- once through a real tree, once through a hand-rolled
// approximation of one. The findings all came from the second. Parse errors are
// deliberately not caught: the same file is parsed unguarded by
// `collectCustomProperties` before this runs, so an unreadable stylesheet
// already fails the scan loudly rather than degrading into a silent miss here.
function atRuleNames(source) {
  const names = new Map();
  postcss.parse(source).walkAtRules((rule) => names.set(rule.source.start.offset, rule.name));
  return names;
}

function blankCssComments(source) {
  const opens = atRuleNames(source);
  let out = '';
  let quote = null;
  let index = 0;
  let prelude = false;
  let condition = false;
  while (index < source.length) {
    const char = source[index];
    if (condition && char !== '{' && char !== ';') {
      out += char === '\n' ? char : ' ';
      index += 1;
      continue;
    }
    if (quote) {
      if (char === '\\') {
        // A line continuation must keep its newline, or every line number after
        // this string shifts by one and the offences point at the wrong place.
        out += prelude ? char : ' ';
        if (index + 1 < source.length) {
          out += prelude || source[index + 1] === '\n' ? source[index + 1] : ' ';
        }
        index += 2;
        continue;
      }
      if (char === quote) { quote = null; out += char; index += 1; continue; }
      out += prelude || char === '\n' ? char : ' ';
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
      if (char === '@' && opens.has(index)) {
        prelude = !CONDITION_AT_RULES.has(opens.get(index));
        condition = !prelude;
      } else if (char === '{' || char === ';') {
        prelude = false;
        condition = false;
      }
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
// The parse, shared by the two callers that need it. Comments are kept apart
// from the other non-rendering spans because one caller wants the boundary and
// the other wants the prose: `glowScale.test.mjs` reads the design annotations
// that document a component's frame, which are comments and never page copy.
// The grammar follows the extension, because TSX is not a superset of TS. In a
// `.ts` file `<T>(x: T) => …` is a generic arrow; read as TSX it is an unclosed
// JSX element, everything after it becomes `JsxText`, and the blanking below
// erases the rest of the file -- a real glow in it renders and is never seen.
// Only `.ts` is narrowed: it is the one extension where the TSX grammar changes
// what the bytes mean, and leaving everything else on TSX keeps JSX readable in
// the files that may contain it.
const scriptKind = (file) =>
  /\.(m|c)?ts$/.test(file) ? ts.ScriptKind.TS : ts.ScriptKind.TSX;

// A regular expression describes a shadow, it never draws one. `/box-shadow:
// 0 0 8px red/` is a PATTERN: the only thing a browser can do with it is test a
// string, and no assignment anywhere turns a regex literal into a declaration.
// So it is prose about CSS by the same argument as a comment, and it joins them
// here rather than being talked out of the scan one call site at a time.
//
// It is also the third node kind in a row that TypeScript already knew about
// and this file had to be told. The parser draws every one of these boundaries
// for free; the cost each round is that the scan below reads bytes, so a span
// the tree has already classified has to be re-classified by hand to keep the
// regexes off it.
const NON_RENDERING_KINDS = new Set([
  ts.SyntaxKind.RegularExpressionLiteral,
]);

// Page copy, whichever of the two ways it is written. `<code>box-shadow: 0 0
// 93px red</code>` is `JsxText`, and `<code>{'box-shadow: 0 0 93px red'}</code>`
// -- the spelling a lint rule pushes you to the moment the text contains a
// brace or a quote -- is a `StringLiteral` inside a `JsxExpression`. They render
// identically and neither can draw light, but only the first was blanked, so
// documentation and diagnostic copy failed `validate:theme` for containing the
// string it was documenting.
//
// The two were one rule all along, and splitting them was the defect: what makes
// a span copy is its POSITION -- a child of a JSX element -- not the node kind
// that happens to carry it there. Asking about position also keeps the answer
// correct where kind cannot be: `className={'shadow-[0_0_93px_red]'}` is the
// same `StringLiteral` in a `JsxExpression`, and it renders, because its
// expression belongs to an attribute rather than to the element's children.
//
// And a position is reached, not adjoined. `{show ? 'box-shadow: …' : ''}` is
// the same copy in the same place, but the literal's parent is the conditional
// rather than the expression, so requiring a DIRECT `JsxExpression` parent
// answered "is this copy" with "is this copy written without a branch" -- and
// failed displayed text for containing the string it was displaying. What has
// to be walked is the chain of operators that hand a value onward unchanged,
// because that chain is exactly how the literal becomes the element's text.
const HANDS_VALUE_ONWARD = (node, parent) => {
  switch (parent.kind) {
    case ts.SyntaxKind.ParenthesizedExpression:
      return true;
    // The branches render; the condition is read.
    case ts.SyntaxKind.ConditionalExpression:
      return node === parent.whenTrue || node === parent.whenFalse;
    case ts.SyntaxKind.BinaryExpression:
      switch (parent.operatorToken.kind) {
        // Either side of a concatenation ends up in the output, and either side
        // of `||` / `??` can be the value that survives.
        case ts.SyntaxKind.PlusToken:
        case ts.SyntaxKind.BarBarToken:
        case ts.SyntaxKind.QuestionQuestionToken:
          return true;
        // `cond && 'text'` renders its right side only; the left is a test.
        case ts.SyntaxKind.AmpersandAmpersandToken:
          return node === parent.right;
        default:
          return false;
      }
    default:
      return false;
  }
};

// Deny by default, which is the safe direction here: an operator this does not
// know stops the walk, so the span stays in the scan and a real glow is still
// caught. Walking through anything at all -- a call, an arrow function, a
// property assignment -- would be the opposite, blanking
// `{items.map((i) => <span style={{ boxShadow: '0 0 93px red' }} />)}` because
// it is somewhere under a JSX child.
const isJsxChild = (node) => {
  if (node.kind === ts.SyntaxKind.JsxText) return true;
  const isLiteral = node.kind === ts.SyntaxKind.StringLiteral
    || node.kind === ts.SyntaxKind.NoSubstitutionTemplateLiteral;
  if (!isLiteral) return false;

  let child = node;
  let parent = node.parent;
  while (parent && HANDS_VALUE_ONWARD(child, parent)) {
    child = parent;
    parent = parent.parent;
  }

  return parent?.kind === ts.SyntaxKind.JsxExpression
    && (parent.parent?.kind === ts.SyntaxKind.JsxElement
      || parent.parent?.kind === ts.SyntaxKind.JsxFragment);
};

// Except in the one element where JSX children are not copy. `<style>` hands
// them to the CSS parser, so `.probe { box-shadow: 0 0 93px red }` written there
// is a declaration that renders -- and blanking it removed a real glow before
// any channel could see it. The parent decides, not the child: this is the same
// question the surrounding module answers everywhere else, asked one node up,
// and the tree already knows the answer.
//
// It climbs rather than checking one parent, because a `<style>` child is
// routinely a string in an expression -- `<style>{'.a { … }'}</style>` -- which
// sits one level deeper than the bare text it used to be written for.
const isStyleElementChild = (node, tree) => {
  for (let up = node.parent; up; up = up.parent) {
    if (up.kind === ts.SyntaxKind.JsxElement) {
      return up.openingElement?.tagName?.getText(tree) === 'style';
    }
  }
  return false;
};

// One parse of one file, shared. `styleWrite.mjs` needs the same tree to answer
// where a style value ends, and parsing is the expensive part of this scan --
// the cache keys on the exact source text, so a caller that hands over the same
// blanked string gets the same tree rather than a second parse of it.
let lastParse = { file: null, source: null, tree: null };

function parseSource(source, file) {
  if (lastParse.file !== file || lastParse.source !== source) {
    lastParse = {
      file,
      source,
      tree: ts.createSourceFile(file, source, ts.ScriptTarget.Latest, true, scriptKind(file)),
    };
  }
  return lastParse.tree;
}

function nonRenderingRanges(source, file) {
  const tree = parseSource(source, file);
  const comments = [];
  const literals = [];
  const collect = (ranges) => {
    for (const range of ranges ?? []) comments.push([range.pos, range.end]);
  };

  const visit = (node) => {
    collect(ts.getLeadingCommentRanges(source, node.getFullStart()));
    collect(ts.getTrailingCommentRanges(source, node.getEnd()));
    if ((NON_RENDERING_KINDS.has(node.kind) || isJsxChild(node)) && !isStyleElementChild(node, tree)) {
      literals.push([node.getStart(tree), node.getEnd()]);
    }
    node.getChildren(tree).forEach(visit);
  };
  visit(tree);

  return { comments, literals };
}

// Every comment in a file, as text. A design annotation is a comment by
// construction, so this is the same boundary read from the other side.
function typeScriptComments(source, file) {
  const seen = new Set();
  return nonRenderingRanges(source, file)
    .comments.filter(([start, end]) => !seen.has(`${start}:${end}`) && seen.add(`${start}:${end}`))
    .map(([start, end]) => source.slice(start, end));
}

function blankTypeScriptComments(source, file) {
  const { comments, literals } = nonRenderingRanges(source, file);
  const blanks = [...comments, ...literals];

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

// The same question at the granularity above bytes: a whole FILE can be text the
// page never sees. A test never enters the bundle, so a fixture asserting on
// `'box-shadow: 0 0 93px red'` documents a value rather than drawing one -- and
// the scan failed on exactly that, which is a lint gate blocking a test for
// containing the string it is testing.
//
// It lives here because "renders" already has a home, and answering it in the
// scan's file loop instead is how this file's own lesson gets relearned: the
// byte-level rule was inline and keyed off `.css` until that turned a rule about
// rendering into a rule about suffixes. Two granularities, one module.
const NON_RENDERING_FILES = /(^|\/)(__tests__|__mocks__)\/|\.(test|spec)\.[cm]?tsx?$/;

const rendersAtAll = (file) => !NON_RENDERING_FILES.test(file.replaceAll('\\', '/'));

export {
  blankCssComments,
  blankTypeScriptComments,
  parseSource,
  rendersAtAll,
  typeScriptComments,
  withoutNonRenderingText,
};
