import fs from 'node:fs';
import { fileURLToPath } from 'node:url';

import postcss from 'postcss';
import { describe, expect, it } from 'vitest';

import { intendedFiles } from './lintPolicy.mjs';
import {
  cssBearingRangesIn,
  cssRangesIn,
  CSS_BEARING_ATTRIBUTES,
  CSS_TEXT_SINKS,
  rendersAtAll,
  withoutNonRenderingText,
} from './nonRenderingText.mjs';
import { WHOLE_TREE_SCAN } from './wholeTreeScan.mjs';

// `validate:theme` reads whole source files with regular expressions, so the set
// of bytes it treats as CSS is decided here and nowhere else. Ten review rounds
// on that scan were misses -- a spelling it walked past. The eleventh was the
// opposite, and worse: it failed on `// box-shadow: 0 0 8px red`, a comment,
// which would have failed CI for a pull request that was correct. A miss leaves
// the tree where it already was; a false positive blocks work that was never
// wrong.
//
// So these cases are the property, stated in both directions. A position that
// cannot render must be blanked, and -- the half that keeps the first half
// honest -- a position that CAN render must survive intact right next to it.
// Blanking too much would silence the whole guard while every case above it
// still passed.

const UI_ROOT = fileURLToPath(new URL('../', import.meta.url));

// A glow no role in the scale carries, so its presence in the output is only
// ever the fixture's own.
const GLOW = '0 0 93px red';

const NON_RENDERING = [
  ['a TypeScript line comment', 'probe.ts', `const a = 1; // box-shadow: ${GLOW}\n`],
  ['a TypeScript block comment', 'probe.ts', `/* box-shadow: ${GLOW} */\nconst a = 1;\n`],
  ['a JSX comment', 'probe.tsx', `const a = <div>{/* box-shadow: ${GLOW} */}</div>;\n`],
  ['JSX text', 'probe.tsx', `const a = <div>box-shadow: ${GLOW}</div>;\n`],
  // Page copy, spelled as an expression. What makes a span copy is its POSITION
  // -- a child of a JSX element -- and not the node kind, so listing `JsxText`
  // and leaving the braces around it alone blanked one spelling of one thing.
  ['a string a JSX element renders as copy', 'probe.tsx', `const a = <div>{'box-shadow: ${GLOW}'}</div>;\n`],
  // The same copy, in the same place, reached through the operators that hand a
  // value onward. Requiring the literal's DIRECT parent to be the expression
  // asked whether the copy was written without a branch, which is not a
  // question about whether it renders as text -- and every one of these failed
  // `validate:theme` for displaying the string it was documenting.
  ['copy in a conditional branch', 'probe.tsx', `const a = <div>{x ? 'box-shadow: ${GLOW}' : ''}</div>;\n`],
  ['copy in the other conditional branch', 'probe.tsx', `const a = <div>{x ? '' : 'box-shadow: ${GLOW}'}</div>;\n`],
  ['copy guarded by &&', 'probe.tsx', `const a = <div>{x && 'box-shadow: ${GLOW}'}</div>;\n`],
  ['copy defaulted with ??', 'probe.tsx', `const a = <div>{x ?? 'box-shadow: ${GLOW}'}</div>;\n`],
  ['copy concatenated with a value', 'probe.tsx', `const a = <div>{'box-shadow: ${GLOW}' + x}</div>;\n`],
  ['copy inside parentheses', 'probe.tsx', `const a = <div>{('box-shadow: ${GLOW}')}</div>;\n`],
  ['copy in a fragment', 'probe.tsx', `const a = <>{x ? 'box-shadow: ${GLOW}' : ''}</>;\n`],
  // A pattern, not a value. Nothing assigns a regex literal to a style, so it
  // describes CSS in exactly the sense a comment does.
  ['a regular expression literal', 'probe.ts', `export const RE = /box-shadow: ${GLOW}/;\n`],
  // A feature query names a value to ask whether the browser understands it.
  // It is the one construct in CSS whose entire purpose is to write a
  // declaration that does not apply.
  ['an @supports condition', 'probe.css', `@supports (box-shadow: ${GLOW}) {\n  .a { color: red; }\n}\n`],
  ['a @media condition', 'probe.css', '@media (min-width: 93px) {\n  .a { color: red; }\n}\n'],
  ['a @container condition', 'probe.css', '@container (min-width: 93px) {\n  .a { color: red; }\n}\n'],
  ['a CSS comment', 'probe.css', `/* box-shadow: ${GLOW} */\n.a { color: red; }\n`],
  ['a CSS string', 'probe.css', `.a { content: "box-shadow: ${GLOW}"; }\n`],
  ['a comment opener inside a CSS string', 'probe.css', `.a { content: "/* box-shadow: ${GLOW} */"; }\n`],
];

