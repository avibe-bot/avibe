import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

import * as ts from 'typescript';
import { describe, expect, it } from 'vitest';

type FailureConsumer = { site: string; classifies: boolean };

function isCallTo(node: ts.Node | undefined, name: string): node is ts.CallExpression {
  return Boolean(
    node
      && ts.isCallExpression(node)
      && ts.isIdentifier(node.expression)
      && node.expression.text === name,
  );
}

/** Every normalized API failure in the dialog, found rather than listed. */
function failureConsumers(url: URL): FailureConsumer[] {
  const path = fileURLToPath(url);
  const source = ts.createSourceFile(
    path,
    readFileSync(url, 'utf8'),
    ts.ScriptTarget.Latest,
    true,
    ts.ScriptKind.TSX,
  );
  const found: FailureConsumer[] = [];
  const visit = (node: ts.Node) => {
    if (
      ts.isVariableDeclaration(node)
      && ts.isIdentifier(node.name)
      && isCallTo(node.initializer, 'apiFailure')
    ) {
      // Read out here, where the guard above still holds: a narrowing on
      // `node.name` does not survive into the nested closure below.
      const declared = node.name.text;
      let scope: ts.Node | undefined = node.parent;
      while (scope && !ts.isBlock(scope)) scope = scope.parent;
      let classifies = false;
      const inspect = (child: ts.Node) => {
        if (
          isCallTo(child, 'classifyOAuthFailure')
          && child.arguments.some(
            (argument) => ts.isIdentifier(argument) && argument.text === declared,
          )
        ) {
          classifies = true;
        }
        ts.forEachChild(child, inspect);
      };
      if (scope) inspect(scope);
      const line = source.getLineAndCharacterOfPosition(node.getStart(source)).line + 1;
      found.push({ site: `${path.slice(path.lastIndexOf('/') + 1)}:${line}`, classifies });
    }
    ts.forEachChild(node, visit);
  };
  visit(source);
  return found;
}

describe('OAuth failure classification invariant', () => {
  it('classifies every API failure consumed by the dialog', () => {
    const consumers = failureConsumers(new URL('./OAuthConnectDialog.tsx', import.meta.url));

    expect(consumers.length).toBeGreaterThan(0);
    expect(consumers.filter(({ classifies }) => !classifies)).toEqual([]);
  });
});
