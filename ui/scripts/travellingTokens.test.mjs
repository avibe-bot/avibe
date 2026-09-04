import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

import postcss from 'postcss';

import { intendedFiles } from './lintPolicy.mjs';
import { rendersAtAll } from './nonRenderingText.mjs';
import { eachStylesheet } from './stylesheets.mjs';
import {
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

  it('reports a declaration guarded by anything the use does not ask for too', () => {
    // Each of these adds a condition the use's own subject does not carry, so
    // the declaration is on the element only some of the time.
    for (const guard of ['.travels:hover', '.travels[data-open="true"]', 'div.travels']) {
      const sheets = sheetsOf(`${guard} { --gap: 6px } .travels { gap: var(--gap); }`);

      expect(unscopedTokens(sheets, ['travels'])).toHaveLength(1);
    }
  });

  it('accepts a declaration whose guard the use carries as well', () => {
    // The guard is a condition, not a disqualification: where the use applies,
    // it applies too. A rule that declares a token and reads it back in the
    // same body is the shape this most often takes.
    for (const selector of ['.travels:hover', '.travels[data-open]', 'div.travels']) {
      const sheets = sheetsOf(`${selector} { --hover-ink: red; color: var(--hover-ink); }`);

      expect(unscopedTokens(sheets, ['travels'])).toEqual([]);
    }
  });

  it('accepts a rule that declares and reads a token behind a combinator', () => {
    // The declaration lands on the span, and so does the use. There is no
    // second element for the combinator to be wrong about.
    const sheets = sheetsOf('.travels > span { --pad: 8px; padding: var(--pad); }');

    expect(unscopedTokens(sheets, ['travels'])).toEqual([]);
  });

  it('reports a token only a narrower subtree declares, which the class cannot reach', () => {
    // The same combinator, now between the two: the span's children inherit it
    // and no sibling of the span does, so `.travels` does not resolve it.
    const sheets = sheetsOf('.travels > span { --pad: 8px } .travels { padding: var(--pad); }');

    expect(unscopedTokens(sheets, ['travels'])).toHaveLength(1);
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

  it('reads any attribute, because a styling prop is a third spelling of one', () => {
    // `className` and `style` are the two an element applies itself; a component
    // that forwards `contentClassName` or `trackStyle` to a child applies the
    // same use one level down. Which name a component chose for that is not a
    // question worth asking, so none is asked.
    const source = [
      'const a = <Panel contentClassName="gap-[var(--row-gap)]" />;',
      "const b = <Track trackStyle={{ height: 'var(--row-pad)' }} />;",
    ].join('\n');

    expect(tokensUsedInMarkup(source, PROBE)).toEqual(new Set(['--row-gap', '--row-pad']));
  });

  it('reads a helper call and a conditional whole, not their first branch', () => {
    const source = "const a = <div className={cn('gap-[var(--row-gap)]', flag && 'p-[var(--row-pad)]')} />;";

    expect(tokensUsedInMarkup(source, PROBE)).toEqual(new Set(['--row-gap', '--row-pad']));
  });

  it('leaves a fallback-bearing use alone, which draws something either way', () => {
    expect(tokensUsedInMarkup('<ul className="gap-[var(--row-gap,4px)]" />', PROBE)).toEqual(new Set());
  });

  it('ignores a use that is commented out, which asks nothing of any scope', () => {
    // The cost of reading it is not a missed defect but an invented one: the
    // token would have to be declared globally to satisfy markup that does not
    // render, and every remedy for that is a change to shipping CSS.
    for (const source of [
      "// <li style={{ gap: 'var(--dead-gap)' }} />\nconst a = <ul className=\"gap-[var(--row-gap)]\" />;",
      "/* <li style={{ gap: 'var(--dead-gap)' }} /> */\nconst a = <ul className=\"gap-[var(--row-gap)]\" />;",
      'const a = (\n  <ul className="gap-[var(--row-gap)]">\n'
      + "    {/* <li style={{ gap: 'var(--dead-gap)' }} /> */}\n  </ul>\n);",
    ]) {
      expect(tokensUsedInMarkup(source, PROBE)).toEqual(new Set(['--row-gap']));
    }
  });

  it('reads only an attribute of an element, not every binding that shares its name', () => {
    // `className` is an ordinary identifier as well as an attribute name, and a
    // variable holding one is not markup: nothing here renders, so nothing here
    // is a token anything has to resolve.
    const source = "const className = 'gap-[var(--local-gap)]';";

    expect(tokensUsedInMarkup(source, PROBE)).toEqual(new Set());
  });

  it('still reads the element that does render, standing next to one that does not', () => {
    const source = [
      "const className = 'gap-[var(--local-gap)]';",
      'const a = <div className="gap-[var(--row-gap)]" />;',
    ].join('\n');

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

  it('reports an alias whose token a query restates at the very same root', () => {
    // A bare `:root` under `@media` is not every element unconditionally: the
    // value differs where the query holds, and the alias was substituted where
    // it does not. The selector alone cannot tell the two apart.
    const sheets = sheetsOf(
      ':root { --ink: white; --label-ink: var(--ink) }'
      + ' @media (prefers-color-scheme: light) { :root { --ink: black } }',
    );

    expect(frozenAliases(sheets)).toEqual([
      expect.objectContaining({ property: '--label-ink', reads: '--ink' }),
    ]);
  });

  it('accepts it once the query restates the alias too', () => {
    const sheets = sheetsOf(
      ':root { --ink: white; --label-ink: var(--ink) }'
      + ' @media (prefers-color-scheme: light) { :root { --ink: black; --label-ink: var(--ink) } }',
    );

    expect(frozenAliases(sheets)).toEqual([]);
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

  it('says nothing about a `@theme` entry, which is not a scope this project placed', () => {
    const sheets = sheetsOf(`${THEMED} @theme inline { --color-label: var(--ink) }`);

    expect(frozenAliases(sheets)).toEqual([]);
  });
});

describe('unanchoredMarkupTokens', () => {
  const sheetsOf = (css) => [['inline', postcss.parse(css)]];
  const sourcesOf = (tsx) => [['Component.tsx', tsx]];
  const REPORTED = [{ origin: 'Component.tsx', property: '--row-gap' }];

  it('accepts a name a scope every element inherits declares', () => {
    const sheets = sheetsOf(':root { --row-gap: 14px }');

    expect(unanchoredMarkupTokens(sheets, sourcesOf('<ul className="gap-[var(--row-gap)]" />'))).toEqual([]);
  });

  it('accepts a name a `@theme` block declares, which Tailwind emits at the root', () => {
    // `@theme` entries are emitted into `@layer theme { :root, :host { … } }`,
    // so every element inherits them; `inline` decides how a generated utility
    // references the value, not whether the variable exists.
    const sheets = sheetsOf('@theme inline { --row-gap: 14px }');

    expect(unanchoredMarkupTokens(sheets, sourcesOf('<ul className="gap-[var(--row-gap)]" />'))).toEqual([]);
  });

  it('reports a name only some class declares, because markup names no subject', () => {
    const sheets = sheetsOf('.dialog { --row-gap: 14px }');

    expect(unanchoredMarkupTokens(sheets, sourcesOf('<ul className="gap-[var(--row-gap)]" />'))).toEqual(REPORTED);
  });

  it('reports it even where the element is written carrying that very class', () => {
    // The class is right there in the literal, and it still answers nothing:
    // whether it is on the element that renders is a question about props,
    // branches and variant maps, so no answer read out of one file is one. A
    // component-scoped token is consumed in the stylesheet, where a selector
    // says which elements the value is for.
    const sheets = sheetsOf('.row { --row-gap: 14px }');

    expect(unanchoredMarkupTokens(sheets, sourcesOf('<ul className="row gap-[var(--row-gap)]" />'))).toEqual(REPORTED);
  });

  it('reports a name nothing declares anywhere, which draws nothing at all', () => {
    // The previous rule left this one alone as somebody else's to declare, and
    // a typo is spelled exactly the same way.
    expect(unanchoredMarkupTokens(sheetsOf(''), sourcesOf('<ul className="gap-[var(--row-gap)]" />'))).toEqual(REPORTED);
  });

  it('reports a name declared at the root only under a query', () => {
    // A use on an element is not itself guarded by that query, so the value is
    // missing wherever it does not hold.
    const sheets = sheetsOf('@media (min-width: 40rem) { :root { --row-gap: 14px } }');

    expect(unanchoredMarkupTokens(sheets, sourcesOf('<ul className="gap-[var(--row-gap)]" />'))).toEqual(REPORTED);
  });

  it('says nothing about a name another runtime writes onto the element', () => {
    // `--radix-popover-trigger-width` is written onto the element by Radix at
    // open time. This project could not declare it if it wanted to, so it is
    // named as an exception rather than inferred from being undeclared.
    const sources = sourcesOf('<ul className="w-[var(--radix-popover-trigger-width)]" />');

    expect(unanchoredMarkupTokens(sheetsOf(''), sources)).toEqual([]);
  });

  it('says nothing about a use carrying a fallback, which draws something either way', () => {
    const sources = sourcesOf('<ul className="gap-[var(--row-gap,4px)]" />');

    expect(unanchoredMarkupTokens(sheetsOf('.row { --row-gap: 14px }'), sources)).toEqual([]);
  });
});

describe('this project', () => {
  const sheets = [...eachStylesheet(UI_ROOT)];
  const sources = [...shippedSources()];
  const styled = styledSubjects(sheets);

  it('styles classes and reads tokens from markup, so the checks have a subject', () => {
    // Without this the assertions below pass by asking about nothing, which is
    // how a check outlives the thing it checks. Each half is anchored by the
    // walk that half uses: the stylesheets style something, including the class
    // the defect was found on, and the markup reads tokens at all.
    expect(styled.size).toBeGreaterThan(0);
    expect(styled).toContain(FOUND_ON);

    const read = new Set();
    for (const [origin, text] of sources) {
      for (const property of tokensUsedInMarkup(text, origin)) read.add(property);
    }
    expect(read.size).toBeGreaterThan(0);
  });

  it('resolves every token every class it styles consumes, wherever that class mounts', () => {
    // No root is named here on purpose, and none needs to be: a token
    // resolvable from the class itself or from `:root` is resolvable under
    // every root that exists and under one nobody has written yet. Asserted of
    // all of them rather than of the few known to travel, because which class
    // travels is a fact about the React tree that this file cannot see.
    expect(unscopedTokens(sheets, styled)).toEqual([]);
  });

  it('reads only global or runtime-provided tokens from markup, which no selector can scope', () => {
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