const RENDERING = [
  ['a Tailwind arbitrary value', 'probe.tsx', 'const a = <div className="shadow-[0_0_93px_red]" />;\n', '0_0_93px_red'],
  // The other half of the position rule. This expression is a child of an
  // ATTRIBUTE, not of an element, so it is a class name on its way to Tailwind
  // rather than words on a page.
  ['a class name written as an expression', 'probe.tsx', "const a = <div className={'shadow-[0_0_93px_red]'} />;\n", '0_0_93px_red'],
  // Walking to the JSX position rather than checking one parent has to keep
  // that half true: an attribute is still an attribute at the end of a branch.
  ['a class name chosen by a conditional', 'probe.tsx', "const a = <div className={x ? 'shadow-[0_0_93px_red]' : ''} />;\n", '0_0_93px_red'],
  // The limit of the walk, and the reason it is an allowlist. This literal sits
  // under a JSX child, but a call and an arrow function stand between them, and
  // what comes out the other end is an element that draws rather than text.
  ['a style inside a mapped element', 'probe.tsx', `const a = <div>{xs.map((x) => <span style={{ boxShadow: '${GLOW}' }} />)}</div>;\n`, GLOW],
  // A condition is read, not rendered. Blanking it would erase a declaration
  // that a comparison merely happens to mention next to one that draws.
  ['a glow tested in a condition', 'probe.tsx', `const a = <div>{s === 'box-shadow: ${GLOW}' ? 'y' : 'n'}</div>;\n`, GLOW],
  ['an inline style object', 'probe.tsx', `const a = <div style={{ boxShadow: '${GLOW}' }} />;\n`, GLOW],
  ['a template literal', 'probe.ts', `const a = { boxShadow: \`${GLOW}\` };\n`, GLOW],
  ['a CSSOM setter', 'probe.ts', `el.style.setProperty('box-shadow', '${GLOW}');\n`, GLOW],
  ['a CSS declaration', 'probe.css', `.a { box-shadow: ${GLOW}; }\n`, GLOW],
  ['a declaration after a comment on the same line', 'probe.css', `/* note */ .a { box-shadow: ${GLOW}; }\n`, GLOW],
  ['a declaration after a string on the same line', 'probe.css', `.a { content: "x"; box-shadow: ${GLOW}; }\n`, GLOW],
  ['a URL whose // must not open a comment', 'probe.ts', `const u = 'https://x//y'; const a = { boxShadow: '${GLOW}' };\n`, GLOW],
  // Blanking regex literals is only safe because the parser decides which
  // slashes open one. A hand-rolled scanner reads `/ 2; const a = { boxShadow: '`
  // as a regex and erases the declaration after it.
  ['a division that is not a regex', 'probe.ts', `const n = 1 / 2; const a = { boxShadow: '${GLOW}' };\n`, GLOW],
  // Blanking a condition must stop at the brace. The rule inside an @supports
  // block renders exactly like any other rule.
  ['a rule inside an @supports block', 'probe.css', `@supports (display: grid) {\n  .a { box-shadow: ${GLOW}; }\n}\n`, GLOW],
  // The at-rules that GENERATE CSS are an open set -- Tailwind adds to it, and
  // the next release will add more -- while the ones that TEST it are the three
  // above. Listing the generators to keep meant every at-rule nobody had listed
  // was silently blanked, taking a real glow with it; naming the conditions
  // instead makes that residual risk a loud failure rather than a quiet miss.
  ['a utility pulled in by @apply', 'probe.css', '.a {\n  @apply shadow-[0_0_93px_red];\n}\n', '0_0_93px_red'],
  // The one element where JSX text is not page copy. `<style>` hands its
  // children to the CSS parser, so blanking them removed a declaration that
  // really does draw light.
  ['CSS inside a JSX style element', 'probe.tsx', `const a = <style>{'.a'} {'{'} box-shadow: ${GLOW} {'}'}</style>;\n`, GLOW],
  // `@` is an ordinary character in a declaration value, and `@2x` is how a
  // retina asset is named. Reading it as an at-rule opener blanked the rest of
  // the declaration and took the glow beside it along.
  ['a glow beside an @ inside url()', 'probe.css', `.a { filter: url(logo@2x.png) drop-shadow(${GLOW}); }\n`, GLOW],
];

