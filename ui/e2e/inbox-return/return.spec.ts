import { expect, test, type Page } from '@playwright/test';
import { copy } from '../support/copy';

const row = (page: Page, id: number) => page.locator(`[data-inbox-session-id="session-${id}"]`);
const rows = (page: Page) => page.locator('[data-inbox-session-id]');
const position = (page: Page) => page.evaluate(() => {
  const shell = document.getElementById('app-shell-scroll')!;
  const owner = getComputedStyle(shell).overflowY === 'visible' ? document.scrollingElement! : shell;
  return { top: owner.scrollTop, max: owner.scrollHeight - owner.clientHeight };
});
const scrollTo = (page: Page, top: number) => page.evaluate((value) => {
  const shell = document.getElementById('app-shell-scroll')!;
  const owner = getComputedStyle(shell).overflowY === 'visible' ? document.scrollingElement! : shell;
  owner.scrollTop = value;
}, top);
const alignTop = (page: Page, id: number) => row(page, id).evaluate((el) => {
  const shell = document.getElementById('app-shell-scroll')!;
  const documentScrolls = getComputedStyle(shell).overflowY === 'visible';
  const owner = documentScrolls ? document.scrollingElement! : shell;
  owner.scrollTop += el.getBoundingClientRect().top - (documentScrolls ? 0 : shell.getBoundingClientRect().top);
});
const open = async (page: Page, query = '') => {
  await page.route('**/api/**', (route) => route.fulfill({ status: 503, json: { error: 'No backend in this fixture' } }));
  await page.goto(`/e2e/inbox-return/fixture.html${query}#/inbox`);
  await expect(rows(page)).toHaveCount(60);
};
const enter = async (page: Page, id: number) => {
  await row(page, id).getByRole('button', { name: copy('workbench.inbox.openSession') }).click();
  await expect(page.getByTestId('chat-detail')).toBeVisible();
};

test('returns to the same reading position through browser history and the Back button', async ({ page }, testInfo) => {
  await open(page);
  await page.getByRole('button', { name: copy('workbench.inbox.filterAll'), exact: true }).click();
  for (const browserBack of [true, false, true]) {
    await row(page, 50).scrollIntoViewIfNeeded();
    const before = await position(page);
    const offset = await row(page, 50).evaluate((el) => el.getBoundingClientRect().top);
    await enter(page, 50);
    if (browserBack) await page.goBack();
    else await page.getByRole('button', { name: 'Back', exact: true }).click();
    await expect(rows(page)).toHaveCount(60);
    await expect.poll(async () => Math.abs((await position(page)).top - before.top)).toBeLessThan(1);
    expect(Math.abs(await row(page, 50).evaluate((el) => el.getBoundingClientRect().top) - offset)).toBeLessThan(1);
  }
  await page.screenshot({ path: testInfo.outputPath('inbox-return.png'), scale: 'css' });
});

test('keeps bottom-up unread triage near the remaining bottom after each read', async ({ page }) => {
  await open(page);
  for (const id of [60, 59, 58]) {
    await scrollTo(page, (await position(page)).max);
    await enter(page, id);
    await page.goBack();
    await expect(row(page, id)).toHaveCount(0);
    await expect(rows(page)).toHaveCount(id - 1);
    await expect.poll(async () => {
      const p = await position(page);
      return Math.abs(p.max - p.top);
    }).toBeLessThan(1);
    expect((await position(page)).top).toBeGreaterThan(1000);
  }
});

test('restores a surviving visible row when the opened unread conversation disappears', async ({ page }) => {
  await open(page);
  await alignTop(page, 40);
  const before = await row(page, 41).evaluate((el) => el.getBoundingClientRect().top);
  await enter(page, 40);
  await page.goBack();
  await expect(row(page, 40)).toHaveCount(0);
  await expect.poll(async () => Math.abs(await row(page, 41).evaluate((el) => el.getBoundingClientRect().top) - before)).toBeLessThan(1);
});

test('preserves the read neighborhood across new activity and loaded additional pages', async ({ page }) => {
  await open(page);
  await page.getByRole('button', { name: copy('workbench.inbox.loadMore'), exact: true }).click();
  await expect(rows(page)).toHaveCount(90);
  await page.getByRole('button', { name: copy('workbench.inbox.filterAll'), exact: true }).click();
  await row(page, 80).scrollIntoViewIfNeeded();
  const before = await row(page, 80).evaluate((el) => el.getBoundingClientRect().top);
  await enter(page, 80);
  await page.getByRole('button', { name: 'New activity', exact: true }).click();
  await page.goBack();
  await expect(rows(page)).toHaveCount(91);
  await expect.poll(async () => Math.abs(await row(page, 80).evaluate((el) => el.getBoundingClientRect().top) - before)).toBeLessThan(1);
});

test('keeps a late mark-read update anchored until the user resumes scrolling', async ({ page }) => {
  await open(page, '?delay=800');
  await alignTop(page, 40);
  const before = await row(page, 41).evaluate((el) => el.getBoundingClientRect().top);
  await enter(page, 40);
  await page.goBack();
  await expect(row(page, 40)).toHaveCount(1);
  await expect(row(page, 40)).toHaveCount(0);
  await expect.poll(async () => Math.abs(await row(page, 41).evaluate((el) => el.getBoundingClientRect().top) - before)).toBeLessThan(1);
  await row(page, 1).evaluate((el) => { el.style.minHeight = '500px'; });
  await expect.poll(async () => Math.abs(await row(page, 41).evaluate((el) => el.getBoundingClientRect().top) - before)).toBeLessThan(1);
  await page.locator('#app-shell-scroll').dispatchEvent('wheel', { deltaY: -1 });
  await scrollTo(page, 900);
  await row(page, 2).evaluate((el) => { el.style.minHeight = '500px'; });
  // Native browser anchoring may move the offset after a resize; it must not
  // be forced back into the old reading neighborhood near conversation 40.
  await page.waitForTimeout(150);
  expect((await position(page)).top).toBeLessThan(2000);
});

test('a fresh Inbox navigation does not inherit an older history entry position', async ({ page }) => {
  await open(page);
  await row(page, 40).scrollIntoViewIfNeeded();
  await enter(page, 40);
  await page.getByRole('button', { name: 'New Inbox entry', exact: true }).click();
  await expect(rows(page)).toHaveCount(59);
  expect((await position(page)).top).toBe(0);
});

test('does not reuse an old Chat position when a later Search visit returns', async ({ page, isMobile }) => {
  await open(page);
  await page.getByRole('button', { name: copy('workbench.inbox.filterAll'), exact: true }).click();
  await row(page, 50).scrollIntoViewIfNeeded();
  const before = await position(page);
  await enter(page, 50);
  await page.goBack();
  await expect.poll(async () => Math.abs((await position(page)).top - before.top)).toBeLessThan(1);
  await page.locator('#app-shell-scroll').dispatchEvent('wheel', { deltaY: -1 });
  await scrollTo(page, 0);
  if (isMobile) await page.getByText(copy('workbench.search.entry'), { exact: true }).click();
  else await page.evaluate(() => { window.location.hash = '/search'; });
  await expect(page.getByTestId('search-detail')).toBeVisible();
  await page.goBack();
  await expect(rows(page)).toHaveCount(60);
  await expect.poll(async () => (await position(page)).top).toBe(0);
  await row(page, 1).evaluate((el) => { el.style.minHeight = '500px'; });
  await page.waitForTimeout(150);
  expect((await position(page)).top).toBe(0);
});
