import { expect, test } from '@playwright/test';
import type { Locator, Page } from '@playwright/test';

import { DESKTOP, NARROW, open, serveProduct } from './support';

/**
 * Sidebar resizing (#2044), measured in a real browser: the gesture uses actual
 * pointer capture, and the claim is about boxes — the sidebar, the content
 * offset and the Settings overlay's left edge all following ONE width — which no
 * class name or jsdom pointer event can answer.
 */

const SIDEBAR = 'aside.fixed';
const SHELL_SCROLL = 'main#app-shell-scroll';
const SEPARATOR = 'aside.fixed [role="separator"]';
const SETTINGS_TOGGLE = 'aside [data-settings-toggle="true"]';
const OVERLAY = '[data-settings-overlay="true"]';
const MIN = 248;
const MAX = 496;
const SHOTS = 'e2e/.artifacts/workbench-general/shots';

const boxOf = async (locator: Locator, what: string) => {
  const box = await locator.boundingBox();
  expect(box, `${what} should be laid out`).not.toBeNull();
  return box!;
};

/** The sidebar's width and, independently, where the content beside it starts. */
const layout = async (page: Page) => ({
  sidebar: (await boxOf(page.locator(SIDEBAR), 'sidebar')).width,
  contentLeft: (await boxOf(page.locator(SHELL_SCROLL), 'shell content')).x,
});

/**
 * One real gesture on the edge, ending wherever the travel ends — off the strip
 * and over the page, which is the case pointer capture exists for. Each move
 * also travels vertically, so a width that followed anything but the x axis
 * would show up.
 */
const dragEdge = async (page: Page, ...offsets: number[]) => {
  const strip = await boxOf(page.locator(SEPARATOR), 'separator');
  const startX = strip.x + strip.width / 2;
  const startY = strip.y + strip.height / 2;
  await page.mouse.move(startX, startY);
  await page.mouse.down();
  for (const offset of offsets) {
    await page.mouse.move(startX + offset, startY + 60, { steps: 6 });
  }
  await page.mouse.up();
};

/** What `var(--cyan)` actually computes to in this theme. */
const cyanOf = (page: Page) => page.evaluate(() => {
  const probe = document.createElement('span');
  probe.style.color = 'var(--cyan)';
  document.body.append(probe);
  const value = getComputedStyle(probe).color;
  probe.remove();
  return value;
});

const TRANSPARENT = 'rgba(0, 0, 0, 0)';

/** The line's colour once its own transition has settled on it. */
const expectEdgeLine = (page: Page, color: string) => expect
  .poll(async () => (await edgeStyle(page)).borderRightColor, { timeout: 2_000 })
  .toBe(color);

const edgeStyle = (page: Page) => page.locator(SEPARATOR).evaluate((node) => {
  const style = getComputedStyle(node);
  return {
    borderRightColor: style.borderRightColor,
    borderRightWidth: style.borderRightWidth,
    backgroundColor: style.backgroundColor,
    cursor: style.cursor,
    touchAction: style.touchAction,
    width: style.width,
    // A handle, grip, dot or centered ornament would have to be one of these.
    children: node.childElementCount,
    before: getComputedStyle(node, '::before').content,
    after: getComputedStyle(node, '::after').content,
  };
});

