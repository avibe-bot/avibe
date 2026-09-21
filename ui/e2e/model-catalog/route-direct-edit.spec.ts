import { expect, test, type Locator } from '@playwright/test';
import { hub } from '../support/copy';

// The picker zooms in, and every box read before that lands is a scaled box:
// heights that are not layout pixels, and a panel that is smaller than the one
// the user is left looking at.
const settled = (panel: Locator) => panel.evaluate(async (element: HTMLElement) => {
  await Promise.all(element.getAnimations({ subtree: true }).map((animation) => animation.finished));
});

for (const backend of ['claude', 'codex', 'opencode']) {
  for (const lang of ['en', 'zh'] as const) {
    for (const origin of ['automatic', 'passthrough', 'manual']) {
      test(`MH-ROUTING-007: direct editing and local cancel preserve intent (${backend}, ${lang}, ${origin})`, async ({ page }) => {
        const copy = (key: string) => hub(key, {}, lang);
        const apiRequests: string[] = [];
        page.on('request', (request) => {
          if (new URL(request.url()).pathname.startsWith('/api/')) apiRequests.push(request.url());
        });
        await page.goto(`/e2e/model-catalog/fixture.html?view=route&backend=${backend}&lang=${lang}&origin=${origin}`);
        await page.getByRole('button', { name: 'Open route', exact: true }).click();
        const dialog = page.locator('.model-hub-route-dialog');
        const foot = dialog.locator('.model-hub-route-foot');
        const save = foot.getByRole('button', { name: copy('routeDialog.save'), exact: true });
        const state = async () => JSON.parse(await page.getByTestId('route-state').innerText());
        const baseline = (await state()).saved;
        await expect(dialog.getByRole('button', { name: copy('routeDialog.editHop'), exact: true })).toHaveCount(2);
        await expect(save).toBeDisabled();

        // Real nested portal/touch dismissal must leave the parent and intent intact.
        await dialog.getByRole('button', { name: copy('routeDialog.editHop'), exact: true }).first().click();
        await expect(page.locator('.model-hub-route-selector')).toBeVisible();
        await page.keyboard.press('Escape');
        await expect(page.locator('.model-hub-route-selector')).toHaveCount(0);
        await expect(dialog).toBeVisible();
        await expect(save).toBeDisabled();

        await dialog.getByRole('button', { name: copy('routeDialog.removeHop'), exact: true }).first().click();
        await expect(dialog.locator('.model-hub-route-hop-name')).toHaveText(['Provider B']);
        await expect(save).toBeEnabled();
        await foot.getByRole('button', { name: copy('routing.cancelChanges'), exact: true }).click();
        await expect(dialog).toBeVisible();
        await expect(dialog.locator('.model-hub-route-hop-name')).toHaveText(['Provider A', 'Provider B']);
        await expect(save).toBeDisabled();
        expect((await state()).reads).toBe(2);
        expect((await state()).saved).toEqual(baseline);
        expect((await state()).writes).toEqual([]);

        await dialog.getByRole('button', { name: copy('routeDialog.editHop'), exact: true }).first().click();
        const selector = page.locator('.model-hub-route-selector');
        await selector.getByRole('button', { name: copy('routeDialog.add.manual'), exact: true }).click();
        await selector.getByLabel(copy('routing.exactModel'), { exact: true }).fill('exact/model-中文');
        await selector.getByRole('button', { name: copy('routeDialog.edit.confirm'), exact: true }).click();
        await expect(dialog).toBeVisible();
        await expect(dialog.locator('.model-hub-route-hop-model')).toHaveText(['exact/model-中文', 'gpt-test']);
        expect((await state()).writes).toEqual([]);
        await save.click();
        await expect.poll(async () => (await state()).writes).toEqual([{
          method: 'PUT',
          hops: [{ source_id: 'src_a', model_id: 'exact/model-中文' }, { source_id: 'src_b', model_id: 'gpt-test' }],
        }]);
        await foot.getByRole('button', { name: copy('routeDialog.impact.done'), exact: true }).click();
        await expect(dialog).toHaveCount(0);
        expect(apiRequests).toEqual([]);
      });
    }
  }
}

