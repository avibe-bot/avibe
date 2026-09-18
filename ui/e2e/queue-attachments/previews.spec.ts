import { expect, test, type Locator, type Page } from '@playwright/test';

/**
 * Queued attachment previews, measured in a browser rather than inferred from
 * class names (issue #2042; contract:
 * docs/plans/2026-09-19-queued-attachment-previews.md).
 *
 * The geometry half needs a browser specifically. "A row with files is exactly
 * as tall as a row without" is a claim about resolved boxes: the thumbnail, the
 * chip, the `+N` pill and the row's own icon buttons are four independently
 * sized things sharing one line, and the breakpoint deciding how many of them
 * are drawn is a media query. Source alone answers neither question.
 *
 * The suite runs at a desktop width and at 390px, and every capacity assertion
 * states which of the two it is about.
 */

// A 1x1 PNG. Every allowed preview resolves to this, so a thumbnail that paints
// proves the row asked for its own URL — no byte of it comes off the network.
const PNG = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==',
  'base64',
);

const META: Record<string, { name: string; content_type: string; ext: string }> = {
  med_1: { name: 'annotation-region.png', content_type: 'image/png', ext: 'png' },
  med_2: { name: 'two.png', content_type: 'image/png', ext: 'png' },
  med_3: { name: 'three.png', content_type: 'image/png', ext: 'png' },
  med_4: { name: 'release-notes-2026-09.pdf', content_type: 'application/pdf', ext: 'pdf' },
  med_5: { name: 'console-log.txt', content_type: 'text/plain', ext: 'txt' },
  med_im1: { name: 'feishu-screenshot.png', content_type: 'image/png', ext: 'png' },
  med_im2: { name: 'trace.log', content_type: 'text/plain', ext: 'log' },
};

// The fixture's queue, in order. Addressing rows by position rather than by text
// is what keeps the image-only rows — which have no text of their own — reachable.
const ORDER = ['q-text', 'q-image', 'q-mixed', 'q-broken', 'q-file', 'q-remote', 'q-token'];
const rowIndex = (id: string) => ORDER.indexOf(id);

const DESKTOP_CAPACITY = 3;
const NARROW_CAPACITY = 2;
const capacityOf = (page: Page) =>
  page.viewportSize()!.width >= 640 ? DESKTOP_CAPACITY : NARROW_CAPACITY;

const row = (page: Page, id: string) => page.locator('[data-queue-row="true"]').nth(rowIndex(id));

// `hidden` is display:none, so "how many are drawn at this width" is exactly
// "how many are visible" — no class-name reading required.
const visible = (locator: Locator) => locator.locator('visible=true');

const previews = (page: Page, id: string) => visible(row(page, id).locator('[data-queue-attachment]'));
const thumbs = (page: Page, id: string) =>
  visible(row(page, id).locator('[data-queue-attachment^="image"]'));
const moreButton = (page: Page, id: string) =>
  visible(row(page, id).locator('[data-queue-attachment-more="true"]'));

const boxOf = async (locator: Locator, what: string) => {
  const box = await locator.boundingBox();
  expect(box, `${what} should be laid out`).not.toBeNull();
  return box!;
};

// Touch where the device has it, mouse where it does not: the point of these two
// tests is that the full filename is reachable by an *action*, and on the mobile
// project that action is a real tap rather than a hover the device cannot make.
const activate = async (page: Page, locator: Locator) => {
  if (await page.evaluate(() => 'ontouchstart' in window)) await locator.tap();
  else await locator.click();
};

test.beforeEach(async ({ page }) => {
  // Broadest first: Playwright gives priority to the most recently registered
  // handler, so the specific routes below override this one.
  await page.route('**/api/media/**', (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith('/med_5') || path.endsWith('/med_im2')) {
      return route.fulfill({ status: 200, contentType: 'text/plain', body: 'queued log line\n' });
    }
    return route.fulfill({ status: 200, contentType: 'image/png', body: PNG });
  });
  await page.route('**/api/media/*/meta', (route) => {
    const id = new URL(route.request().url()).pathname.split('/').at(-2) ?? '';
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ size: 12, ...(META[id] ?? { name: id, content_type: null, ext: null }) }),
    });
  });
  // A genuinely failed image load, produced by the transport rather than by a
  // test-only prop the product does not have.
  await page.route('**/api/media/med_broken**', (route) => route.abort('failed'));

  await page.goto('/e2e/queue-attachments/fixture.html');
  await expect(page.getByTestId('queue-fixture')).toBeVisible();
});

