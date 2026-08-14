// Node environment on purpose: the AST sweep below reads this directory through
// `import.meta.url`, which is only a file URL outside a browser-like environment.
// `window` is stubbed per test instead, which is all `providerTab` touches.
import { readdirSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

import * as ts from 'typescript';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { handOffProviderTab, preopenProviderTab, takeHandedProviderTab } from './providerTab';

type FakeTab = { closed: boolean; opener: unknown };

function fakeTab(closed = false): FakeTab {
  return { closed, opener: {} };
}

/** Install a `window.open` and report what it was asked for. */
function stubWindowOpen(behavior: () => Window | null): { calls: unknown[][] } {
  const calls: unknown[][] = [];
  vi.stubGlobal('window', {
    open: (...args: unknown[]) => {
      calls.push(args);
      return behavior();
    },
  });
  return { calls };
}

function sourceFiles(directory: URL): URL[] {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const child = new URL(`${entry.name}${entry.isDirectory() ? '/' : ''}`, directory);
    if (entry.isDirectory()) return sourceFiles(child);
    return /\.(ts|tsx)$/.test(entry.name) && !/\.test\.(ts|tsx)$/.test(entry.name) ? [child] : [];
  });
}

/** Names a call by its identifier, so `foo()` and `obj.foo()` read the same. */
function calledNames(node: ts.Node): Set<string> {
  const names = new Set<string>();
  const visit = (child: ts.Node) => {
    if (ts.isCallExpression(child)) {
      const target = child.expression;
      if (ts.isIdentifier(target)) names.add(target.text);
      else if (ts.isPropertyAccessExpression(target)) names.add(target.name.text);
      if (
        ts.isIdentifier(target) &&
        target.text === 'setPhase' &&
        child.arguments.length > 0 &&
        ts.isStringLiteral(child.arguments[0])
      ) {
        names.add(`setPhase:${child.arguments[0].text}`);
      }
    }
    ts.forEachChild(child, visit);
  };
  ts.forEachChild(node, visit);
  return names;
}

/**
 * Every inline JSX event handler in this feature, with what it does: whether it
 * puts an OAuth journey into its flow phase, and whether it allocates the
 * provider tab while the user's gesture is still on the stack.
 *
 * Found by walking, never listed: a handler added later is swept by construction.
 * The limit is honest — a handler that delegates to a named function defined
 * elsewhere is not followed, which is why both journeys are also asserted
 * end-to-end in `SettingsModelsPage.render.test.tsx`.
 */
function journeyStartHandlers(url: URL): { site: string; allocates: boolean }[] {
  const path = fileURLToPath(url);
  const source = ts.createSourceFile(
    path,
    readFileSync(url, 'utf8'),
    ts.ScriptTarget.Latest,
    true,
    path.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
  );
  const starts = ['retryStart', 'onReauth', 'setPhase:flow'];
  const allocators = ['preopenProviderWindow', 'preopenProviderTab', 'handOffProviderTab'];
  const found: { site: string; allocates: boolean }[] = [];
  const visit = (node: ts.Node) => {
    if (
      ts.isJsxAttribute(node) &&
      node.initializer &&
      ts.isJsxExpression(node.initializer) &&
      node.initializer.expression &&
      ts.isFunctionLike(node.initializer.expression)
    ) {
      const handler = node.initializer.expression;
      const names = calledNames(handler);
      if (starts.some((name) => names.has(name))) {
        const line = source.getLineAndCharacterOfPosition(handler.getStart(source)).line + 1;
        found.push({
          site: `${path.slice(path.lastIndexOf('/') + 1)}:${line} ${node.name.getText(source)}`,
          allocates: allocators.some((name) => names.has(name)),
        });
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(source);
  return found;
}

afterEach(() => {
  vi.unstubAllGlobals();
  takeHandedProviderTab();
});

describe('provider tab', () => {
  it('severs opener on the tab it allocates', () => {
    const tab = fakeTab();
    const { calls } = stubWindowOpen(() => tab as unknown as Window);

    expect(preopenProviderTab()).toBe(tab);
    expect(calls).toEqual([['about:blank', '_blank']]);
    expect(tab.opener).toBeNull();
  });

  it.each([
    ['a blocked popup', () => null],
    ['a throwing window.open', () => { throw new Error('blocked'); }],
  ])('reports no tab for %s', (_label, behavior) => {
    stubWindowOpen(behavior as () => Window | null);

    expect(preopenProviderTab()).toBeNull();
  });

  it('hands the gesture-allocated tab to the journey exactly once', () => {
    const tab = fakeTab();
    stubWindowOpen(() => tab as unknown as Window);

    handOffProviderTab();

    expect(takeHandedProviderTab()).toBe(tab);
    // A second claim must not hand the same tab to a later journey: it has been
    // navigated to the provider by then, and reusing it would replace the page
    // the user is working in.
    expect(takeHandedProviderTab()).toBeNull();
  });

  it('does not hand over a tab the user closed', () => {
    stubWindowOpen(() => fakeTab(true) as unknown as Window);

    handOffProviderTab();

    expect(takeHandedProviderTab()).toBeNull();
  });

  it('has no tab to hand over before a gesture asks for one', () => {
    expect(takeHandedProviderTab()).toBeNull();
  });

  // The invariant behind both re-auth findings on b594ef986: a journey's provider
  // tab must be allocated by the gesture that starts it. The create journey's
  // gesture is inside the dialog, the re-auth journey's is the confirm outside it,
  // and the browser grants the tab to neither one after the start response.
  it('allocates the provider tab in every gesture that starts a journey', () => {
    const handlers = sourceFiles(new URL('./', import.meta.url)).flatMap(journeyStartHandlers);

    // The pattern must exist somewhere, or this asserts nothing at all.
    expect(handlers.length).toBeGreaterThan(0);
    expect(handlers.filter(({ allocates }) => !allocates)).toEqual([]);
  });
});
