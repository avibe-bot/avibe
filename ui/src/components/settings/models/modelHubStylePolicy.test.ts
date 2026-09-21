import { readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import postcss from 'postcss';

import { describe, expect, it } from 'vitest';

const productFiles = (directory: string): string[] => readdirSync(directory, { withFileTypes: true })
  .flatMap((entry) => {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) return productFiles(path);
    return /\.(?:ts|tsx)$/.test(entry.name) && !/\.test\.(?:ts|tsx)$/.test(entry.name) ? [path] : [];
  });

const surfaceCss = readFileSync(join(__dirname, 'modelHubSurface.css'), 'utf8');
const surfaceCssBody = surfaceCss.replace(/\/\*[\s\S]*?\*\//g, '');

// Cascade correctness of this file's light overrides — that both light blocks
// resolve alike and that every channel-borne ink is re-anchored — is asserted by
// `scripts/validate-theme.mjs`, which resolves the real cascade with postcss and
// already owns the same contract for index.css. What stays here is the source
// hygiene that keeps a dark-only value from being written in the first place.
describe('Model Hub theme token policy', () => {
  it('keeps every surface color on a theme token instead of a baked literal', () => {
    expect(surfaceCssBody.match(/#[\da-f]{3,8}\b/gi) ?? []).toEqual([]);
    const rawChannels = [...surfaceCssBody.matchAll(/\b(?:rgba?|hsla?|oklch)\(\s*(?!var\()/g)];
    expect(rawChannels.map((match) => match[0])).toEqual([]);
  });

  it('leaves no dark-frame "white" vocabulary for light theme to inherit', () => {
    const sources = [...productFiles(__dirname), join(__dirname, 'modelHubSurface.css')];
    const violations = sources.flatMap((path) => (
      [...readFileSync(path, 'utf8').matchAll(/model-hub-[\w-]*white[\w-]*/g)].map((match) => `${path}:${match[0]}`)
    ));

    expect(violations).toEqual([]);
  });
});

describe('Model Hub visual token policy', () => {
  it('uses the passthrough badge ink for active passthrough wires and their legend', () => {
    const root = postcss.parse(surfaceCss);
    const declarations = (selector: string) => {
      const result: Record<string, string> = {};
      root.walkRules(selector, (rule) => rule.walkDecls((decl) => { result[decl.prop] = decl.value; }));
      return result;
    };
    const ink = declarations('.model-hub-route-origin--passthrough').color;
    expect(ink).toBe('var(--gold-ink)');
    expect(declarations('.model-hub-wire--passthrough')).toMatchObject({
      stroke: ink, 'stroke-width': 'var(--model-hub-wire-active-width)',
    });
    expect(declarations('.model-hub-legend-swatch--passthrough').background).toBe(ink);
    expect(declarations('.model-hub-wire--unavailable')['stroke-width']).toBe('var(--model-hub-wire-muted-width)');
  });

  it('contains full Default Routing text in stretched columns and growing rows', () => {
    const root = postcss.parse(surfaceCss);
    const declarations = (selector: string) => {
      const result: Record<string, string> = {};
      root.walkRules((rule) => {
        if (rule.parent?.type === 'root' && rule.selectors.includes(selector)) {
          rule.walkDecls((decl) => { result[decl.prop] = decl.value; });
        }
      });
      return result;
    };
    // Source-order checks of the actual shared owners, not JSDOM geometry.
    expect(declarations('.model-hub-order-identity')).toMatchObject({
      display: 'flex', flex: '1', 'min-width': '0', 'flex-direction': 'column', 'align-items': 'stretch',
    });
    for (const selector of ['.model-hub-order-name', '.model-hub-order-meta']) {
      expect(declarations(selector)).toMatchObject({ 'white-space': 'normal', 'overflow-wrap': 'anywhere' });
    }
    const row = declarations('.model-hub-order-row');
    expect(row).toMatchObject({
      height: 'auto', 'min-height': 'var(--model-hub-order-row-height)',
      '--model-hub-order-row-padding-y': '8px', 'padding-block': 'var(--model-hub-order-row-padding-y)',
      'padding-inline': 'var(--model-hub-order-row-padding-x)',
    });
    expect(row.overflow ?? '').not.toMatch(/hidden|clip/);
    expect(surfaceCss).toContain('--model-hub-order-row-height: 58px;');
    expect(declarations('.model-hub-order-section-head')).toMatchObject({ 'flex-wrap': 'wrap', 'row-gap': '4px' });
    expect(declarations('.model-hub-order-section-head > .model-hub-order-section-explanation')).toMatchObject({
      display: 'block', flex: '1 1 100%', 'min-width': '0', padding: '0', border: '0',
      'border-radius': '0', background: 'transparent', color: 'var(--muted)',
      'font-size': 'var(--model-hub-order-meta-size)', 'font-weight': '400',
      'line-height': '15px', 'white-space': 'normal', 'overflow-wrap': 'anywhere',
    });
    expect(declarations('.model-hub-order-section-head > span')).toEqual({});
  });

  it('keeps Default Routing actions at the shared compact size after drawer styles apply', () => {
    const root = postcss.parse(surfaceCss);
    const declarations = (...selectors: string[]) => {
      const result: Record<string, string> = {};
      root.walkRules((rule) => {
        if (rule.parent?.type === 'root' && rule.selectors.some((selector) => selectors.includes(selector))) {
          rule.walkDecls((decl) => { result[decl.prop] = decl.value; });
        }
      });
      return result;
    };
    // Equal-specificity scoped rules, in source order. Browser geometry is a separate gate.
    expect(declarations('.model-hub-route-action', '.model-hub-order-row-action')).toMatchObject({
      width: '26px', height: '26px', 'min-height': '0', padding: '0',
      'border-radius': '6px', flex: 'none', border: '1px solid var(--border)',
      background: 'var(--model-hub-wash-0a)',
    });
    expect(declarations('.model-hub-route-action > svg', '.model-hub-order-row-action > svg')).toMatchObject({ width: '13px', height: '13px' });
    expect(declarations('.model-hub-order-row-actions')).toMatchObject({
      display: 'flex', flex: 'none', 'align-items': 'center', gap: '3px',
    });
    expect(declarations('.model-hub-order-identity')).toMatchObject({ flex: '1', 'min-width': '0' });
  });

  it('keeps gateway header actions together on the controls row', () => {
    const root = postcss.parse(surfaceCss);
    const actions: Record<string, string> = {};
    root.walkRules('.model-hub-agent-head-actions', (rule) => {
      rule.walkDecls((decl) => { actions[decl.prop] = decl.value; });
    });
    expect(actions).toMatchObject({
      display: 'flex',
      'min-width': '0',
      'flex-wrap': 'nowrap',
      'align-items': 'stretch',
      gap: '8px',
    });
    const button: Record<string, string> = {};
    root.walkRules('.model-hub-agent-head-actions > .model-hub-agent-head-action', (rule) => {
      rule.walkDecls((decl) => { button[decl.prop] = decl.value; });
    });
    expect(button).toMatchObject({
      'min-width': '0', 'min-height': 'var(--model-hub-agent-head-action-height, 36px)',
      height: 'auto', 'white-space': 'normal',
    });
  });

  it('lets the actual narrow footer grow after the complete surface cascade while preserving body scroll', () => {
    // This checks CSS ownership and source order, not browser geometry.
    const root = postcss.parse(surfaceCss);
    const footer: Record<string, string> = {};
    root.walkRules('.model-hub-route-foot', (rule) => {
      expect(rule.parent?.type === 'root' || (rule.parent?.type === 'atrule'
        && rule.parent.name === 'media' && rule.parent.params === '(max-width: 640px)')).toBe(true);
      rule.walkDecls((decl) => { footer[decl.prop] = decl.value; });
    });
    expect(footer).toMatchObject({
      height: 'auto', 'min-height': 'var(--model-hub-dialog-foot-height)',
      'flex-wrap': 'wrap', 'flex-shrink': '0',
    });
    root.walkRules('.model-hub-route-body', (rule) => {
      const body: Record<string, string> = {};
      rule.walkDecls((decl) => { body[decl.prop] = decl.value; });
      expect(body).toMatchObject({ 'min-height': '0', flex: '0 1 auto', 'overflow-y': 'auto' });
    });
  });

  it('grows detail rows from their approved minimum and wraps exact unbroken identities', () => {
    const root = postcss.parse(surfaceCss);
    const declarations = (selector: string) => {
      const values: Record<string, string> = {};
      root.walkRules((rule) => {
        if (rule.parent?.type === 'root' && rule.selectors.includes(selector)) {
          rule.walkDecls((decl) => { values[decl.prop] = decl.value; });
        }
      });
      return values;
    };
    expect(declarations('.model-hub-route-hop')).toMatchObject({
      'min-height': 'var(--model-hub-route-hop-height)', height: 'auto', padding: '6px 10px',
    });
    for (const selector of ['.model-hub-route-hop-name', '.model-hub-route-hop-model']) {
      expect(declarations(selector)).toMatchObject({ 'overflow-wrap': 'anywhere', 'white-space': 'normal' });
    }
  });

  it('lets the settings route pane own overview scrolling', () => {
    const overviewGrid = surfaceCss.match(/\.model-hub-overview-grid\s*\{([^}]*)\}/)?.[1] ?? '';
    const overviewBody = surfaceCss.match(/\.model-hub-overview-body\s*\{([^}]*)\}/)?.[1] ?? '';
    const legendBlocks = [...surfaceCss.matchAll(/\.model-hub-legend\s*\{([^}]*)\}/g)]
      .map((match) => match[1]);

    expect(overviewGrid).not.toMatch(/(?:^|;)\s*height\s*:/);
    expect(overviewBody).not.toMatch(/(?:^|;)\s*height\s*:/);
    expect(legendBlocks).not.toEqual([]);
    for (const legend of legendBlocks) expect(legend).not.toContain('position: absolute');
  });

  it('keeps approximate utility colors out of product surfaces', () => {
    const forbidden = /(?:text-(?:foreground|muted)\/\d+|bg-foreground\/\[[^\]]+\]|border-foreground\/\d+|\btext-(?:gold|violet|mint)\b)/g;
    const violations = productFiles(__dirname).flatMap((path) => (
      [...readFileSync(path, 'utf8').matchAll(forbidden)].map((match) => `${path}:${match[0]}`)
    ));

    expect(violations).toEqual([]);
  });

  it('routes accent-role colors through named tokens', () => {
    const roleBodies = [...surfaceCss.matchAll(/\.model-hub-accent-(?:tile|pill)--[\w-]+\s*\{([^}]*)\}/g)]
      .map((match) => match[1]);
    const literals = roleBodies.flatMap((body) => body.match(/#[\da-f]{6,8}/gi) ?? []);

    expect(literals).toEqual([]);
  });

  // Every modal in `design.pen` whose frame carries an explicit height sits at
  // exactly (viewport - dialog) / 2. This one is the file's single hug-height
  // frame, so its 300px `y` is a round stand-in for the 332.5px its own
  // head/body/foot measure to — and read as a literal `top` it held at the
  // 1100px artboard and nowhere else, which is how the dialog came to sit near
  // the bottom of an ordinary laptop viewport.
  it('centres the route dialog and leaves it the room a centred box has', () => {
    const dialog = surfaceCssBody.match(/\.model-hub-route-dialog\s*\{([^}]*)\}/)?.[1] ?? '';
    const body = surfaceCssBody.match(/\.model-hub-route-body\s*\{([^}]*)\}/)?.[1] ?? '';
    const root = readFileSync(join(__dirname, 'RouteChainDialog.tsx'), 'utf8');

    // Placement through the shared modal idiom, so nothing here has to track
    // the dialog's measured height.
    expect(root).toContain('model-hub-route-dialog fixed left-1/2 top-1/2');
    expect(root).toContain('-translate-x-1/2 -translate-y-1/2');
    expect(dialog).not.toMatch(/(?:^|;)\s*top\s*:/);
    expect(dialog).not.toContain('--model-hub-route-top');
    expect(dialog).not.toContain('--model-hub-route-offset');
    expect(dialog).toContain('box-shadow: var(--model-hub-dialog-shadow)');
    // A centred box of at most this height clears both insets by construction,
    // so a long chain can no longer push the footer off the bottom.
    expect(dialog).toMatch(/max-height:\s*calc\(\s*100dvh - var\(--model-hub-route-viewport-inset\) -\s*var\(--model-hub-route-viewport-inset\)\s*\)/);
    expect(body).not.toMatch(/^\s*height:/m);
    expect(body).toContain('overflow-y: auto');
  });

  // Containment only: that the picker cannot grow past the room the popover
  // reports, and that the overflow lands on the list rather than on the panel.
  // It deliberately says nothing about whether a wheel reaches that list — CSS
  // can declare a perfectly scrollable box whose wheel events are cancelled by
  // an ancestor's scroll lock, which is exactly the defect this file's earlier
  // version read as fixed. That property is an event outcome and is owned by
  // `ui/src/components/ui/anchored-selection-scroll.test.tsx` and by the add-hop
  // case in `RouteChainDialog.test.tsx`.
  it('bounds the add-hop picker against the space the popover reports', () => {
    const selector = surfaceCssBody.match(/\.model-hub-route-selector\s*\{([^}]*)\}/)?.[1] ?? '';
    const list = surfaceCssBody.match(/\.model-hub-route-selector-list\s*\{([^}]*)\}/)?.[1] ?? '';

    expect(selector).toContain('--radix-popover-content-available-height');
    expect(selector).toMatch(/max-height:\s*max\(\s*calc\(var\(--model-hub-route-selector-bands\) \+ var\(--model-hub-route-selector-frame\)\),\s*min\(\s*var\(--model-hub-route-selector-max\),\s*var\(--model-hub-route-selector-room\)/);
    expect(list).toContain('overflow-y: auto');
  });

  // The panel is one flexible list plus bands that cannot shrink, so the list is
  // the only child a new band can be paid for out of — which is what happened:
  // the manual source/model pair went straight into the column, nothing
  // recomputed, and the list was left a few pixels of green. The floor makes
  // that arithmetic, and this is where the arithmetic has to hold. That the
  // panel drawn in a browser contains nothing the arithmetic missed is the other
  // half, and belongs to the rendered census in `RouteChainDialog.test.tsx`.
  it('keeps a three-row list floor no fixed band can take back', () => {
    const selector = surfaceCssBody.match(/\.model-hub-route-selector\s*\{([^}]*)\}/)?.[1] ?? '';
    const list = surfaceCssBody.match(/\.model-hub-route-selector-list\s*\{([^}]*)\}/)?.[1] ?? '';
    const candidate = surfaceCssBody.match(/\.model-hub-route-candidate\s*\{([^}]*)\}/)?.[1] ?? '';
    const px = (source: string, property: string) => {
      const value = source.match(new RegExp(`${property}:\\s*(\\d+(?:\\.\\d+)?)px`))?.[1];
      expect(value, `${property} is not declared in px`).toBeDefined();
      return Number(value);
    };

    // Three rows wherever the room reaches, and the room minus the bands where
    // it does not — a floor held past that point would not produce a list, only
    // a confirm button below the fold, which is unreachable under a popover
    // that neither flips nor scrolls the page.
    expect(list).toMatch(/min-height:\s*clamp\(\s*0px,\s*calc\(\s*min\(var\(--model-hub-route-selector-max\), var\(--model-hub-route-selector-room\)\) -\s*var\(--model-hub-route-selector-frame\) - var\(--model-hub-route-selector-bands\)\s*\),\s*var\(--model-hub-route-selector-list-min\)\s*\)/);
    expect(px(selector, '--model-hub-route-selector-list-min'))
      .toBeGreaterThanOrEqual(px(candidate, 'min-height') * 3);

    // Every band that cannot shrink has a term, so the room the floor is
    // measured against is the room the list can actually be given.
    const bands = [
      '--model-hub-route-selector-search-height',
      '--model-hub-route-selector-head-height',
      '--model-hub-route-selector-manual-height',
      '--model-hub-route-selector-manual-fields-height',
      '--model-hub-route-selector-foot-height',
    ];
    const summed = [...(selector.match(/--model-hub-route-selector-bands:([^;]*);/)?.[1] ?? '')
      .matchAll(/var\((--[\w-]+)\)/g)].map((match) => match[1]);
    expect(summed).toEqual(bands);

    // What each term comes to is a rendered length — a term is a composition of
    // other terms and jsdom resolves no `calc()` — so the arithmetic itself
    // (every term against the band it draws, and the folded bands plus the
    // floor still under the cap) is measured in the rendered census in
    // `e2e/model-catalog/route-direct-edit.spec.ts`. What belongs here is the
    // shape that arithmetic rests on: the bands are drawn with a hairline
    // between them and the popover draws one around itself, and `max-height` is
    // border-box. Left out, both were the panel overflowing the bound it had
    // just declared by exactly the border it keeps around the confirm row.
    expect(selector).toMatch(
      /--model-hub-route-selector-frame:\s*calc\(var\(--model-hub-route-selector-hairline\) \* 2\)/,
    );
    for (const band of ['search-height', 'foot-height']) {
      expect(selector.match(new RegExp(`--model-hub-route-selector-${band}:([^;]*);`))?.[1])
        .toContain('var(--model-hub-route-selector-hairline)');
    }

    // Each half of the disclosure is a band only where it is drawn — an
    // inventory with nothing to type by hand renders neither, and a folded one
    // only its toggle. Both default to zero and are turned on by the state the
    // panel is in, so the lower bound never holds the panel open for a row that
    // is not on screen. Opened, the fields term is every part of what they draw
    // rather than a number measured once off a screenshot.
    const state = (modifier: string) =>
      surfaceCssBody.match(new RegExp(`\\.model-hub-route-selector--${modifier}\\s*\\{([^}]*)\\}`))?.[1] ?? '';
    expect(px(selector, '--model-hub-route-selector-manual-height')).toBe(0);
    expect(state('manual\\b')).toMatch(
      /--model-hub-route-selector-manual-height:\s*calc\(\s*var\(--model-hub-route-selector-hairline\) \+ var\(--model-hub-route-selector-manual-row\)\s*\)/,
    );
    expect(px(selector, '--model-hub-route-selector-manual-fields-height')).toBe(0);
    const opened = state('manual-open');
    expect([...opened.matchAll(/var\((--[\w-]+)\)/g)].map((match) => match[1])).toEqual([
      '--model-hub-route-custom-label-height',
      '--model-hub-route-custom-field-gap',
      '--model-hub-route-custom-source-height',
      '--model-hub-route-custom-model-height',
      '--model-hub-route-custom-pad-block-end',
    ]);
    expect(opened).toMatch(/--model-hub-route-selector-manual-fields-height:\s*calc\(/);
    // The pair is a two-column grid, so what it adds is the taller field column
    // — not both controls summed as though one sat under the other, which is a
    // row's worth of list room given up for nothing.
    expect(opened).toMatch(
      /max\(\s*var\(--model-hub-route-custom-source-height\),\s*var\(--model-hub-route-custom-model-height\)\s*\)/,
    );

    // The toggle draws the height its own term names, not the budgeted one:
    // that one is zero unless the panel says the row is there.
    const toggle = surfaceCssBody.match(/\.model-hub-route-selector-manual-toggle\s*\{([^}]*)\}/)?.[1] ?? '';
    expect(toggle).toContain('height: var(--model-hub-route-selector-manual-row)');
  });

  // The surface draws the same 10.5px status pill in six places (upstream count,
  // gateway port, model-count badge, source kind, model mode, direct kind). Each
  // used to carry its own copy of the utility bundle, which is how all six ended
  // up 1px taller than the frame at once. One class owns the box now; a seventh
  // pill written the old way fails here rather than costing a review round.
  //
  // A pill is a *padded* round box at that type size — which is what separates it
  // from the fixed-size round markers (the mint step numbers) that legitimately
  // share the type size without sharing the shape.
  it('gives the 10.5px status pill exactly one shape owner', () => {
    const pill = surfaceCss.match(/\.model-hub-pill\s*\{([^}]*)\}/)?.[1] ?? '';
    expect(pill).toContain('font-size: 10.5px');
    expect(pill).toMatch(/line-height:\s*\d/);

    const restated = productFiles(__dirname).flatMap((path) => (
      [...readFileSync(path, 'utf8').matchAll(/(['"`])((?:(?!\1)[\s\S])*)\1/g)]
        .filter((match) => /rounded-full/.test(match[2])
          && /text-\[10\.5px\]/.test(match[2])
          && /(?:^|\s)px-/.test(match[2]))
        .map((match) => `${path}:${match[2]}`)
    ));

    expect(restated).toEqual([]);
  });

  // Both properties are decided by CSS that jsdom does not resolve — a grid track
  // and a line break — so they are asserted where they are declared. They are
  // stated as one rule each rather than as the rows that must not break: the
  // manual-draft band broke because 手动添加 has no min-content floor to overflow
  // against, and the same shape was already latent on any committed row whose
  // model id was long enough to make its own pill the part that gives.
  it('never lets a source-table pill be the part of a row that gives', () => {
    const pill = surfaceCss.match(/\.model-hub-source-pill \{([^}]*)\}/)?.[1] ?? '';
    expect(pill).toContain('white-space: nowrap');
    expect(pill).toContain('flex-shrink: 0');

    // And no call site hands the give back with a utility, whichever pill it draws.
    const reopened = productFiles(__dirname).flatMap((path) => (
      [...readFileSync(path, 'utf8').matchAll(/(['"`])((?:(?!\1)[\s\S])*)\1/g)]
        .filter((match) => /model-hub-source-pill/.test(match[2])
          && /(?:^|\s)(?:whitespace-(?!nowrap)|shrink(?!-0)|flex-shrink)/.test(match[2]))
        .map((match) => `${path}:${match[2]}`)
    ));

    expect(reopened).toEqual([]);
  });

  // A column heading is a claim about the cells beneath it, so the draft either
  // keeps the row's tracks or stops standing in them. It cannot do both: its
  // controls are words where a row's are two 26px icons, and the only cell with
  // room to pay the difference is the shared 1fr — which slides the tier cell
  // out from under 推理强度. The draft takes its own two lines instead.
  it('keeps the manual draft off the row columns and out of column arithmetic', () => {
    // The property rather than the draft's own rule: whichever rule hands out
    // the row template, its selector list must not name the draft. Reading only
    // the override would pass while the shared rule still declared the token on
    // the draft earlier and left source order to clean up after it.
    const sharing = [...surfaceCssBody.matchAll(/([^{}]*)\{[^{}]*var\(--model-hub-source-table-columns\)[^{}]*\}/g)]
      .map((match) => match[1]);
    expect(sharing.some((selector) => selector.includes('model-hub-source-table-row'))).toBe(true);
    for (const selector of sharing) expect(selector).not.toContain('model-hub-source-table-draft');

    // Anchored on the preceding `}` so this is the draft's own rule, not the
    // shared band whose selector list also ends in this class.
    const draft = surfaceCssBody.match(/\}\s*\.model-hub-source-table-draft \{([^}]*)\}/)?.[1] ?? '';
    expect(draft).toContain('grid-template-columns: minmax(0, 1fr) auto');

    // Full width as a span to both edges, never as a count: the count has
    // already changed once, and the cell that carried it through the change is
    // how the failure line lands in an implicit fourth track.
    const line = surfaceCssBody.match(/\.model-hub-source-draft-line \{([^}]*)\}/)?.[1] ?? '';
    expect(line).toContain('grid-column: 1 / -1');
  });
});
