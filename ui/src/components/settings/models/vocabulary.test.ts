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
    // 「透传」 is jargon; the page says 「原样转发」.
    expect(copy).not.toMatch(/透传/);
    // The own-login mode reads 「自己登录」, matching English "Own login".
    expect(copy).not.toMatch(/Agent 管理/);
    expect(leaves(en.settings.models).join('\n')).not.toMatch(/passthrough|pass through/i);
    expect(zh.settings.models.billing.monthly).toBe('订阅');
    expect(zh.settings.models.billing.metered).toBe('按量付费');
    expect(en.settings.models.billing.monthly).toBe('Subscription');
    expect(en.settings.models.billing.metered).toBe('Pay as you go');
    // A model either follows default routing or has a route set separately; the
    // page uses those two names everywhere and none of the older ones.
    expect(copy).not.toMatch(/手动路由|固定路由|自动路由|继承路由/);
    expect(leaves(en.settings.models).join('\n')).not.toMatch(/manual routes?|fixed routes?|automatic routes?|inherited rout/i);
    // Switching a model back is saved like any other change; the dialog does not call it a restore.
    expect(leaves(zh.settings.models.routing).join('\n')).not.toMatch(/恢复后|撤销恢复/);
    expect(leaves(en.settings.models.routing).join('\n')).not.toMatch(/after restore|undo the restore/i);
    expect(zh.settings.models.routeDialog.mode.follow).toBe('跟随默认路由');
    expect(zh.settings.models.routeDialog.mode.custom).toBe('单独设置');
    expect(en.settings.models.routeDialog.mode.follow).toBe('Follow default routing');
    expect(en.settings.models.routeDialog.mode.custom).toBe('Set separately');
    expect(zh.settings.models.supplyMode.fixHint).toBe('去「模型」页添加或启用供应商即可恢复。');
    expect('policy' in zh.settings.models.agents).toBe(false);
  });
});
