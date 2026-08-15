import fs from 'node:fs';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

import { intendedFiles } from './lintPolicy.mjs';
import { withoutNonRenderingText } from './nonRenderingText.mjs';

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
  ['a CSS comment', 'probe.css', `/* box-shadow: ${GLOW} */\n.a { color: red; }\n`],
  ['a CSS string', 'probe.css', `.a { content: "box-shadow: ${GLOW}"; }\n`],
  ['a comment opener inside a CSS string', 'probe.css', `.a { content: "/* box-shadow: ${GLOW} */"; }\n`],
];

const RENDERING = [
  ['a Tailwind arbitrary value', 'probe.tsx', 'const a = <div className="shadow-[0_0_93px_red]" />;\n', '0_0_93px_red'],
  ['an inline style object', 'probe.tsx', `const a = <div style={{ boxShadow: '${GLOW}' }} />;\n`, GLOW],
  ['a template literal', 'probe.ts', `const a = { boxShadow: \`${GLOW}\` };\n`, GLOW],
  ['a CSSOM setter', 'probe.ts', `el.style.setProperty('box-shadow', '${GLOW}');\n`, GLOW],
  ['a CSS declaration', 'probe.css', `.a { box-shadow: ${GLOW}; }\n`, GLOW],
  ['a declaration after a comment on the same line', 'probe.css', `/* note */ .a { box-shadow: ${GLOW}; }\n`, GLOW],
  ['a declaration after a string on the same line', 'probe.css', `.a { content: "x"; box-shadow: ${GLOW}; }\n`, GLOW],
  ['a URL whose // must not open a comment', 'probe.ts', `const u = 'https://x//y'; const a = { boxShadow: '${GLOW}' };\n`, GLOW],
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
