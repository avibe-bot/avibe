import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

import postcss from 'postcss';

import { intendedFiles } from './lintPolicy.mjs';
import { rendersAtAll } from './nonRenderingText.mjs';
import { eachStylesheet } from './stylesheets.mjs';
import {
  classesRenderedBy,
  firstCompound,
  frozenAliases,
  styledSubjects,
  tokensUsedInMarkup,
  unanchoredMarkupTokens,
  unscopedTokens,
} from './travellingTokens.mjs';

const UI_ROOT = fileURLToPath(new URL('../', import.meta.url));

// The class the original defect was found on: the evidence body of a guarded
// Model Hub mutation, which `SourceDetailPanel`, `SourceMutationReport`,
// `BackendModelCatalogDialog`, `AddApiKeyDialog` and `RouteChainDialog` each
// mount under a different root. It is named here as an anchor -- proof the
// derived subject set below really reaches the case that started this -- and
// nowhere as the subject of the check, which is every class this project styles.
const FOUND_ON = 'model-hub-guard-body';

// Every source that ships, as `[origin, text]`. `intendedFiles` and
// `rendersAtAll` are the same pair `eachStylesheet` uses, so "which files ship"
// has one answer here rather than a second one written next to the check.
function* shippedSources() {
  for (const relative of intendedFiles(UI_ROOT, { extensions: ['.ts', '.tsx'] })) {
    if (!rendersAtAll(relative)) continue;
    yield [relative, fs.readFileSync(path.join(UI_ROOT, relative), 'utf8')];
  }
}

describe('firstCompound', () => {
  it('keeps the whole selector when nothing follows the subject', () => {
    expect(firstCompound('.model-hub-guard-label')).toBe('.model-hub-guard-label');
  });

  it('stops at every combinator, so a descendant rule is attributed to its subject', () => {
    for (const combinator of [' ', ' > ', ' + ', ' ~ ', '>']) {
      expect(firstCompound(`.a${combinator}span`)).toBe('.a');
    }
  });

  it('does not mistake a combinator inside a functional or attribute part for the end', () => {
    expect(firstCompound('.a:not(.b > .c)[data-x="y > z"] span')).toBe('.a:not(.b > .c)[data-x="y > z"]');
  });
});

describe('classesRenderedBy', () => {
  const PROBE = 'Component.tsx';

  it('reads a plain literal, a helper call and a conditional alike', () => {
    const source = [
      'const a = <div className="one two" />;',
      "const b = <div className={cn('three', flag && 'four')} />;",
      'const c = <div className={`five`} />;',
    ].join('\n');

    expect(classesRenderedBy(source, PROBE)).toEqual(new Set(['one', 'two', 'three', 'four', 'five']));
  });

  it('ignores a class name that is only talked about', () => {
    const source = '// .model-hub-guard-label is styled elsewhere\nconst tip = ".model-hub-guard-hop";';

    expect(classesRenderedBy(source, PROBE)).toEqual(new Set());
  });

  it('ignores markup that is commented out, in either spelling', () => {
    // A component half-reworked leaves the old tree sitting right there, and it
    // is markup by every syntactic measure. What it is not is rendered, so a
    // class only it carries is not a class this project styles anything with.
    for (const source of [
      'const a = <div className="live" />;\n// const b = <div className="dead" />;',
      'const a = <div className="live" />;\n/* <div className="dead" /> */',
      'const a = (\n  <div className="live">\n    {/* <span className="dead" /> */}\n  </div>\n);',
    ]) {
      expect(classesRenderedBy(source, PROBE)).toEqual(new Set(['live']));
    }
  });

  it('keeps reading the markup that is still there around it', () => {
    // Blanking has to leave the file's shape alone: a comment between two
    // elements must not swallow the second one.
    const source = 'const a = (\n  <div className="one">\n    {/* was: two */}\n    <span className="three" />\n  </div>\n);';

    expect(classesRenderedBy(source, PROBE)).toEqual(new Set(['one', 'three']));
  });
});

