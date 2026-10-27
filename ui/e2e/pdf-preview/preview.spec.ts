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
});
