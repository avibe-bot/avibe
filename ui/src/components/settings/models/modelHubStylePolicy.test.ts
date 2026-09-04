import { readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';

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

  it('pins the route dialog placement, shadow, and bounded body scroll to named roles', () => {
    const dialog = surfaceCss.match(/\.model-hub-route-dialog\s*\{([^}]*)\}/)?.[1] ?? '';
    const body = surfaceCss.match(/\.model-hub-route-body\s*\{([^}]*)\}/)?.[1] ?? '';

    expect(dialog).toContain('--model-hub-route-offset: min(');
    expect(dialog).toContain('--model-hub-route-top: 300px');
    expect(dialog).toContain('var(--model-hub-route-top)');
    expect(dialog).toContain('top: var(--model-hub-route-offset)');
    expect(dialog).toContain('box-shadow: var(--model-hub-dialog-shadow)');
    // The dialog grows with its chain, so what bounds it is the room below the
    // offset it is anchored at — not the viewport minus two insets.
    expect(dialog).toMatch(/max-height:\s*calc\([\s\S]*100dvh - var\(--model-hub-route-offset\) -[\s\S]*var\(--model-hub-route-viewport-inset\)/);
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
    const selector = surfaceCss.match(/\.model-hub-route-selector\s*\{([^}]*)\}/)?.[1] ?? '';
    const list = surfaceCss.match(/\.model-hub-route-selector-list\s*\{([^}]*)\}/)?.[1] ?? '';

    expect(selector).toContain('--radix-popover-content-available-height');
    expect(selector).toContain('max-height: min(');
    expect(list).toContain('overflow-y: auto');
    expect(list).toContain('min-height: 0');
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
