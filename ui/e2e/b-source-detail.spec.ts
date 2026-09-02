// Suite B (continued) — what a source's own panel lets a user do to it.
//
// Scenario IDs are from docs/plans/model-hub-e2e-test-plan.md §3.
//
// The source these specs act on is created through the API, not through the Add
// dialog: their subject is what happens AFTER a source exists, and re-driving
// Add here would report Add's failures under a rename test's name.
import { hub as copy } from './support/copy';
import { E2E_SOURCE_PREFIX, mockBaseUrl } from './support/env';
import {
  expect,
  expectVisibleWithout,
  requireMockUpstream,
  requireModelHub,
  requireRuntimeRunning,
  requireSource,
  test,
} from './support/fixtures';
import { anthropicInventory } from './support/mock';

test.describe('B · the source detail panel', () => {
  test.beforeEach(async ({ api, mock }) => {
    await requireModelHub(api);
    await requireRuntimeRunning(api);
    await requireMockUpstream(mock);
  });

  test.afterEach(async ({ api }) => {
    await api.removeSuiteSources();
  });

  test('B6 · renaming a source goes through the manage menu and sticks', async ({ hub, mock, api }) => {
    await mock.configure({
      auth: 'ok',
      protocol: 'anthropic',
      models_endpoint: 'ok',
      models: anthropicInventory(['e2e-rename-a']),
    });
    const before = `${E2E_SOURCE_PREFIX}rename-before`;
    const after = `${E2E_SOURCE_PREFIX}rename-after`;
    const source = await requireSource(api, before, mockBaseUrl());

    await hub.goto();
    await hub.openSource(source.id);
    await expect(hub.sourceDetailDialog).toBeVisible();

    await hub.manageMenuTrigger(before).click();
    await hub.manageItem('edit_source').click();

    // The edit dialog and the guard dialog share a class, so this names the one
    // it means by its title rather than by hoping only one is mounted.
    const edit = hub.dialogTitled(copy('sourceDetail.edit.title', { source: before }));
    await expect(edit).toBeVisible();
    await edit.getByLabel(copy('sourceDetail.edit.name'), { exact: true }).fill(after);
    await edit.getByRole('button', { name: copy('sourceDetail.edit.save'), exact: true }).click();

    // Nothing routes through this source, so the rename settles with no guard
    // and the panel is showing the new name by the time the dialog is gone.
    await expect(edit).toHaveCount(0, { timeout: 30_000 });
    await expect(hub.sourceDetailDialog).toContainText(after);
    await expect
      .poll(async () => (await api.sources()).find((s) => s.id === source.id)?.display_name, {
        timeout: 15_000,
      })
      .toBe(after);
  });

  test('B9 · a refetch marks what arrived and names what left', async ({ hub, mock, api }) => {
    // The upstream's inventory changes between the two fetches. That diff — not
    // the final list — is what the panel owes the user.
    await mock.configure({
      auth: 'ok',
      protocol: 'anthropic',
      models_endpoint: 'ok',
      models: anthropicInventory(['e2e-keep', 'e2e-drop']),
    });
    const name = `${E2E_SOURCE_PREFIX}refetch`;
    const source = await requireSource(api, name, mockBaseUrl());
    // The mock has been seeded and has answered; a source that comes back
    // without the inventory it was handed is the product's doing, not the
    // environment's (§5a).
    expect(
      source.models.length,
      'The precondition source did not take the seeded inventory, so there is no diff to observe.',
    ).toBe(2);

    await mock.configure({ models: anthropicInventory(['e2e-keep', 'e2e-arrive']) });

    await hub.goto();
    await hub.openSource(source.id);
    await hub.sourceDetailDialog
      .getByRole('button', { name: copy('sourceDetail.action.refetch'), exact: true })
      .click();

    // Arrived: the row exists and is flagged as new. Left: named in a line, not
    // silently absent — a user who never saw `e2e-drop` vanish cannot act on it.
    await expect(hub.modelRow('e2e-arrive')).toBeVisible({ timeout: 30_000 });
    await expect(hub.modelRow('e2e-arrive')).toContainText(copy('sourceDetail.refetch.added'));
    await expect(hub.sourceDetailDialog).toContainText(
      copy('sourceDetail.refetch.removed', { count: 1, models: 'e2e-drop' }),
    );
    await expect(hub.modelRow('e2e-drop')).toHaveCount(0);
    // The survivor is STILL THERE and is not re-announced: "New" means new.
    // Both halves, because a refetch that dropped `e2e-keep` alongside `e2e-drop`
    // satisfies the second one all by itself.
    await expectVisibleWithout(hub.modelRow('e2e-keep'), copy('sourceDetail.refetch.added'));

    // NOTE (spec §3 B9): the other half of B9 — that a surviving model keeps its
    // original `discovered_at` — is not observable from the browser at all. It
    // belongs to the pytest lane; asserting a rendered timestamp here would only
    // test the formatter.
  });

  test('B10 · a model added by hand carries its tiers, and can be taken back out', async ({ hub, mock, api }) => {
    await mock.configure({
      auth: 'ok',
      protocol: 'anthropic',
      models_endpoint: 'ok',
      models: anthropicInventory(['e2e-discovered']),
    });
    const name = `${E2E_SOURCE_PREFIX}manual-model`;
    const source = await requireSource(api, name, mockBaseUrl());

    await hub.goto();
    await hub.openSource(source.id);
    await hub.sourceDetailDialog
      .getByRole('button', { name: copy('sourceDetail.action.addModel'), exact: true })
      .click();

    const draft = hub.manualDraftRow;
    await expect(draft).toBeVisible();
    await draft.getByPlaceholder(copy('sourceDetail.col.id'), { exact: true }).fill('e2e-by-hand');
    // Tiers are free text committed with Enter — the product says so in the
    // placeholder, and this is the path a user actually takes.
    const tierInput = draft.getByPlaceholder(copy('sourceDetail.tiers.inputHint'), { exact: true });
    await tierInput.fill('e2e-low');
    await tierInput.press('Enter');
    await tierInput.fill('e2e-high');
    await tierInput.press('Enter');
    // Typed, then thought better of: removing a tier before committing must
    // actually drop it, not just hide the chip.
    await draft.getByRole('button', { name: copy('sourceDetail.tiers.remove', { tier: 'e2e-high' }), exact: true }).click();

    await draft.getByRole('button', { name: copy('sourceDetail.action.addModel'), exact: true }).click();

    const row = hub.modelRow('e2e-by-hand');
    await expect(row).toBeVisible({ timeout: 30_000 });
    // Provenance is visible, because a hand-added model behaves differently on
    // the next refetch than a discovered one.
    await expect(row).toContainText(copy('sourceDetail.entry.manual'));
    await expect(row).toContainText('e2e-low');
    await expectVisibleWithout(row, 'e2e-high');
    await expect
      .poll(
        async () =>
          (await api.sources())
            .find((s) => s.id === source.id)
            ?.models.find((model) => model.id === 'e2e-by-hand')?.reasoning_efforts,
        { timeout: 15_000 },
      )
      .toEqual(['e2e-low']);

    // And back out again. No route runs through it, so nothing guards this.
    await row.getByRole('button', { name: `${copy('sourceDetail.row.remove')} e2e-by-hand`, exact: true }).click();
    await hub.page
      .getByRole('menuitem', { name: copy('sourceDetail.row.remove'), exact: true })
      .click();
    await expect(row).toHaveCount(0, { timeout: 30_000 });
    // The discovered model is untouched: removing one model is not a refetch.
    await expect(hub.modelRow('e2e-discovered')).toBeVisible();
  });
});
