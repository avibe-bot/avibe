import { expect, test, type Locator } from '@playwright/test';
import { hub } from '../support/copy';

// The picker zooms in, and every box read before that lands is a scaled box:
// heights that are not layout pixels, and a panel that is smaller than the one
// the user is left looking at.
const settled = (panel: Locator) => panel.evaluate(async (element: HTMLElement) => {
  await Promise.all(element.getAnimations({ subtree: true }).map((animation) => animation.finished));
});

// Rows are editable only on a custom route. On a phone the two row actions fold
// into one menu, so every spec reaches them through the same path either way.
const enterCustom = async (dialog: Locator, copy: (key: string) => string) => {
  const custom = dialog.getByRole('radio', { name: copy('routeDialog.mode.custom'), exact: true });
  if (await custom.getAttribute('aria-checked') !== 'true') await custom.click();
  await expect(custom).toHaveAttribute('aria-checked', 'true');
};
const hopAction = async (dialog: Locator, copy: (key: string) => string, action: 'editHop' | 'removeHop', index = 0) => {
  const page = dialog.page();
  const direct = dialog.getByRole('button', { name: copy(`routeDialog.${action}`), exact: true });
  if (await dialog.getByRole('button', { name: copy('routeDialog.hopActions'), exact: true }).count()) {
    await dialog.getByRole('button', { name: copy('routeDialog.hopActions'), exact: true }).nth(index).click();
    await page.getByRole('menuitem', { name: copy(`routeDialog.${action}`), exact: true }).click();
  } else await direct.nth(index).click();
};

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
        await expect(dialog.locator('.model-hub-route-hop')).toHaveCount(2);
        await expect(save).toBeDisabled();
        // An inherited route is read only until the explicit Custom switch.
        const inherited = origin !== 'manual';
        await expect(dialog.locator('.model-hub-route-hop[data-editable]')).toHaveCount(inherited ? 0 : 2);
        await enterCustom(dialog, copy);
        await expect(dialog.locator('.model-hub-route-hop[data-editable]')).toHaveCount(2);
        if (inherited) await expect(save).toBeEnabled();
        else await expect(save).toBeDisabled();

        // Real nested portal/touch dismissal must leave the parent and intent intact.
        await hopAction(dialog, copy, 'editHop');
        await expect(page.locator('.model-hub-route-selector')).toBeVisible();
        await page.keyboard.press('Escape');
        await expect(page.locator('.model-hub-route-selector')).toHaveCount(0);
        await expect(dialog).toBeVisible();
        if (inherited) await expect(save).toBeEnabled();
        else await expect(save).toBeDisabled();

        await hopAction(dialog, copy, 'removeHop');
        await expect(dialog.locator('.model-hub-route-hop-name')).toHaveText(['Provider B']);
        await expect(save).toBeEnabled();
        await foot.getByRole('button', { name: copy('routing.cancelChanges'), exact: true }).click();
        await expect(dialog).toBeVisible();
        await expect(dialog.locator('.model-hub-route-hop-name')).toHaveText(['Provider A', 'Provider B']);
        await expect(save).toBeDisabled();
        expect((await state()).reads).toBe(2);
        expect((await state()).saved).toEqual(baseline);
        expect((await state()).writes).toEqual([]);

        await enterCustom(dialog, copy);
        await hopAction(dialog, copy, 'editHop');
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
          return [...element.querySelectorAll<HTMLElement>('.model-hub-route-foot button, .model-hub-route-hop, .model-hub-route-mode, .model-hub-route-pending, .model-hub-route-title')]
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
      await enterCustom(dialog, copy);
      await expect(dialog.locator('.model-hub-route-pending')).toBeVisible();
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

    await enterCustom(dialog, copy);
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
  await enterCustom(dialog, (key) => hub(key, {}, 'en'));
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
    await enterCustom(dialog, copy);
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

