import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import ts from 'typescript';
import { describe, expect, it } from 'vitest';

/**
 * The catch-up edge has exactly one owner, stated as a property of every
 * subscription in the tree rather than as a list of the consumers that got it
 * wrong.
 *
 * Three rounds of review found the same defect at four call sites: a consumer
 * decided for itself whether a signal applied to it. Twice it was keyed to call
 * order, once to the signal's payload -- which round 8 deleted, so that form is
 * now untypeable. What a type cannot express is the remaining form: reading the
 * *level* (`onEventBridgeStatus`) as if it were the edge. A consumer that
 * refetches there pays twice for a controller bounce, because that recovery also
 * arrives as `onConnected`; a consumer that only listens there never catches up
 * on a resume, because a returning page has no bridge transition to report.
 *
 * So the level may move state and nothing else, and anything reading it must
 * also subscribe to the edge. A fifth consumer written the old way fails here
 * instead of costing a review round.
 */

const SRC = join(__dirname, '..');

const sourceFiles = () =>
  readdirSync(SRC, { recursive: true, encoding: 'utf8' })
    .filter((file) => /\.tsx?$/.test(file) && !/\.test\.tsx?$/.test(file))
    .map((file) => join(SRC, file));

const rightmostName = (expression: ts.Expression): string | undefined => {
  if (ts.isIdentifier(expression)) return expression.text;
  if (ts.isPropertyAccessExpression(expression)) return expression.name.text;
  return undefined;
};

type Subscription = { file: string; handlers: Map<string, ts.Expression> };

const subscriptions = (): Subscription[] => {
  const found: Subscription[] = [];
  for (const path of sourceFiles()) {
    const text = readFileSync(path, 'utf8');
    if (!text.includes('connectWorkbenchEvents')) continue;
    const source = ts.createSourceFile(path, text, ts.ScriptTarget.Latest, true);
    const visit = (node: ts.Node) => {
      if (
        ts.isCallExpression(node) &&
        rightmostName(node.expression) === 'connectWorkbenchEvents' &&
        node.arguments.length > 0 &&
        ts.isObjectLiteralExpression(node.arguments[0])
      ) {
        const handlers = new Map<string, ts.Expression>();
        for (const property of node.arguments[0].properties) {
          if (!ts.isPropertyAssignment(property) || !property.name) continue;
          const name = ts.isIdentifier(property.name) || ts.isStringLiteral(property.name)
            ? property.name.text
            : undefined;
          if (name) handlers.set(name, property.initializer);
        }
        found.push({ file: path.slice(SRC.length + 1), handlers });
      }
      ts.forEachChild(node, visit);
    };
    visit(source);
  }
  return found;
};

describe('workbench event ownership', () => {
  const all = subscriptions();

  // Vacuity guard: the scan is only a property if it is actually reaching the
  // consumers. Bounds rather than exact counts, so adding a consumer does not
  // edit this test -- but deleting the whole call, renaming the method, or
  // breaking the parse does.
  it('reaches every subscription in the tree', () => {
    expect(all.length).toBeGreaterThanOrEqual(12);
    expect(all.filter(({ handlers }) => handlers.has('onConnected')).length)
      .toBeGreaterThanOrEqual(5);
    expect(all.filter(({ handlers }) => handlers.has('onEventBridgeStatus')).length)
      .toBeGreaterThanOrEqual(4);
  });

  it('never reads the bridge level without the catch-up edge', () => {
    const missing = all
      .filter(({ handlers }) => handlers.has('onEventBridgeStatus') && !handlers.has('onConnected'))
      .map(({ file }) => file);
    expect(missing, 'a bridge-status subscriber must also catch up on onConnected').toEqual([]);
  });

  it('calls nothing but state setters from the bridge level', () => {
    const offenders: string[] = [];
    for (const { file, handlers } of all) {
      const handler = handlers.get('onEventBridgeStatus');
      if (!handler) continue;
      const visit = (node: ts.Node) => {
        if (ts.isCallExpression(node)) {
          const called = rightmostName(node.expression) ?? '<expression>';
          if (!/^set[A-Z]/.test(called)) offenders.push(`${file}: ${called}()`);
        }
        ts.forEachChild(node, visit);
      };
      visit(handler);
    }
    // A read here is the defect: the level is not an edge, so it either pays
    // twice for a bounce that already dispatched `onConnected`, or stands in for
    // an edge it cannot see. Whitelisting setters rather than blacklisting the
    // reads keeps a newly named read from slipping through.
    expect(offenders, 'the bridge level moves state; the catch-up belongs on onConnected').toEqual([]);
  });
});
