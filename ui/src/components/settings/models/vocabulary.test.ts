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
  it('describes an empty inherited route as lacking an eligible default provider', () => {
    expect(en.settings.models.routeDialog.empty).toBe('This model follows default routing, but no eligible provider can route it.');
    expect(zh.settings.models.routeDialog.empty).toBe('此模型跟随默认路由，但目前没有符合条件的供应商。');
  });

  it('contains none of the retired user-facing terms', () => {
    const copy = leaves(zh.settings.models).join('\n');
    expect(copy).not.toMatch(/菜单固定|跟随推荐|中枢 Hub|按量\s*\$|映射|供给|来源|型号|自定义顺序|已改为手动|恢复默认/);
    expect(copy).not.toMatch(/按量(?!付费)/);
    expect(zh.settings.models.billing.monthly).toBe('订阅');
    expect(zh.settings.models.billing.metered).toBe('按量付费');
    expect(en.settings.models.billing.monthly).toBe('Subscription');
    expect(en.settings.models.billing.metered).toBe('Pay as you go');
    expect(zh.settings.models.order.subtitle).toBe('作用于自动和透传路由，已保存的手动路由保持不变。');
    expect(en.settings.models.order.subtitle).toBe('Applies to automatic and passthrough routes. Saved manual routes stay unchanged.');
    expect(zh.settings.models.order.section['heldOut.note']).toBe('可用于手动路由。');
    expect(en.settings.models.order.section['heldOut.note']).toBe('Available for manual routes.');
    expect(zh.settings.models.supplyMode.fixHint).toBe('去「模型」页添加或启用供应商即可恢复。');
    expect('policy' in zh.settings.models.agents).toBe(false);
  });
});
