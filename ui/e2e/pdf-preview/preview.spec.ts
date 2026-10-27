import { expect, test, type Locator, type Page } from '@playwright/test';

import { BAND, PAGE_HEIGHT, PAGE_WIDTH, buildPdf, pageColor, type Rgb } from './pdf';

/**
 * The chat file viewer's PDF preview, in real engines.
 *
 * Framing a PDF in an <iframe> hands it to the browser's own viewer. On iOS that
 * viewer paints only page one, as an image that does not scroll; on Android
 * there is no viewer at all. Phones and tablets therefore get a page reader
 * drawn with pdf.js, and these tests hold it to what the frame could not do:
 * every page is reachable and drawn, and it is the right page. Pixels are read
 * back from the canvases, so "drawn" means the page's own colour is on screen,
 * not that an element exists.
 */

const DOCS: Record<string, Buffer> = {
  doc_five: buildPdf({ pages: 5 }),
  doc_long: buildPdf({ pages: 14 }),
  doc_cjk: buildPdf({ pages: 2, cjk: true }),
  doc_radicals: buildPdf({ pages: 1, cjk: true, radicals: true }),
  doc_rot90: buildPdf({ pages: 1, rotate: 90 }),
  doc_rot180: buildPdf({ pages: 1, rotate: 180 }),
  doc_rot270: buildPdf({ pages: 1, rotate: 270 }),
  doc_broken: Buffer.from('%PDF-1.4\nthis is not a PDF document\n'),
};

test.beforeEach(async ({ page }) => {
  await page.route('**/api/media/**', (route) => {
    const parts = new URL(route.request().url()).pathname.split('/');
    const isMeta = parts.at(-1) === 'meta';
    const id = (isMeta ? parts.at(-2) : parts.at(-1)) ?? '';
    const body = DOCS[id];
    if (!body) return route.fulfill({ status: 404, body: 'not found' });
    if (isMeta) {
      return route.fulfill({ json: { name: `${id}.pdf`, size: body.length, content_type: 'application/pdf', ext: 'pdf' } });
    }
    return route.fulfill({ status: 200, contentType: 'application/pdf', body });
  });
});

const openPreview = async (page: Page, file: string) => {
  await page.goto(`/e2e/pdf-preview/fixture.html?file=${file}`);
  await page.getByRole('button', { name: 'Preview' }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog).toBeVisible();
  return dialog;
};

const pageBox = (dialog: Locator, n: number) => dialog.locator(`[data-pdf-page="${n}"]`);
const scroller = (dialog: Locator) => dialog.locator('[data-pdf-scroller]');
const position = (dialog: Locator) => dialog.locator('[data-pdf-position]');

// The colour at the centre of the page's band, read from its canvas.
const bandColor = (dialog: Locator, n: number): Promise<Rgb> =>
  pageBox(dialog, n)
    .locator('canvas')
    .evaluate(
      (canvas: HTMLCanvasElement, geometry) => {
        const x = Math.floor((canvas.width * (geometry.x + geometry.width / 2)) / geometry.pageWidth);
        const y = Math.floor(canvas.height * (1 - (geometry.y + geometry.height / 2) / geometry.pageHeight));
        const [r, g, b] = canvas.getContext('2d')!.getImageData(x, y, 1, 1).data;
        return [r, g, b] as const;
      },
      { ...BAND, pageWidth: PAGE_WIDTH, pageHeight: PAGE_HEIGHT },
    );

const expectDrawn = async (dialog: Locator, n: number) => {
  await expect(pageBox(dialog, n)).toHaveAttribute('data-pdf-rendered', 'true');
  const [r, g, b] = await bandColor(dialog, n);
  const [er, eg, eb] = pageColor(n - 1);
  const close = (a: number, e: number) => Math.abs(a - e) <= 16;
  expect(close(r, er) && close(g, eg) && close(b, eb), `page ${n} band is rgb(${r}, ${g}, ${b}), expected rgb(${er}, ${eg}, ${eb})`).toBe(true);
};

