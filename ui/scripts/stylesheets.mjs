// Every stretch of stylesheet text the project ships, parsed once each.
//
// Extracted for the reason `customProperties.mjs` and `cssDeclarations.mjs`
// were: `validate-theme.mjs` runs its whole validation at import time, so
// nothing inside it can be called from a test. This one had a second caller
// waiting -- `glowScale.test.mjs` was deriving the sanctioned token set from
// `src/index.css` alone while the validator sanctioned `@theme` declarations
// from every stylesheet, so a component stylesheet could declare
// `--shadow-glow-rogue-mint: 0 0 93px red`, have the validator trust it as
// managed, and have every rule in the scale test skip it. Two answers to "which
// stylesheets are there" is the shape that produced that gap, so there is one.
//
// `.css` was the wrong unit for it. That is a FILE EXTENSION, and the question
// being asked is the same one the scan asks about its call sites -- where in
// this project is text CSS -- which `cssRangesIn` answers and no extension can.
// A `<style>` body and a string handed to `insertRule` are stylesheets that
// ship, so a token declared in one was invisible while a call site reading that
// token was scanned; the name resolved to nothing and the gate reported correct
// CSS as unanchored. That is a false positive, which fails somebody else's pull
// request over code that is right.

import fs from 'node:fs';
import path from 'node:path';

import postcss from 'postcss';

import { intendedFiles } from './lintPolicy.mjs';
import { cssRangesIn, rendersAtAll } from './nonRenderingText.mjs';

/**
 * Yield ``[origin, root]`` for every stylesheet under ``root``.
 *
 * ``origin`` is a human-readable place to report, and ``root`` a parsed postcss
 * tree. One walk feeds every fold that wants the token layer, where it used to
 * be a read and a parse of every stylesheet per question about the same tree.
 */
function* eachStylesheet(root) {
  for (const relative of intendedFiles(root, { extensions: ['.ts', '.tsx', '.css'] })) {
    // A fixture stylesheet is not the token layer, by the same argument that
    // keeps a test file out of the scan: it documents values rather than
    // shipping them.
    if (!rendersAtAll(relative)) continue;

    const file = path.join(root, relative);
    const source = fs.readFileSync(file, 'utf8');

    // `origin` names the stretch, not just the file, because a `.tsx` file can
    // hold several and a line number inside one of them counts from its own
    // start. For the two `.css` files this project actually has, that is the
    // path and nothing more.
    const ranges = cssRangesIn(source, file);
    for (const [start, end] of ranges) {
      const text = source.slice(start, end);
      const origin = ranges.length > 1 ? `${relative} (offset ${start})` : relative;

      // A `.css` file is parsed unguarded on purpose, and `nonRenderingText.mjs`
      // depends on that: text a stylesheet cannot parse is a broken stylesheet,
      // and the gate should say so out loud. A stretch lifted out of TypeScript
      // carries no such promise -- a template can interpolate a whole rule, and
      // `` `${rules} .a {}` `` is CSS only after the substitution runs -- so one
      // that will not parse is a stretch this fold cannot read, not a file that
      // is wrong, and failing the build over it would be the same false positive
      // one layer along. What it cannot read it also cannot scan, so the miss is
      // symmetric; `src` has no such stretch today.
      if (file.endsWith('.css')) {
        yield [origin, postcss.parse(text)];
        continue;
      }
      let sheet;
      try {
        sheet = postcss.parse(text);
      } catch {
        continue;
      }
      yield [origin, sheet];
    }
  }
}

export { eachStylesheet };
