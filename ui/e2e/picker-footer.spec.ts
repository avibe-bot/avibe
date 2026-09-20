// Where the picker's footer actions are PAINTED, at a phone width and at a
// desktop one.
//
// No scenario ID, for `model-list.spec.ts`'s reason: `docs/plans/
// model-hub-e2e-test-plan.md` §3 has no family for a dialog's footer layout,
// so the title states the property instead of borrowing a letter the plan
// cannot resolve.
//
// This lives here rather than in `BackendModelPickerDialog.test.tsx` because
// the claim is about the cascade. The footer's three actions are in one DOM
// order at every width and are painted in two different ones; jsdom loads no
// stylesheet and computes no layout, so a unit test cannot tell the two apart —
// it would pass identically against a footer that stacks the wrong way up. A
// real browser with the real bundle is the only place the question has an
// answer.
//
// Read-only: it opens the picker and cancels out of it, leaving the instance's
// model list exactly as it found it.
import { hub as copy } from './support/copy';
import {
  requireMockUpstream,
  requireModelHub,
  requireRuntimeRunning,
} from './support/fixtures';
import { expect, test } from './support/gateway';
import { labelledButton, type ModelHubPage } from './support/hub';
import type { Locator } from '@playwright/test';

/** The box a visible element occupies, as a value the assertions can compare.
 *  `boundingBox()` is nullable for elements that render nothing, which is a
 *  different failure than the one under test — so it is asserted here, once,
 *  instead of being widened away with `?.` at every use. */
const boxOf = async (locator: Locator) => {
  const box = await locator.boundingBox();
  expect(box, 'the element occupies no box, so its position cannot be read').not.toBeNull();
  return box!;
};

/** Two widths on either side of the one breakpoint the footer changes at.
 *  Named by what they stand for rather than by the device that suggested them:
 *  the property is "below the breakpoint" and "above it", and the numbers are
 *  one ordinary phone and one ordinary desktop from that side of it. */
const PHONE = { width: 390, height: 844 };
const DESKTOP = { width: 1440, height: 900 };

test.describe('Model picker footer · where its actions are painted', () => {
  test.beforeEach(async ({ api, mock }) => {
    await requireModelHub(api);
    await requireRuntimeRunning(api);
    await requireMockUpstream(mock);
  });

  /** The picker, reached the one way the product offers: the backend's model
   *  list, then that list's add action. */
  const openPicker = async (hub: ModelHubPage, backend: string) => {
    await hub.goto();
    await hub.manageModelsButton(backend).click();
    const catalog = hub.catalogDialog;
    await expect(catalog).toBeVisible();
    const addModels = labelledButton(catalog, copy('gateway.catalog.add'));
    await expect(addModels).toBeEnabled();
    await addModels.click();
    const picker = hub.pickerDialog;
    await expect(picker).toBeVisible();
    return { catalog, picker };
  };

  test('below the breakpoint the custom-model action is above the decision, which sits last', async ({
    page,
    hub,
    gateway,
  }) => {
    await page.setViewportSize(PHONE);
    const { catalog, picker } = await openPicker(hub, gateway.backend);

    const footer = picker.locator('.model-hub-catalog-foot');
    const custom = labelledButton(footer, copy('gateway.picker.custom'));
    const cancel = labelledButton(footer, copy('gateway.catalog.cancel'));
    await expect(custom).toBeVisible();
    await expect(cancel).toBeVisible();

    const customBox = await boxOf(custom);
    const cancelBox = await boxOf(cancel);

    // The property, in the order a thumb meets it: the tertiary action is a
    // full row of its own, entirely above the row that decides the dialog —
    // so the decision is the last thing on the screen, nearest the thumb.
    expect(
      customBox.y + customBox.height,
      'the custom-model action must end above where the decision row begins',
    ).toBeLessThanOrEqual(cancelBox.y);
    expect(customBox.width, 'the custom-model action spans its own row').toBeGreaterThan(cancelBox.width);

    await labelledButton(picker, copy('gateway.catalog.cancel')).click();
    await labelledButton(catalog, copy('gateway.catalog.cancel')).click();
  });

  test('above the breakpoint the same three actions share one row, custom first', async ({
    page,
    hub,
    gateway,
  }) => {
    await page.setViewportSize(DESKTOP);
    const { catalog, picker } = await openPicker(hub, gateway.backend);

    const footer = picker.locator('.model-hub-catalog-foot');
    const custom = labelledButton(footer, copy('gateway.picker.custom'));
    const cancel = labelledButton(footer, copy('gateway.catalog.cancel'));
    await expect(custom).toBeVisible();
    await expect(cancel).toBeVisible();

    const customBox = await boxOf(custom);
    const cancelBox = await boxOf(cancel);

    // Sharing a row is asserted as overlapping vertical extents rather than as
    // equal `y`: the three controls are centred against each other, and two
    // buttons of different heights on one row do not start at the same pixel.
    expect(
      customBox.y,
      'the custom-model action and the decision row overlap vertically, i.e. one row',
    ).toBeLessThan(cancelBox.y + cancelBox.height);
    expect(cancelBox.y).toBeLessThan(customBox.y + customBox.height);
    expect(customBox.x, 'the custom-model action is the leading edge of that row').toBeLessThan(cancelBox.x);

    await labelledButton(picker, copy('gateway.catalog.cancel')).click();
    await labelledButton(catalog, copy('gateway.catalog.cancel')).click();
  });
});