test.describe('sidebar resizing', () => {
  test('moves the sidebar and the content beside it to one width, within fixed bounds', async ({ page }) => {
    const denied = await serveProduct(page);
    await page.setViewportSize(DESKTOP);
    await open(page, '/');
    await expect(page.locator(SIDEBAR)).toBeVisible();

    // The shipped width is the minimum, so nothing moved before the first drag.
    expect(await layout(page)).toEqual({ sidebar: MIN, contentLeft: MIN });

    await dragEdge(page, 100);
    expect(await layout(page)).toEqual({ sidebar: MIN + 100, contentLeft: MIN + 100 });
    expect(await page.locator(SEPARATOR).getAttribute('aria-valuenow')).toBe(String(MIN + 100));

    // Past the maximum and back inside the range in one gesture: the bound
    // clamps, the overshoot is not remembered, and the release lands far from
    // the edge — over the page, where only pointer capture still delivers it.
    await dragEdge(page, 900, 60);
    expect(await layout(page)).toEqual({ sidebar: MIN + 160, contentLeft: MIN + 160 });

    // Repeated gestures stop at the same two numbers rather than walking past them.
    await dragEdge(page, 900);
    expect(await layout(page)).toEqual({ sidebar: MAX, contentLeft: MAX });
    await dragEdge(page, 900);
    expect(await layout(page)).toEqual({ sidebar: MAX, contentLeft: MAX });
    await dragEdge(page, -900);
    expect(await layout(page)).toEqual({ sidebar: MIN, contentLeft: MIN });
    await dragEdge(page, -900);
    expect(await layout(page)).toEqual({ sidebar: MIN, contentLeft: MIN });

    // Released, the page selects text again — the drag only borrowed that.
    expect(await page.evaluate(() => [
      document.body.style.userSelect,
      document.body.style.cursor,
      getComputedStyle(document.body).userSelect,
    ])).toEqual(['', '', 'auto']);

    expect(denied).toEqual([]);
  });

  test('recolors the divider and nothing else', async ({ page }) => {
    const denied = await serveProduct(page);
    await page.setViewportSize(DESKTOP);
    await open(page, '/');
    await expect(page.locator(SIDEBAR)).toBeVisible();

    const cyan = await cyanOf(page);
    const sidebarBorder = () => page.locator(SIDEBAR).evaluate(
      (node) => getComputedStyle(node).borderRightColor,
    );
    const restingSidebarBorder = await sidebarBorder();

    // At rest the visible line is the sidebar's own border; the strip adds nothing.
    await expectEdgeLine(page, TRANSPARENT);
    expect(await edgeStyle(page)).toMatchObject({
      backgroundColor: TRANSPARENT,
      borderRightWidth: '1px',
      width: '8px',
      children: 0,
      before: 'none',
      after: 'none',
    });

    // The strip ends exactly at the sidebar's edge, so the line it recolors is
    // that divider and not a second one beside it.
    const strip = await boxOf(page.locator(SEPARATOR), 'separator');
    expect(strip.x + strip.width).toBe(MIN);

    await page.locator(SEPARATOR).hover();
    await expectEdgeLine(page, cyan);
    // Still only a line: no box, no ornament, no width change.
    expect(await edgeStyle(page)).toMatchObject({
      cursor: 'col-resize',
      backgroundColor: TRANSPARENT,
      borderRightWidth: '1px',
      width: '8px',
      children: 0,
      before: 'none',
      after: 'none',
    });
    // The sidebar itself is untouched — the highlight is the strip's own border.
    expect(await sidebarBorder()).toBe(restingSidebarBorder);

    // The highlight holds for the whole drag, including with the pointer far
    // away from the strip, and goes back when the gesture ends.
    const startX = strip.x + strip.width / 2;
    const startY = strip.y + strip.height / 2;
    await page.mouse.move(startX, startY);
    await page.mouse.down();
    await page.mouse.move(startX + 500, startY + 300, { steps: 8 });
    await expectEdgeLine(page, cyan);
    await page.mouse.up();
    await page.mouse.move(startX + 500, startY + 300);
    await expectEdgeLine(page, TRANSPARENT);

    // Keyboard focus is the same affordance, and only for keyboard focus.
    const focused = await page.evaluate(() => {
      const focusables = [...document.querySelectorAll<HTMLElement>('aside.fixed button, aside.fixed a[href]')];
      focusables[focusables.length - 1]?.focus();
      return focusables.length;
    });
    expect(focused).toBeGreaterThan(0);
    await page.keyboard.press('Tab');
    expect(await page.locator(SEPARATOR).evaluate((node) => [
      node === document.activeElement, node.matches(':focus-visible'),
    ])).toEqual([true, true]);
    await expectEdgeLine(page, cyan);

    expect(denied).toEqual([]);
  });

  test('adjusts the real layout from the keyboard within the same bounds', async ({ page }) => {
    const denied = await serveProduct(page);
    await page.setViewportSize(DESKTOP);
    await open(page, '/');
    await page.locator(SEPARATOR).focus();

    await page.keyboard.press('ArrowRight');
    const stepped = await layout(page);
    expect(stepped.sidebar).toBeGreaterThan(MIN);
    expect(stepped.contentLeft).toBe(stepped.sidebar);

    await page.keyboard.press('Home');
    expect(await layout(page)).toEqual({ sidebar: MIN, contentLeft: MIN });
    await page.keyboard.press('ArrowLeft');
    expect(await layout(page)).toEqual({ sidebar: MIN, contentLeft: MIN });

    await page.keyboard.press('End');
    expect(await layout(page)).toEqual({ sidebar: MAX, contentLeft: MAX });
    await page.keyboard.press('ArrowRight');
    expect(await layout(page)).toEqual({ sidebar: MAX, contentLeft: MAX });

    expect(denied).toEqual([]);
  });

  test('keeps the Settings overlay open and on the edge it is being dragged to', async ({ page }) => {
    const denied = await serveProduct(page);
    await page.setViewportSize(DESKTOP);
    await open(page, '/');

    await page.locator(SETTINGS_TOGGLE).click();
    const overlay = page.locator(OVERLAY);
    await expect(overlay).toBeVisible();

    // The overlay is portaled outside the shell, so this is the case the shared
    // document-level width exists for. Grabbing the edge must not dismiss it.
    await dragEdge(page, 900);
    await expect(overlay).toBeVisible();

    const surface = await boxOf(overlay, 'settings overlay');
    expect(await layout(page)).toEqual({ sidebar: MAX, contentLeft: MAX });
    expect(surface.x).toBe(MAX);
    expect(surface.width).toBe(DESKTOP.width - MAX);
    await expect(page.locator(SIDEBAR)).toBeVisible();

    // And back: the overlay follows the sidebar in, not just out.
    await dragEdge(page, -900);
    const narrowed = await boxOf(overlay, 'settings overlay');
    expect(narrowed.x).toBe(MIN);
    expect(narrowed.width).toBe(DESKTOP.width - MIN);

    expect(denied).toEqual([]);
  });

  /**
   * A touchscreen can report this breakpoint too — a tablet in landscape, a
   * convertible laptop — and there the browser, not the page, decides whether a
   * finger on this strip is a drag or its own pan. Pointer capture does not
   * enter that negotiation: touch-action does, and losing it means Chromium
   * cancels the pointer part-way through the gesture. So this drives real touch
   * input through CDP rather than asserting the utility class that asks for it.
   */
  test.describe('with a finger', () => {
    test.use({ hasTouch: true });

    test('resizes from a touch drag the browser does not take over', async ({ page }) => {
      const denied = await serveProduct(page);
      await page.setViewportSize(DESKTOP);
      await open(page, '/');
      await expect(page.locator(SIDEBAR)).toBeVisible();

      // What the browser was given for the decision.
      expect(await edgeStyle(page)).toMatchObject({ touchAction: 'none' });

      // What it then did with the gesture: a cancelled pointer is exactly how a
      // pan taking the finger over shows up at the element.
      await page.locator(SEPARATOR).evaluate((node) => {
        const seen: string[] = [];
        (window as unknown as { __edgePointerLog: string[] }).__edgePointerLog = seen;
        for (const type of ['pointerdown', 'pointermove', 'pointercancel', 'pointerup']) {
          node.addEventListener(type, (event) => seen.push(event.type));
        }
      });

      const strip = await boxOf(page.locator(SEPARATOR), 'separator');
      const from = { x: strip.x + strip.width / 2, y: strip.y + strip.height / 2 };
      const cdp = await page.context().newCDPSession(page);
      const point = (x: number, y: number) => (
        [{ x, y, radiusX: 8, radiusY: 8, force: 1, id: 1 }]
      );
      await cdp.send('Input.dispatchTouchEvent', {
        type: 'touchStart',
        touchPoints: point(from.x, from.y),
      });
      // Stepped, and with a vertical component, so the travel crosses Chromium's
      // slop threshold the way a finger does — a single jump would not be
      // recognised as a pan either, and would prove nothing about the default.
      const steps = 10;
      for (let step = 1; step <= steps; step += 1) {
        await cdp.send('Input.dispatchTouchEvent', {
          type: 'touchMove',
          touchPoints: point(from.x + (120 * step) / steps, from.y + (40 * step) / steps),
        });
      }
      await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
      await cdp.detach();

      expect(await layout(page)).toEqual({ sidebar: MIN + 120, contentLeft: MIN + 120 });
      const log = await page.evaluate(
        () => (window as unknown as { __edgePointerLog: string[] }).__edgePointerLog,
      );
      expect(log).toContain('pointermove');
      expect(log).not.toContain('pointercancel');

      // And the finger let go of the page as cleanly as the mouse does.
      expect(await page.evaluate(() => [
        document.body.style.userSelect,
        document.body.style.cursor,
      ])).toEqual(['', '']);

      expect(denied).toEqual([]);
    });
  });

  test('leaves a phone with no edge to drag', async ({ page }) => {
    await serveProduct(page);
    await page.setViewportSize(NARROW);
    await open(page, '/');
    await expect(page.locator(SHELL_SCROLL)).toBeVisible();

    await expect(page.locator(SEPARATOR)).toHaveCount(0);
  });

  // The bounded visual pass: the widened sidebar in both themes, so the rows it
  // holds can be looked at rather than inferred from a width.
  for (const theme of ['dark', 'light'] as const) {
    test(`captures the widened sidebar (${theme})`, async ({ page }) => {
      await serveProduct(page);
      await page.emulateMedia({ reducedMotion: 'reduce' });
      await page.setViewportSize(DESKTOP);
      await open(page, '/', { theme });
      await expect(page.locator(SIDEBAR)).toBeVisible();
      await page.waitForTimeout(400);

      await page.locator(SEPARATOR).focus();
      await page.keyboard.press('End');
      expect((await layout(page)).sidebar).toBe(MAX);
      await page.screenshot({ path: `${SHOTS}/sidebar-resized-${theme}-desktop.png` });
    });
  }
});
