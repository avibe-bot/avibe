import { expect, test, type Page } from '@playwright/test';

const tree = (page: Page) => page.getByTestId('desktop-tree').or(page.getByTestId('mobile-tree')).filter({ visible: true });
const rows = (page: Page) => tree(page).locator('[data-project-id]');
const header = (page: Page, index: number) => rows(page).nth(index).locator('.project-drag-header');
const ids = (page: Page) => rows(page).evaluateAll((elements) => elements.map((el) => el.getAttribute('data-project-id')));

test.beforeEach(async ({ page }) => {
  await page.goto('/e2e/project-order/fixture.html');
  await expect(rows(page)).toHaveCount(8);
});

test('header click expands; a direct drag animates neighbors and persists across reload', async ({ page, isMobile }, testInfo) => {
  await header(page, 0).click();
  await expect(rows(page).first().getByText('Conversation 1', { exact: true })).toBeVisible();
  await header(page, 0).click();
  const first = (await header(page, 0).boundingBox())!;
  const third = (await header(page, 2).boundingBox())!;
  const x = first.x + first.width / 2;
  const startY = first.y + first.height / 2;
  const endY = third.y + third.height / 2;
  const cdp = isMobile ? await page.context().newCDPSession(page) : null;
  if (cdp) {
    await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x, y: startY }] });
    await expect(page.locator('[data-project-drag-preview]')).toBeVisible();
    await cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x, y: endY }] });
  } else {
    await page.mouse.move(x, startY);
    await page.mouse.down();
    await page.mouse.move(x, endY, { steps: 12 });
  }
  await expect(page.locator('[data-project-drag-preview]')).toBeVisible();
  await expect.poll(() => rows(page).nth(1).evaluate((el) => getComputedStyle(el).transform)).not.toBe('none');
  expect(await rows(page).nth(1).evaluate((el) => getComputedStyle(el).transitionDuration)).toBe('0.22s');
  await page.screenshot({ path: testInfo.outputPath('project-drag.png'), scale: 'css' });
  if (cdp) await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
  else await page.mouse.up();
  await expect.poll(() => ids(page)).toEqual(['project-1', 'project-2', 'project-0', 'project-3', 'project-4', 'project-5', 'project-6', 'project-7']);
  await expect(page.locator('[data-project-drag-preview]')).toHaveCount(0);
  await expect(rows(page).nth(2).getByText('Conversation 1', { exact: true })).not.toBeVisible();
  await page.reload();
  await expect.poll(() => ids(page)).toEqual(['project-1', 'project-2', 'project-0', 'project-3', 'project-4', 'project-5', 'project-6', 'project-7']);
  await page.screenshot({ path: testInfo.outputPath('project-order.png'), scale: 'css' });
});

test('keyboard movement and cancellation use the existing header', async ({ page }) => {
  const original = await ids(page);
  await header(page, 0).focus();
  // The sensor installs document listeners on the next task after pickup.
  await page.keyboard.press('Space', { delay: 80 });
  await expect(page.locator('[data-project-drag-preview]')).toBeVisible();
  await page.keyboard.press('ArrowDown');
  await page.keyboard.press('Escape');
  await expect(page.locator('[data-project-drag-preview]')).toHaveCount(0);
  expect(await ids(page)).toEqual(original);
  await header(page, 0).focus();
  await page.keyboard.press('Space', { delay: 80 });
  await expect(page.locator('[data-project-drag-preview]')).toBeVisible();
  await page.keyboard.press('ArrowDown');
  await expect.poll(() => rows(page).nth(1).evaluate((el) => getComputedStyle(el).transform)).not.toBe('matrix(1, 0, 0, 1, 0, 0)');
  await page.keyboard.press('Space');
  await expect.poll(() => ids(page)).toEqual([original[1], original[0], ...original.slice(2)]);
});

