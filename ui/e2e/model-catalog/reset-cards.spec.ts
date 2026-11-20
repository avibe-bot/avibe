import { mkdirSync } from 'node:fs';
import { join } from 'node:path';

import { expect, test, type Locator, type Page } from '@playwright/test';
import { hub, type CopyLocale } from '../support/copy';

// ui/design: models/quota-reset-cards/* in the built Models page, every vendor answer
// stubbed at `modelsApi` (resetCardsFixture.tsx). Each state is reached the way a user
// reaches it, in zh and en, light and dark, at desktop and phone width. The layout
// contracts: a card's expiry never breaks inside its date (the confirm row at phone
// width included), at phone width the status chip goes under the account name so the
// refresh does not truncate it, and nothing overflows the page.
//
// RESET_CARDS_SHOT_DIR saves each state's panel there; otherwise into the test output.

const STATES = ['none', 'refreshing', 'one-kind', 'both-kinds', 'confirm', 'using', 'used', 'not-used', 'unconfirmed', 'not-usable'] as const;
type State = (typeof STATES)[number];

const lineCount = (locator: Locator) => locator.evaluate((element) => {
  const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
  const tops: number[] = [];
  while (walker.nextNode()) {
    const range = document.createRange();
    range.selectNodeContents(walker.currentNode);
    for (const rect of range.getClientRects()) {
      if (rect.width && rect.height && !tops.some((top) => Math.abs(top - rect.top) <= 1)) tops.push(rect.top);
    }
  }
  return tops.length;
});

const reach = async (page: Page, state: State, lang: CopyLocale) => {
  const claude = page.getByRole('article', { name: 'Claude Max' });
  const useFable = claude.getByRole('button', { name: hub('quota.resetCards.useNamed', { name: hub('quota.resetCards.kind.fable', {}, lang) }, lang), exact: true });
  if (state === 'refreshing') {
    await claude.getByRole('button', { name: hub('quota.refreshThisAccount', {}, lang), exact: true }).click();
    await expect(claude.locator('.model-hub-quota-account-refresh svg')).toHaveClass(/animate-spin/);
    return;
  }
  if (!['confirm', 'using', 'used', 'not-used', 'unconfirmed'].includes(state)) return;
  await useFable.click();
  await expect(claude.getByRole('status')).toHaveText(hub('quota.resetCards.confirmNote', {}, lang));
  if (state === 'confirm') return;
  await claude.getByRole('button', { name: hub('quota.resetCards.confirm', {}, lang), exact: true }).click();
  const said = {
    using: () => expect(claude.getByRole('button', { name: hub('quota.resetCards.using', {}, lang) })).toBeDisabled(),
    used: () => expect(claude.getByRole('status')).toHaveText(hub('quota.resetCards.usedDone', { done: hub('quota.resetCards.done.fable', {}, lang) }, lang)),
    'not-used': () => expect(claude.getByRole('alert')).toHaveText(hub('quota.resetCards.notUsed.cooldown', {}, lang)),
    unconfirmed: () => expect(claude.getByRole('alert')).toHaveText(hub('quota.resetCards.unconfirmed', {}, lang)),
  } as const;
  await said[state as keyof typeof said]();
};

for (const lang of ['zh', 'en'] as const) {
  for (const theme of ['light', 'dark'] as const) {
    for (const state of STATES) {
      test(`MH-QUOTA-078: ${state} holds its layout (${lang}, ${theme})`, async ({ page }, testInfo) => {
        const phone = testInfo.project.name === 'mobile';
        await page.goto(`/e2e/model-catalog/fixture.html?view=reset-cards&state=${state}&lang=${lang}&theme=${theme}`);
        await page.getByRole('tab', { name: hub('shell.tab.quota', {}, lang), exact: true }).click();
        const grid = page.locator('.model-hub-quota-grid');
        await expect(grid.getByRole('article')).toHaveCount(2);
        await reach(page, state, lang);

        const cards = grid.getByRole('region', { name: hub('quota.resetCards.title', {}, lang), exact: true });
        await expect(cards).toHaveCount(state === 'none' ? 0 : 2);
        for (const expiry of await page.locator('[data-reset-card-expiry]').all()) expect(await lineCount(expiry)).toBe(1);
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
        for (const article of await grid.getByRole('article').all()) {
          const name = article.locator('.model-hub-quota-account > span').first();
          const status = article.locator('.model-hub-quota-status');
          const [nameBox, statusBox] = [(await name.boundingBox())!, (await status.boundingBox())!];
          if (phone) {
            expect(statusBox.y).toBeGreaterThanOrEqual(nameBox.y + nameBox.height - 1);
            expect(await name.evaluate((element) => element.scrollWidth <= element.clientWidth)).toBe(true);
          }
        }

        // Tall enough to hold the whole panel unscrolled; the width, which decides the layout, stays.
        await page.setViewportSize({ width: page.viewportSize()!.width, height: 3200 });
        const dir = process.env.RESET_CARDS_SHOT_DIR;
        if (dir) mkdirSync(dir, { recursive: true });
        await grid.screenshot({
          path: dir ? join(dir, `${state}.${lang}.${theme}.${phone ? 'phone' : 'desktop'}.png`) : testInfo.outputPath(`${state}.png`),
          animations: 'disabled',
        });
      });
    }
  }
}