// The text layer's span for ``text`` on page ``n``, measured against the page's own drawing: the
// share of dark pixels under the span, and beside it on the same line, on whichever side has room
// (blank in every fixture). Text that is selectable but not where it is drawn would highlight the
// wrong words.
const textOverInk = (dialog: Locator, n: number, text: string) =>
  pageBox(dialog, n).evaluate((box, wanted) => {
    const span = Array.from(box.querySelectorAll('.vr-pdf-text > span')).find((el) => el.textContent === wanted);
    const canvas = box.querySelector('canvas');
    if (!span || !canvas) return null;
    const page = box.getBoundingClientRect();
    const rect = span.getBoundingClientRect();
    const sx = canvas.width / page.width;
    const sy = canvas.height / page.height;
    const ctx = canvas.getContext('2d')!;
    const ink = (x: number, y: number, w: number, h: number) => {
      const data = ctx.getImageData(Math.round((x - page.left) * sx), Math.round((y - page.top) * sy), Math.max(1, Math.round(w * sx)), Math.max(1, Math.round(h * sy))).data;
      let dark = 0;
      for (let i = 0; i < data.length; i += 4) if (data[i] < 100 && data[i + 1] < 100 && data[i + 2] < 100 && data[i + 3] > 128) dark += 1;
      return dark / (data.length / 4);
    };
    const besideX = rect.right + 4 + rect.width <= page.right ? rect.right + 4 : rect.left - 4 - rect.width;
    return { over: ink(rect.left, rect.top, rect.width, rect.height), beside: ink(besideX, rect.top, rect.width, rect.height) };
  }, text);

const expectTextOverInk = async (dialog: Locator, n: number, text: string) => {
  await expect(pageBox(dialog, n)).toHaveAttribute('data-pdf-text', 'true');
  const ink = await textOverInk(dialog, n, text);
  expect(ink, `page ${n} has a text span "${text}"`).not.toBeNull();
  expect(ink!.over, `ink under "${text}"`).toBeGreaterThan(0.08);
  expect(ink!.beside, `ink beside "${text}"`).toBeLessThan(0.01);
};

const spanBox = async (dialog: Locator, n: number, text: string) =>
  (await pageBox(dialog, n).locator('.vr-pdf-text > span').filter({ hasText: text }).first().boundingBox())!;

const selectionText = (page: Page) => page.evaluate(() => document.getSelection()?.toString() ?? '');

// Press on the start of the first line and drag to a point, as a mouse or a dragged selection handle
// does; reports the selection while the pointer is still down.
const dragSelect = async (page: Page, from: { x: number; y: number }, to: { x: number; y: number }) => {
  await page.mouse.move(from.x, from.y);
  await page.mouse.down();
  for (let i = 1; i <= 12; i += 1) await page.mouse.move(from.x + ((to.x - from.x) * i) / 12, from.y + ((to.y - from.y) * i) / 12);
  const selected = await selectionText(page);
  await page.mouse.up();
  return selected;
};

const scrollToPage = async (dialog: Locator, n: number) => {
  await scroller(dialog).evaluate((el, top) => el.scrollTo({ top }), await pageBox(dialog, n).evaluate((p) => (p as HTMLElement).offsetTop));
};

