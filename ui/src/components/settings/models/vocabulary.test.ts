import { describe, expect, it } from 'vitest';

import zh from '../../../i18n/zh.json';

const leaves = (node: unknown): string[] =>
  typeof node === 'string'
    ? [node]
    : node && typeof node === 'object'
      ? Object.values(node as Record<string, unknown>).flatMap(leaves)
      : [];

describe('Models page vocabulary', () => {
  it('contains none of the retired user-facing terms', () => {
    const copy = leaves(zh.settings.models).join('\n');
    expect(copy).not.toMatch(/菜单固定|跟随推荐|中枢 Hub|按量\s*\$|映射|供给引擎|自定义顺序/);
    expect(zh.settings.models.order.customized).toBe('已改为手动');
    expect(zh.settings.models.order.restore).toBe('恢复默认');
  });
});
