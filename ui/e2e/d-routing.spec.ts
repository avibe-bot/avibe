// Route editing and backend defaults have independent persistence owners.
import { hub as copy } from './support/copy';
import { E2E_SOURCE_PREFIX, mockBaseUrl } from './support/env';
import { requireMockUpstream, requireModelHub, requireRuntimeRunning } from './support/fixtures';
import { expect, test } from './support/gateway';
import { labelledButton } from './support/hub';
import { captureAgentChain, restoreAgentChain } from './support/restore';

test.describe('D: inherited routes, manual editing, and default routing', () => {
  test.beforeEach(async ({ api, mock }) => {
    await requireModelHub(api);
    await requireRuntimeRunning(api);
    await requireMockUpstream(mock);
  });

  test('D: inherited inspection stays read-only until Edit route', async ({ api, hub, gateway }) => {
    const original = await captureAgentChain(api, gateway);
    try {
      expect(await api.deleteAgentChain(gateway.backend, gateway.model)).toBe(true);
      await hub.goto();
      await hub.openRoute(gateway.backend, gateway.model);
      const dialog = hub.routeDialog;
      await expect(dialog).toBeVisible();
      await expect(dialog.getByRole('button', { name: copy('routeDialog.addHop'), exact: true })).toHaveCount(0);
      await expect(dialog.getByRole('button', { name: copy('routeDialog.grip'), exact: true })).toHaveCount(0);
      await labelledButton(dialog, copy('routing.editRoute')).click();
      await expect(dialog.getByRole('button', { name: copy('routeDialog.addHop'), exact: true })).toBeVisible();
      await expect(dialog.getByRole('button', { name: copy('routeDialog.grip'), exact: true }).first()).toBeVisible();
      expect((await api.agentChain(gateway.backend, gateway.model)).manual_override).toBeNull();
      await labelledButton(dialog, copy('routeDialog.cancel')).click();
      await expect(dialog).toHaveCount(0);
      expect((await api.agentChain(gateway.backend, gateway.model)).manual_override).toBeNull();
    } finally {
      await restoreAgentChain(api, gateway, original);
    }
  });

  test('D: manual add, reorder, Escape, and remove work by keyboard without committing the draft', async ({ api, hub, gateway, page }) => {
    const original = await captureAgentChain(api, gateway);
    const arranged = [{ source_id: gateway.sources[0].id, model_id: gateway.sources[0].models[0].id }];
    try {
      expect(await api.putAgentChain(gateway.backend, gateway.model, arranged)).toBe(true);
      await hub.goto();
      await hub.openRoute(gateway.backend, gateway.model);
      const dialog = hub.routeDialog;
      const hops = dialog.locator('.model-hub-route-hop');
      await expect(hops).toHaveCount(1);
      const add = dialog.getByRole('button', { name: copy('routeDialog.addHop'), exact: true });
      await add.focus();
      await add.press('Enter');
      const search = page.getByPlaceholder(copy('routeDialog.add.search'), { exact: true });
      await expect(search).toBeFocused();
      await search.press('ArrowDown');
      await search.press('Enter');
      await search.press('Tab');
      const confirm = page.getByRole('button', { name: copy('routeDialog.add.confirm'), exact: true });
      await expect(confirm).toBeFocused();
      await confirm.press(' ');
      await expect(hops).toHaveCount(2);

      const models = dialog.locator('.model-hub-route-hop-model');
      const before = await models.allTextContents();
      const announcer = dialog.locator('[aria-live="polite"]');
      const gripName = copy('routeDialog.grip');
      const grip = hops.first().getByRole('button', { name: gripName, exact: true });
      await grip.focus();
      await grip.press(' ');
      await expect(announcer).toHaveText(copy('routeDialog.reorder.grabbed', { position: 1 }));
      await grip.press('ArrowDown');
      await expect(announcer).toHaveText(copy('routeDialog.reorder.position', { position: 2 }));
      await dialog.locator('[aria-grabbed="true"]').press(' ');
      await expect(announcer).toHaveText(copy('routeDialog.reorder.dropped', { position: 2 }));
      await expect(models).toHaveText([...before].reverse());

      const lastGrip = hops.last().getByRole('button', { name: gripName, exact: true });
      await lastGrip.press(' ');
      await lastGrip.press('ArrowUp');
      await page.keyboard.press('Escape');
      await expect(dialog).toBeVisible();
      await expect(announcer).toHaveText(copy('routeDialog.reorder.cancelled', { position: 2 }));
      await expect(dialog.locator('[aria-grabbed="true"]')).toHaveCount(0);
      await expect(models).toHaveText([...before].reverse());
      await expect(hops.last().getByRole('button', { name: gripName, exact: true })).toBeFocused();
      await expect(labelledButton(dialog, copy('routeDialog.save'))).toBeEnabled();

      const remove = hops.last().getByRole('button', { name: copy('routeDialog.removeHop'), exact: true });
      await remove.focus();
      await remove.press('Enter');
      await expect(hops).toHaveCount(1);
      await labelledButton(dialog, copy('routeDialog.cancel')).click();
      expect((await api.agentChain(gateway.backend, gateway.model)).manual_override).toEqual({ hops: arranged });
    } finally {
      await restoreAgentChain(api, gateway, original);
    }
  });

  test('D: Default routing saves its order without reordering manual routes', async ({ api, hub, gateway }) => {
    const original = await captureAgentChain(api, gateway);
    const originalOrder = await api.defaultSourceOrder(gateway.backend);
    const order = gateway.sources.map((source) => source.id);
    const manual = gateway.sources.map((source) => ({ source_id: source.id, model_id: gateway.model }));
    try {
      expect(await api.putAgentChain(gateway.backend, gateway.model, manual)).toBe(true);
      await api.setDefaultSourceOrder(gateway.backend, order);
      await hub.goto();
      await hub.adjustPriorityButton(gateway.backend).click();
      const drawer = hub.orderDrawer;
      const ordered = drawer.locator('.model-hub-order-row--ordered');
      await expect(ordered).toHaveCount(2);
      await ordered.first().getByRole('button', { name: copy('routing.moveDown'), exact: true }).click();
      await labelledButton(drawer, copy('order.save')).click();
      await expect(drawer).toHaveCount(0);
      expect(await api.defaultSourceOrder(gateway.backend)).toEqual([...order].reverse());
      expect((await api.agentChain(gateway.backend, gateway.model)).manual_override).toEqual({ hops: manual });
      await hub.openRoute(gateway.backend, gateway.model);
      await expect(hub.routeDialog.locator('.model-hub-route-hop-name')).toHaveText(gateway.sources.map((source) => source.display_name));
      await labelledButton(hub.routeDialog, copy('routeDialog.cancel')).click();
    } finally {
      try {
        await api.setDefaultSourceOrder(gateway.backend, originalOrder);
      } finally {
        await restoreAgentChain(api, gateway, original);
      }
    }
  });

  test('D: Default routing keyboard Escape and Cancel restore unsaved order', async ({ api, hub, gateway, page }) => {
    const originalOrder = await api.defaultSourceOrder(gateway.backend);
    try {
      await api.setDefaultSourceOrder(gateway.backend, gateway.sources.map((source) => source.id));
      await hub.goto();
      await hub.adjustPriorityButton(gateway.backend).click();
      const drawer = hub.orderDrawer;
      const ordered = drawer.locator('.model-hub-order-row--ordered');
      await expect(ordered).toHaveCount(2);
      const firstName = gateway.sources[0].display_name;
      const grip = ordered.first().getByRole('button', { name: copy('order.reorder'), exact: true });
      await grip.focus();
      await grip.press(' ');
      await expect(drawer.locator('[aria-live="polite"]')).toHaveText(copy('order.grabbed', { source: firstName, position: 1, count: 2 }));
      await grip.press('ArrowDown');
      await page.keyboard.press('Escape');
      await expect(drawer.locator('[aria-live="polite"]')).toHaveText(copy('order.grabCancelled', { source: firstName }));
      await expect(ordered.first()).toContainText(firstName);
      await labelledButton(drawer, copy('order.cancel')).click();
      expect(await api.defaultSourceOrder(gateway.backend)).toEqual(gateway.sources.map((source) => source.id));
    } finally {
      await api.setDefaultSourceOrder(gateway.backend, originalOrder);
    }
  });

  test('D1: a new API-key source appends to existing backend defaults', async ({ api, gateway }) => {
    const before = await api.defaultSourceOrder(gateway.backend);
    let added: string | undefined;
    try {
      const source = await api.createApiKeySource(E2E_SOURCE_PREFIX + 'default-tail', mockBaseUrl());
      expect(source).not.toBeNull();
      added = source!.id;
      expect(await api.defaultSourceOrder(gateway.backend)).toEqual([...before, added]);
    } finally {
      if (added) await api.deleteSource(added);
      await api.setDefaultSourceOrder(gateway.backend, before);
    }
  });
});
