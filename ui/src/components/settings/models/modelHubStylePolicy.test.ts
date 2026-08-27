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
});
