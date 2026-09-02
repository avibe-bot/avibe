// Suite D — the two surfaces that decide which source serves a request: a
// model's own route chain, and the backend-wide priority order.
//
// Scenario IDs are from docs/plans/model-hub-e2e-test-plan.md §3.
//
// Both surfaces here are edited and then CANCELLED. That is not timidity: this
// suite runs against a live instance whose routing may be real, and the
// behaviour under test is what the editor does while you are editing it. The
// commit path is scenario B7's subject, on a source this suite owns outright.
import { hub as copy, hubOrNull } from './support/copy';
import { requireMockUpstream, requireModelHub, requireRuntimeRunning } from './support/fixtures';
import { expect, test } from './support/gateway';
import { labelledButton } from './support/hub';

const escapeRegExp = (value: string): string => value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

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

    // --- add ---------------------------------------------------------------
    const addHop = dialog.getByRole('button', { name: copy('routeDialog.addHop'), exact: true });
    const addOneHop = async (): Promise<void> => {
      await addHop.click();
      await page.locator('.model-hub-route-candidate').first().click();
      await page.getByRole('button', { name: copy('routeDialog.add.confirm'), exact: true }).click();
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

    // --- remove -------------------------------------------------------------
    // Back down to what was there before, one row at a time — the editor has to
    // give back everything it took, not just the last thing.
    for (let remaining = added; remaining > before; remaining -= 1) {
      await hops.last().getByRole('button', { name: copy('routeDialog.removeHop'), exact: true }).click();
      await expect(hops).toHaveCount(remaining - 1);
    }

    // Cancel: this spec is about the editor, and the instance's real routing is
    // not this spec's to change.
    await labelledButton(dialog, copy('routeDialog.cancel')).click();
    await expect(dialog).toHaveCount(0);
  });

  // A defect this suite found, not a scenario from §3, and reported rather than
  // patched (this lane changes no product code).
  //
  // Escape during a keyboard grab is meant to be an undo: `onGripKeyDown` puts
  // the hop back, refocuses it, and announces `reorder.cancelled`. It does all
  // three — and then the dialog closes on the same keystroke, discarding every
  // unsaved edit, so nobody ever reads that announcement. The handler calls
  // `preventDefault()` only; Radix's dismissable layer listens on the document
  // and does not consult `defaultPrevented`. `SourceOrderDrawer` gets this right
  // twice over: `stopPropagation()` in the row handler AND an `onEscapeKeyDown`
  // guard on the dialog content. The route dialog has neither.
  //
  // Unfixme once the route dialog carries the same guard; the assertion is the
  // two lines below, which is exactly what the drawer spec already asserts.
  test.fixme('D · Escape cancels a grab without discarding the chain edit', async ({ hub, gateway, page }) => {
    await hub.goto();
    await hub.firstRouteRow(gateway.backend).click();
    const dialog = hub.routeDialog;
    const grip = dialog.locator('.model-hub-route-hop').first().getByRole('button', {
      name: copy('routeDialog.grip'),
      exact: true,
    });
    await grip.press(' ');
    await page.keyboard.press('Escape');
    await expect(dialog).toBeVisible();
    await expect(dialog.locator('[aria-live="polite"]')).toHaveText(
      copy('routeDialog.reorder.cancelled', { position: 1 }),
    );
  });

  test('D · Reorder by Source order restates the chain, and says which it did', async ({ hub, gateway }) => {
    await hub.goto();
    await hub.firstRouteRow(gateway.backend).click();
    const dialog = hub.routeDialog;
    await expect(dialog).toBeVisible();

    const announcer = dialog.locator('[aria-live="polite"]');
    await dialog.getByRole('button', { name: copy('routeDialog.reorder.label'), exact: true }).click();

    // Two honest outcomes, and the product distinguishes them: it reordered, or
    // the chain already matched. A test that accepted either message without
    // saying so would also accept silence.
    await expect(announcer).toHaveText(
      new RegExp(
        `^(${escapeRegExp(copy('routeDialog.reorder.sorted'))}|${escapeRegExp(copy('routeDialog.reorder.unchanged'))})$`,
      ),
    );

    await labelledButton(dialog, copy('routeDialog.cancel')).click();
    await expect(dialog).toHaveCount(0);
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
