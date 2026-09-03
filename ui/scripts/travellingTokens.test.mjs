import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

import postcss from 'postcss';

import { eachStylesheet } from './stylesheets.mjs';
import { classesRenderedBy, firstCompound, unscopedTokens } from './travellingTokens.mjs';

const UI_ROOT = fileURLToPath(new URL('../', import.meta.url));

// The components whose styling has to survive being mounted somewhere else.
// This is the one thing the stylesheet cannot know and the check cannot derive:
// a class is only "travelling" because more than one dialog renders it, which
// is a fact about the React tree. So it is stated once, as the subject of the
// check -- and the classes themselves are then read out of these files, so the
// enumeration ends here rather than being copied into an assertion.
//
// Add a component to this list when it becomes shared. Both of these are: they
// are the evidence body of a guarded Model Hub mutation, and `SourceDetailPanel`,
// `SourceMutationReport`, `BackendModelCatalogDialog`, `AddApiKeyDialog` and
// `RouteChainDialog` each mount them under a different root class.
const TRAVELLING_COMPONENTS = [
  'src/components/settings/models/GuardImpact.tsx',
  'src/components/settings/models/GuardGapList.tsx',
];

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
  it('reads a plain literal, a helper call and a conditional alike', () => {
    const source = [
      'const a = <div className="one two" />;',
      "const b = <div className={cn('three', flag && 'four')} />;",
      'const c = <div className={`five`} />;',
    ].join('\n');

    expect(classesRenderedBy(source)).toEqual(new Set(['one', 'two', 'three', 'four', 'five']));
  });

  it('ignores a class name that is only talked about', () => {
    const source = '// .model-hub-guard-label is styled elsewhere\nconst tip = ".model-hub-guard-hop";';

    expect(classesRenderedBy(source)).toEqual(new Set());
  });
});

describe('unscopedTokens', () => {
  const sheetsOf = (css) => [['inline', postcss.parse(css)]];

  it('accepts a token the class declares on itself', () => {
    const sheets = sheetsOf('.travels { --gap: 6px; gap: var(--gap); }');

    expect(unscopedTokens(sheets, ['travels'])).toEqual([]);
  });

  it('accepts a token from a scope every element inherits', () => {
    for (const scope of [':root', 'html', '[data-theme="light"]', ':root:not([data-theme="dark"])']) {
      const sheets = sheetsOf(`${scope} { --gap: 6px } .travels { gap: var(--gap); }`);

      expect(unscopedTokens(sheets, ['travels'])).toEqual([]);
    }
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
});

describe('the shared guard body', () => {
  const sheets = [...eachStylesheet(UI_ROOT)];
  const travelling = new Set();
  for (const relative of TRAVELLING_COMPONENTS) {
    for (const name of classesRenderedBy(fs.readFileSync(path.join(UI_ROOT, relative), 'utf8'))) {
      travelling.add(name);
    }
  }

  it('renders classes this project styles, so the check has a subject', () => {
    // Without this the assertion below passes by asking about nothing, which is
    // how a check outlives the thing it checks. `readFileSync` already fails
    // loudly if a component moves; this covers the quieter half, where the
    // files still parse but nothing recognisable comes out of them.
    expect(travelling.size).toBeGreaterThan(0);

    // Not every class needs a rule here: these components also render Tailwind
    // utilities (`flex-1`, `min-w-0`), which are generated at build time and
    // carry no custom properties, and `unscopedTokens` never reaches a class no
    // rule takes as its subject. So the requirement is that the project styles
    // some of what they render -- not all of it.
    const styled = new Set();
    for (const [, root] of sheets) {
      root.walkRules((rule) => {
        for (const one of rule.selectors) {
          for (const name of travelling) if (one.includes(`.${name}`)) styled.add(name);
        }
      });
    }
    expect(styled.size).toBeGreaterThan(0);
  });

  it('resolves every token it consumes wherever a caller mounts it', () => {
    // The four roots are `.model-hub-guard-dialog`, `.model-hub-catalog-dialog`,
    // `.model-hub-add-key-dialog` and `.model-hub-route-dialog`. None of them is
    // named here on purpose: a token resolvable from the class itself or from
    // `:root` is resolvable under all four and under a fifth nobody has written
    // yet, which is the property that makes the body shared rather than the
    // guard dialog's.
    expect(unscopedTokens(sheets, travelling)).toEqual([]);
  });
});