test('a queued image is visible, 20px, centred, and costs the row no height', async ({ page }) => {
  const plain = await boxOf(row(page, 'q-text'), 'the text-only row');
  const withImage = await boxOf(row(page, 'q-image'), 'the image-only row');
  const target = await boxOf(thumbs(page, 'q-image').first(), 'the thumbnail target');
  const thumb = await boxOf(thumbs(page, 'q-image').first().locator('span').first(), 'the thumbnail');

  // 20px of picture inside a 24px target: the visual is the size the design
  // asks for, and the thing a finger has to hit is the size the row's own icon
  // buttons already are.
  expect(Math.round(thumb.width)).toBe(20);
  expect(Math.round(thumb.height)).toBe(20);
  expect(Math.round(target.height)).toBe(24);
  // Same line, same height: the preview lives in the band those buttons occupy.
  expect(Math.round(withImage.height)).toBe(Math.round(plain.height));
  const centre = withImage.y + withImage.height / 2;
  expect(Math.abs(thumb.y + thumb.height / 2 - centre)).toBeLessThanOrEqual(1);

  // It is the file, not a stand-in for one: the row's own URL, actually decoded.
  const img = thumbs(page, 'q-image').first().locator('img');
  await expect(img).toHaveAttribute('src', '/api/media/med_1');
  await expect
    .poll(() => img.evaluate((el: HTMLImageElement) => el.naturalWidth))
    .toBeGreaterThan(0);
});

test('an image-only queued message still says what it is', async ({ page }) => {
  await expect(row(page, 'q-image')).toContainText('annotation-region.png');
});

test('the inline run is capped per width, and the disclosure counts what that width hides', async ({
  page,
}) => {
  const capacity = capacityOf(page);
  await expect(previews(page, 'q-mixed')).toHaveCount(capacity);

  // Exactly one `+N` is reachable at any one width, and it names the remainder
  // for that width rather than for the other one.
  await expect(moreButton(page, 'q-mixed')).toHaveCount(1);
  await expect(moreButton(page, 'q-mixed')).toHaveAttribute(
    'aria-label',
    `Show ${5 - capacity} more attachments`,
  );

  // The previews took width from neither the text nor the row's actions.
  const strip = await boxOf(row(page, 'q-mixed'), 'the mixed row');
  const remove = await boxOf(
    row(page, 'q-mixed').getByRole('button', { name: 'Remove from queue' }),
    'the remove button',
  );
  expect(remove.x + remove.width).toBeLessThanOrEqual(strip.x + strip.width + 1);
  await expect(row(page, 'q-mixed')).toContainText('Compare these against the spec');
});

test('every attachment type shares one row height, disclosed or not', async ({ page }) => {
  const heights = async () =>
    Promise.all(ORDER.map(async (id) => Math.round((await boxOf(row(page, id), id)).height)));
  const collapsed = await heights();
  expect(new Set(collapsed).size, `rows differed in height: ${collapsed.join(', ')}`).toBe(1);

  // Disclosure is the one attachment action allowed to make a row taller, and
  // only its own row.
  await moreButton(page, 'q-mixed').click();
  const disclosed = await heights();
  expect(disclosed[rowIndex('q-mixed')]).toBeGreaterThan(collapsed[rowIndex('q-mixed')]);
  expect(disclosed.filter((_, i) => i !== rowIndex('q-mixed'))).toEqual(
    collapsed.filter((_, i) => i !== rowIndex('q-mixed')),
  );

  await moreButton(page, 'q-mixed').click();
  expect(await heights()).toEqual(collapsed);
});

test('disclosing shows every file in order, and collapsing puts them back', async ({ page }) => {
  const sheet = row(page, 'q-mixed').locator('[data-queue-attachments="disclosed"]');

  await moreButton(page, 'q-mixed').click();
  const items = sheet.locator('[data-queue-attachment]');
  await expect(items).toHaveCount(5);
  expect(await items.evaluateAll((els) => els.map((el) => el.getAttribute('aria-label')))).toEqual([
    'one.png · PNG',
    'two.png · PNG',
    'three.png · PNG',
    'release-notes-2026-09.pdf · PDF',
    'console-log.txt · TXT',
  ]);

  await moreButton(page, 'q-mixed').click();
  await expect(sheet).toHaveCount(0);
  await expect(previews(page, 'q-mixed')).toHaveCount(capacityOf(page));
});

