import type { AgentChain } from './support/api';
import { hub as copy } from './support/copy';
import { BASE_URL } from './support/env';
import { expect, test } from './support/gateway';
import { labelledButton } from './support/hub';
import { captureAgentChain, restoreAgentChain } from './support/restore';

test('MH-ROUTING-007 restore, undo, cancel, and save preserve explicit route intent', async ({ api, gateway, hub, page }) => {
  const original = await captureAgentChain(api, gateway);
  const originalOrder = await api.defaultSourceOrder(gateway.backend);
  const manual = { hops: [{ source_id: gateway.sources[0].id, model_id: `${gateway.model}/manual-original` }] };
  const unsavedModel = `${gateway.model}/manual-draft`;
  const identities = (chain: AgentChain) => chain.chain.map(({ source_id, model_id }) => ({ source_id, model_id }));
  const chainUrl = new URL(`/api/models/agents/${gateway.backend}/chain?model=${encodeURIComponent(gateway.model)}`, BASE_URL).href;
  const previewUrl = new URL(`/api/models/agents/${gateway.backend}/chain/preview?model=${encodeURIComponent(gateway.model)}`, BASE_URL).href;
  try {
    await api.setDefaultSourceOrder(gateway.backend, gateway.sources.map((source) => source.id));
    expect(await api.deleteAgentChain(gateway.backend, gateway.model)).toBe(true);
    const inherited = await api.agentChain(gateway.backend, gateway.model);
    expect(inherited.manual_override).toBeNull();
    expect(inherited.chain.length).toBeGreaterThan(0);
    expect(identities(inherited).every((hop) => gateway.sources.some((source) => source.id === hop.source_id))).toBe(true);
    expect(await api.putAgentChain(gateway.backend, gateway.model, manual.hops)).toBe(true);
    await hub.goto();
    await hub.openRoute(gateway.backend, gateway.model);
    const dialog = hub.routeDialog;
    await expect(dialog).toBeVisible();
    const mutations: string[] = [];
    page.on('request', (request) => {
      if (['PUT', 'DELETE'].includes(request.method()) && request.url() === chainUrl) {
        mutations.push(request.method());
      }
    });
    const preview = async (action: () => Promise<void>) => {
      const responsePromise = page.waitForResponse((response) => response.url() === previewUrl && response.request().method() === 'POST');
      await action();
      const response = await responsePromise;
      expect(response.status()).toBe(200);
      expect(response.request().postDataJSON()).toEqual({ manual_override: null });
      const body = await response.json() as { ok: boolean; contract_version: number; chain: AgentChain };
      expect(body.ok).toBe(true);
      expect(body.contract_version).toBe(10);
      expect(body.chain.manual_override).toBeNull();
      expect(body.chain.route_origin).toBe(inherited.route_origin);
      expect(identities(body.chain)).toEqual(identities(inherited));
      await expect(dialog.locator('.model-hub-route-preview')).toHaveAttribute('data-origin', inherited.route_origin!);
      await expect(dialog.locator('.model-hub-route-hop-model')).toHaveText(inherited.chain.map((hop) => hop.model_id));
      await expect(labelledButton(dialog, copy('routing.undoRestore'))).toBeEnabled();
      await expect(labelledButton(dialog, copy('routeDialog.save'))).toBeEnabled();
      expect((await api.agentChain(gateway.backend, gateway.model)).manual_override).toEqual(manual);
      expect(mutations).toEqual([]);
    };
    const removeLast = async () => {
      await expect(dialog.locator('.model-hub-route-hop')).toHaveCount(1);
      await preview(() => dialog.getByRole('button', { name: copy('routeDialog.removeHop'), exact: true }).press('Enter'));
    };
    await preview(() => labelledButton(dialog, copy('routing.restoreAutomatic')).click());
    await labelledButton(dialog, copy('routing.undoRestore')).click();
    await expect(labelledButton(dialog, copy('routing.restoreAutomatic'))).toBeVisible();
    await expect(dialog.locator('.model-hub-route-hop-model')).toHaveText([manual.hops[0].model_id]);
    expect((await api.agentChain(gateway.backend, gateway.model)).manual_override).toEqual(manual);
    expect(mutations).toEqual([]);

    // Undo must preserve the unsaved candidate, not reconstruct the saved route.
    await dialog.getByRole('button', { name: copy('routeDialog.editHop'), exact: true }).click();
    const selector = page.locator('.model-hub-route-selector');
    await selector.getByLabel(copy('routeDialog.add.source'), { exact: true }).selectOption(gateway.sources[0].id);
    await selector.getByLabel(copy('routing.exactModel'), { exact: true }).fill(unsavedModel);
    await expect(selector.getByRole('button', { name: copy('routeDialog.add.confirm'), exact: true })).toHaveCount(0);
    const replace = selector.getByRole('button', { name: copy('routeDialog.edit.confirm'), exact: true });
    await expect(replace).toBeEnabled();
    await replace.click();
    await expect(selector).toHaveCount(0);
    await expect(dialog.locator('.model-hub-route-hop-name')).toHaveText([gateway.sources[0].display_name]);
    await expect(dialog.locator('.model-hub-route-hop-model')).toHaveText([unsavedModel]);
    await removeLast();
    await labelledButton(dialog, copy('routing.undoRestore')).click();
    await expect(dialog.locator('.model-hub-route-hop-name')).toHaveText([gateway.sources[0].display_name]);
    await expect(dialog.locator('.model-hub-route-hop-model')).toHaveText([unsavedModel]);
    await removeLast();
    await labelledButton(dialog, copy('routeDialog.cancel')).click();
    await expect(dialog).not.toBeVisible();
    expect((await api.agentChain(gateway.backend, gateway.model)).manual_override).toEqual(manual);
    expect(mutations).toEqual([]);

    await hub.openRoute(gateway.backend, gateway.model);
    await removeLast();
    const refusedPromise = page.waitForResponse((response) => response.url() === chainUrl && response.request().method() === 'DELETE');
    await labelledButton(dialog, copy('routeDialog.save')).click();
    const refused = await refusedPromise;
    expect(refused.status()).toBe(409);
    expect(refused.request().postDataJSON()).toEqual({});
    const plan = {
      would_remove_hops: [{ backend: gateway.backend, menu_model: gateway.model, ...manual.hops[0], position: 1 }],
      would_interrupt: [],
    };
    expect(await refused.json()).toMatchObject({ ok: false, contract_version: 10, error: 'source_in_route_chain', ...plan });
    expect((await api.agentChain(gateway.backend, gateway.model)).manual_override).toEqual(manual);
    await expect(dialog.locator('.model-hub-guard-hop')).toHaveCount(1);
    await expect(dialog.locator('.model-hub-guard-hop strong')).toHaveText(`${copy(`backends.${gateway.backend}`)} · ${gateway.model}`);
    await expect(dialog.locator('.model-hub-guard-hop > span > span')).toHaveText(`${manual.hops[0].model_id} · ${copy('guard.hop.position', { n: 1 })}`);
    const confirm = labelledButton(dialog, copy('guard.confirm.saveRoute'));
    await expect(confirm).toBeVisible();
    const committedPromise = page.waitForResponse((response) => response.url() === chainUrl && response.request().method() === 'DELETE');
    await confirm.click();
    const committed = await committedPromise;
    expect(committed.status()).toBe(200);
    expect(committed.request().postDataJSON()).toEqual({ force: true, ...plan });
    await expect.poll(async () => (await api.agentChain(gateway.backend, gateway.model)).manual_override).toBeNull();
    expect(identities(await api.agentChain(gateway.backend, gateway.model))).toEqual(identities(inherited));
    expect(mutations).toEqual(['DELETE', 'DELETE']);
    const done = labelledButton(dialog, copy('routeDialog.impact.done'));
    await expect(done).toBeEnabled();
    await done.click();
    await expect(dialog).toHaveCount(0);
    await page.reload();
    const reloaded = await api.agentChain(gateway.backend, gateway.model);
    expect(reloaded.manual_override).toBeNull();
    expect(reloaded.route_origin).toBe(inherited.route_origin);
    expect(identities(reloaded)).toEqual(identities(inherited));
    await expect(hub.routeRow(gateway.backend, gateway.model).locator('button.model-hub-route-origin')).toHaveText(copy(`routing.origin.${reloaded.route_origin}`));
    await hub.openRoute(gateway.backend, gateway.model);
    await expect(labelledButton(dialog, copy('routing.editRoute'))).toBeVisible();
    await labelledButton(dialog, copy('routing.close')).click();
  } finally {
    try {
      await api.setDefaultSourceOrder(gateway.backend, originalOrder);
    } finally {
      await restoreAgentChain(api, gateway, original);
    }
  }
});
