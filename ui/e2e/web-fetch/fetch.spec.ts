import { test, expect } from '@playwright/test';
import { mkdir } from 'node:fs/promises';
import path from 'node:path';

const shots = process.env.WF2V_SCREENSHOT_DIR;
test.beforeEach(async ({ page }) => {
  await page.route('**/*', (route) => new URL(route.request().url()).hostname === '127.0.0.1' ? route.continue() : route.abort());
});

test('Chinese Tools card: real projection, order writes, built-in details and add badges', async ({ page }) => {
  await page.goto('/e2e/web-fetch/fixture.html');
  const card = page.locator('[data-tool="web_fetch"]');
  await expect(card).toContainText('最近一次由 本机');
  await expect(card.locator('[data-executor]')).toHaveCount(4);
  if (shots) {
    await mkdir(shots, { recursive: true });
    await card.screenshot({ path: path.join(shots, 'tools-zh.png') });
    await page.screenshot({ path: path.join(shots, 'tools-page-zh.png'), fullPage: true });
  }
  await card.getByRole('button', { name: '上移 Jina Reader（免费）' }).click();
  await expect(page.locator('[data-writes]')).toContainText('"executor_order":["src_localfetch","src_tavily01","src_jinakeyless","src_exakeyless"]');
  await card.getByRole('button', { name: '本机', exact: true }).click();
  await expect(page.getByRole('dialog')).toContainText('不读取用户 Cookie');
  await page.keyboard.press('Escape');
  await card.getByRole('button', { name: '添加读取服务' }).click();
  await expect(page.getByRole('dialog').getByText('读取', { exact: true }).first()).toBeVisible();
  if (shots) await page.screenshot({ path: path.join(shots, 'add-services-zh.png'), fullPage: true });
});

test('Chinese chat and IM cards are real projections and page quotes stay inert', async ({ page }) => {
  await page.goto('/e2e/web-fetch/fixture.html?view=chat');
  await page.locator('[data-hosted-call="local_truncated"]').getByRole('button').click();
  await expect(page.locator('[data-hosted-fetch]')).toContainText('正文超过单次读取上限');
  await expect(page.locator('[data-hosted-policy="policy"]')).toContainText('未转发第三方');
  await expect(page.locator('[data-im="jina_fallback"]')).toContainText('Jina Reader（免费）');
  await expect(page.locator('img')).toHaveCount(0);
  if (shots) { await mkdir(shots, { recursive: true }); await page.screenshot({ path: path.join(shots, 'chat-im-zh.png'), fullPage: true }); }
});

test('flag-off read exposes no active fetch UI', async ({ page }) => {
  await page.goto('/e2e/web-fetch/fixture.html?off');
  await expect(page.locator('[data-tool="web_fetch"]')).toHaveCount(0);
  await expect(page.getByRole('button', { name: '添加读取服务' })).toHaveCount(0);
});
