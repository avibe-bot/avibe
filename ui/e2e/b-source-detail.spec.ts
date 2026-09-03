// Suite B (continued) — what a source's own panel lets a user do to it.
//
// Scenario IDs are from docs/plans/model-hub-e2e-test-plan.md §3.
//
// The source these specs act on is created through the API, not through the Add
// dialog: their subject is what happens AFTER a source exists, and re-driving
// Add here would report Add's failures under a rename test's name.
import type { Source } from './support/api';
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
import type { ModelHubPage } from './support/hub';
import { anthropicInventory } from './support/mock';
import { PROTOCOL_TIER_SUGGESTIONS } from './support/vocabulary';

type Supplied = Source['models'][number];
type ManagedRung = 'upstream' | 'catalog';

/**
 * The lock this UI can show only exists once the server has stamped the rung.
 * A server that predates the field leaves the row editable, which is the
 * pre-ladder behavior — not a product failure on this instance. `test.fixme`
 * rather than `test.skip`: the report names the missing backend, and the
 * same spec starts asserting the moment the field lands, with no edit here.
 */
const requireManagedRung = (
  model: Supplied | undefined,
  modelId: string,
  rung: ManagedRung,
): boolean => {
  const stamped = model?.reasoning_efforts_source;
  if (stamped === rung) return true;
  // `test.fixme` is the skip the report should show; the return is what
  // keeps this body from then asserting a lock the instance cannot draw.
  test.fixme(
    true,
    `backend lane has not stamped reasoning_efforts_source=${rung} on ${modelId}`
      + ` (got ${stamped === undefined ? 'absent' : JSON.stringify(stamped)}); `
      + 'locked editor and provenance badge cannot be reached on this instance.',
  );
  return false;
};