test('a queued image opens on its own, even though the gallery holds it too', async ({ page }) => {
  await thumbs(page, 'q-image').first().click();

  const viewer = page.getByRole('dialog');
  await expect(viewer).toBeVisible();
  // The fixture's gallery contains `/api/media/med_1`, so an isolation that was
  // merely inferred from absence would hand this viewer paging controls.
  await expect(viewer.getByRole('button', { name: 'Next' })).toHaveCount(0);
  await expect(viewer.getByRole('button', { name: 'Previous' })).toHaveCount(0);
  await page.keyboard.press('ArrowRight');
  await expect(viewer.locator('img').first()).toHaveAttribute('src', '/api/media/med_1');

  await viewer.getByRole('button', { name: 'Close' }).click();
  await expect(viewer).toHaveCount(0);
});

test('an unavailable image keeps its slot and hands over its full filename', async ({ page }) => {
  await row(page, 'q-broken').scrollIntoViewIfNeeded();
  const slot = thumbs(page, 'q-broken').first();
  await expect(slot).toHaveAttribute('data-queue-attachment', 'image-unavailable');
  await expect(slot).toHaveAttribute('aria-label', 'console-log.png · PNG · Preview unavailable');

  const box = await boxOf(slot.locator('span').first(), 'the unavailable slot');
  expect(Math.round(box.width)).toBe(20);
  expect(Math.round(box.height)).toBe(20);
  expect(Math.round((await boxOf(row(page, 'q-broken'), 'the broken row')).height)).toBe(
    Math.round((await boxOf(row(page, 'q-text'), 'the text-only row')).height),
  );

  // Not hover-only: the complete name is one action away even on a touch device.
  await activate(page, slot);
  await expect(page.getByRole('dialog')).toContainText('console-log.png');
});

test('a single unsupported file exposes its whole name to a tap', async ({ page }) => {
  await row(page, 'q-file').scrollIntoViewIfNeeded();
  const chip = previews(page, 'q-file').first();
  await expect(chip).toHaveAttribute('aria-label', 'console-log.txt · TXT');

  await activate(page, chip);
  await expect(page.getByRole('dialog')).toContainText('console-log.txt');
});

test('keyboard reaches the previews in reading order and works the disclosure', async ({ page }) => {
  const capacity = capacityOf(page);
  const text = row(page, 'q-mixed').locator('div[role="button"]');
  await text.focus();
  await expect(text).toHaveAttribute('aria-expanded', 'false');

  const reached: (string | null)[] = [];
  for (let i = 0; i < capacity + 1; i += 1) {
    await page.keyboard.press('Tab');
    reached.push(
      await page.evaluate(
        () =>
          document.activeElement?.getAttribute('aria-label') ??
          document.activeElement?.getAttribute('data-queue-attachment-more') ??
          null,
      ),
    );
  }
  expect(reached.slice(0, capacity)).toEqual(
    ['one.png · PNG', 'two.png · PNG', 'three.png · PNG'].slice(0, capacity),
  );
  expect(reached[reached.length - 1]).toBe(`Show ${5 - capacity} more attachments`);

  await page.keyboard.press('Enter');
  await expect(row(page, 'q-mixed').locator('[data-queue-attachments="disclosed"]')).toHaveCount(1);
  // The row's own text control is exactly where it was: disclosing the files is
  // not expanding the message.
  await expect(text).toHaveAttribute('aria-expanded', 'false');
});

