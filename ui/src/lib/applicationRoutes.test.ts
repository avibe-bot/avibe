import { readFileSync } from 'node:fs';

import { describe, expect, it } from 'vitest';

import {
  APPLICATION_DYNAMIC_ROUTE_PATHS,
  APPLICATION_ROUTE_PATHS,
  isApplicationRouteHref,
} from './applicationRoutes';

describe('AppShell route policy', () => {
  it('matches every path declared by App.tsx', () => {
    const appSource = readFileSync(new URL('../App.tsx', import.meta.url), 'utf8');
    const declared = Array.from(appSource.matchAll(/<Route\s+path="([^"]+)"/g), (match) => match[1]);
    const catalog = [...APPLICATION_ROUTE_PATHS, ...APPLICATION_DYNAMIC_ROUTE_PATHS];

    expect([...declared].sort()).toEqual([...catalog].sort());
  });

  it('recognizes exact and dynamic routes without reserving their namespaces', () => {
    for (const path of APPLICATION_ROUTE_PATHS) {
      expect(isApplicationRouteHref(path), path).toBe(true);
    }
    expect(isApplicationRouteHref('/chat/session-123')).toBe(true);
    expect(isApplicationRouteHref('/apps/show/session-123')).toBe(true);
    expect(isApplicationRouteHref('/projects/report.md')).toBe(false);
    expect(isApplicationRouteHref('/admin/settings/custom.json')).toBe(false);
  });
});
