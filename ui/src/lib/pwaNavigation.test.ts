import { readdirSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

import * as ts from 'typescript';
import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  internalPwaLinkTarget,
  openLinkInNewContext,
  shouldBlockPwaLoopbackLink,
} from './pwaNavigation';

function sourceFiles(directory: URL): URL[] {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const child = new URL(`${entry.name}${entry.isDirectory() ? '/' : ''}`, directory);
    if (entry.isDirectory()) return sourceFiles(child);
    return /\.(ts|tsx)$/.test(entry.name) && !/\.test\.(ts|tsx)$/.test(entry.name) ? [child] : [];
  });
}

function directWindowOpenCount(url: URL): number {
  const path = fileURLToPath(url);
  const source = ts.createSourceFile(
    path,
    readFileSync(url, 'utf8'),
    ts.ScriptTarget.Latest,
    true,
    path.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
  );
  let count = 0;
  const visit = (node: ts.Node) => {
    if (
      ts.isCallExpression(node) &&
      ts.isPropertyAccessExpression(node.expression) &&
      ts.isIdentifier(node.expression.expression) &&
      node.expression.expression.text === 'window' &&
      node.expression.name.text === 'open'
    ) {
      count += 1;
    }
    ts.forEachChild(node, visit);
  };
  visit(source);
  return count;
}

function seversOpener(node: ts.Node): boolean {
  let found = false;
  const visit = (child: ts.Node) => {
    if (
      ts.isBinaryExpression(child) &&
      child.operatorToken.kind === ts.SyntaxKind.EqualsToken &&
      ts.isPropertyAccessExpression(child.left) &&
      child.left.name.text === 'opener' &&
      child.right.kind === ts.SyntaxKind.NullKeyword
    ) {
      found = true;
    }
    ts.forEachChild(child, visit);
  };
  ts.forEachChild(node, visit);
  return found;
}

/**
 * Preallocated blank tabs that keep an `opener` are reverse-tabnabbing holes:
 * whatever is navigated into them later can drive this window. Report each
 * such tab with whether the function that opened it severs the reference.
 */
