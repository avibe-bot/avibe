// Suite D — the two surfaces that decide which source serves a request: a
// model's own route chain, and the backend-wide priority order.
//
// Scenario IDs are from docs/plans/model-hub-e2e-test-plan.md §3.
//
// Both surfaces here are edited and then CANCELLED. That is not timidity: this
// suite runs against a live instance whose routing may be real, and the
// behaviour under test is what the editor does while you are editing it. The
// commit path is scenario B7's subject, on a source this suite owns outright.
import type { RouteHop } from './support/api';
import { hub as copy, hubOrNull } from './support/copy';
import { requireMockUpstream, requireModelHub, requireRuntimeRunning } from './support/fixtures';
import { expect, test } from './support/gateway';
import { labelledButton } from './support/hub';
import { captureAgentChain, restoreAgentChain } from './support/restore';

/** The product falls back to the raw backend id for a backend it has no label
 *  for, so this does too rather than throwing on an id the bundle never named. */
const backendLabel = (backend: string): string => hubOrNull(`backends.${backend}`) ?? backend;

test.describe('D · route chains and priority order', () => {
  test.beforeEach(async ({ api, mock }) => {
    await requireModelHub(api);
    await requireRuntimeRunning(api);
    await requireMockUpstream(mock);
  });

  test('D · a hop can be added, removed, and reordered by keyboard alone', async ({ hub, gateway, page }) => {
    await hub.goto();
    const row = hub.firstRouteRow(gateway.backend);
    await expect(row).toBeVisible();
    await row.click();

    const dialog = hub.routeDialog;
    await expect(dialog).toBeVisible();
    const hops = dialog.locator('.model-hub-route-hop');
    const before = await hops.count();

    // --- add (keyboard) -------------------------------------------------------
    // The whole scenario is a keyboard claim, so every step below drives focus
    // and keystrokes the way the claim's reader assumes it was driven: trigger,
    // candidate, confirm, and removal are all reached without a pointer.
    const addHop = dialog.getByRole('button', { name: copy('routeDialog.addHop'), exact: true });
    const addOneHop = async (): Promise<void> => {
      // The trigger itself is key-activated too. It lives in the dialog, outside
      // the popover's cmdk panel — so Enter presses it (like the remove buttons
      // below); the Enter-swallowing rule only begins once the panel is open.
      await addHop.focus();
      await addHop.press('Enter');
      // The picker autofocuses its search box; cmdk's list takes ArrowDown to
      // move the selection and Enter to fire `onSelect`, which is the product's
      // own keyboard path for choosing a candidate.
      const search = page.getByPlaceholder(copy('routeDialog.add.search'), { exact: true });
      await expect(search).toBeFocused();
      await search.press('ArrowDown');
      await search.press('Enter');
      // The footer confirm is the next tabbable after the search box, and —
      // inside a cmdk panel — it answers Space, not Enter: cmdk's root handler
      // calls preventDefault() on every Enter that reaches it (Enter belongs to
      // the list there), which suppresses the button's native Enter activation.
      // Space is the activation that genuinely works, so Space is what a
      // keyboard user actually presses here.
      await search.press('Tab');
      const confirm = page.getByRole('button', { name: copy('routeDialog.add.confirm'), exact: true });
      await expect(confirm).toBeFocused();
      await confirm.press(' ');
    };
    test.skip(
      await addHop.isDisabled(),
      'Every available source/model pair is already in this chain, so there is nothing left to add.',
    );
    await addOneHop();
    await expect(hops).toHaveCount(before + 1);

    // The reorder below is the other half of this spec, and a one-hop chain has
    // no order to change — which is exactly what an empty chain plus one add
    // is. So take a second hop while the instance still offers one, rather than
    // skipping the keyboard path on an arrangement this spec can fix itself.
    if (before + 1 < 2 && !(await addHop.isDisabled())) {
      await addOneHop();
      await expect(hops).toHaveCount(before + 2);
    }

    // --- keyboard reorder ---------------------------------------------------
    const added = await hops.count();
    test.skip(
      added < 2,
      'A single-hop chain has no order to change; this instance offered only one candidate.',
    );
    const announcer = dialog.locator('[aria-live="polite"]');
    const firstGrip = hops.first().getByRole('button', { name: copy('routeDialog.grip'), exact: true });
    await firstGrip.focus();
    await firstGrip.press(' ');
    // The grab is announced, and the row says it is held — a sighted user sees
    // the lift, and this is the same fact stated for everyone else.
    await expect(announcer).toHaveText(copy('routeDialog.reorder.grabbed', { position: 1 }));
    await expect(firstGrip).toHaveAttribute('aria-grabbed', 'true');

    await firstGrip.press('ArrowDown');
    await expect(announcer).toHaveText(copy('routeDialog.reorder.position', { position: 2 }));

    // Space again drops what Space picked up, and says where it landed. The
    // OTHER way out of a grab — Escape, which should put the hop back — closes
    // the whole editor instead; see the fixme below.
    //
    // The drop is addressed by the row that reports itself held, not by
    // `hops.first()`: the move just made the first row a different hop, and a
    // locator re-resolves every time it is used.
    const heldGrip = dialog.locator('[aria-grabbed="true"]');
    await heldGrip.press(' ');
    await expect(announcer).toHaveText(copy('routeDialog.reorder.dropped', { position: 2 }));
    await expect(dialog.locator('[aria-grabbed="true"]')).toHaveCount(0);

    // --- remove (keyboard) ----------------------------------------------------
    // Back down to what was there before, one row at a time — the editor has to
    // give back everything it took, not just the last thing. The remove buttons
    // are roving-tabindex siblings of the grips, so each is focused directly and
    // pressed rather than clicked.
    for (let remaining = added; remaining > before; remaining -= 1) {
      const remove = hops.last().getByRole('button', { name: copy('routeDialog.removeHop'), exact: true });
      await remove.focus();
      await remove.press('Enter');
      await expect(hops).toHaveCount(remaining - 1);
    }

    // Cancel: this spec is about the editor, and the instance's real routing is
    // not this spec's to change.
    await labelledButton(dialog, copy('routeDialog.cancel')).click();
    await expect(dialog).toHaveCount(0);
  });

  test('D · Escape cancels a grab without discarding the chain edit', async ({ api, hub, gateway, page }) => {
    const original = await captureAgentChain(api, gateway);
    const arranged = gateway.sources.map((source) => ({
      source_id: source.id,
      model_id: source.models[0].id,
    }));
    try {
      expect(await api.putAgentChain(gateway.backend, gateway.model, arranged)).toBe(true);
      await hub.goto();
      await hub.routeRow(gateway.backend, gateway.model).click();

      const dialog = hub.routeDialog;
      const hops = dialog.locator('.model-hub-route-hop');
      const models = dialog.locator('.model-hub-route-hop-model');
      const announcer = dialog.locator('[aria-live="polite"]');
      const gripName = copy('routeDialog.grip');
      const unsavedOrder = [arranged[1].model_id, arranged[0].model_id];
      const firstGrip = hops.first().getByRole('button', { name: gripName, exact: true });

      await firstGrip.focus();
      await firstGrip.press(' ');
      await firstGrip.press('ArrowDown');
      const heldGrip = dialog.locator('[aria-grabbed="true"]');
      await expect(heldGrip).toBeFocused();
      await heldGrip.press(' ');
      await expect(models).toHaveText(unsavedOrder);
      await expect(labelledButton(dialog, copy('routeDialog.save'))).toBeEnabled();

      const unsavedGrip = hops.last().getByRole('button', { name: gripName, exact: true });
      await unsavedGrip.press(' ');
      await unsavedGrip.press('ArrowUp');
      await expect(dialog.locator('[aria-grabbed="true"]')).toBeFocused();
      await page.keyboard.press('Escape');

      await expect(dialog).toBeVisible();
      await expect(announcer).toHaveText(copy('routeDialog.reorder.cancelled', { position: 2 }));
      await expect(dialog.locator('[aria-grabbed="true"]')).toHaveCount(0);
      await expect(models).toHaveText(unsavedOrder);
      await expect(hops.last().getByRole('button', { name: gripName, exact: true })).toBeFocused();
      await expect(labelledButton(dialog, copy('routeDialog.save'))).toBeEnabled();

      await labelledButton(dialog, copy('routeDialog.cancel')).click();
    } finally {
      await restoreAgentChain(api, gateway, original);
    }
  });

  test('D · Reorder by Source order restates the chain, and says which it did', async ({ hub, gateway, api }) => {
    // Arranged, not hoped for. On a fresh hermetic instance the fixture's
    // `e2e-route-*` models do not match the backend menu, so this route begins
    // EMPTY — and an empty chain leaves the button with nothing to sort, so the
    // `unchanged` branch passes without the sorter ever running. The two hops
    // below are therefore the actual subject: deliberately opposite to Source
    // order, so the sort has a real permutation to perform.
    const supply = gateway.sources.flatMap((source) =>
      source.models.map((model) => ({ source_id: source.id, model_id: model.id })),
    );
    test.skip(
      supply.length < 2,
      'This instance offers fewer than two source/model pairs, so no unsorted chain can be arranged.',
    );
    // The order of `gateway.sources` is the suite's own; the order the instance
    // routes by is Source order. Arranging route-b before route-a is opposite to
    // creation order only when creation order became priority order — which the
    // API call below establishes rather than assumes.
    const arrangeHops = [
      { source_id: gateway.sources[1].id, model_id: gateway.sources[1].models[0].id },
      { source_id: gateway.sources[0].id, model_id: gateway.sources[0].models[0].id },
    ];
    const original: RouteHop[] = await captureAgentChain(api, gateway);
    try {
      // Entered BEFORE the arranged PUT, not after it succeeds: a PUT whose
      // response is lost or times out rejects that await with the chain
      // already replaced server-side, and a finally outside it would leave
      // the user's route swapped for the arrangement — then the fixture's
      // source sweep empties it entirely.
      expect(
        await api.putAgentChain(gateway.backend, gateway.model, arrangeHops),
        'The instance refused the arranged route, so there is no chain to sort.',
      ).toBe(true);

      await hub.goto();
      // The row for THIS model, not the card's first: the first row a card
      // shows is whichever model the supply ranks first — a different chain
      // than the one arranged above. And a collapsed card renders only its
      // first six models, so a selected model seventh-or-later has no row in
      // the DOM at all until the card is expanded.
      const card = hub.agentCard(gateway.backend);
      const expand = card.locator('.model-hub-model-collapse').first();
      if (await expand.count()) await expand.click();
      await hub.routeRow(gateway.backend, gateway.model).click();
      const dialog = hub.routeDialog;
      await expect(dialog).toBeVisible();

      const announcer = dialog.locator('[aria-live="polite"]');
      await dialog.getByRole('button', { name: copy('routeDialog.reorder.label'), exact: true }).click();

      // The announcement must say it sorted — the draft was arranged opposite
      // to Source order, so "already matches" would be the wrong claim.
      await expect(announcer).toHaveText(copy('routeDialog.reorder.sorted'));
      // And the hops must now BE in Source order, asserted by display name: the
      // name cell carries the id only for unjoined sources, so the display name
      // is the one observable every rendering shares. Each fixture source
      // contributes exactly one hop here, so name order IS hop order.
      const hops = dialog.locator('.model-hub-route-hop-name');
      await expect(hops).toHaveCount(2);
      await expect(hops.nth(0)).toHaveText(gateway.sources[0].display_name);
      await expect(hops.nth(1)).toHaveText(gateway.sources[1].display_name);

      await labelledButton(dialog, copy('routeDialog.cancel')).click();
      await expect(dialog).toHaveCount(0);
    } finally {
      await restoreAgentChain(api, gateway, original);
    }
  });

  test('D · the priority drawer moves a source by keyboard and can be backed out of', async ({ hub, gateway, page }) => {
    await hub.goto();
    await hub.adjustPriorityButton(gateway.backend).click();

    const drawer = hub.orderDrawer;
    await expect(drawer).toBeVisible();
    await expect(drawer).toContainText(
      copy('order.title', { backend: backendLabel(gateway.backend) }),
    );

    const ordered = drawer.locator('.model-hub-order-row--ordered');
    const count = await ordered.count();
    test.skip(count < 2, 'Fewer than two sources are in this backend\'s order, so there is nothing to reorder.');

    const announcer = drawer.locator('[aria-live="polite"]');
    // The row's own name cell, not its first line: the row leads with its
    // position ordinal, so `innerText().split('\n')[0]` is "1" and the expected
    // announcement becomes "Grabbed 1, position 1 of 3."
    const firstName = (await ordered.first().locator('.model-hub-order-name').innerText()).trim();
    const grip = ordered.first().getByRole('button', { name: copy('order.reorder'), exact: true });
    await grip.focus();
    await grip.press(' ');
    await expect(announcer).toHaveText(
      copy('order.grabbed', { source: firstName, position: 1, count }),
    );

    await grip.press('ArrowDown');
    await expect(announcer).toHaveText(
      copy('order.moved', { source: firstName, position: 2, count }),
    );

    await page.keyboard.press('Escape');
    await expect(announcer).toHaveText(copy('order.grabCancelled', { source: firstName }));
    // Cancelling restored the position, so the first row is the one it started as.
    await expect(ordered.first()).toContainText(firstName);

    // Closed without saving: nothing this drawer did reaches the instance.
    await labelledButton(drawer, copy('order.cancel')).click();
    await expect(drawer).toHaveCount(0);
  });

  // D1 — "a newly added source lands at the tail of the order rather than
  // displacing an existing one" — needs a SECOND source to arrive while a
  // non-trivial order already exists, and the plan's D1 states it for a
  // subscription source. A subscription source cannot be created from a browser
  // without a real OAuth provider, and this lane may not add a product test
  // hook, so the observable half is left to the pytest lane.
  test.fixme('D1 · a new source is appended to the priority order, not inserted', async () => {
    // Blocked on: no browser-reachable way to add a subscription source against
    // a mock. Reported to the orchestrator as a coverage gap, not a defect.
  });
});
