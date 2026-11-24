import { mkdir } from 'node:fs/promises';
import { join } from 'node:path';
import { expect, test, type Locator } from '../support/browserTest';
import en from '../../src/i18n/en.json' with { type: 'json' };
import { open, serveProduct } from './support';

// The card skeleton travels with the Inbox route. A pending read after that
// route is loaded must not need another script, even when the browser is offline.
// API responses are synthetic; no live instance or browser profile is used.
async function capture(region: Locator, name: string) {
  const directory = process.env.INBOX_SKELETON_SHOTS;
  if (!directory) return;
  await mkdir(directory, { recursive: true });
  await region.screenshot({ path: join(directory, `${name}.png`), animations: 'disabled' });
}

test('loaded Inbox can first show its loading cards offline without another script', async ({ page, context }) => {
  const denied = await serveProduct(page);
  await page.addInitScript(() => {
    window.requestIdleCallback = () => 0;
    window.cancelIdleCallback = () => undefined;
  });
  await page.emulateMedia({ reducedMotion: 'reduce' });
  const errors: string[] = [];
  const scripts: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  page.on('request', (request) => {
    if (request.resourceType() === 'script') scripts.push(request.url());
  });
  let hold = false;
  let release!: () => void;
  const pending = new Promise<void>((resolve) => { release = resolve; });
  await page.route('**/api/inbox?**', async (route) => {
    if (hold) await pending;
    await route.fulfill({ status: 503, json: { error: 'Synthetic unavailable feed' } });
  });
  await open(page, '/inbox', { theme: 'light' });
  const failed = page.getByRole('alert').filter({ hasText: en.workbench.inbox.loadError });
  await expect(failed).toBeVisible();
  const loadedScripts = [...scripts];
  expect(loadedScripts.some((url) => /\/InboxPage(?:-[^/]+\.js|\.tsx)(?:\?|$)/.test(url))).toBe(true);
  hold = true;
  await context.setOffline(true);
  try {
    await failed.getByRole('button', { name: en.common.retry }).click();
    const cards = page.locator('main [role="status"][aria-busy="true"]').filter({
      hasText: en.common.loading,
    });
    await expect(cards).toBeVisible();
    await expect(cards.locator('.skeleton-shimmer')).toHaveCount(3);
    await capture(cards, 'inbox-offline-loading');
    expect(scripts).toEqual(loadedScripts);
    expect(errors).toEqual([]);
    expect(denied).toEqual([]);
  } finally {
    release();
    await context.setOffline(false);
  }
});

test('home sidebar keeps its synchronous first-read skeleton', async ({ page }) => {
  const denied = await serveProduct(page);
  await page.emulateMedia({ reducedMotion: 'reduce' });
  let release!: () => void;
  const pending = new Promise<void>((resolve) => { release = resolve; });
  await page.route('**/api/workbench/projects-bootstrap**', async (route) => {
    await pending;
    await route.fulfill({ json: { projects: [], sessions: {} } });
  });
  await page.route('**/api/projects**', async (route) => {
    await pending;
    await route.fulfill({ json: { projects: [], sessions: {} } });
  });
  try {
    await open(page, '/', { theme: 'light' });
    const skeleton = page.getByRole('status', { name: en.workbench.projectsLoading });
    await expect(skeleton).toBeVisible();
    await capture(skeleton, 'home-sidebar-loading');
    expect(denied).toEqual([]);
  } finally {
    release();
  }
});