describe('unscopedTokens', () => {
  const sheetsOf = (css) => [['inline', postcss.parse(css)]];

  it('accepts a token the class declares on itself', () => {
    const sheets = sheetsOf('.travels { --gap: 6px; gap: var(--gap); }');

    expect(unscopedTokens(sheets, ['travels'])).toEqual([]);
  });

  it('accepts a token from a scope every element inherits', () => {
    for (const scope of [':root', 'html', 'body', '*']) {
      const sheets = sheetsOf(`${scope} { --gap: 6px } .travels { gap: var(--gap); }`);

      expect(unscopedTokens(sheets, ['travels'])).toEqual([]);
    }
  });

  it('reports a token only a qualified root declares, because the other half has none', () => {
    // A themed root reaches every element only while that theme applies, so it
    // cannot answer for a use that applies always.
    for (const scope of ['[data-theme="light"]', ':root:not([data-theme="dark"])', 'html[data-theme="dark"]']) {
      const sheets = sheetsOf(`${scope} { --gap: 6px } .travels { gap: var(--gap); }`);

      expect(unscopedTokens(sheets, ['travels'])).toHaveLength(1);
    }
  });

  it('accepts a themed override once a root base declares the same token', () => {
    // The project's own shape: `:root` states the value, the theme overrides it.
    const sheets = sheetsOf(':root { --gap: 6px } [data-theme="dark"] { --gap: 8px } .travels { gap: var(--gap); }');

    expect(unscopedTokens(sheets, ['travels'])).toEqual([]);
  });

  it('accepts a token the class declares on itself and a descendant consumes', () => {
    const sheets = sheetsOf('.travels { --pad: 8px } .travels > span { padding: var(--pad); }');

    expect(unscopedTokens(sheets, ['travels'])).toEqual([]);
  });

  it('reports a token only some ancestor declares, naming the class and the property', () => {
    const sheets = sheetsOf('.host { --gap: 6px } .travels { gap: var(--gap); }');

    expect(unscopedTokens(sheets, ['travels'])).toEqual([
      { className: 'travels', property: '--gap', origin: 'inline', selector: '.travels', declaration: 'gap' },
    ]);
  });

  it('reports it for a descendant of the class too', () => {
    const sheets = sheetsOf('.host { --pad: 8px } .travels > span { padding: var(--pad); }');

    expect(unscopedTokens(sheets, ['travels'])).toHaveLength(1);
  });

  it('leaves a rule about one host alone, because that rule is not travelling', () => {
    const sheets = sheetsOf('.host { --gap: 6px } .host .travels { gap: var(--gap); }');

    expect(unscopedTokens(sheets, ['travels'])).toEqual([]);
  });

  it('accepts a use that carries a fallback, which draws something either way', () => {
    const sheets = sheetsOf('.host { --gap: 6px } .travels { gap: var(--gap, 6px); }');

    expect(unscopedTokens(sheets, ['travels'])).toEqual([]);
  });

  it('says nothing about a class that does not travel', () => {
    const sheets = sheetsOf('.host { --gap: 6px } .stays-put { gap: var(--gap); }');

    expect(unscopedTokens(sheets, ['travels'])).toEqual([]);
  });

  it('reports a token the class declares only inside a query the use does not share', () => {
    const sheets = sheetsOf('@media (min-width: 600px) { .travels { --gap: 6px } } .travels { gap: var(--gap); }');

    expect(unscopedTokens(sheets, ['travels'])).toHaveLength(1);
  });

  it('accepts it when the use stands under the same query', () => {
    const sheets = sheetsOf('@media (min-width: 600px) { .travels { --gap: 6px } .travels { gap: var(--gap); } }');

    expect(unscopedTokens(sheets, ['travels'])).toEqual([]);
  });

  it('accepts a use nested deeper than the declaration it relies on', () => {
    // The use cannot apply without the query that guards the declaration, so
    // the declaration cannot be missing where the use is.
    const sheets = sheetsOf(
      '@media (min-width: 600px) { .travels { --gap: 6px } @supports (gap: 1px) { .travels { gap: var(--gap); } } }',
    );

    expect(unscopedTokens(sheets, ['travels'])).toEqual([]);
  });

  it('reports an inherited scope that is itself conditional, registration included', () => {
    // The same miss as a class token: `:root`, `@theme` and `@property` each
    // promise every element, and a query around any of them takes that back.
    for (const declared of [
      ':root { --gap: 6px }',
      '@theme { --gap: 6px }',
      '@property --gap { syntax: "*"; inherits: true; initial-value: 6px }',
    ]) {
      const sheets = sheetsOf(`@media print { ${declared} } .travels { gap: var(--gap); }`);

      expect(unscopedTokens(sheets, ['travels'])).toHaveLength(1);
    }
  });

  it('treats a layer as no condition at all, because its body always applies', () => {
    const sheets = sheetsOf('@layer base { .travels { --gap: 6px } } .travels { gap: var(--gap); }');

    expect(unscopedTokens(sheets, ['travels'])).toEqual([]);
  });

  it('accepts a declaration on fewer of the same element classes than the use carries', () => {
    // `.travels` and `.is-open` sit on one element, so a declaration naming only
    // the first is a declaration on the very element the use applies to.
    const sheets = sheetsOf('.travels { --gap: 6px } .travels.is-open { gap: var(--gap); }');

    expect(unscopedTokens(sheets, ['travels'])).toEqual([]);
  });

  it('reports a declaration that asks for more classes than the use does', () => {
    // The other direction is a different element: `.travels` alone matches
    // without `.is-open`, and there the declaration is not there.
    const sheets = sheetsOf('.travels.is-open { --gap: 6px } .travels { gap: var(--gap); }');

    expect(unscopedTokens(sheets, ['travels'])).toHaveLength(1);
  });

  it('reports a declaration guarded by anything that is not a class', () => {
    // Each of these adds a condition the use's own subject does not carry, so
    // the declaration is on the element only some of the time.
    for (const guard of ['.travels:hover', '.travels[data-open="true"]', 'div.travels']) {
      const sheets = sheetsOf(`${guard} { --gap: 6px } .travels { gap: var(--gap); }`);

      expect(unscopedTokens(sheets, ['travels'])).toHaveLength(1);
    }
  });
});