function blankTabOpeners(url: URL): { line: number; severed: boolean }[] {
  const path = fileURLToPath(url);
  const source = ts.createSourceFile(
    path,
    readFileSync(url, 'utf8'),
    ts.ScriptTarget.Latest,
    true,
    path.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
  );
  const found: { line: number; severed: boolean }[] = [];
  const visit = (node: ts.Node) => {
    if (
      ts.isCallExpression(node) &&
      ts.isPropertyAccessExpression(node.expression) &&
      ts.isIdentifier(node.expression.expression) &&
      node.expression.expression.text === 'window' &&
      node.expression.name.text === 'open' &&
      node.arguments.length > 0 &&
      ts.isStringLiteral(node.arguments[0]) &&
      node.arguments[0].text === 'about:blank'
    ) {
      let scope: ts.Node = node;
      while (scope.parent && !ts.isFunctionLike(scope)) scope = scope.parent;
      found.push({
        line: source.getLineAndCharacterOfPosition(node.getStart(source)).line + 1,
        severed: seversOpener(scope),
      });
    }
    ts.forEachChild(node, visit);
  };
  visit(source);
  return found;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('PWA navigation', () => {
  const remotePage = 'https://alex-app.avibe.bot/chat/session-123';

  it.each([
    'http://localhost:5123',
    'http://dev.localhost:5173/path',
    'http://127.0.0.1:15130/chat/session-456',
    'http://127.12.34.56/path',
    'http://[::1]:5123/path',
  ])('blocks a loopback target from a remote page: %s', (href) => {
    expect(shouldBlockPwaLoopbackLink(href, remotePage)).toBe(true);
  });

  it.each([
    '/chat/session-456',
    'https://github.com/avibe-bot/avibe',
    'https://192.168.1.20:5123',
    'mailto:hello@example.com',
    'not a url',
  ])('allows a non-loopback target: %s', (href) => {
    expect(shouldBlockPwaLoopbackLink(href, remotePage)).toBe(false);
  });

  it('allows loopback links when Avibe itself is open on loopback', () => {
    expect(
      shouldBlockPwaLoopbackLink(
        'http://127.0.0.1:15130/chat/session-456',
        'http://127.0.0.1:5123/chat/session-123',
      ),
    ).toBe(false);
  });
});

describe('internalPwaLinkTarget', () => {
  const current = 'https://alex-app.avibe.bot/chat/session-123';

  it('opens private Show Pages at their literal document route', () => {
    expect(internalPwaLinkTarget('/show/ses_123/', current)).toEqual({
      path: '/show/ses_123/',
      navigation: 'document',
    });
    expect(internalPwaLinkTarget('https://alex-app.avibe.bot/show/a%20b%2Fc/', current)).toEqual({
      path: '/show/a%20b%2Fc/',
      navigation: 'document',
    });
  });

  it('preserves private Show Page query, fragment, and nested route state', () => {
    expect(internalPwaLinkTarget('/show/ses_123/?tab=flow#top', current)).toEqual({
      path: '/show/ses_123/?tab=flow#top',
      navigation: 'document',
    });
    expect(internalPwaLinkTarget('/show/ses_123/projects/alpha?mode=edit#node-2', current)).toEqual({
      path: '/show/ses_123/projects/alpha?mode=edit#node-2',
      navigation: 'document',
    });
  });

  it('keeps public Show Pages in context while preserving their server document', () => {
    expect(internalPwaLinkTarget('/p/share_123/?theme=dark#chart', current)).toEqual({
      path: '/p/share_123/?theme=dark#chart',
      navigation: 'document',
    });
    expect(internalPwaLinkTarget('/p/share_123/projects/alpha?theme=dark#chart', current)).toEqual({
      path: '/p/share_123/projects/alpha?theme=dark#chart',
      navigation: 'document',
    });
  });

  it('keeps canonical app routes on the SPA path', () => {
    expect(internalPwaLinkTarget('/chat/session-456?msg=latest#reply', current)).toEqual({
      path: '/chat/session-456?msg=latest#reply',
      navigation: 'spa',
    });
    expect(internalPwaLinkTarget('/admin/settings/models?source=custom', current)).toEqual({
      path: '/admin/settings/models?source=custom',
      navigation: 'spa',
    });
    expect(internalPwaLinkTarget('/admin/settings/memory#profile', current)).toEqual({
      path: '/admin/settings/memory#profile',
      navigation: 'spa',
    });
  });

  it('keeps every other same-origin destination in the current document', () => {
    expect(internalPwaLinkTarget('/api/files/report.pdf?download=0#page=2', current)).toEqual({
      path: '/api/files/report.pdf?download=0#page=2',
      navigation: 'document',
    });
    expect(internalPwaLinkTarget('/custom/help?topic=pwa#recovery', current)).toEqual({
      path: '/custom/help?topic=pwa#recovery',
      navigation: 'document',
    });
  });

  it('leaves external and non-http destinations to their existing handlers', () => {
    expect(internalPwaLinkTarget('https://github.com/avibe-bot/avibe', current)).toBeNull();
    expect(internalPwaLinkTarget('https://alex-app.avibe.bot:8443/help', current)).toBeNull();
    expect(internalPwaLinkTarget('mailto:hello@example.com', current)).toBeNull();
  });
});

describe('programmatic PWA navigation', () => {
  it('uses the installed-PWA bridge before opening a new context', () => {
    const bridge = vi.fn((href: string) => href.startsWith('/chat/'));
    const popup = {} as Window;
    const nativeOpen = vi.fn(() => popup);
    vi.stubGlobal('window', {
      __AVIBE_PWA_NAVIGATE_SAME_ORIGIN__: bridge,
      open: nativeOpen,
    });

    expect(openLinkInNewContext('/chat/session-456', 'noopener')).toBeNull();
    expect(nativeOpen).not.toHaveBeenCalled();

    expect(openLinkInNewContext('https://github.com/avibe-bot/avibe', 'noopener,noreferrer')).toBe(popup);
    expect(nativeOpen).toHaveBeenCalledWith(
      'https://github.com/avibe-bot/avibe',
      '_blank',
      'noopener,noreferrer',
    );
  });

  it('keeps direct window.open calls confined to the shared helper and the preallocated desktop tab', () => {
    const sourceRoot = new URL('../', import.meta.url);
    const directCalls = sourceFiles(sourceRoot)
      .map((url) => ({
        path: fileURLToPath(url).slice(fileURLToPath(sourceRoot).length),
        count: directWindowOpenCount(url),
      }))
      .filter(({ count }) => count > 0)
      .sort((left, right) => left.path.localeCompare(right.path));

    expect(directCalls).toEqual([
      { path: 'components/settings/models/providerTab.ts', count: 1 },
      { path: 'components/workbench/ShowPageLaunchControl.tsx', count: 1 },
      { path: 'lib/pwaNavigation.ts', count: 1 },
    ]);

  });

  it('severs the opener of every preallocated blank tab', () => {
    const sourceRoot = new URL('../', import.meta.url);
    const rootPath = fileURLToPath(sourceRoot);
    const tabs = sourceFiles(sourceRoot).flatMap((url) =>
      blankTabOpeners(url).map(({ line, severed }) => ({
        site: `${fileURLToPath(url).slice(rootPath.length)}:${line}`,
        severed,
      })),
    );

    // The pattern must exist somewhere, or this asserts nothing at all.
    expect(tabs.length).toBeGreaterThan(0);
    expect(tabs.filter(({ severed }) => !severed)).toEqual([]);
  });
});
