import { describe, expect, it } from 'vitest';

import { customPropertiesIn } from './customProperties.mjs';

// `validate:theme` resolves `box-shadow: var(--x)` by looking `--x` up in
// everything the scanned stylesheets declare, and reports a name it cannot find
// as unresolvable. So "what declares a name" decides which correct stylesheets
// the guard rejects: a construct this misses is not a miss at all, it is a
// failure on CSS that renders exactly as intended.

const valuesOf = (css, name) => {
  const found = customPropertiesIn(css).get(name);
  return found === undefined ? undefined : [...found];
};

describe('customPropertiesIn', () => {
  it('collects an ordinary declaration', () => {
    expect(valuesOf(':root { --glow: 0 0 8px red; }', '--glow')).toEqual(['0 0 8px red']);
  });

  // A property is declared once per theme, and a glow smuggled into just one of
  // those blocks still ships. Last-write-wins would hide it behind whichever
  // block the file happens to end with.
  it('keeps every value a name is given, not the last one', () => {
    const css = ':root { --glow: none; }\n[data-theme="light"] { --glow: 0 0 8px red; }\n';

    expect(valuesOf(css, '--glow')).toEqual(['none', '0 0 8px red']);
  });

  it('records the same value once', () => {
    expect(valuesOf(':root { --glow: none; }\n.a { --glow: none; }', '--glow')).toEqual(['none']);
  });

  it('ignores declarations that are not custom properties', () => {
    expect(valuesOf('.a { box-shadow: 0 0 8px red; }', 'box-shadow')).toBeUndefined();
  });

  // The registration form. Its name is in the at-rule's params, and the only
  // declarations inside it are `syntax`, `inherits` and `initial-value` -- so a
  // walk over declarations alone collected nothing, and `var()` on a registered
  // property was reported as naming something no stylesheet declares.
  it('collects a name registered by @property, at its initial value', () => {
    const css = '@property --elevation {\n  syntax: "*";\n  inherits: false;\n  initial-value: none;\n}\n';

    expect(valuesOf(css, '--elevation')).toEqual(['none']);
  });

  it('leaves @property\'s own parameters out of the declared names', () => {
    const css = '@property --elevation { syntax: "*"; inherits: false; initial-value: none; }';
    const collected = [...customPropertiesIn(css).keys()];

    expect(collected).toEqual(['--elevation']);
  });

  // A registration with no initial value still registers the name. With
  // `syntax: "*"` that is legal, and `var()` on it is invalid at computed-value
  // time, so the declaration draws nothing -- declared, and resolving to nothing
  // that could carry a glow. Omitting the name reports the same false positive
  // one spelling further along.
  it('declares a registered name that has no initial value, worth nothing', () => {
    const css = '@property --elevation { syntax: "*"; inherits: false; }';

    expect(valuesOf(css, '--elevation')).toEqual([]);
  });

  // A registration does not exempt the value it registers. `initial-value` is
  // what an unset `var()` resolves to, so a glow written there renders.
  it('carries a glow written as an initial value through to the value set', () => {
    const css = '@property --elevation { syntax: "*"; initial-value: 0 0 93px red; }';

    expect(valuesOf(css, '--elevation')).toEqual(['0 0 93px red']);
  });

  // Sorted, because these two values are found by different walks and their
  // relative order is a fact about traversal rather than about CSS. Asserting
  // it would be a test of how this module happens to be written -- which is the
  // kind of assertion that fails on a change that preserved every behaviour.
  it('lets a registration and a later declaration both count', () => {
    const css = '@property --glow { syntax: "*"; initial-value: none; }\n:root { --glow: 0 0 8px red; }\n';

    expect(valuesOf(css, '--glow').sort()).toEqual(['0 0 8px red', 'none']);
  });

  // The caller folds many stylesheets into one map, so accumulation has to be
  // the module's business rather than each caller's.
  it('folds several stylesheets into one map', () => {
    const into = customPropertiesIn(':root { --a: 1px; }');
    customPropertiesIn(':root { --b: 2px; }', into);

    expect([...into.keys()]).toEqual(['--a', '--b']);
  });
});