test.describe('phones and tablets', () => {
  test.skip(({ isMobile }) => !isMobile, 'the page reader is the touch-device path');

  test('every page of a PDF can be scrolled to, and each one is drawn', async ({ page }) => {
    const dialog = await openPreview(page, 'doc_five');
    await expect(dialog.locator('iframe')).toHaveCount(0);
    await expect(dialog.locator('[data-pdf-page]')).toHaveCount(5);
    await expectDrawn(dialog, 1);
    await expect(position(dialog)).toHaveText('1 / 5');

    // The pages stack past the viewer's height: page one alone does not fill the document.
    const { scrollHeight, clientHeight } = await scroller(dialog).evaluate((el) => ({ scrollHeight: el.scrollHeight, clientHeight: el.clientHeight }));
    expect(scrollHeight).toBeGreaterThan(clientHeight * 2);

    await scroller(dialog).evaluate((el) => el.scrollTo({ top: el.scrollHeight }));
    await expectDrawn(dialog, 5);
    await expect(position(dialog)).toHaveText('5 / 5');

    await scrollToPage(dialog, 3);
    await expectDrawn(dialog, 3);
    await expect(position(dialog)).toHaveText('3 / 5');
  });

  test('pages are fit to the viewer width', async ({ page }) => {
    const dialog = await openPreview(page, 'doc_five');
    await expectDrawn(dialog, 1);
    const view = (await scroller(dialog).boundingBox())!;
    const first = (await pageBox(dialog, 1).boundingBox())!;
    expect(first.width).toBeGreaterThan(view.width - 40);
    expect(first.width).toBeLessThanOrEqual(view.width);
    expect(first.height / first.width).toBeCloseTo(PAGE_HEIGHT / PAGE_WIDTH, 1);
  });

  test('a long document only keeps canvases near what is on screen', async ({ page }) => {
    const dialog = await openPreview(page, 'doc_long');
    await expect(dialog.locator('[data-pdf-page]')).toHaveCount(14);
    await expectDrawn(dialog, 1);
    // Far below the fold at the top of the document.
    await expect(pageBox(dialog, 14).locator('canvas')).toHaveCount(0);

    await scroller(dialog).evaluate((el) => el.scrollTo({ top: el.scrollHeight }));
    await expectDrawn(dialog, 14);
    await expect(pageBox(dialog, 1).locator('canvas')).toHaveCount(0);
    await expect(pageBox(dialog, 1).locator('.vr-pdf-text')).toHaveCount(0);
    expect(await dialog.locator('[data-pdf-page] canvas').count()).toBeLessThan(14);
  });

  test('zooming keeps the page being read in view, and fit restores it', async ({ page }) => {
    const dialog = await openPreview(page, 'doc_five');
    await expectDrawn(dialog, 1);
    const fitWidth = (await pageBox(dialog, 1).boundingBox())!.width;

    await scrollToPage(dialog, 3);
    await expect(position(dialog)).toHaveText('3 / 5');
    await dialog.getByRole('button', { name: 'Zoom in' }).click();
    await dialog.getByRole('button', { name: 'Zoom in' }).click();
    await expect(dialog.getByText('150%')).toBeVisible();
    await expect.poll(async () => Math.round((await pageBox(dialog, 3).boundingBox())!.width)).toBe(Math.round(fitWidth * 1.5));

    // The viewport's centre is still on page three, and page three is redrawn at the new size.
    const view = (await scroller(dialog).boundingBox())!;
    const third = (await pageBox(dialog, 3).boundingBox())!;
    const centreY = view.y + view.height / 2;
    expect(centreY).toBeGreaterThan(third.y);
    expect(centreY).toBeLessThan(third.y + third.height);
    await expect(position(dialog)).toHaveText('3 / 5');
    await expectDrawn(dialog, 3);
    // Once the page is redrawn at the new size, its selectable text is still over the drawing.
    await expect
      .poll(() =>
        pageBox(dialog, 3).evaluate((box) => {
          const canvas = box.querySelector('canvas');
          return !!canvas && canvas.width >= Math.floor(box.getBoundingClientRect().width * window.devicePixelRatio) - 1;
        }),
      )
      .toBe(true);
    await expectTextOverInk(dialog, 3, 'Page 3');

    await dialog.getByRole('button', { name: 'Reset zoom' }).click();
    await expect(dialog.getByText('100%')).toBeVisible();
    await expect.poll(async () => Math.round((await pageBox(dialog, 3).boundingBox())!.width)).toBe(Math.round(fitWidth));
  });

  test('a two-finger pinch zooms the document', async ({ page, browserName }) => {
    test.skip(browserName !== 'chromium', 'multi-touch input is driven through the Chromium DevTools protocol');
    const dialog = await openPreview(page, 'doc_five');
    await expectDrawn(dialog, 1);
    const fitWidth = (await pageBox(dialog, 1).boundingBox())!.width;
    const view = (await scroller(dialog).boundingBox())!;
    const cx = view.x + view.width / 2;
    const cy = view.y + view.height / 3;

    const cdp = await page.context().newCDPSession(page);
    const fingers = (spread: number) => [
      { x: cx - spread, y: cy, id: 1 },
      { x: cx + spread, y: cy, id: 2 },
    ];
    await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: fingers(30) });
    for (let spread = 40; spread <= 90; spread += 10) {
      await cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: fingers(spread) });
    }
    await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });

    // Spread 60px → 180px: three times the fit width.
    await expect(dialog.getByText('300%')).toBeVisible();
    await expect.poll(async () => Math.round((await pageBox(dialog, 1).boundingBox())!.width)).toBe(Math.round(fitWidth * 3));
    // The browser's own page zoom stayed out of it.
    expect(await page.evaluate(() => window.visualViewport?.scale ?? 1)).toBe(1);
  });

  test('Chinese text set through a predefined CMap is decoded with the bundled CMaps', async ({ page }) => {
    const cmapRequests: string[] = [];
    page.on('request', (request) => {
      if (/UniGB-UCS2-H[^/]*\.bcmap/.test(request.url())) cmapRequests.push(request.url());
    });
    const dialog = await openPreview(page, 'doc_cjk');
    await expectDrawn(dialog, 1);
    expect(cmapRequests.length).toBeGreaterThan(0);
    // And it is selectable as the characters it shows, so a copy gives back real Chinese text.
    await expect(pageBox(dialog, 1).locator('.vr-pdf-text')).toContainText('中文预览');
    await expectTextOverInk(dialog, 1, '中文预览');
  });

  test('characters a font maps to look-alike radicals are selected as the characters shown', async ({ page }) => {
    const dialog = await openPreview(page, 'doc_radicals');
    await expectTextOverInk(dialog, 1, '月份');
    await expect(pageBox(dialog, 1).locator('.vr-pdf-text')).not.toContainText('\u2F49');
  });

  test('on a page with /Rotate the text is turned with the drawing', async ({ page }) => {
    for (const file of ['doc_rot90', 'doc_rot180', 'doc_rot270']) {
      const dialog = await openPreview(page, file);
      await expectTextOverInk(dialog, 1, 'Page 1');
      await dialog.getByRole('button', { name: 'Close' }).click();
      await expect(dialog).toHaveCount(0);
    }
  });

  test("each page's text lies over its drawing and is what a touch on it reaches", async ({ page }) => {
    const dialog = await openPreview(page, 'doc_five');
    await expectTextOverInk(dialog, 1, 'Page 1');
    const box = await spanBox(dialog, 1, 'Page 1');
    const reached = await page.evaluate(({ x, y }) => document.elementFromPoint(x, y)?.textContent, { x: box.x + box.width / 4, y: box.y + box.height / 2 });
    expect(reached).toBe('Page 1');

    await scrollToPage(dialog, 4);
    await expectTextOverInk(dialog, 4, 'Page 4');
  });

  test('a long press selects the word under the finger', async ({ page, browserName }) => {
    test.skip(browserName !== 'chromium', 'a native long press is driven through the Chromium DevTools protocol');
    const dialog = await openPreview(page, 'doc_five');
    await expectTextOverInk(dialog, 1, 'Page 1');
    const box = await spanBox(dialog, 1, 'Page 1');
    const cdp = await page.context().newCDPSession(page);
    await cdp.send('Input.synthesizeTapGesture', { x: box.x + box.width / 5, y: box.y + box.height / 2, duration: 1200, tapCount: 1, gestureSourceType: 'touch' });
    await expect.poll(() => selectionText(page)).toBe('Page');
  });

  test('extending a selection into blank space keeps it on the text', async ({ page }) => {
    const dialog = await openPreview(page, 'doc_cjk');
    await expectTextOverInk(dialog, 1, 'Page 1');
    const first = await spanBox(dialog, 1, 'Page 1');
    const sheet = (await pageBox(dialog, 1).boundingBox())!;
    const start = { x: first.x + 1, y: first.y + first.height / 2 };
    // Past the end of the line, in the margin: WebKit would otherwise snap the selection's end to the
    // start of the page and select nothing.
    expect(await dragSelect(page, start, { x: sheet.x + sheet.width - 20, y: start.y })).toMatch(/^Page/);
    await page.evaluate(() => document.getSelection()?.removeAllRanges());
    // Below both lines of text, above the coloured band.
    expect(await dragSelect(page, start, { x: sheet.x + sheet.width / 2, y: sheet.y + sheet.height * 0.26 })).toMatch(/^Page 1/);
  });

  test('a file that is not a valid PDF says so instead of drawing blank pages', async ({ page }) => {
    const dialog = await openPreview(page, 'doc_broken');
    await expect(dialog.getByText("Couldn't load this file for preview.")).toBeVisible();
    await expect(dialog.locator('[data-pdf-page]')).toHaveCount(0);
  });
});

