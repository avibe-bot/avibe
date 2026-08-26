import { describe, expect, it } from 'vitest';

import en from '../../../i18n/en.json';
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
    expect(copy).not.toMatch(/菜单固定|跟随推荐|中枢 Hub|按量\s*\$|映射|供给引擎|自定义顺序|已改为手动|恢复默认/);
    expect(copy).not.toMatch(/按量(?!付费)/);
    expect(zh.settings.models.billing.monthly).toBe('订阅');
    expect(zh.settings.models.billing.metered).toBe('按量付费');
    expect(en.settings.models.billing.monthly).toBe('Subscription');
    expect(en.settings.models.billing.metered).toBe('Pay as you go');
    expect(zh.settings.models.order.subtitle).toBe('排在前面的上游将优先被使用。当额度不足或出错时自动切换下一优先级。');
    expect(en.settings.models.order.subtitle).toBe('Upstream sources at the top are used first. When quota is insufficient or an error occurs, the next priority is used automatically.');
    expect('policy' in zh.settings.models.agents).toBe(false);
  });
});