for (const lang of ['en', 'zh'] as const) {
  for (const theme of ['light', 'dark']) {
    test(`MH-ROUTING-007: direct editing geometry and explicit pin (${lang}, ${theme})`, async ({ page }) => {
      const copy = (key: string) => hub(key, {}, lang);
      await page.goto(`/e2e/model-catalog/fixture.html?view=route&backend=codex&lang=${lang}&long=1`);
      await page.evaluate((value) => { document.documentElement.dataset.theme = value; }, theme);
      await expect(page.locator('html')).toHaveAttribute('data-theme', theme);
      await page.getByRole('button', { name: 'Open route', exact: true }).click();
      const dialog = page.locator('.model-hub-route-dialog');
      const foot = dialog.locator('.model-hub-route-foot');
      const save = foot.getByRole('button', { name: copy('routeDialog.save'), exact: true });
      await expect(dialog.locator('.model-hub-route-hop')).toHaveCount(2);
      const checkGeometry = async () => {
        const titleBox = await dialog.locator('.model-hub-route-title').boundingBox();
        const headBox = await dialog.locator('.model-hub-route-head').boundingBox();
        const bodyBox = await dialog.locator('.model-hub-route-body').boundingBox();
        const footBox = await foot.boundingBox();
        expect(titleBox!.y + titleBox!.height).toBeLessThanOrEqual(headBox!.y + headBox!.height);
        expect(headBox!.y + headBox!.height).toBeLessThanOrEqual(bodyBox!.y + 1);
        expect(footBox!.y + footBox!.height).toBeLessThanOrEqual(page.viewportSize()!.height);
        const overflow = await dialog.evaluate((element) => {
          const box = element.getBoundingClientRect();
          return [...element.querySelectorAll<HTMLElement>('.model-hub-route-foot button, .model-hub-route-hop, .model-hub-route-origin-line, .model-hub-route-title')]
            .filter((node) => {
              const rect = node.getBoundingClientRect();
              return rect.left < box.left - 1 || rect.right > box.right + 1
                || node.scrollWidth > node.clientWidth + 1;
            }).map((node) => node.textContent);
        });
        expect(overflow).toEqual([]);
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
      };
      await checkGeometry();
      await page.screenshot({ path: test.info().outputPath(`route-${lang}-${theme}-clean.png`), scale: 'css' });
      await foot.getByRole('button', { name: copy('routing.pinRoute'), exact: true }).click();
      await expect(save).toBeEnabled();
      await checkGeometry();
      await page.screenshot({ path: test.info().outputPath(`route-${lang}-${theme}-dirty.png`), scale: 'css' });
      const state = async () => JSON.parse(await page.getByTestId('route-state').innerText());
      const inheritedHops = (await state()).saved.chain.map(({ source_id, model_id }: { source_id: string; model_id: string }) => ({ source_id, model_id }));
      expect((await state()).writes).toEqual([]);
      await save.click();
      await expect.poll(async () => (await state()).writes).toEqual([{ method: 'PUT', hops: inheritedHops }]);
    });
  }
}