test.describe('desktop browsers', () => {
  test.skip(({ isMobile }) => isMobile, 'desktop reader selection');

  const reportViewer = (page: Page, enabled: boolean) =>
    page.addInitScript((value) => {
      Object.defineProperty(Navigator.prototype, 'pdfViewerEnabled', { configurable: true, get: () => value });
    }, enabled);

  test('a browser with a built-in PDF viewer keeps it, with its search and printing', async ({ page }) => {
    await reportViewer(page, true);
    const dialog = await openPreview(page, 'doc_five');
    await expect(dialog.locator('iframe')).toHaveAttribute('src', '/api/media/doc_five');
    await expect(dialog.locator('[data-pdf-scroller]')).toHaveCount(0);
  });

  test('a browser without one gets the page reader', async ({ page }) => {
    await reportViewer(page, false);
    const dialog = await openPreview(page, 'doc_five');
    await expect(dialog.locator('iframe')).toHaveCount(0);
    await expect(dialog.locator('[data-pdf-page]')).toHaveCount(5);
    await expectDrawn(dialog, 1);
  });

  test("the page reader's text can be selected with the mouse and copied", async ({ page, context }) => {
    await reportViewer(page, false);
    await context.grantPermissions(['clipboard-read', 'clipboard-write']);
    const dialog = await openPreview(page, 'doc_five');
    await expectTextOverInk(dialog, 1, 'Page 1');
    const box = await spanBox(dialog, 1, 'Page 1');
    const selected = await dragSelect(page, { x: box.x + 1, y: box.y + box.height / 2 }, { x: box.x + box.width - 1, y: box.y + box.height / 2 });
    expect(selected).toBe('Page 1');
    await page.keyboard.press('ControlOrMeta+C');
    await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toBe('Page 1');
  });
});