describe('withoutNonRenderingText', () => {
  it.each(NON_RENDERING)('blanks %s', (_label, file, source) => {
    expect(withoutNonRenderingText(source, file)).not.toContain('93px');
  });

  it.each(RENDERING)('leaves %s intact', (_label, file, source, rendered) => {
    expect(withoutNonRenderingText(source, file)).toContain(rendered);
  });

  // Every offence the scan reports is located by slicing the source up to a
  // match index, so a blanked file that is one character shorter than its
  // original moves every line number after it. Blanking replaces bodies with
  // spaces for exactly this reason, and the property is asserted over the real
  // tree rather than over fixtures: three files here contain astral characters,
  // whose UTF-16 code units are what TypeScript's offsets count and what a
  // code-point-wise rewrite silently loses.
  it('preserves length and line breaks across every scanned file', () => {
    const drifted = [];

    for (const relative of intendedFiles(UI_ROOT, { extensions: ['.ts', '.tsx', '.css'] })) {
      const raw = fs.readFileSync(new URL(relative, new URL(UI_ROOT, 'file:')), 'utf8');
      const blanked = withoutNonRenderingText(raw, relative);
      if (blanked.length !== raw.length || blanked.split('\n').length !== raw.split('\n').length) {
        drifted.push(relative);
      }
    }

    expect(drifted).toEqual([]);
  }, WHOLE_TREE_SCAN);

  // TSX is not a superset of TS. `<T>(x: T) => …` is a generic arrow in a `.ts`
  // file and an unclosed JSX element in a `.tsx` one, so reading every file with
  // the TSX grammar turns the rest of that file into `JsxText` -- and blanking
  // then erases a real glow further down before the scan can ever see it. This
  // is the same defect as reading a comment with the wrong language, arriving
  // through the parser's configuration instead of through a hand-rolled regex.
  it('reads each file with the grammar its extension implies', () => {
    const source = 'const fn = <T>(x: T) => x;\nconst s = { boxShadow: "0 0 93px red" };\n';

    expect(withoutNonRenderingText(source, 'probe.ts')).toContain('93px');
  });

  // Where the string sits, not that it is quoted. A quoted value cannot draw
  // light in a declaration -- `box-shadow: "0 0 8px red"` is not valid CSS at
  // all -- but an at-rule prelude is exactly where Tailwind's `@source inline(…)`
  // generates a utility from such a string, and blanking it hides a glow that
  // does reach the page.
  it('keeps strings an at-rule can turn into CSS, and blanks the ones that render as copy', () => {
    const prelude = '@source inline("shadow-[0_0_93px_red]");\n';
    const declaration = '.a { content: "box-shadow: 0 0 93px red"; }\n';

    expect(withoutNonRenderingText(prelude, 'probe.css')).toContain('93px');
    expect(withoutNonRenderingText(declaration, 'probe.css')).not.toContain('93px');
  });

  // Which `@` opens an at-rule is a question about CSS structure, and the two
  // spellings are indistinguishable byte by byte. Asking the parser is what
  // separates them; the alternative is one more hand-rolled rule about where an
  // `@` is allowed to mean something, which is the shape every finding on this
  // scan has taken.
  it('treats @ as an at-rule opener only where an at-rule actually begins', () => {
    const atRule = '@supports (box-shadow: 0 0 93px red) {\n  .a { color: red; }\n}\n';
    const inValue = '.a { filter: url(logo@2x.png) drop-shadow(0 0 93px red); }\n';

    expect(withoutNonRenderingText(atRule, 'probe.css')).not.toContain('93px');
    expect(withoutNonRenderingText(inValue, 'probe.css')).toContain('93px');
  });

  // An at-rule prelude either generates CSS or tests it, and the two want
  // opposite treatment. Getting `@source` right by keeping every prelude was
  // what let a feature query through as a declaration.
  it('keeps a generator prelude and blanks a condition', () => {
    const generator = '@source inline("shadow-[0_0_93px_red]");\n';
    const condition = '@supports (box-shadow: 0 0 93px red) {\n  .a { color: red; }\n}\n';

    expect(withoutNonRenderingText(generator, 'probe.css')).toContain('93px');
    expect(withoutNonRenderingText(condition, 'probe.css')).not.toContain('93px');
  });

  it('is applied to every extension the scan reads', () => {
    const scan = fs.readFileSync(new URL('validate-theme.mjs', import.meta.url), 'utf8');
    const extensions = scan.match(/extensions:\s*\[([^\]]*)\]/g) ?? [];
    const scanned = extensions.at(-1) ?? '';

    // The bug this replaces was a conditional naming one suffix, which turned a
    // rule about rendering into a rule about file names. If the scan grows an
    // extension, the dispatcher has to have an opinion about it.
    for (const extension of scanned.match(/'\.\w+'/g) ?? []) {
      const probe = `probe${extension.slice(1, -1)}`;
      expect(withoutNonRenderingText('/* box-shadow: 0 0 93px red */\n', probe)).not.toContain('93px');
    }
  });
});