// The add-hop picker is bounded by the room the popover reports below its
// trigger, and it neither flips nor scrolls the page, so that room is the whole
// budget. The dialog's literal `top: 300px` spent most of it before the picker
// opened — at 1280x720 it left 103px against 139px of fixed bands, which is a
// candidate list of nothing and a confirm button painting 72px below the fold.
// Centring the dialog returns the room; folding the manual pair stops a rare
// path from holding a third of it; the floor spends what is returned on the
// list. Measured here rather than reasoned about, because every term in that
// sentence is a rendered height.
for (const lang of ['en', 'zh'] as const) {
  test(`MH-ROUTING-007: the add-hop picker opens onto a readable list (${lang})`, async ({ page }) => {
    const copy = (key: string) => hub(key, {}, lang);
    await page.setViewportSize({ width: 1280, height: 720 });
    await page.goto(`/e2e/model-catalog/fixture.html?view=route&backend=codex&lang=${lang}&stocked=1`);
    await page.getByRole('button', { name: 'Open route', exact: true }).click();
    const dialog = page.locator('.model-hub-route-dialog');
    await expect(dialog.locator('.model-hub-route-hop')).toHaveCount(2);

    // Centred, so the room below the dialog is the room above it.
    const viewport = page.viewportSize()!;
    const dialogBox = (await dialog.boundingBox())!;
    expect(Math.abs(dialogBox.y + dialogBox.height / 2 - viewport.height / 2)).toBeLessThanOrEqual(1);

    await dialog.getByRole('button', { name: copy('routeDialog.addHop'), exact: true }).click();
    const selector = page.locator('.model-hub-route-selector');
    await expect(selector).toBeVisible();
    await settled(selector);
    // The manual pair is folded away: the panel opens on what it is for.
    await expect(selector.locator('.model-hub-route-custom')).toHaveCount(0);

    const measure = async () => {
      const panel = (await selector.boundingBox())!;
      const list = (await selector.locator('.model-hub-route-selector-list').boundingBox())!;
      const row = (await selector.locator('.model-hub-route-candidate').first().boundingBox())!;
      const confirm = (await selector.locator('.model-hub-route-selector-confirm').boundingBox())!;
      return { rows: list.height / row.height, confirm, panelBottom: panel.y + panel.height };
    };

    const folded = await measure();
    expect(folded.rows).toBeGreaterThanOrEqual(3);
    // The panel keeps its own border around the confirm row, and the row stays
    // on screen: under a popover that cannot flip, below the fold is unreachable.
    expect(folded.confirm.y + folded.confirm.height).toBeLessThanOrEqual(folded.panelBottom + 1);
    expect(folded.confirm.y + folded.confirm.height).toBeLessThanOrEqual(viewport.height);

    // Opening the rare path costs list room, never the confirm button.
    await selector.getByRole('button', { name: copy('routeDialog.add.manual'), exact: true }).click();
    await expect(selector.getByLabel(copy('routing.exactModel'), { exact: true })).toBeVisible();
    await settled(selector);
    const opened = await measure();
    expect(opened.confirm.y + opened.confirm.height).toBeLessThanOrEqual(opened.panelBottom + 1);
    expect(opened.confirm.y + opened.confirm.height).toBeLessThanOrEqual(viewport.height);
  });
}

// The disclosure is one of those bands, and a subscription-only inventory does
// not draw it. Budgeting it anyway is 30px the panel will not give back, taken
// off the bottom of the screen — which is where the confirm button is.
test('MH-ROUTING-007: a panel with nothing to type by hand is a band shorter', async ({ page }) => {
  await page.setViewportSize({ width: 900, height: 600 });
  await page.goto('/e2e/model-catalog/fixture.html?view=route&backend=codex&lang=en&stocked=1&subscription=1&long=1');
  await page.getByRole('button', { name: 'Open route', exact: true }).click();
  const dialog = page.locator('.model-hub-route-dialog');
  await expect(dialog.locator('.model-hub-route-hop')).toHaveCount(2);
  await dialog.getByRole('button', { name: hub('routeDialog.addHop', {}, 'en'), exact: true }).click();
  const selector = page.locator('.model-hub-route-selector');
  await expect(selector).toBeVisible();
  await settled(selector);
  await expect(selector.locator('.model-hub-route-selector-manual')).toHaveCount(0);

  // A long chain leaves less room than the panel's own bands, so the panel is
  // held at exactly those bands — search, column head and confirm foot, and not
  // a toggle that is not there — plus the border it keeps around them.
  const panel = (await selector.boundingBox())!;
  const bands = ['[cmdk-input-wrapper]', '.model-hub-route-selector-head', '.model-hub-route-selector-foot'];
  const drawn = await Promise.all(bands.map(async (band) => (await selector.locator(band).boundingBox())!.height));
  const frame = await selector.evaluate((element: HTMLElement) => {
    const style = getComputedStyle(element);
    return parseFloat(style.borderTopWidth) + parseFloat(style.borderBottomWidth);
  });
  expect(panel.height).toBeLessThanOrEqual(drawn.reduce((total, height) => total + height, frame) + 1);
});

