import fs from 'node:fs';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

import { intendedFiles } from './lintPolicy.mjs';
import { rendersAtAll, withoutNonRenderingText } from './nonRenderingText.mjs';
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