describe('tokensUsedInMarkup', () => {
  const PROBE = 'Component.tsx';

  it('reads a Tailwind arbitrary value and an inline style alike', () => {
    const source = [
      'const a = <ul className="flex gap-[var(--row-gap)]" />;',
      "const b = <li style={{ paddingInline: 'var(--row-pad)' }} />;",
    ].join('\n');

    expect(tokensUsedInMarkup(source, PROBE)).toEqual(new Set(['--row-gap', '--row-pad']));
  });

  it('leaves a fallback-bearing use alone, which draws something either way', () => {
    expect(tokensUsedInMarkup('<ul className="gap-[var(--row-gap,4px)]" />', PROBE)).toEqual(new Set());
  });

  it('ignores a use that is commented out, which asks nothing of any scope', () => {
    // The cost of reading it is not a missed defect but an invented one: the
    // token would have to be anchored at `:root` to satisfy markup that does
    // not render, and every remedy for that is a change to shipping CSS.
    const source = "// <li style={{ gap: 'var(--dead-gap)' }} />\nconst a = <ul className=\"gap-[var(--row-gap)]\" />;";

    expect(tokensUsedInMarkup(source, PROBE)).toEqual(new Set(['--row-gap']));
  });
});

describe('frozenAliases', () => {
  const sheetsOf = (css) => [['inline', postcss.parse(css)]];
  const THEMED = ':root { --ink: white } [data-theme="light"] { --ink: black }';

  it('reports an alias at the root for a token a theme restates', () => {
    // Substituted once, at the root, in whichever theme applies there. An
    // element inside `[data-theme="light"]` inherits that answer and not its own.
    const sheets = sheetsOf(`${THEMED} :root { --label-ink: var(--ink) }`);

    expect(frozenAliases(sheets)).toEqual([{
      origin: 'inline',
      property: '--label-ink',
      reads: '--ink',
      missing: ['[data-theme="light"]'],
    }]);
  });

  it('accepts an alias restated in every scope that restates what it reads', () => {
    const sheets = sheetsOf(
      `${THEMED} :root { --label-ink: var(--ink) } [data-theme="light"] { --label-ink: var(--ink) }`,
    );

    expect(frozenAliases(sheets)).toEqual([]);
  });

  it('compares the query around a scope too, not only its selector', () => {
    const themed = '@media (prefers-color-scheme: light) { :root:not([data-theme="dark"]) { --ink: black } }';
    const sheets = sheetsOf(`:root { --ink: white } ${themed} :root { --label-ink: var(--ink) }`);

    expect(frozenAliases(sheets)).toHaveLength(1);
  });

  it('accepts an alias for a token nothing narrower ever restates', () => {
    const sheets = sheetsOf(':root { --ink: white } :root { --label-ink: var(--ink) }');

    expect(frozenAliases(sheets)).toEqual([]);
  });

  it('says nothing about a value that computes, where placement is a real choice', () => {
    // `color-mix` is doing work, and where that work happens has reasons on both
    // sides. Only a rename is asserted, because a rename has no other job.
    const sheets = sheetsOf(`${THEMED} :root { --label-ink: color-mix(in srgb, var(--ink) 80%, transparent) }`);

    expect(frozenAliases(sheets)).toEqual([]);
  });

  it('says nothing about an alias on a class, whose scope promises nothing global', () => {
    const sheets = sheetsOf(`${THEMED} .panel { --label-ink: var(--ink) }`);

    expect(frozenAliases(sheets)).toEqual([]);
  });

  it('says nothing about a `@theme` entry, which is substituted rather than inherited', () => {
    const sheets = sheetsOf(`${THEMED} @theme inline { --color-label: var(--ink) }`);

    expect(frozenAliases(sheets)).toEqual([]);
  });
});