// A band is budgeted by a term and drawn by a primitive, and nothing keeps the
// two equal on its own. Both drifts are defects, and both were here: the manual
// pair was budgeted as two stacked controls while its grid draws them side by
// side — fourteen pixels of list room given up for nothing — and the hairlines
// between the bands, and the popover's own border, were drawn but budgeted
// nowhere, so a panel held at its bands overflowed the bound it had just
// declared by exactly the border it was supposed to keep around the confirm
// row. Every term is read back as a length here and matched against the band it
// stands for, because each one is a rendered height and none of them is
// checkable by reading the stylesheet.
for (const lang of ['en', 'zh'] as const) {
  test(`MH-ROUTING-007: every budgeted band is the height it draws (${lang})`, async ({ page }) => {
    const copy = (key: string) => hub(key, {}, lang);
    // Room to spare: a band already at its own bound would agree with a wrong
    // term for the wrong reason.
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.goto(`/e2e/model-catalog/fixture.html?view=route&backend=codex&lang=${lang}&stocked=1`);
    await page.getByRole('button', { name: 'Open route', exact: true }).click();
    const dialog = page.locator('.model-hub-route-dialog');
    await expect(dialog.locator('.model-hub-route-hop')).toHaveCount(2);
    await dialog.getByRole('button', { name: copy('routeDialog.addHop'), exact: true }).click();
    const selector = page.locator('.model-hub-route-selector');
    await expect(selector).toBeVisible();
    await settled(selector);

    // An unregistered custom property reads back as its token stream rather than
    // a length, so what a term came to can only be seen by making something draw
    // it — out of flow, or the panel's flex column shrinks the probe too.
    const terms = (...properties: string[]) => selector.evaluate((panel: HTMLElement, names: string[]) => {
      const probe = document.createElement('div');
      probe.style.cssText = 'position:absolute;visibility:hidden;flex:none';
      panel.appendChild(probe);
      const lengths = names.map((name) => {
        probe.style.height = `var(${name})`;
        return probe.getBoundingClientRect().height;
      });
      probe.remove();
      return lengths;
    }, properties);
    const drawn = async (css: string) => (await selector.locator(css).boundingBox())!.height;

    const [search, head, toggleRow, foot, foldedBands, frame, listMin, max] = await terms(
      '--model-hub-route-selector-search-height',
      '--model-hub-route-selector-head-height',
      '--model-hub-route-selector-manual-height',
      '--model-hub-route-selector-foot-height',
      '--model-hub-route-selector-bands',
      '--model-hub-route-selector-frame',
      '--model-hub-route-selector-list-min',
      '--model-hub-route-selector-max',
    );
    expect(search).toBe(await drawn('[cmdk-input-wrapper]'));
    expect(head).toBe(await drawn('.model-hub-route-selector-head'));
    expect(foot).toBe(await drawn('.model-hub-route-selector-foot'));
    // Folded, the disclosure is its toggle and the hairline above it, and its
    // fields are off the budget rather than budgeted at nothing.
    expect(toggleRow).toBe(await drawn('.model-hub-route-selector-manual'));
    await expect(selector.locator('.model-hub-route-custom')).toHaveCount(0);
    // Which is what leaves the fullest folded panel able to reach its three-row
    // floor under the cap instead of stopping short of its own floor.
    expect(foldedBands + frame + listMin).toBeLessThanOrEqual(max);

    await selector.getByRole('button', { name: copy('routeDialog.add.manual'), exact: true }).click();
    await expect(selector.getByLabel(copy('routing.exactModel'), { exact: true })).toBeVisible();
    await settled(selector);
    const [fields, bands] = await terms(
      '--model-hub-route-selector-manual-fields-height',
      '--model-hub-route-selector-bands',
    );
    // One complete field column, not two controls stacked: the pair is a grid.
    expect(fields).toBe(await drawn('.model-hub-route-custom'));
    expect(toggleRow + fields).toBe(await drawn('.model-hub-route-selector-manual'));

    // And the column adds up: the budget is every band, the frame is the
    // popover's own border, and what is left over is the list. A band added
    // without a term shows up here as a panel that does not.
    expect((await selector.boundingBox())!.height)
      .toBe(frame + bands + await drawn('.model-hub-route-selector-list'));
  });
}