// The row action is the one trigger narrower than the panel it opens, and it
// sits at the right edge of the row. Aligned to its end under a popover with
// collision handling off, the panel's left edge is placed a panel-width to the
// left of that — off screen on a phone, where the first column and the search
// field were simply cut away. Vertically the same panel is bounded by what the
// engine reports is on screen; this is that reading on the other axis, so the
// assertion is the one the screenshot would have failed: both edges inside the
// viewport, at every width a phone actually is.
// The Add trigger sits at the left of the tools instead, so its panel hangs
// right from there; aligned to the trigger's end it had only the trigger's own
// width to the padding line and shrank to a sliver.
for (const [label, width] of [['small', 390], ['large', 430]] as const) for (const picker of ['edit', 'add'] as const) {
  test(`MH-ROUTING-007: the ${picker} picker stays on screen on a ${label} phone`, async ({ page }) => {
    const copy = (key: string) => hub(key, {}, 'zh');
    await page.setViewportSize({ width, height: 844 });
    await page.goto('/e2e/model-catalog/fixture.html?view=route&backend=codex&lang=zh&stocked=1');
    await page.getByRole('button', { name: 'Open route', exact: true }).click();
    const dialog = page.locator('.model-hub-route-dialog');
    await enterCustom(dialog, copy);
    if (picker === 'edit') await hopAction(dialog, copy, 'editHop');
    else await dialog.getByRole('button', { name: copy('routeDialog.addHop'), exact: true }).click();
    const selector = page.locator('.model-hub-route-selector');
    await expect(selector).toBeVisible();
    await settled(selector);

    const panel = (await selector.boundingBox())!;
    expect(panel.x).toBeGreaterThanOrEqual(0);
    expect(panel.x + panel.width).toBeLessThanOrEqual(width);
    // Contained by shrinking, not by collapsing: a panel bounded to nothing
    // would satisfy the edges above and still be unusable.
    expect(panel.width).toBeGreaterThanOrEqual(Math.min(420, width - 64));
    // And the column that was being cut is the one that has to survive: its
    // left edge is what leaves the viewport first.
    const head = (await selector.locator('.model-hub-route-selector-list').boundingBox())!;
    expect(head.x).toBeGreaterThanOrEqual(0);
  });
}

// Two columns, and the left one names the source that supplies the models in the
// right one. Written into that source's first row, the name is on screen for
// exactly as long as that one row is: a few models down the left column is
// blank, and the reader is left to remember what they are looking at. The name
// belongs to the group, so the assertion is the reader's own question — look at
// the left column, level with the models on screen, and read what is there. Not
// which element the fix draws it in, and not the accessibility tree, where a
// screen-reader-only label would answer correctly while the column beside the
// models sat empty.
//
// Read off the painted boxes rather than by hit-testing the point: the name is
// `pointer-events: none` so that it does not take the clicks of the row it
// covers, and a question asked with `elementsFromPoint` would be answered by
// whatever is underneath it. Everything with text of its own that covers the
// point is returned, so a column that is somehow saying two things at once is
// not quietly reported as saying the first of them.
const sourceColumnAtTop = (list: Locator) => list.evaluate((element: HTMLElement) => {
  const edge = element.getBoundingClientRect();
  const x = edge.left + edge.width * 0.25;
  const y = edge.top + 8;
  return [...element.querySelectorAll<HTMLElement>('*')]
    .filter((node) => {
      const own = [...node.childNodes]
        .filter((child) => child.nodeType === Node.TEXT_NODE)
        .map((child) => child.textContent?.trim() ?? '')
        .join('');
      if (!own) return false;
      const style = getComputedStyle(node);
      if (style.visibility === 'hidden' || style.display === 'none' || Number(style.opacity) === 0) return false;
      const rect = node.getBoundingClientRect();
      return rect.left <= x && x <= rect.right && rect.top <= y && y <= rect.bottom;
    })
    .map((node) => node.textContent?.trim() ?? '');
});

test('MH-ROUTING-007: the picker keeps each source beside its own models', async ({ page }) => {
  const copy = (key: string) => hub(key, {}, 'zh');
  await page.goto('/e2e/model-catalog/fixture.html?view=route&backend=codex&lang=zh&stocked=1');
  await page.getByRole('button', { name: 'Open route', exact: true }).click();
  const dialog = page.locator('.model-hub-route-dialog');
  await enterCustom(dialog, copy);
  await hopAction(dialog, copy, 'editHop');
  const selector = page.locator('.model-hub-route-selector');
  await expect(selector).toBeVisible();
  await settled(selector);

  const list = selector.locator('.model-hub-route-selector-list');
  const firstRow = list.locator('.model-hub-route-selector-group').first()
    .locator('.model-hub-route-candidate').first();
  const box = (await list.boundingBox())!;
  const restingRow = (await firstRow.boundingBox())!;
  expect(await sourceColumnAtTop(list)).toEqual(['Provider A']);

  const scrolled = await list.evaluate((element: HTMLElement) => {
    const own = [...element.querySelector('.model-hub-route-selector-group')!
      .querySelectorAll<HTMLElement>('.model-hub-route-candidate')];
    // As far down this one group as the list goes: the name has to hold for as
    // long as any of its own models are on screen, not only for its first.
    element.scrollTop = Math.min(
      own[own.length - 1].offsetTop - own[0].offsetTop,
      element.scrollHeight - element.clientHeight,
    );
    return element.scrollTop;
  });
  // A list with nothing below its fold would make everything after this vacuous,
  // and the row the name used to be written into has gone off the top of it,
  // which is exactly what used to leave the column blank.
  expect(scrolled).toBeGreaterThan(0);
  const movedRow = (await firstRow.boundingBox())!;
  expect(movedRow.y).toBeLessThan(restingRow.y);
  expect(movedRow.y + movedRow.height).toBeLessThanOrEqual(box.y);

  // This source's own models are still what is being read, so its name is still
  // owed. Past its last one the next group's name takes the top, which is the
  // order the groups are read in anyway.
  const showing = await list.evaluate((element: HTMLElement) => {
    const edge = element.getBoundingClientRect();
    return [...element.querySelector('.model-hub-route-selector-group')!
      .querySelectorAll<HTMLElement>('.model-hub-route-candidate')]
      .filter((row) => {
        const rect = row.getBoundingClientRect();
        return rect.bottom > edge.top && rect.top < edge.bottom;
      }).length;
  });
  expect(showing).toBeGreaterThan(0);
  expect(await sourceColumnAtTop(list)).toEqual(['Provider A']);
});