const expectLockedRow = async (
  page: ModelHubPage,
  modelId: string,
  rung: ManagedRung,
): Promise<void> => {
  const cell = page.managedTierCell(modelId, rung);
  await expect(cell).toBeVisible();
  await expect(cell).toContainText(copy(`sourceDetail.tiers.managed.${rung}`));
  await expect(page.tierAddAffordance(modelId)).toHaveCount(0);
  await expect(page.modelRow(modelId).getByRole('textbox')).toHaveCount(0);
  await expect(page.tierSuggestions(modelId)).toHaveCount(0);
  await cell.click();
  await expect(page.modelRow(modelId).getByRole('textbox')).toHaveCount(0);
  await expect(page.tierSuggestions(modelId)).toHaveCount(0);
  for (const tier of await page.tierChips(modelId).allTextContents()) {
    await expect(
      page.modelRow(modelId).getByRole('button', {
        name: copy('sourceDetail.tiers.remove', { tier }),
        exact: true,
      }),
    ).toHaveCount(0);
  }
};

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

  // B10 grows the provenance cases the spec's D-5 decision added: the editor
  // is a door only when the server does not own the list. Locked rows, the
  // badge that says which rung owns them, ghost suggestions matching the
  // protocol vocabulary, and the 409 copy that must never offer a retry.

  test('B10 · ghost suggestions match the protocol vocabulary', async ({ hub, mock, api }) => {
    await mock.configure({
      auth: 'ok',
      protocol: 'anthropic',
      models_endpoint: 'ok',
      models: anthropicInventory(['e2e-vocab']),
    });
    const name = `${E2E_SOURCE_PREFIX}tier-vocab`;
    const source = await requireSource(api, name, mockBaseUrl());
    const model = source.models.find((entry) => entry.id === 'e2e-vocab');
    expect(model, 'The precondition source did not take the seeded inventory, so there is no editor to open.').toBeTruthy();
    const expected = PROTOCOL_TIER_SUGGESTIONS[source.protocol];
    expect(expected, `no suggestion list for protocol ${source.protocol}`).toBeTruthy();
    if (!expected) return;

    await hub.goto();
    await hub.openSource(source.id);
    await hub.tierCell('e2e-vocab').click();

    await expect(hub.modelRow('e2e-vocab').getByRole('textbox')).toBeVisible();
    await expect(hub.tierSuggestions('e2e-vocab')).toHaveText([...expected]);
    await expect(hub.managedTierCell('e2e-vocab', 'upstream')).toHaveCount(0);
    await expect(hub.managedTierCell('e2e-vocab', 'catalog')).toHaveCount(0);
  });

  test('B10 · a catalog-declared model shows its provenance and cannot be edited', async ({ hub, mock, api }) => {
    // `claude-opus-4-6` is a real builtin-catalog id. A server that has landed
    // the provenance ladder stamps it `catalog` and re-applies the catalog
    // list on every refresh; a server that has not leaves the field absent
    // and the row editable — which is the pre-ladder behavior, not a lock
    // this UI can show.
    const catalogId = 'claude-opus-4-6';
    const siblingId = 'e2e-not-in-catalog';
    await mock.configure({
      auth: 'ok',
      protocol: 'anthropic',
      models_endpoint: 'ok',
      models: anthropicInventory([catalogId, siblingId]),
    });
    const name = `${E2E_SOURCE_PREFIX}tier-catalog`;
    const source = await requireSource(api, name, mockBaseUrl());
    const catalogModel = source.models.find((entry) => entry.id === catalogId);
    expect(catalogModel, 'The precondition source did not take the catalog id, so there is no row to lock.').toBeTruthy();
    if (!requireManagedRung(catalogModel, catalogId, 'catalog')) return;

    await hub.goto();
    await hub.openSource(source.id);
    await expectLockedRow(hub, catalogId, 'catalog');

    // Locking is per model: the sibling the catalog does not know stays a door.
    await hub.tierCell(siblingId).click();
    await expect(hub.modelRow(siblingId).getByRole('textbox')).toBeVisible();
  });

  test('B10 · an upstream-declared model shows its provenance and cannot be edited', async ({ hub, mock, api }) => {
    // OpenRouter-shape `supported_parameters` carrying a reasoning signal is
    // the v1 capture the spec names. The id is deliberately not a catalog
    // entry, so a server that has landed rung 1 stamps `upstream` rather
    // than falling through to rung 2.
    const upstreamId = 'e2e-reasoner';
    const siblingId = 'e2e-no-signal';
    await mock.configure({
      auth: 'ok',
      protocol: 'anthropic',
      models_endpoint: 'ok',
      models: [
        {
          id: upstreamId,
          type: 'model',
          display_name: `${upstreamId} (upstream label)`,
          supported_parameters: ['reasoning'],
        },
        ...anthropicInventory([siblingId]),
      ],
    });
    const name = `${E2E_SOURCE_PREFIX}tier-upstream`;
    const source = await requireSource(api, name, mockBaseUrl());
    const upstreamModel = source.models.find((entry) => entry.id === upstreamId);
    expect(upstreamModel, 'The precondition source did not take the seeded reasoner, so there is no row to lock.').toBeTruthy();
    if (!requireManagedRung(upstreamModel, upstreamId, 'upstream')) return;

    await hub.goto();
    await hub.openSource(source.id);
    await expectLockedRow(hub, upstreamId, 'upstream');

    await hub.tierCell(siblingId).click();
    await expect(hub.modelRow(siblingId).getByRole('textbox')).toBeVisible();
  });

  for (const locale of ['en', 'zh'] as const) {
    test(`B10 · a managed-tier refusal is a sentence in ${locale}, not a retry`, async ({ hub, mock, api, page }) => {
      // The UI does not offer the write on a locked row, so the authentic
      // browser path is a state race: the editor is open because this client
      // still believes the row is editable, and the PATCH comes back as the
      // server owning the list. Intercepting that one call is how the copy
      // is reached on an instance whose backend has not landed the guard —
      // and remains the path after it has, because a locked row never sends
      // the write. The instance's language is not saved; Chinese is a
      // config-GET rewrite for this page only.
      if (locale === 'zh') {
        await page.route('**/api/config', async (route) => {
          if (route.request().method() !== 'GET') {
            await route.continue();
            return;
          }
          const response = await route.fetch();
          await route.fulfill({
            response,
            json: { ...(await response.json() as Record<string, unknown>), language: 'zh' },
          });
        });
      }
      await page.route('**/api/models/sources/*/models/*', async (route) => {
        if (route.request().method() !== 'PATCH') {
          await route.continue();
          return;
        }
        await route.fulfill({
          status: 409,
          json: { ok: false, error: 'source_model_tiers_managed' },
        });
      });

      await mock.configure({
        auth: 'ok',
        protocol: 'anthropic',
        models_endpoint: 'ok',
        models: anthropicInventory(['e2e-refusal']),
      });
      const name = `${E2E_SOURCE_PREFIX}tier-refusal-${locale}`;
      const source = await requireSource(api, name, mockBaseUrl());
      const expected = PROTOCOL_TIER_SUGGESTIONS[source.protocol];
      expect(expected, `no suggestion list for protocol ${source.protocol}`).toBeTruthy();
      const first = expected![0];
      expect(first, 'the protocol vocabulary is empty, so there is no suggestion to click').toBeTruthy();
      if (!first) return;

      await hub.goto();
      await hub.openSource(source.id);
      await expect(
        hub.sourceDetailDialog.getByRole('button', {
          name: copy('sourceDetail.action.refetch', undefined, locale),
          exact: true,
        }),
      ).toBeVisible();

      await hub.tierCell('e2e-refusal').click();
      await expect(hub.modelRow('e2e-refusal').getByRole('textbox')).toBeVisible();
      await hub.modelRow('e2e-refusal').getByRole('button', {
        name: copy('sourceDetail.tiers.suggest', { tier: first }, locale),
        exact: true,
      }).click();

      await expect(hub.tierFailure('e2e-refusal', 'managed')).toBeVisible();
      await expect(hub.tierFailure('e2e-refusal', 'managed')).toHaveText(
        copy('sourceDetail.fail.tierManaged', undefined, locale),
      );
      await expect(hub.tierFailure('e2e-refusal', 'retryable')).toHaveCount(0);
      await expect(
        hub.modelRow('e2e-refusal').getByRole('button', {
          name: copy('sourceDetail.retry', undefined, locale),
          exact: true,
        }),
      ).toHaveCount(0);
      await expectVisibleWithout(
        hub.modelRow('e2e-refusal'),
        copy('sourceDetail.fail.tier', undefined, locale),
      );
      await expect(hub.tierChips('e2e-refusal')).toHaveCount(0);
    });
  }
});
