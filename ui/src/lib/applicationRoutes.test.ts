import { readFileSync } from 'node:fs';

import * as ts from 'typescript';
import { describe, expect, it } from 'vitest';

import {
  APPLICATION_DYNAMIC_ROUTE_PATHS,
  APPLICATION_ROUTE_PATHS,
  inAppChatPath,
  isApplicationRouteHref,
} from './applicationRoutes';
import { LEGACY_SETTINGS_REDIRECTS } from './settingsRoutes';

function routePath(element: ts.JsxOpeningLikeElement): string | null {
  if (element.tagName.getText() !== 'Route') return null;
  const path = jsxAttribute(element, 'path')?.initializer;
  return path && ts.isStringLiteral(path) ? path.text : null;
}

function jsxAttribute(
  element: ts.JsxOpeningLikeElement,
  name: string,
): ts.JsxAttribute | undefined {
  return element.attributes.properties.find(
    (property): property is ts.JsxAttribute =>
      ts.isJsxAttribute(property) && property.name.getText() === name,
  );
}

/** The `to` of a `<Route element={<Navigate to="…" />} />`, or null for a page route. */
function redirectTarget(element: ts.JsxOpeningLikeElement): string | null {
  const initializer = jsxAttribute(element, 'element')?.initializer;
  if (!initializer || !ts.isJsxExpression(initializer)) return null;
  const rendered = initializer.expression;
  if (!rendered || !ts.isJsxSelfClosingElement(rendered)) return null;
  if (rendered.tagName.getText() !== 'Navigate') return null;
  const to = jsxAttribute(rendered, 'to')?.initializer;
  return to && ts.isStringLiteral(to) ? to.text : null;
}

function resolveRoutePath(path: string, parentPath: string | null): string {
  if (path.startsWith('/') || path === '*') return path;
  return parentPath ? `${parentPath.replace(/\/$/, '')}/${path}` : path;
}

type DeclaredRoutes = {
  paths: string[];
  /** Resolved route path -> the path its `<Navigate>` element sends callers to. */
  redirects: Record<string, string>;
};

function declaredRoutes(sourceText: string): DeclaredRoutes {
  const source = ts.createSourceFile(
    'App.tsx',
    sourceText,
    ts.ScriptTarget.Latest,
    true,
    ts.ScriptKind.TSX,
  );
  const paths: string[] = [];
  const redirects: Record<string, string> = {};

  const record = (element: ts.JsxOpeningLikeElement, resolvedPath: string) => {
    paths.push(resolvedPath);
    const target = redirectTarget(element);
    if (target !== null) redirects[resolvedPath] = target;
  };

  const visit = (node: ts.Node, parentPath: string | null) => {
    if (ts.isJsxElement(node)) {
      const path = routePath(node.openingElement);
      const resolvedPath = path === null ? parentPath : resolveRoutePath(path, parentPath);
      if (path !== null) record(node.openingElement, resolvedPath as string);
      node.children.forEach((child) => visit(child, resolvedPath));
      return;
    }
    if (ts.isJsxSelfClosingElement(node)) {
      const path = routePath(node);
      if (path !== null) record(node, resolveRoutePath(path, parentPath));
      return;
    }
    ts.forEachChild(node, (child) => visit(child, parentPath));
  };

  visit(source, null);
  return { paths, redirects };
}

const appRoutes = () =>
  declaredRoutes(readFileSync(new URL('../App.tsx', import.meta.url), 'utf8'));

describe('AppShell route policy', () => {
  it('matches every page and generated legacy redirect declared by App.tsx', () => {
    const declared = [
      ...appRoutes().paths,
      ...LEGACY_SETTINGS_REDIRECTS.map((redirect) => redirect.from),
    ];
    const catalog = [...APPLICATION_ROUTE_PATHS, ...APPLICATION_DYNAMIC_ROUTE_PATHS];

    expect([...declared].sort()).toEqual([...catalog].sort());
  });

  it('sends each retired Settings alias to the page that took its content over', () => {
    // Read off App.tsx itself: an alias that still points at the page it was
    // moved away from is a live wrong destination, not a stale constant.
    const { redirects } = appRoutes();

    // Theme controls moved to General; the rest of Appearance did not survive.
    expect(redirects['/settings/appearance']).toBe('/settings/general');
    // Account remains part of the Replies page, so its alias is unchanged.
    expect(redirects['/settings/account']).toBe('/settings/replies');
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

  it('keeps same-origin chat destinations on the SPA path', () => {
    expect(APPLICATION_DYNAMIC_ROUTE_PATHS).toContain('/chat/:sessionId');
    const current = 'https://alex-app.avibe.bot/chat/session-123';
    expect(inAppChatPath('/chat/session-456?msg=latest#reply')).toBe(
      '/chat/session-456?msg=latest#reply',
    );
    expect(inAppChatPath('/chat/session-456/')).toBe('/chat/session-456');
    expect(inAppChatPath('https://alex-app.avibe.bot/chat/session-456', current)).toBe(
      '/chat/session-456',
    );
    expect(inAppChatPath('https://github.com/avibe-bot/avibe/chat/session-456', current)).toBeNull();
    expect(inAppChatPath('/apps/files')).toBeNull();
    expect(inAppChatPath('/chat/session-456/notes.md')).toBeNull();
    expect(inAppChatPath('./chat/session-456')).toBeNull();
    expect(inAppChatPath('https://alex-app.avibe.bot/chat/session-456')).toBeNull();
  });
});
