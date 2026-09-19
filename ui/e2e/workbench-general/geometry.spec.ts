import { expect, test } from '@playwright/test';

import { DESKTOP, NARROW, ORIGIN, WIDE, open, serveProduct } from './support';

/**
 * The packet's geometry claims, measured in a browser rather than inferred from
 * class names. Workbench keeps its adjustable 248px default/sidebar offset;
 * standalone Settings has a 196px rail and a 944px outer content frame with
 * 880px of common content at the desktop reference width.
 */

const SIDEBAR = 'aside.fixed';
const SHELL_SCROLL = 'main#app-shell-scroll';
const SHELL_CONTENT = 'main#app-shell-scroll > div';
const SETTINGS_RAIL = 'nav[aria-label="Settings sections"]';
const SETTINGS_PAGE = 'nav[aria-label="Settings sections"] + section';
const SETTINGS_CONTENT = 'nav[aria-label="Settings sections"] + section > div';
const APPEARANCE_CARD = 'section.bg-surface-2:has([role="radiogroup"])';
// The composer's own workspace chip, not the sidebar tree row: it is the
// selection the overlay has to hand back, and its label carries the fixture's
// non-ASCII path.
const WORKSPACE_CHIP = 'Workspace: /Users/max/工作区/中文项目';
const MOBILE_NAV = 'nav.fixed.bottom-0';
const ULTRA = { width: 1920, height: 1000 };
// A phone one iPhone-generation shorter. The composer has to clear the tab bar
// because of where it is anchored, not because 844 happens to leave room.
const NARROW_SHORT = { width: 390, height: 667 };

const widthOf = async (page: import('@playwright/test').Page, selector: string) => {
  const box = await page.locator(selector).first().boundingBox();
  expect(box, `${selector} should be laid out`).not.toBeNull();
  return box!.width;
};

/**
 * What the user's finger would actually land on. `toBeVisible()` cannot answer
 * this: an element covered by a fixed bar is still visible, still has a box, and
 * still fails every tap.
 */
const topmostOver = async (
  page: import('@playwright/test').Page,
  locator: import('@playwright/test').Locator,
) => {
  const box = await locator.boundingBox();
  expect(box, 'control should be laid out').not.toBeNull();
  return page.evaluate(({ x, y }) => {
    const node = document.elementFromPoint(x, y);
    if (!node) return 'nothing';
    const control = node.closest('button, a');
    return control?.getAttribute('aria-label') ?? control?.textContent?.trim() ?? node.tagName;
  }, { x: box!.x + box!.width / 2, y: box!.y + box!.height / 2 });
};

const bottomOf = async (locator: import('@playwright/test').Locator) => {
  const box = await locator.boundingBox();
  expect(box, 'element should be laid out').not.toBeNull();
  return box!.y + box!.height;
};

