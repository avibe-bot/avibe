// A backend's model list: the one action that adds to it, the picker that
// action opens, and the editor the picker hands off to.
//
// No scenario ID. docs/plans/model-hub-e2e-test-plan.md §3 has no family for a
// backend's model list, and its Playwright scope for this round does not name
// one, so allocating an ID belongs to whoever owns that plan; until then these
// tests are named by the property they assert.
//
// Edited and then CANCELLED, for suite D's reason: the list here belongs to the
// instance, not to this suite, and a committed row changes what that backend
// actually offers its users. Two claims therefore sit with the integration pass
// that has the compose-from-providers server rather than in this file — that a
// saved addition persists, and that the supplier chips a candidate is shown
// with are exactly the hops its route is seeded with.
import { hub as copy, hubOrNull } from './support/copy';
import { E2E_SOURCE_PREFIX } from './support/env';
import {
  expectVisibleWithout,
  requireMockUpstream,
  requireModelHub,
  requireRuntimeRunning,
} from './support/fixtures';
import { expect, test } from './support/gateway';
import { labelledButton, pickerRowId } from './support/hub';

/** The product falls back to the raw backend id for a backend it has no label
 *  for, so this does too rather than throwing on an id the bundle never named. */
const backendLabel = (backend: string): string => hubOrNull(`backends.${backend}`) ?? backend;

test.describe('Model list · what a backend can be given', () => {
  test.beforeEach(async ({ api, mock }) => {
    await requireModelHub(api);
    await requireRuntimeRunning(api);
    await requireMockUpstream(mock);
  });

  test('one way in, ending at an id the backend accepts, and a cancel that leaves nothing', async ({ hub, gateway }) => {
    await hub.goto();
    await hub.manageModelsButton(gateway.backend).click();

    const catalog = hub.catalogDialog;
    await expect(catalog).toBeVisible();
    // Enabled is this dialog's own "ready": it reads the saved list first, and
    // a backend whose list cannot be read offers nothing to add to it. Counting
    // rows before that would count a list still arriving.
    const addModels = labelledButton(catalog, copy('gateway.catalog.add'));
    await expect(addModels).toBeEnabled();
    const before = await hub.catalogRows.count();

    // One decision with a fallback, not two entry points to choose between: the
    // list's add action opens the picker, and the editor is reached from inside
    // it — never instead of it.
    await addModels.click();
    const picker = hub.pickerDialog;
    await expect(picker).toBeVisible();
    await expect(picker).toContainText(copy('gateway.picker.title', { backend: backendLabel(gateway.backend) }));
    await expect(hub.modelEditorDialog).toHaveCount(0);

    // The footer's own action, which is offered whatever the candidates read
    // did: a model nobody can list is exactly the one a user needs to type.
    await picker.getByRole('button', { name: copy('gateway.picker.custom'), exact: true }).click();
    const editor = hub.modelEditorDialog;
    await expect(editor).toBeVisible();

    // An id models.dev has never heard of, which is the case the typeahead's
    // last row exists for. That row is what this asserts, rather than the
    // matches above it: whether models.dev answers at all is a fact about this
    // instance's network, not about the product.
    const typed = `${E2E_SOURCE_PREFIX}model-${Date.now()}`;
    const field = hub.modelIdField;
    await field.fill(typed);
    const escape = hub.literalIdOption;
    await expect(escape).toBeVisible();
    const offered = (await escape.innerText()).trim();
    await escape.click();

    // What the row offered is what the field now holds, and what it holds is
    // what was typed. Every backend's id is the query itself — the row is read
    // for the id rather than assumed to be it, because the offer is what a user
    // reads before clicking, and an offer that named some other string would be
    // the surface promising one id and creating another.
    const created = await field.inputValue();
    expect(offered).toBe(copy('gateway.modelEditor.useAsId', { query: created }));
    expect(created).toBe(typed);

    await labelledButton(editor, copy('gateway.modelEditor.add')).click();
    await expect(editor).toHaveCount(0);
    await expect(hub.catalogRow(created)).toBeVisible();
    await expect(hub.catalogRows).toHaveCount(before + 1);

    // Cancelled, and what the cancel owes is nothing left behind: not on the
    // card the dialog was opened from, and not in the list the next read of it
    // builds — which is the instance's own answer, not this dialog's memory.
    await labelledButton(catalog, copy('gateway.catalog.cancel')).click();
    await expect(catalog).toHaveCount(0);
    await expectVisibleWithout(hub.agentCard(gateway.backend), created);

    await hub.manageModelsButton(gateway.backend).click();
    await expect(addModels).toBeEnabled();
    await expect(hub.catalogRow(created)).toHaveCount(0);
    await expect(hub.catalogRows).toHaveCount(before);
    await labelledButton(catalog, copy('gateway.catalog.cancel')).click();
  });

  test('every model the picker offers can be added, and the confirm says how many', async ({ api, hub, gateway }) => {
    test.skip(
      !(await api.servesModelCandidates(gateway.backend)),
      'This instance does not serve the models a backend can be given '
        + '(GET /api/models/agents/<backend>/models/candidates), so the picker has no built-in group and no '
        + 'provider group to offer. That read arrives with the compose-from-providers server, and the '
        + 'integration pass on a server that has it is the layer that can execute this scenario.',
    );

    await hub.goto();
    await hub.manageModelsButton(gateway.backend).click();

    const catalog = hub.catalogDialog;
    const addModels = labelledButton(catalog, copy('gateway.catalog.add'));
    await expect(addModels).toBeEnabled();
    const before = await hub.catalogRows.count();

    await addModels.click();
    const picker = hub.pickerDialog;
    await expect(picker).toBeVisible();
    // Settled is either an offer or the answer that there is nothing to offer;
    // waiting on a row alone would spend the whole timeout on the second one.
    const offers = hub.pickerOffers;
    await expect(offers.first().or(picker.getByText(copy('gateway.picker.noMatch')))).toBeVisible();

    const available = await offers.count();
    test.skip(
      available === 0,
      'Every model this instance can offer this backend is already in its list, so the picker has nothing '
        + 'left to add.',
    );

    // Two if the instance has two, because the confirm counts what was picked
    // and a count of one cannot tell a count from a constant.
    const take = Math.min(2, available);
    const picked: string[] = [];
    for (let index = 0; index < take; index += 1) {
      const row = offers.nth(index);
      picked.push(await pickerRowId(row));
      await row.click();
    }

    await labelledButton(picker, copy('gateway.picker.confirm', { count: take })).click();
    await expect(picker).toHaveCount(0);
    for (const id of picked) await expect(hub.catalogRow(id)).toBeVisible();
    await expect(hub.catalogRows).toHaveCount(before + take);

    await labelledButton(catalog, copy('gateway.catalog.cancel')).click();
    await expect(catalog).toHaveCount(0);
  });
});