// Focus is not a class name: only the browser can say where it went after the
// button under it stopped existing. It used to fall to the document, which ends
// the keyboard user's position in the row.
test('the disclosure keeps keyboard focus through expand and collapse', async ({ page }) => {
  const collapsedLabel = `Show ${5 - capacityOf(page)} more attachments`;
  const control = moreButton(page, 'q-mixed');
  const sheet = row(page, 'q-mixed').locator('[data-queue-attachments="disclosed"]');

  await control.focus();
  await expect(control).toHaveAttribute('aria-label', collapsedLabel);

  await page.keyboard.press('Enter');
  await expect(sheet).toHaveCount(1);
  // The same element, relabelled — not a replacement that dropped focus.
  await expect(control).toBeFocused();
  await expect(control).toHaveAttribute('aria-label', 'Collapse attachments');

  await page.keyboard.press('Enter');
  await expect(sheet).toHaveCount(0);
  await expect(control).toBeFocused();
  await expect(control).toHaveAttribute('aria-label', collapsedLabel);
});

test('a third-party file is an explicit external link, never fetched for us', async ({ page }) => {
  const signed = 'https://files.example.com/spec.pdf?sig=abc123&expires=1789';
  let hits = 0;
  // Routed on the context so the popup is served too — the request never leaves
  // this machine, and the counter is what proves the row did not make one.
  await page.context().route('https://files.example.com/**', (route) => {
    hits += 1;
    return route.fulfill({ status: 200, contentType: 'text/html', body: '<title>spec</title>' });
  });

  await row(page, 'q-remote').scrollIntoViewIfNeeded();
  const chip = previews(page, 'q-remote').first();
  await expect(chip).toHaveAttribute('data-queue-attachment', 'file-external');
  await expect(chip).toHaveAttribute('target', '_blank');
  await expect(chip).toHaveAttribute('rel', 'noopener noreferrer');
  expect(hits, 'the row must not fetch a third-party host to paint').toBe(0);

  const [popup] = await Promise.all([page.waitForEvent('popup'), chip.click()]);
  // Byte-for-byte the URL we were given: a signature survives only if the query
  // is neither rewritten nor appended to.
  expect(popup.url()).toBe(signed);
  // And it did not land in the in-app viewer, which cannot fetch that host.
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await popup.close();
});

test('an IM inbound attachment previews from its token instead of going inert', async ({ page }) => {
  await row(page, 'q-token').scrollIntoViewIfNeeded();
  const img = thumbs(page, 'q-token').first().locator('img');

  await expect(img).toHaveAttribute('src', '/api/media/med_im1');
  await expect
    .poll(() => img.evaluate((el: HTMLImageElement) => el.naturalWidth))
    .toBeGreaterThan(0);
  await expect(row(page, 'q-token')).toContainText('feishu-screenshot.png');

  // The file beside it is an action, not a label nobody can open.
  const chip = previews(page, 'q-token').nth(1);
  await expect(chip).toHaveAttribute('data-queue-attachment', 'file');
  await activate(page, chip);
  await expect(page.getByRole('dialog')).toContainText('trace.log');
});

test('inspecting files changes nothing about the queue itself', async ({ page }) => {
  const rows = page.locator('[data-queue-row="true"]');
  await expect(rows).toHaveCount(ORDER.length);
  await expect(page.getByText(`Queued · ${ORDER.length}`)).toBeVisible();

  await thumbs(page, 'q-image').first().click();
  await page.getByRole('dialog').getByRole('button', { name: 'Close' }).click();
  await moreButton(page, 'q-mixed').click();
  await moreButton(page, 'q-mixed').click();

  await expect(rows).toHaveCount(ORDER.length);
  await expect(page.getByText(`Queued · ${ORDER.length}`)).toBeVisible();
  await expect(page.getByTestId('queue-fixture')).toHaveAttribute('data-sent', '0');
  // Same rows, same order, same contents.
  await expect(row(page, 'q-image')).toContainText('annotation-region.png');
  await expect(row(page, 'q-mixed')).toContainText('Compare these against the spec');
});

test('the queue body stays capped and the composer stays on screen', async ({ page }) => {
  const body = page.locator('[data-queue-batch="true"]');
  await expect(body).toHaveCSS('max-height', '128px');

  const bounded = async () => {
    const box = await boxOf(body, 'the queue body');
    expect(box.height).toBeLessThanOrEqual(128);
    const composer = await boxOf(page.getByTestId('composer'), 'the composer');
    expect(composer.y + composer.height).toBeLessThanOrEqual(page.viewportSize()!.height + 1);
  };

  await bounded();
  await moreButton(page, 'q-mixed').click();
  await bounded();
});