test('an expanded project carries its sessions through a drag', async ({ page, isMobile }) => {
  await header(page, 0).click();
  await expect(rows(page).first().getByText('Conversation 1', { exact: true })).toBeVisible();
  const source = (await header(page, 0).boundingBox())!;
  const target = (await header(page, 1).boundingBox())!;
  const x = source.x + source.width / 2;
  const y = source.y + source.height / 2;
  const dest = target.y + target.height / 2;
  if (isMobile) {
    const cdp = await page.context().newCDPSession(page);
    await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x, y }] });
    await expect(page.locator('[data-project-drag-preview]')).toBeVisible();
    await cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x, y: dest }] });
    await expect.poll(() => rows(page).nth(1).evaluate((el) => getComputedStyle(el).transform)).not.toBe('none');
    await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
  } else {
    await page.mouse.move(x, y);
    await page.mouse.down();
    await page.mouse.move(x, dest, { steps: 15 });
    await page.mouse.up();
  }
  await expect.poll(async () => (await ids(page)).indexOf('project-0')).toBe(1);
  await expect(tree(page).locator('[data-project-id="project-0"]').getByText('Conversation 1', { exact: true })).toBeVisible();
});

test('a normal touch swipe scrolls without changing order', async ({ page, isMobile }) => {
  test.skip(!isMobile);
  await header(page, 0).click();
  await expect(rows(page).first().getByText('Conversation 1', { exact: true })).toBeVisible();
  const original = await ids(page);
  const box = (await header(page, 1).boundingBox())!;
  const x = box.x + box.width / 2;
  const y = box.y + box.height / 2;
  const cdp = await page.context().newCDPSession(page);
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x, y }] });
  for (let offset = 20; offset <= 180; offset += 20) {
    await cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x, y: y - offset }] });
  }
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
  await expect.poll(() => page.locator('#app-shell-scroll').evaluate((el) => el.scrollTop)).toBeGreaterThan(40);
  await expect(page.locator('[data-project-drag-preview]')).toHaveCount(0);
  expect(await ids(page)).toEqual(original);
});

test('touch cancellation abandons the move without expanding the project', async ({ page, isMobile }) => {
  test.skip(!isMobile);
  const original = await ids(page);
  const first = (await header(page, 0).boundingBox())!;
  const second = (await header(page, 1).boundingBox())!;
  const x = first.x + first.width / 2;
  const cdp = await page.context().newCDPSession(page);
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x, y: first.y + first.height / 2 }] });
  await expect(page.locator('[data-project-drag-preview]')).toBeVisible();
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x, y: second.y + second.height / 2 }] });
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchCancel', touchPoints: [] });
  await expect(page.locator('[data-project-drag-preview]')).toHaveCount(0);
  expect(await ids(page)).toEqual(original);
  await expect(rows(page).first().getByText('Conversation 1', { exact: true })).not.toBeVisible();
});

test('reduced motion disables pickup, keyboard movement, and neighbor animations', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.reload();
  await expect(rows(page)).toHaveCount(8);
  await header(page, 0).focus();
  await page.keyboard.press('Space', { delay: 80 });
  const preview = page.locator('[data-project-drag-preview]');
  await expect(preview).toBeVisible();
  await page.keyboard.press('ArrowDown');
  await expect.poll(() => rows(page).nth(1).evaluate((el) => getComputedStyle(el).transform)).toMatch(/-\d/);
  expect(await rows(page).nth(1).evaluate((el) => getComputedStyle(el).transitionDuration)).toBe('0s');
  expect(await preview.evaluate((el) => getComputedStyle(el).animationName)).toBe('none');
  expect(await preview.evaluate((el) => getComputedStyle(el.parentElement!).transitionDuration)).toBe('0s');
  await page.keyboard.press('Space');
  await expect.poll(() => ids(page)).toEqual(['project-1', 'project-0', 'project-2', 'project-3', 'project-4', 'project-5', 'project-6', 'project-7']);
});

test('another open page receives the saved order without reloading', async ({ page, context }) => {
  const other = await context.newPage();
  await other.goto('/e2e/project-order/fixture.html');
  await expect(rows(other)).toHaveCount(8);
  await header(page, 0).focus();
  await page.keyboard.press('Space', { delay: 80 });
  await expect(page.locator('[data-project-drag-preview]')).toBeVisible();
  await page.keyboard.press('ArrowDown');
  await expect.poll(() => rows(page).nth(1).evaluate((el) => getComputedStyle(el).transform)).toMatch(/-\d/);
  await page.keyboard.press('Space');
  await expect.poll(() => ids(other)).toEqual(['project-1', 'project-0', 'project-2', 'project-3', 'project-4', 'project-5', 'project-6', 'project-7']);
  await other.close();
});
