import { expect, test, type Page } from '@playwright/test';
import en from '../../src/i18n/en.json' with { type: 'json' };

// A take whose network fails (ui/design: composer/hold-desktop/*): the realtime socket dies or is
// refused, the cloud token hangs, avibe.bot and the local relay cannot be reached. The fake
// microphone records real audio; `network.tsx` steers every request and logs where each segment
// went. Speech is never lost: the words land, or the take stays on the card to be retried.

type Net = {
  token: 'ok' | 'fail' | 'hang';
  socket: 'ok' | 'refuse' | 'kill-mid';
  cloud: 'ok' | 'down';
  relay: 'ok' | 'down';
  cloudFailOnce: number[];
};

declare global {
  interface Window {
    voiceNet: Net;
    voiceNetLog: {
      tokens: string[];
      sockets: string[];
      dictations: { path: 'cloud' | 'relay'; sequence: number; final: boolean; receipts: string[] }[];
      sent: string[];
    };
  }
}

const FINAL_TEXT = 'Every word, over HTTP';
const REALTIME_TEXT = 'Every word, live';

const open = async (page: Page, net: Partial<Net> = {}) => {
  await page.goto('/e2e/voice-shortcut/network.html');
  await page.evaluate((next) => Object.assign(window.voiceNet, next), net);
  const box = page.locator('[data-hold-to-talk="ready"]');
  await expect(box).toBeVisible();
  const rect = (await box.boundingBox())!;
  return { x: rect.x + rect.width * 0.4, y: rect.y + 22 };
};

/** A mouse hold on the composer for `ms`, then a release. */
const holdFor = async (page: Page, at: { x: number; y: number }, ms: number) => {
  await page.mouse.move(at.x, at.y);
  await page.mouse.down();
  await page.waitForTimeout(ms);
  await page.mouse.up();
};

const sent = (page: Page) => page.evaluate(() => window.voiceNetLog.sent);
const dictations = (page: Page) => page.evaluate(() => window.voiceNetLog.dictations);

test('a realtime socket that dies mid-take: the whole take still lands over HTTP', async ({ page }) => {
  const at = await open(page, { socket: 'kill-mid' });
  await holdFor(page, at, 4_000);

  await expect.poll(() => sent(page)).toEqual([FINAL_TEXT]);
  expect((await dictations(page)).at(-1)).toMatchObject({ path: 'cloud', final: true });
});

test('one failed minute does not void the take: the failed segment is sent again on its own', async ({ page }) => {
  // Five-second segments; segment 1 meets a transient service failure once.
  const at = await open(page, { socket: 'refuse', cloudFailOnce: [1] });
  await holdFor(page, at, 12_000);

  await expect.poll(() => sent(page), { timeout: 20_000 }).toEqual([FINAL_TEXT]);
  const requests = await dictations(page);
  expect(requests.filter((request) => !request.final).map((request) => request.sequence)).toEqual([0, 1]);
  expect(requests.at(-1)).toMatchObject({ final: true, receipts: ['receipt-0', 'receipt-1'] });
});

test('the token hangs and nothing is reachable: the take is kept, said honestly, and lands on retry', async ({ page }) => {
  test.setTimeout(90_000);
  const at = await open(page, { token: 'hang', cloud: 'down', relay: 'down' });
  await holdFor(page, at, 12_000);
  const releasedAt = Date.now();

  const card = page.getByRole('dialog');
  await expect(card.getByText(en.voice.cutOff)).toBeVisible({ timeout: 60_000 });
  console.log(`cut off ${Date.now() - releasedAt} ms after the release`);
  // Nothing came back, so the card says so: no "everything heard is above" over an empty card.
  await expect(card.getByText(en.voice.noText)).toBeVisible();
  await expect(card.getByText(en.voice.interrupted.droppedEmpty)).toBeVisible();
  await expect(card.getByText(/above/)).toHaveCount(0);
  expect(await sent(page)).toEqual([]);

  // The network comes back in the same document: the retry sends the kept audio and lands.
  await page.evaluate(() => Object.assign(window.voiceNet, { token: 'ok', cloud: 'ok', relay: 'ok' }));
  await card.getByRole('button', { name: en.voice.retry }).click();
  await expect.poll(() => sent(page)).toEqual([FINAL_TEXT]);
  const finalRequest = (await dictations(page)).at(-1)!;
  expect(finalRequest).toMatchObject({ final: true, receipts: ['receipt-0', 'receipt-1'] });

  // And the next take in this document works as on a fresh page.
  await holdFor(page, at, 2_000);
  await expect.poll(() => sent(page)).toEqual([FINAL_TEXT, REALTIME_TEXT]);
});

test('a refused realtime socket costs its token: the next take opens with a fresh one', async ({ page }) => {
  const at = await open(page, { socket: 'refuse' });
  await holdFor(page, at, 2_000);
  await expect.poll(() => sent(page)).toEqual([FINAL_TEXT]);

  await page.evaluate(() => { window.voiceNet.socket = 'ok'; });
  await holdFor(page, at, 2_000);
  await expect.poll(() => sent(page)).toEqual([FINAL_TEXT, REALTIME_TEXT]);
  const sockets = await page.evaluate(() => window.voiceNetLog.sockets);
  expect(sockets).toHaveLength(2);
  expect(sockets[1]).not.toBe(sockets[0]);
});
