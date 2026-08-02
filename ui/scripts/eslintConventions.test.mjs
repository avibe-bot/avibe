import { beforeAll, describe, expect, it } from 'vitest';
import { ESLint } from 'eslint';

// Probes the real `eslint.config.js` rather than a stand-in, so an accidental
// loosening of the shared config fails here instead of quietly widening what
// the lint gate tolerates across the whole UI.
let eslint;

beforeAll(() => {
  eslint = new ESLint();
});

async function ruleIdsFor(code, filePath = 'src/__probe__/probe.ts') {
  const [result] = await eslint.lintText(code, { filePath });
  return result.messages.map((message) => message.ruleId);
}

const UNUSED = '@typescript-eslint/no-unused-vars';

describe('no-unused-vars honours the `_` opt-out convention', () => {
  it('allows an intentionally unused function parameter', async () => {
    expect(await ruleIdsFor('export const f = (_value: string) => 1;\n')).not.toContain(UNUSED);
  });

  it('allows an intentionally unused local binding', async () => {
    expect(await ruleIdsFor('export function f() {\n  const _legacy = 1;\n  return 2;\n}\n')).not.toContain(UNUSED);
  });

  it('allows an intentionally unused caught error', async () => {
    expect(await ruleIdsFor('export function f() {\n  try {\n    JSON.parse("1");\n  } catch (_err) {\n    return null;\n  }\n  return 1;\n}\n')).not.toContain(UNUSED);
  });

  it('allows an intentionally unused destructured array slot', async () => {
    expect(await ruleIdsFor('export function f(pair: [number, number]) {\n  const [_drop, keep] = pair;\n  return keep;\n}\n')).not.toContain(UNUSED);
  });
});

describe('no-unused-vars still reports genuinely dead bindings', () => {
  it('reports an unused function parameter that is not opted out', async () => {
    expect(await ruleIdsFor('export const f = (value: string) => 1;\n')).toContain(UNUSED);
  });

  it('reports an unused local binding that is not opted out', async () => {
    expect(await ruleIdsFor('export function f() {\n  const legacy = 1;\n  return 2;\n}\n')).toContain(UNUSED);
  });

  it('reports an unused caught error that is not opted out', async () => {
    expect(await ruleIdsFor('export function f() {\n  try {\n    JSON.parse("1");\n  } catch (err) {\n    return null;\n  }\n  return 1;\n}\n')).toContain(UNUSED);
  });
});