// The pinned name is drawn over the top visible row and is that row's sibling,
// not its child, so the half of the row nearest the pointer is the half a click
// would land on the label instead — and that row would be the one that does not
// answer. Aimed at the source column of the row the name is covering, which is
// the row a reader reaches for first.
test('MH-ROUTING-007: the pinned source does not take the row\'s clicks', async ({ page }) => {
  const copy = (key: string) => hub(key, {}, 'zh');
  await page.goto('/e2e/model-catalog/fixture.html?view=route&backend=codex&lang=zh&stocked=1');
  await page.getByRole('button', { name: 'Open route', exact: true }).click();
  const dialog = page.locator('.model-hub-route-dialog');
  await enterCustom(dialog, copy);
  await hopAction(dialog, copy, 'editHop');
  const selector = page.locator('.model-hub-route-selector');
  await expect(selector).toBeVisible();
  await settled(selector);

  const list = selector.locator('.model-hub-route-selector-list');
  const group = list.locator('.model-hub-route-selector-group').first();
  const row = group.locator('.model-hub-route-candidate').first();
  // The row the name is on top of, and the quarter of its width the name covers.
  const heading = (await group.locator('[cmdk-group-heading]').boundingBox())!;
  const box = (await row.boundingBox())!;
  const aim = { x: box.x + box.width * 0.25, y: box.y + box.height / 2 };
  expect(aim.x).toBeGreaterThan(heading.x);
  expect(aim.x).toBeLessThan(heading.x + heading.width);
  expect(aim.y).toBeGreaterThan(heading.y);
  expect(aim.y).toBeLessThan(heading.y + heading.height);

  // What the pointer would reach there, before asking what happens when it does:
  // a label that answers here is the whole defect, and it answers silently.
  expect(await page.evaluate(({ x, y }) => {
    const hit = document.elementFromPoint(x, y);
    return hit?.closest('.model-hub-route-candidate') ? 'row' : (hit?.tagName ?? 'none');
  }, aim)).toBe('row');

  await expect(row).toHaveAttribute('data-selected', 'false');
  await page.mouse.click(aim.x, aim.y);
  await expect(row).toHaveAttribute('data-selected', 'true');
});

// A row you can click says so under the pointer. The global rule in `index.css`
// covers buttons and anything wearing a button role; a candidate is neither — it
// is `role="option"` — and the primitive it is built from ships an arrow on
// purpose. Every item in this codebase carries an `onSelect`, so the cursor is
// read off the rendered row rather than off the class meant to produce it.
test('MH-ROUTING-007: a candidate row reads as clickable', async ({ page }) => {
  const copy = (key: string) => hub(key, {}, 'zh');
  await page.goto('/e2e/model-catalog/fixture.html?view=route&backend=codex&lang=zh&stocked=1');
  await page.getByRole('button', { name: 'Open route', exact: true }).click();
  const dialog = page.locator('.model-hub-route-dialog');
  await enterCustom(dialog, copy);
  await hopAction(dialog, copy, 'editHop');
  const selector = page.locator('.model-hub-route-selector');
  await expect(selector).toBeVisible();

  const row = selector.locator('.model-hub-route-candidate').first();
  await row.hover();
  expect(await row.evaluate((element: HTMLElement) => getComputedStyle(element).cursor)).toBe('pointer');
});
