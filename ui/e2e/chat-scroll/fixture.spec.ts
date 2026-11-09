import { expect, test, type Page } from '../support/browserTest';

// The fixture (fixture.tsx) behaves as a server would: what it reports when the page asks agrees with the events
// it sends. The page reconciles a turn it believes running against `/turn-state` on an interval (every 10 s
// while the event stream is not known to be connected, which the fixture's never is), so a fixture that sent
// `turn.start` but still reported an idle turn would end the turn under any spec that runs past a
// reconciliation.

const transcript = (page: Page) => page.getByTestId('chat-transcript');
const card = (page: Page) => page.locator('[data-transcript-key="activity-live"]');

test('a turn the fixture starts stays running through the page\'s reconciliation, and ends when the fixture ends it', async ({ page }) => {
  // No request from this page may reach a running Avibe service.
  await page.route('**/api/**', (route) => route.fulfill({ status: 503, json: { error: 'No backend in this fixture' } }));
  await page.goto('/e2e/chat-scroll/fixture.html?images=0&delay=0&activity=1');
  await expect(transcript(page).locator('[data-message-id]').first()).toBeVisible();
  await page.evaluate(() => window.__scrollLab.turn('start'));
  await page.evaluate(() => window.__scrollLab.arrive({ author: 'agent', type: 'tool_call', source: 'agent', author_id: null, text: 'Read ui/src/lib/motion.ts' }));
  await expect(card(page)).toBeVisible();
  const readsAtStart = await page.evaluate(() => window.__scrollLab.turnStateReads());
  // At least one reconciliation reads the turn after it started (the fixture counts what the page asks it): the
  // turn is still running after it.
  await expect.poll(() => page.evaluate(() => window.__scrollLab.turnStateReads()), { timeout: 15_000, intervals: [250] }).toBeGreaterThan(readsAtStart);
  await page.waitForTimeout(500);
  await expect(card(page)).toBeVisible();
  await expect(page.locator('[data-transcript-key="activity-live"] button[aria-expanded]').first()).toBeVisible();
  // The fixture ends it: it ends.
  await page.evaluate(() => window.__scrollLab.turn('end'));
  await expect(card(page)).toHaveCount(0);
});