// The same question one granularity up: a file the bundler never reaches draws
// nothing, whatever it contains. A test asserting on `'box-shadow: 0 0 93px
// red'` documents a value; scanning it turns the gate into one that fails a
// test for containing the string it tests.
describe('rendersAtAll', () => {
  it.each([
    'src/components/Dashboard.tsx',
    'src/index.css',
    'src/lib/testHelpers.ts',
    'src/components/protest/Banner.tsx',
  ])('scans %s', (file) => {
    expect(rendersAtAll(file)).toBe(true);
  });

  it.each([
    'src/components/settings/models/modelHubStylePolicy.test.ts',
    'src/lib/agentGraph.test.tsx',
    'src/lib/util.spec.ts',
    'src/__tests__/Dashboard.tsx',
    'src/__mocks__/server.ts',
  ])('skips %s', (file) => {
    expect(rendersAtAll(file)).toBe(false);
  });
});

// The other half of "what is CSS", and the half the scan got wrong in the
// opposite direction from everything above. Blanking answers WHICH TEXT CANNOT
// RENDER; this answers WHICH TEXT IS A STYLESHEET -- and reading every `.ts`
// file as though it were one failed `const example = 'box-shadow: 0 0 93px red'`,
// a sentence, as a rendered glow.
//
// The rule is where the text is handed to a CSS parser, not what the text
// spells. So the cases are stated as sinks and non-sinks: a string that reaches
// a parser is CSS wherever it is written, and one that does not is prose however
// exactly it quotes a declaration.
describe('cssRangesIn', () => {
  // The CSS a file contains, read back through the ranges, so a case says what
  // it means -- `['box-shadow: 0 0 93px red']` -- instead of comparing offsets.
  const cssIn = (source, file) => cssRangesIn(source, file)
    .map(([start, end]) => source.slice(start, end).trim());

  // Each row carries what it should extract, because the interesting one does
  // not extract the same text as the others: a template with a substitution
  // reaches a CSS parser with the `${…}` still in it, and that is what makes it
  // a separate node kind rather than a spelling of the two above.
  it.each([
    ['an assignment to .style.cssText', 'probe.ts', `el.style.cssText = 'box-shadow: ${GLOW}';`, `box-shadow: ${GLOW}`],
    ['a template assigned to .style.cssText', 'probe.ts', `el.style.cssText = \`box-shadow: ${GLOW}\`;`, `box-shadow: ${GLOW}`],
    ['setAttribute with the style attribute', 'probe.ts', `el.setAttribute('style', 'box-shadow: ${GLOW}');`, `box-shadow: ${GLOW}`],
    ['a template with a substitution', 'probe.ts', 'el.style.cssText = `box-shadow: 0 0 93px ${c}`;', 'box-shadow: 0 0 93px ${c}'],
  ])('reads %s as CSS', (_label, file, source, css) => {
    expect(cssIn(source, file)).toEqual([css]);
  });

  it.each([
    ['a plain string', 'probe.ts', `const example = 'box-shadow: ${GLOW}';`],
    ['a documented constant', 'probe.ts', `export const EXAMPLE = \`box-shadow: ${GLOW}\`;`],
    ['a string passed to something else', 'probe.ts', `log('box-shadow: ${GLOW}');`],
    // The attribute is what makes `setAttribute` a CSS sink, not the method.
    ['setAttribute with another attribute', 'probe.ts', `el.setAttribute('title', 'box-shadow: ${GLOW}');`],
    ['page copy', 'probe.tsx', `const a = <code>box-shadow: ${GLOW}</code>;`],
  ])('reads %s as text about CSS', (_label, file, source) => {
    expect(cssIn(source, file)).toEqual([]);
  });

  // `<style>` is still answered by the ELEMENT: its children are CSS however
  // they are spelled, and asking each child kind in turn is how the JSX half of
  // this module has been wrong before.
  //
  // What the children are read AS changed, and JSX is what bounds it. `{` opens
  // an expression container, so a rule cannot be written unquoted inside a
  // `<style>` at all -- every one in compiling TSX holds a literal. Reading the
  // children therefore loses no CSS that can ship, where taking the raw span
  // between the tags handed a parser the `{'` and `'}` around it and had it read
  // a JavaScript brace as the start of a rule.
  it.each([
    ['bare text', '<style>@import url("a.css");</style>', '@import'],
    ['a string in an expression', `<style>{'.a { box-shadow: ${GLOW} }'}</style>`, 'box-shadow'],
    ['a template with a substitution', '<style>{`.a { box-shadow: 0 0 93px ${c} }`}</style>', 'box-shadow'],
  ])('reads a <style> child written as %s as CSS', (_label, element, css) => {
    expect(cssIn(`const a = ${element};`, 'probe.tsx').join('')).toContain(css);
  });

  // And reads it as text a parser accepts, which the raw span was not. Stated as
  // a parse rather than as an offset, because the point is not where the range
  // falls but that postcss can be handed what is inside it -- the token layer
  // now folds exactly these stretches, and a stretch it cannot parse contributes
  // no tokens at all while looking like it did.
  it('reads a <style> child as text postcss accepts', () => {
    const source = `const a = <style>{'.a { --tint: red }'}</style>;`;

    const declared = cssRangesIn(source, 'probe.tsx')
      .flatMap(([start, end]) => [...postcss.parse(source.slice(start, end)).nodes]);

    expect(declared.map((node) => node.selector)).toEqual(['.a']);
  });

  // A `.css` file is CSS end to end, which is the boundary case a suffix test
  // gets right and an empty file gets wrong: a range of length zero is a range,
  // and a caller folding it would parse an empty stylesheet forever.
  it('reads a stylesheet whole', () => {
    expect(cssRangesIn('.a { color: red }', 'probe.css')).toEqual([[0, 17]]);
    expect(cssRangesIn('', 'probe.css')).toEqual([]);
  });

  // Which calls hand text to a CSS parser is a fact about the web platform, not
  // about this codebase, so it is a list -- and a list is the shape that is
  // silently short. `replaceSync` was missing, and a constructable stylesheet
  // full of glow read as prose; worse, once the declaration channel learned to
  // REFUSE a match outside CSS, the same gap stopped being a miss and became a
  // proof that the glow was not CSS at all.
  //
  // The array is the single declaration and this is the test that walks it: a
  // sink added without a case fails here, which costs one edit, instead of
  // shipping unread, which costs a review round.
  const sinkKey = ({ assignedTo, called }) => String(assignedTo ?? called);

  const SINK_PROBES = {
    '/(^|\\.)cssText$/': `el.style.cssText = 'box-shadow: ${GLOW}';`,
    '/(^|\\.)setAttribute$/': `el.setAttribute('style', 'box-shadow: ${GLOW}');`,
    '/(^|\\.)insertRule$/': `sheet.insertRule('.a { box-shadow: ${GLOW} }');`,
    '/(^|\\.)replaceSync$/': `sheet.replaceSync('.a { box-shadow: ${GLOW} }');`,
    '/(^|\\.)replace$/': `sheet.replace('.a { box-shadow: ${GLOW} }');`,
  };

  it('reads every sink it enumerates, and enumerates every sink it reads', () => {
    expect(Object.keys(SINK_PROBES).sort()).toEqual(CSS_TEXT_SINKS.map(sinkKey).sort());

    for (const [sink, source] of Object.entries(SINK_PROBES)) {
      expect(cssIn(source, 'probe.ts'), `${sink} is enumerated but not read`).not.toEqual([]);
    }
  });

  // The one spelling on that list another builtin also owns. A stylesheet's
  // `replace` is handed its whole text and nothing else, while a string's is
  // always handed a replacement too, so arity tells them apart without types --
  // and reading the string one as CSS would fail every file that edits a string.
  it('reads a two-argument .replace as a substitution, not a stylesheet', () => {
    expect(cssIn(`const s = css.replace('box-shadow: ${GLOW}', '');`, 'probe.ts')).toEqual([]);
  });

  // The limit, stated rather than guessed at. A value that arrives through a
  // variable hands CSS to CSS with no literal at the sink to read, and treating
  // every string as a possible stylesheet to cover it would fail every file that
  // merely quotes a declaration. This is the dataflow boundary the whole scan
  // has; recording it here is what keeps it a known gap instead of a surprise.
  it('does not follow a value that reaches a sink through a variable', () => {
    const source = `const rule = 'box-shadow: ${GLOW}';\nel.style.cssText = rule;`;

    expect(cssIn(source, 'probe.ts')).toEqual([]);
  });
});

