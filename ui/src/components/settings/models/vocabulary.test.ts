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
    expect(zh.settings.models.order.subtitle).toBe('在每个型号已有路由中的来源里，排在前面的上游将优先被使用。当额度不足、触发限流、服务端、认证或网络出错导致请求无法完成时，自动切换下一优先级。');
    expect(en.settings.models.order.subtitle).toBe("Among sources already configured in each model's route, those at the top are used first. When quota, rate-limit, server, authentication, or network failures prevent a request, the next priority is used automatically.");
    expect(zh.settings.models.order.section['heldOut.note']).toBe('这些来源仍保留在已有路由里，排在这条顺序之后。要从路由中移除，请编辑对应型号的网关路由。');
    expect(en.settings.models.order.section['heldOut.note']).toBe("These sources remain in existing routes after the ordered sources. To remove one from a route, edit that model's gateway route.");
    expect('policy' in zh.settings.models.agents).toBe(false);
  });
});
