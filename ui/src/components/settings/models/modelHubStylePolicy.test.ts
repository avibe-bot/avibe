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

describe('Model Hub visual token policy', () => {
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

    expect(dialog).toContain('top: min(');
    expect(dialog).toContain('--model-hub-route-top: 300px');
    expect(dialog).toContain('var(--model-hub-route-top)');
    expect(dialog).toContain('box-shadow: var(--model-hub-dialog-shadow)');
    expect(dialog).toMatch(/max-height:\s*calc\([\s\S]*100dvh - var\(--model-hub-route-viewport-inset\) -[\s\S]*var\(--model-hub-route-viewport-inset\)/);
    expect(body).toContain('overflow-y: auto');
  });
});