// The wider question, and the reason it has to be asked separately. `cssRangesIn`
// answers WHICH TEXT IS A STYLESHEET; this answers WHICH TEXT REACHES A RENDERER
// AS CSS, which is weaker, because `filter: drop-shadow(…)` ships three ways --
// in a rule, in a utility class and in a style object -- and only the first is
// text a CSS parser is handed whole.
//
// The `drop-shadow(` channel asked neither and failed a log line; asking the
// narrower one instead would have swapped that false positive for two misses.
// Both directions are stated here, because either alone is satisfiable by the
// wrong answer.
describe('cssBearingRangesIn', () => {
  const borne = (source, file) => cssBearingRangesIn(source, file)
    .map(([start, end]) => source.slice(start, end).trim());

  it.each([
    ['a Tailwind arbitrary property', `const a = <div className="[filter:drop-shadow(${GLOW})]" />;`],
    ['a style object value', `const a = <div style={{ filter: 'drop-shadow(${GLOW})' }} />;`],
    ['a CSSOM property write', `el.style.filter = 'drop-shadow(${GLOW})';`],
    ['a stylesheet handed to a parser', `sheet.replaceSync('.a { filter: drop-shadow(${GLOW}) }');`],
  ])('bears %s', (_label, source) => {
    expect(borne(source, 'probe.tsx').join('')).toContain('drop-shadow');
  });

  it.each([
    ['a diagnostic', `log('filter: drop-shadow(${GLOW})');`],
    ['a documented constant', `export const EXAMPLE = 'drop-shadow(${GLOW})';`],
    // An attribute holding a function holds a promise to compute a value later,
    // not a value. Walking through the body would read every string in every
    // event handler as CSS.
    ['a handler body inside an attribute', `const a = <div onClick={() => log('drop-shadow(${GLOW})')} />;`],
    ['page copy', `const a = <code>drop-shadow(${GLOW})</code>;`],
  ])('does not bear %s', (_label, source) => {
    expect(borne(source, 'probe.tsx')).toEqual([]);
  });

  // Which attributes hand text to the style system, in both directions. Taking
  // every attribute read `<div title="filter: drop-shadow(…)" />` -- a tooltip a
  // screen reader speaks -- as CSS, and failed CI over text no parser is handed;
  // matching `className` exactly would have swapped that for one miss per
  // forwarding prop. The list is the single declaration, and this walks it, so a
  // shape added without a case fails here instead of shipping unread.
  const ATTRIBUTE_PROBES = {
    '/(^|[a-z])[Cc]lass([Nn]ame)?$/': `const a = <div className="[filter:drop-shadow(${GLOW})]" />;`,
    '/(^|[a-z])[Ss]tyle$/': `const a = <div style={{ filter: 'drop-shadow(${GLOW})' }} />;`,
  };

  it('bears every attribute shape it enumerates, and enumerates every one it bears', () => {
    expect(Object.keys(ATTRIBUTE_PROBES).sort()).toEqual(CSS_BEARING_ATTRIBUTES.map(String).sort());

    for (const [shape, source] of Object.entries(ATTRIBUTE_PROBES)) {
      expect(borne(source, 'probe.tsx').join(''), `${shape} is enumerated but not read`).toContain('drop-shadow');
    }
  });

  // A prop that forwards a class or a style is one whatever it is called, and
  // React composition names them by suffix. These all ship in `src` today.
  it.each([
    'className', 'class', 'wrapperClassName', 'iconClassName', 'triggerClassName',
    'labelClassName', 'rowClass', 'containerClass', 'style', 'wrapperStyle', 'bodyStyle',
  ])('bears a value handed to %s', (prop) => {
    expect(borne(`const a = <Tile ${prop}={'[filter:drop-shadow(${GLOW})]'} />;`, 'probe.tsx').join(''))
      .toContain('drop-shadow');
  });

  // And the attributes that hold copy rather than style. Each is text a person
  // reads or a machine speaks; each would have failed a correct pull request.
  it.each(['title', 'aria-label', 'alt', 'placeholder', 'data-glow'])(
    'does not bear copy handed to %s',
    (attribute) => {
      expect(borne(`const a = <div ${attribute}="filter: drop-shadow(${GLOW})" />;`, 'probe.tsx')).toEqual([]);
    },
  );

  it('reads a stylesheet whole, exactly as the narrower question does', () => {
    expect(cssBearingRangesIn('.a { color: red }', 'probe.css')).toEqual([[0, 17]]);
  });
});