test.describe('workbench home geometry', () => {
  test('keeps the sidebar at 248 and lets everything beside it grow', async ({ page }) => {
    const denied = await serveProduct(page);

    await page.setViewportSize(DESKTOP);
    await open(page, '/');
    await expect(page.locator(SIDEBAR)).toBeVisible();

    expect(await widthOf(page, SIDEBAR)).toBe(248);
    const atStaging = await widthOf(page, SHELL_CONTENT);

    await page.setViewportSize(WIDE);
    await page.waitForFunction(() => window.innerWidth === 1600);

    // The sidebar is the fixed part; the column beside it is not. It has to take
    // the full extra 400, because a cap of any size would swallow some of it.
    expect(await widthOf(page, SIDEBAR)).toBe(248);
    const atWide = await widthOf(page, SHELL_CONTENT);
    expect(atWide - atStaging).toBe(WIDE.width - DESKTOP.width);

    // eslint-disable-next-line no-console
    console.log(`home content: ${atStaging} @1200, ${atWide} @1600`);
    expect(denied).toEqual([]);
  });

  test('drops the sidebar on a phone and still fills the width', async ({ page }) => {
    await serveProduct(page);
    await page.setViewportSize(NARROW);
    await open(page, '/');

    await expect(page.locator(SIDEBAR)).toBeHidden();
    expect(await widthOf(page, SHELL_CONTENT)).toBe(NARROW.width);
  });

  /**
   * The phone home is taller than the screen, so whatever ends the page sits
   * under the fixed tab bar until someone scrolls. What ends the page is the
   * composer's own action row — Agent, workspace, Send — so the defect is a
   * dead primary button on first paint, not a cropped screenshot. Both
   * languages, because the row's left side is translated text whose width moves.
   */
  const PHONE_COMPOSER: { lang: 'en' | 'zh'; placeholder: string; send: string; lastRow: string }[] = [
    { lang: 'en', placeholder: 'Describe a task or ask a question...', send: 'Send', lastRow: 'Continue on your phone' },
    { lang: 'zh', placeholder: '给你的助手安排任务…', send: '发送', lastRow: '在手机 APP 上继续' },
  ];

  for (const { lang, placeholder, send, lastRow } of PHONE_COMPOSER) {
    test(`keeps the composer's controls off the tab bar on a phone (${lang})`, async ({ page }) => {
      const denied = await serveProduct(page, lang);
      await page.setViewportSize(NARROW);
      await open(page, '/', { lang });

      const composer = page.getByPlaceholder(placeholder);
      const sendButton = page.getByRole('button', { name: send });
      const nav = page.locator(MOBILE_NAV);
      await expect(composer).toBeVisible();
      await expect(nav).toBeVisible();

      // The premise: this page really does overflow, so clearing the bar is the
      // anchoring and not a lucky fit. If this ever stops being true the rest of
      // the test would pass for the wrong reason.
      const overflow = await page.locator(SHELL_SCROLL).evaluate((node) => ({
        top: node.scrollTop,
        hidden: node.scrollHeight - node.clientHeight,
      }));
      expect(overflow.top).toBe(0);
      expect(overflow.hidden).toBeGreaterThan(0);

      // Nothing in the block may reach under the bar — asserted on the block's
      // last row, which is below the action row.
      const navTop = (await nav.boundingBox())!.y;
      expect(await bottomOf(page.getByRole('link', { name: lastRow }))).toBeLessThanOrEqual(navTop);
      // …and the primary control specifically: what a tap would hit, at rest.
      expect(await topmostOver(page, sendButton)).toBe(send);

      // Typing is when it matters, and a focused textarea is also when the page
      // could scroll under the user. It must not have to.
      await composer.click();
      await composer.fill('给手机上的我写一句话');
      await expect(composer).toBeFocused();
      expect(await page.locator(SHELL_SCROLL).evaluate((node) => node.scrollTop)).toBe(0);
      expect(await topmostOver(page, sendButton)).toBe(send);
      // Playwright's own actionability check, which is the product's tap path.
      await sendButton.click({ trial: true });

      // A shorter phone has less room above, never less clearance below.
      await page.setViewportSize(NARROW_SHORT);
      await page.waitForFunction(() => window.innerHeight === 667);
      expect(await topmostOver(page, sendButton)).toBe(send);
      expect(await bottomOf(page.getByRole('link', { name: lastRow })))
        .toBeLessThanOrEqual((await nav.boundingBox())!.y);

      expect(denied).toEqual([]);
    });
  }
});

test.describe('settings overlay geometry', () => {
  test('opens as a standalone zero-offset surface and gives the home back its draft on close', async ({ page }) => {
    const denied = await serveProduct(page);

    await page.setViewportSize(DESKTOP);
    await open(page, '/');

    // A draft and a project selection are what the overlay exists to preserve,
    // so the assertion starts by creating both. Non-ASCII on purpose.
    const draft = '给中文项目写一份说明';
    const composer = page.getByPlaceholder('Describe a task or ask a question...');
    await composer.fill(draft);
    await expect(composer).toHaveValue(draft);
    await expect(page.getByLabel(WORKSPACE_CHIP)).toBeVisible();

    await page.locator('aside [data-settings-toggle="true"]').click();
    await expect(page).toHaveURL(/\/settings\/general$/);

    const overlay = page.locator('[data-settings-overlay="true"]');
    await expect(overlay).toBeVisible();

    // Standalone Settings owns the viewport at every sidebar width. The origin
    // remains mounted behind the surface for draft/session/selection retention,
    // but no Workbench offset is allowed to leak into the foreground.
    const surface = await overlay.boundingBox();
    expect(surface!.x).toBe(0);
    expect(surface!.width).toBe(DESKTOP.width);
    await expect(page.locator(SIDEBAR)).toBeHidden();

    await page.getByRole('button', { name: 'Close Settings' }).click();
    await expect(page).toHaveURL(`${ORIGIN}/`);
    await expect(overlay).toHaveCount(0);
    await expect(page.getByPlaceholder('Describe a task or ask a question...')).toHaveValue(draft);
    await expect(page.getByLabel(WORKSPACE_CHIP)).toBeVisible();

    expect(denied).toEqual([]);
  });
});