describe('unanchoredMarkupTokens', () => {
  const sheetsOf = (css) => [['inline', postcss.parse(css)]];
  const sourcesOf = (tsx) => [['Component.tsx', tsx]];

  it('accepts a name a scope every element inherits declares', () => {
    const sheets = sheetsOf(':root { --row-gap: 14px }');

    expect(unanchoredMarkupTokens(sheets, sourcesOf('<ul className="gap-[var(--row-gap)]" />'))).toEqual([]);
  });

  it('reports a name only some class declares, because markup names no subject', () => {
    const sheets = sheetsOf('.dialog { --row-gap: 14px }');

    expect(unanchoredMarkupTokens(sheets, sourcesOf('<ul className="gap-[var(--row-gap)]" />'))).toEqual([
      { origin: 'Component.tsx', property: '--row-gap' },
    ]);
  });

  it('says nothing about a name no stylesheet here declares, which is somebody else to anchor', () => {
    // `--radix-popover-trigger-width` is written onto the element by Radix at
    // open time. This project could not anchor it if it wanted to.
    const sheets = sheetsOf('.dialog { --row-gap: 14px }');
    const sources = sourcesOf('<ul className="w-[var(--radix-popover-trigger-width)]" />');

    expect(unanchoredMarkupTokens(sheets, sources)).toEqual([]);
  });
});

describe('this project', () => {
  const sheets = [...eachStylesheet(UI_ROOT)];
  const sources = [...shippedSources()];
  const styled = styledSubjects(sheets);

  it('styles classes the product renders, so the checks have a subject', () => {
    // Without this the assertions below pass by asking about nothing, which is
    // how a check outlives the thing it checks. Both sides are covered: the
    // stylesheets style something, the sources render something, and the class
    // the defect was found on is still in both.
    expect(styled.size).toBeGreaterThan(0);
    expect(styled).toContain(FOUND_ON);

    const rendered = new Set();
    for (const [origin, text] of sources) {
      for (const name of classesRenderedBy(text, origin)) rendered.add(name);
    }
    expect(rendered).toContain(FOUND_ON);
  });

  it('resolves every token every class it styles consumes, wherever that class mounts', () => {
    // No root is named here on purpose, and none needs to be: a token
    // resolvable from the class itself or from `:root` is resolvable under
    // every root that exists and under one nobody has written yet. Asserted of
    // all of them rather than of the few known to travel, because which class
    // travels is a fact about the React tree that this file cannot see.
    expect(unscopedTokens(sheets, styled)).toEqual([]);
  });

  it('anchors every token its markup reads directly, which no selector can scope', () => {
    expect(unanchoredMarkupTokens(sheets, sources)).toEqual([]);
  });

  it('renames no token at the root that a theme restates underneath it', () => {
    // The other half of the same question, and the half moving a declaration
    // gets wrong: the name still resolves, so the check above stays green while
    // the value stops following the theme. Asserted over the whole token layer
    // for the same reason as the rest — the next widening is not this one.
    expect(frozenAliases(sheets)).toEqual([]);
  });
});
