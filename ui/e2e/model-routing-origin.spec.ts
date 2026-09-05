import { hub as copy } from './support/copy';
import { expect, test } from './support/gateway';
import { labelledButton } from './support/hub';
import { captureAgentChain, restoreAgentChain } from './support/restore';

test('MH-ROUTING-007 restore, undo, cancel, and save preserve explicit route intent', async ({ api, gateway, hub, page }) => {
  const original = await captureAgentChain(api, gateway);
  const manual = { hops: [{ source_id: gateway.sources[0].id, model_id: gateway.model }] };
  try {
    expect(await api.putAgentChain(gateway.backend, gateway.model, manual.hops)).toBe(true);
    await hub.goto();
    await hub.openRoute(gateway.backend, gateway.model);
    const dialog = hub.routeDialog;
    await expect(dialog).toBeVisible();
    const mutations: string[] = [];
    page.on('request', (request) => {
      if (['PUT', 'DELETE'].includes(request.method()) && new URL(request.url()).pathname.endsWith('/chain')) {
        mutations.push(request.method());
      }
    });
    const restore = () => labelledButton(dialog, copy('routing.restoreAutomatic')).click();
    await restore();
    await expect(labelledButton(dialog, copy('routing.undoRestore'))).toBeVisible();
    expect((await api.agentChain(gateway.backend, gateway.model)).manual_override).toEqual(manual);
    await labelledButton(dialog, copy('routing.undoRestore')).click();
    await expect(labelledButton(dialog, copy('routing.restoreAutomatic'))).toBeVisible();
    expect((await api.agentChain(gateway.backend, gateway.model)).manual_override).toEqual(manual);
    expect(mutations).toEqual([]);

    await restore();
    await expect(labelledButton(dialog, copy('routing.undoRestore'))).toBeVisible();
    await labelledButton(dialog, copy('routeDialog.cancel')).click();
    await expect(dialog).not.toBeVisible();
    expect((await api.agentChain(gateway.backend, gateway.model)).manual_override).toEqual(manual);
    expect(mutations).toEqual([]);

    await hub.openRoute(gateway.backend, gateway.model);
    await restore();
    await expect(labelledButton(dialog, copy('routing.undoRestore'))).toBeVisible();
    await labelledButton(dialog, copy('routeDialog.save')).click();
    await expect.poll(async () => (
      (await api.agentChain(gateway.backend, gateway.model)).manual_override === null
      || await labelledButton(dialog, copy('guard.confirm.saveRoute')).isVisible()
    )).toBe(true);
    const confirm = labelledButton(dialog, copy('guard.confirm.saveRoute'));
    if (await confirm.isVisible()) await confirm.click();
    await expect.poll(async () => (await api.agentChain(gateway.backend, gateway.model)).manual_override).toBeNull();
    expect(mutations.length).toBeGreaterThan(0);
    expect(new Set(mutations)).toEqual(new Set(['DELETE']));
    await page.reload();
    expect((await api.agentChain(gateway.backend, gateway.model)).manual_override).toBeNull();
  } finally {
    await restoreAgentChain(api, gateway, original);
  }
});