test.describe('page background family', () => {
  test('draws the home flat and leaves the console wash on its siblings', async ({ page }) => {
    await serveProduct(page);
    await page.setViewportSize(DESKTOP);

    // Board 04 stages the home on flat $--background.
    await open(page, '/');
    await expect(page.locator(SIDEBAR)).toBeVisible();
    expect(await page.locator(SHELL_SCROLL).evaluate((n) => getComputedStyle(n).backgroundImage)).toBe('none');

    // …and the rest of the console family keeps the aurora it already had, which
    // is the half of this decision a capture of the home alone cannot show.
    await open(page, '/agents');
    expect(await page.locator(SHELL_SCROLL).evaluate((n) => getComputedStyle(n).backgroundImage))
      .toContain('radial-gradient');
  });
});

test.describe('general settings geometry', () => {
  test('keeps the rail at 196 and the common content at 880px', async ({ page }) => {
    const denied = await serveProduct(page);

    await page.setViewportSize(DESKTOP);
    await open(page, '/settings/general');
    await expect(page.locator(SETTINGS_RAIL)).toBeVisible();

    expect(await widthOf(page, SETTINGS_RAIL)).toBe(196);
    const cardAtStaging = await widthOf(page, APPEARANCE_CARD);

    // The desktop Settings frame is 944px wide with 32px horizontal padding,
    // leaving an 880px common content column.
    await page.setViewportSize(ULTRA);
    await page.waitForFunction(() => window.innerWidth === 1920);

    expect(await widthOf(page, SETTINGS_RAIL)).toBe(196);
    const cardAtUltra = await widthOf(page, APPEARANCE_CARD);
    expect(cardAtStaging).toBe(880);
    expect(cardAtUltra).toBe(880);
    expect(denied).toEqual([]);
  });

  test('gives the landing its own heading block and leaves the sections alone', async ({ page }) => {
    await serveProduct(page);
    await page.setViewportSize(DESKTOP);

    const headingOf = async (path: string) => {
      await open(page, path);
      const heading = page.locator(`${SETTINGS_PAGE} h1`).first();
      await expect(heading).toBeVisible();
      return heading.evaluate((node) => {
        const style = getComputedStyle(node);
        return { size: style.fontSize, weight: style.fontWeight };
      });
    };

    // Ozmrf draws the landing at 27/600; every section keeps the 28/700 it
    // already shipped with, which is why the variant is opt-in.
    expect(await headingOf('/settings/general')).toEqual({ size: '27px', weight: '600' });
    expect(await headingOf('/settings/shortcuts')).toEqual({ size: '28px', weight: '700' });
  });

  test('uses the same standalone frame for a direct URL at a real desktop width', async ({ page }) => {
    await serveProduct(page);
    await page.setViewportSize({ width: 1448, height: 900 });
    await open(page, '/settings/general');
    await expect(page.locator(SETTINGS_RAIL)).toBeVisible();

    // dqfES — the source's standalone Settings content frame.
    expect(await widthOf(page, SETTINGS_CONTENT)).toBe(944);
    await expect(page.locator(SIDEBAR)).toBeHidden();
  });

  // A rail label is the only thing that says where a row goes, and English has
  // two neighbours that both ended in "Platform…" at 196. The rail width is the
  // source's, so the labels wrap instead.
  test('shows every rail label in full without widening the rail', async ({ page }) => {
    await serveProduct(page);
    await page.setViewportSize(DESKTOP);
    await open(page, '/settings/platforms');
    await expect(page.locator(SETTINGS_RAIL)).toBeVisible();

    expect(await widthOf(page, SETTINGS_RAIL)).toBe(196);

    // Measured, not eyeballed: a wrapped label's scrollWidth equals its
    // clientWidth; a clipped one exceeds it.
    const clipped = await page
      .locator(`${SETTINGS_RAIL} a[href^="/settings"] span, ${SETTINGS_RAIL} button[aria-expanded] span`)
      .evaluateAll((nodes) => nodes
        .filter((node) => node.getClientRects().length > 0 && node.scrollWidth > node.clientWidth)
        .map((node) => node.textContent?.trim() ?? ''));
    expect(clipped).toEqual([]);

    const rail = page.locator(SETTINGS_RAIL);
    await expect(rail.getByRole('button', { name: 'Messaging Platforms' })).toBeVisible();
    await expect(rail.getByRole('link', { name: 'Platform Connections' })).toBeVisible();

    // Two lines at this size still fit the row height the rail already had, so
    // the rows that never wrapped do not move.
    const rows = await rail.locator('a[href^="/settings"]').evaluateAll((nodes) =>
      nodes.map((node) => Math.round(node.getBoundingClientRect().height)));
    expect(new Set(rows).size).toBe(1);
  });

  test('keeps the common 880px content width on every ordinary settings page', async ({ page }) => {
    await serveProduct(page);
    await page.setViewportSize(ULTRA);
    await open(page, '/settings/shortcuts');
    await expect(page.locator(SETTINGS_RAIL)).toBeVisible();

    expect(await widthOf(page, SETTINGS_CONTENT)).toBe(944);
  });

  test('draws the preference card, the selector and the selected choice to spec', async ({ page }) => {
    await serveProduct(page);
    await page.setViewportSize(DESKTOP);
    await open(page, '/settings/general', { theme: 'dark' });

    const card = page.locator(APPEARANCE_CARD);
    await expect(card).toBeVisible();

    const cardStyle = await card.evaluate((node) => {
      const outer = getComputedStyle(node);
      const inner = getComputedStyle(node.firstElementChild as Element);
      return { radius: outer.borderRadius, pad: inner.padding, gap: inner.rowGap, bg: outer.backgroundColor };
    });
    // le5QU / Q8zxF1: padding 22, radius 12 — not the 16 the older `panel`
    // variant renders, which is why `preference` is its own variant.
    expect(cardStyle.radius).toBe('12px');
    expect(cardStyle.pad).toBe('22px');
    expect(cardStyle.gap).toBe('20px');

    const surface2 = await page.evaluate(() => {
      const probe = document.createElement('div');
      probe.style.backgroundColor = 'var(--surface-2)';
      document.body.appendChild(probe);
      const value = getComputedStyle(probe).backgroundColor;
      probe.remove();
      return value;
    });
    expect(cardStyle.bg).toBe(surface2);

    const select = page.getByLabel('Language', { exact: true });
    const selectBox = await select.boundingBox();
    expect(selectBox!.width).toBe(190);
    expect(selectBox!.height).toBe(40);
    expect(await select.evaluate((node) => getComputedStyle(node).borderRadius)).toBe('9px');

    const dark = page.getByRole('radio', { name: 'Dark' });
    await expect(dark).toHaveAttribute('aria-checked', 'true');
    const selectedBorder = await dark.evaluate((node) => {
      const style = getComputedStyle(node);
      return { width: style.borderTopWidth, color: style.borderTopColor };
    });
    expect(selectedBorder.width).toBe('2px');

    const mint = await page.evaluate(() => {
      const probe = document.createElement('div');
      probe.style.color = 'var(--mint)';
      document.body.appendChild(probe);
      const value = getComputedStyle(probe).color;
      probe.remove();
      return value;
    });
    expect(selectedBorder.color).toBe(mint);

    // Picking a card must not nudge the row: the extra border pixel is absorbed
    // by padding, so every card in the group stays the same size.
    const boxes = await page.getByRole('radio').evaluateAll((nodes) =>
      nodes.map((node) => node.getBoundingClientRect().width));
    expect(new Set(boxes.map((w) => Math.round(w))).size).toBe(1);

    // Q8zxF1: a 14 gutter between the three choices, and an 88-tall preview at
    // radius 6 inside each one.
    expect(await page.locator('[role="radiogroup"]').evaluate((n) => getComputedStyle(n).columnGap)).toBe('14px');
    const preview = await dark.locator('[aria-hidden="true"]').first().evaluate((node) => ({
      height: node.getBoundingClientRect().height,
      radius: getComputedStyle(node).borderRadius,
    }));
    expect(preview.height).toBe(88);
    expect(preview.radius).toBe('6px');
  });
});
