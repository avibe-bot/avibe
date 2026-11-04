import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

import { isBlock, isCallExpression, isIdentifier, isVariableDeclaration, type CallExpression, type Node } from 'typescript/unstable/ast';
import { describe, expect, it } from 'vitest';

import { parseSource } from '../../../../scripts/typescriptSource.mjs';

type FailureConsumer = { site: string; classifies: boolean };

function isCallTo(node: Node | undefined, name: string): node is CallExpression {
  return Boolean(
    node
      && isCallExpression(node)
      && isIdentifier(node.expression)
      && node.expression.text === name,
  );
}

/** Every normalized API failure in the dialog, found rather than listed. */
function failureConsumers(url: URL): FailureConsumer[] {
  const path = fileURLToPath(url);
  // The dialog is a `.tsx` file, so the extension already selects the TSX grammar.
  const source = parseSource(readFileSync(url, 'utf8'), path);
  const found: FailureConsumer[] = [];
  const visit = (node: Node) => {
    if (
      isVariableDeclaration(node)
      && isIdentifier(node.name)
      && isCallTo(node.initializer, 'apiFailure')
    ) {
      // Read out here, where the guard above still holds: a narrowing on
      // `node.name` does not survive into the nested closure below.
      const declared = node.name.text;
      let scope: Node | undefined = node.parent;
      while (scope && !isBlock(scope)) scope = scope.parent;
      let classifies = false;
      const inspect = (child: Node) => {
        if (
          isCallTo(child, 'classifyOAuthFailure')
          && child.arguments.some(
            (argument) => isIdentifier(argument) && argument.text === declared,
          )
        ) {
          classifies = true;
        }
        child.forEachChild(inspect);
      };
      if (scope) inspect(scope);
      const line = source.getLineAndCharacterOfPosition(node.getStart(source)).line + 1;
      found.push({ site: `${path.slice(path.lastIndexOf('/') + 1)}:${line}`, classifies });
    }
    node.forEachChild(visit);
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
