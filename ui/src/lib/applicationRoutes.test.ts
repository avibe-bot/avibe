import { readFileSync } from 'node:fs';

import * as ts from 'typescript';
import { describe, expect, it } from 'vitest';

import {
  APPLICATION_DYNAMIC_ROUTE_PATHS,
  APPLICATION_ROUTE_PATHS,
  isApplicationRouteHref,
} from './applicationRoutes';

function routePath(element: ts.JsxOpeningLikeElement): string | null {
  if (element.tagName.getText() !== 'Route') return null;
  const path = element.attributes.properties.find(
    (property): property is ts.JsxAttribute =>
      ts.isJsxAttribute(property) && property.name.getText() === 'path',
  );
  return path?.initializer && ts.isStringLiteral(path.initializer)
    ? path.initializer.text
    : null;
}

function resolveRoutePath(path: string, parentPath: string | null): string {
  if (path.startsWith('/') || path === '*') return path;
  return parentPath ? `${parentPath.replace(/\/$/, '')}/${path}` : path;
}

function declaredRoutePaths(sourceText: string): string[] {
  const source = ts.createSourceFile(
    'App.tsx',
    sourceText,
    ts.ScriptTarget.Latest,
    true,
    ts.ScriptKind.TSX,
  );
  const declared: string[] = [];

  const visit = (node: ts.Node, parentPath: string | null) => {
    if (ts.isJsxElement(node)) {
      const path = routePath(node.openingElement);
      const resolvedPath = path === null ? parentPath : resolveRoutePath(path, parentPath);
      if (path !== null) declared.push(resolvedPath as string);
      node.children.forEach((child) => visit(child, resolvedPath));
      return;
    }
    if (ts.isJsxSelfClosingElement(node)) {
      const path = routePath(node);
      if (path !== null) declared.push(resolveRoutePath(path, parentPath));
      return;
    }
    ts.forEachChild(node, (child) => visit(child, parentPath));
  };

  visit(source, null);
  return declared;
}

describe('AppShell route policy', () => {
  it('matches every path declared by App.tsx', () => {
    const appSource = readFileSync(new URL('../App.tsx', import.meta.url), 'utf8');
    const declared = declaredRoutePaths(appSource);
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
