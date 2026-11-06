import { expect, test, type Page, type Route } from '@playwright/test';
import { copy } from '../support/copy';

// Owner report (2026-11-06): the Inbox kept a "Load more" with nothing more to
// show, and clicking it did nothing. Against the production provider and page,
// over a routed `GET /api/inbox` that pages the way the server does: the button
// belongs to the view the reader is on, and a click always shows something or
// takes the button away.

type Row = { session_id: string; last_activity_at: string };
const minute = (n: number) => new Date(Date.UTC(2026, 10, 6, 8, 0) - n * 60_000).toISOString();
const row = (n: number) => ({
  session_id: `ses_${String(n).padStart(3, '0')}`, scope_id: null, project_id: 'proj', project_name: 'Regression',
  title: `Conversation ${n}`, last_activity_at: minute(n), last_message_author: 'agent', replied: false,
  preview_text: `Reply ${n}`, preview_at: minute(n), unread_count: 0, unread: false,
});
const cursorOf = (r: Row) => `${r.last_activity_at}|${r.session_id}`;

/** The feed: `count` conversations, newest first, unread where `unread` says. */
const serve = async (page: Page, count: number, unread: number[]) => {
  const rows = Array.from({ length: count }, (_, i) => row(i + 1));
  const unreadBySession = Object.fromEntries(unread.map((n) => [row(n).session_id, 1]));
  const pages: string[] = [];
  const held: Array<() => void> = [];
  const control = {
    pages, hold: false, held: () => held.length,
    release: () => { control.hold = false; for (const go of held.splice(0)) go(); },
  };
  await page.route('**/api/**', (route) => route.fulfill({ status: 503, json: { error: 'No backend in this fixture' } }));
  await page.route('**/api/inbox?*', async (route: Route) => {
    if (control.hold) await new Promise<void>((resolve) => held.push(resolve));
    const url = new URL(route.request().url());
    const limit = Number(url.searchParams.get('limit') ?? 30);
    const before = url.searchParams.get('before');
    const only = url.searchParams.get('session');
    if (before) pages.push(before);
    const matching = rows.filter((r) => (only ? r.session_id === only : !before || cursorOf(r) < before));
    const window = matching.slice(0, limit);
    return route.fulfill({ json: {
      sessions: window,
      next_cursor: matching.length > limit ? cursorOf(window[window.length - 1]) : null,
      unread_by_session: unreadBySession,
      unread_total: unread.length, unread_sessions: unread.length,
      unread_by_category: { bot: 0, session: unread.length }, badge_total: unread.length,
    } });
  });
  return control;
};
const open = async (page: Page, tab: 'unread' | 'all') => {
  await page.addInitScript((value) => {
    localStorage.setItem('vibe-remote:inbox-filter', JSON.stringify({ tab: value, leftAt: 0 }));
  }, tab);
  await page.goto('/e2e/inbox-return/load-more.html#/inbox');
};
const cards = (page: Page) => page.locator('[data-inbox-session-id]');
const loadMore = (page: Page) => page.getByRole('button', { name: copy('workbench.inbox.loadMore'), exact: true });

test('the Unread tab offers no "Load more" once every unread conversation shows', async ({ page }, testInfo) => {
  const { pages } = await serve(page, 70, [1, 3]);
  await open(page, 'unread');
  await expect(cards(page)).toHaveCount(2);
  await page.screenshot({ path: testInfo.outputPath('unread-all-shown.png'), scale: 'css' });
  await expect(loadMore(page)).toHaveCount(0);
  expect(pages).toEqual([]);
});

test('one click on the Unread tab reaches the next unread conversation further down', async ({ page }) => {
  const { pages } = await serve(page, 100, [2, 65]);
  await open(page, 'unread');
  await expect(cards(page)).toHaveCount(1);
  await loadMore(page).click();
  await expect(page.locator('[data-inbox-session-id="ses_065"]')).toBeVisible();
  await expect(cards(page)).toHaveCount(2);
  await expect(loadMore(page)).toHaveCount(0);
  expect(pages).toHaveLength(2);
});

test('the All tab loads the next page and drops the button at the end of the feed', async ({ page }) => {
  const { pages } = await serve(page, 31, []);
  await open(page, 'all');
  await expect(cards(page)).toHaveCount(30);
  await loadMore(page).click();
  await expect(cards(page)).toHaveCount(31);
  await expect(loadMore(page)).toHaveCount(0);
  expect(pages).toHaveLength(1);
});

test('a click while the feed is being refreshed is answered at once, and its page follows', async ({ page }) => {
  const server = await serve(page, 45, []);
  await open(page, 'all');
  await expect(cards(page)).toHaveCount(30);
  // A refresh within the read cache's 1.5 s would be answered from it, never reaching the server.
  await page.waitForTimeout(1600);
  server.hold = true;
  await page.getByRole('button', { name: copy('workbench.inbox.refresh') }).first().click();
  await expect.poll(() => server.held()).toBe(1);
  await loadMore(page).click();
  await expect(loadMore(page)).toHaveCount(0);
  // The skeleton card stands in for the button while the page is on its way.
  await expect(page.locator('[data-flip-id="more"] [role="status"]').first()).toBeVisible();
  server.release();
  await expect(cards(page)).toHaveCount(45);
  await expect(page.locator('[data-flip-id="more"]')).toHaveCount(0);
});
