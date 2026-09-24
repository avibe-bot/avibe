import { expect, test } from '@playwright/test';
import { hub } from '../support/copy';

for (const lang of ['en', 'zh'] as const) {
  for (const theme of ['light', 'dark']) {
    test(`source identity, badge hierarchy and privacy: ${lang} ${theme}`, async ({ page }, info) => {
      const text = (key: string) => hub(key, undefined, lang);
      await page.route('**/api/**', (route) => route.abort());
      await page.goto(`/e2e/model-provider/fixture.html?identity=1&lang=${lang}&theme=${theme}`);
      const cards = page.getByTestId('identity-cards');
      const detail = page.getByTestId('identity-detail');
      const pills = cards.locator('[data-source-id] .model-hub-pill');
      await expect(pills).toHaveText([
        text('upstream.kind.subscription'), 'OpenAI Responses',
        text('upstream.kind.subscription'), 'OpenAI Responses',
        text('upstream.kind.apiKey'), 'OpenAI Chat Completions',
      ]);
      await expect(cards.getByText('first@example.com', { exact: true })).toBeVisible();
      const accountCard = cards.locator('[data-source-id="src_identity001"]');
      const apiCard = cards.locator('[data-source-id="src_fixture001"]');
      await expect(accountCard).toHaveAccessibleName(/first@example\.com/);
      await expect(apiCard).toHaveAccessibleName(/API [kK]ey.*OpenAI Chat Completions.*relay\.example\/v1.*sk-…1234/);
      await expect(detail.getByRole('heading', { name: 'OpenAI', exact: true })).toBeVisible();
      await cards.screenshot({ path: info.outputPath('identity-visible.png') });
      // One header control covers every source; no per-row or per-field eyes.
      await expect(cards.getByRole('button', { name: text('upstream.hidePrivateDetails') })).toHaveCount(1);
      await expect(cards.locator('[data-source-id] button')).toHaveCount(0);
      await cards.getByRole('button', { name: text('upstream.hidePrivateDetails') }).click();
      await expect(cards.getByText(lang === 'zh' ? '已隐藏' : 'Hidden', { exact: true })).toHaveCount(3);
      await expect(cards.locator('[data-source-id] .lucide-eye-off[aria-hidden="true"]')).toHaveCount(3);
      await expect(accountCard).not.toHaveAccessibleName(/first@example\.com/);
      await expect(apiCard).not.toHaveAccessibleName(/relay\.example|sk-…1234/);
      await expect(apiCard).toHaveAccessibleName(/API [kK]ey.*OpenAI Chat Completions.*(?:Hidden|已隐藏)/);
      await expect(detail.getByRole('heading', { name: 'OpenAI', exact: true })).toBeVisible();
      await expect(page.getByText('first@example.com', { exact: true })).toHaveCount(0);
      await expect(page.locator('[title*="@example.com"]')).toHaveCount(0);
      expect(await cards.innerHTML()).not.toContain('relay.example');
      expect(await cards.innerHTML()).not.toContain('sk-…1234');
      await page.reload();
      await expect(page.getByText('first@example.com', { exact: true })).toHaveCount(0);
      await cards.getByRole('button', { name: /^OpenAI 2 / }).click();
      await expect(detail.getByRole('heading', { name: 'OpenAI 2', exact: true })).toBeVisible();
      const show = detail.getByRole('button', { name: text('upstream.showPrivateDetails') });
      await show.focus();
      await page.keyboard.press('Enter');
      await expect(cards.getByText('first@example.com', { exact: true })).toBeVisible();
      await expect(detail.getByText('long-account-name-for-overflow-check@example.com', { exact: true })).toBeVisible();
      await cards.getByRole('button', { name: text('upstream.hidePrivateDetails') }).click();
      await cards.getByRole('button', { name: /^Example relay / }).click();
      await expect(detail.getByRole('heading', { name: 'Example relay', exact: true })).toBeVisible();
      expect(await detail.innerHTML()).not.toContain('relay.example');
      expect(await detail.innerHTML()).not.toContain('sk-…1234');
      await detail.getByRole('button', { name: text('upstream.showPrivateDetails') }).click();
      await expect(detail.getByText('https://relay.example/v1', { exact: true })).toBeVisible();
      await expect(detail.getByText('sk-…1234', { exact: true })).toBeVisible();
      await cards.getByRole('button', { name: text('upstream.hidePrivateDetails') }).click();
      await cards.screenshot({ path: info.outputPath('identity-hidden.png') });
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    });
  }

  test('save, optional selected-model test and manual inventory: ' + lang, async ({ page }, info) => {
    const text = (key: string) => hub(key, undefined, lang);
    await page.route('**/api/**', (route) => route.abort());
    await page.goto('/e2e/model-provider/fixture.html?lang=' + lang);
    await expect(page.getByText('Example relay', { exact: true })).toBeVisible();
    await expect(page.getByText('high', { exact: true })).toHaveCount(0);
    await expect(page.locator('[data-tier-provenance]')).toHaveCount(0);
    await expect(page.getByRole('button', { name: /Advanced settings|高级设置/ })).toHaveCount(0);
    await page.getByRole('button', { name: 'Add provider fixture' }).click();
    await page.getByRole('textbox', { name: 'Base URL', exact: true }).fill('https://relay.example/v1');
    await page.getByLabel(text('addKey.field.apiKey'), { exact: true }).fill('fixture-placeholder');
    await expect(page.getByRole('button', { name: text('addKey.confirm'), exact: true })).toBeInViewport();
    await page.screenshot({ path: info.outputPath('save-dialog.png') });
    await page.getByRole('button', { name: text('addKey.confirm'), exact: true }).click();
    await expect(page.getByRole('dialog')).toHaveCount(0);
    await page.getByRole('button', { name: text('sourceTest.open'), exact: true }).click();
    await expect(page.getByRole('combobox')).toContainText('gpt-5.6-luna');
    await page.getByRole('combobox').click();
    await page.getByRole('option', { name: 'claude-sonnet-5', exact: true }).click();
    await page.getByRole('button', { name: text('sourceTest.run'), exact: true }).click();
    await expect(page.getByRole('status')).toContainText('claude-sonnet-5');
    await page.screenshot({ path: info.outputPath('test-result.png') });
    await page.getByRole('button', { name: lang === 'en' ? 'Close' : '关闭', exact: true }).first().click();
    await page.getByRole('button', { name: text('sourceDetail.action.addModel'), exact: true }).click();
    await page.getByPlaceholder(text('sourceDetail.col.id'), { exact: true }).fill('manual-model');
    await expect(page.locator('[data-manual-model-draft]').getByRole('textbox')).toHaveCount(1);
    await page.locator('.model-hub-source-detail').screenshot({ path: info.outputPath('manual-model.png') });
    await page.getByRole('button', { name: text('sourceDetail.action.addModel'), exact: true }).last().click();
    await expect(page.getByText('manual-model', { exact: true })).toBeVisible();
    expect(await page.getByTestId('calls').textContent()).not.toContain('refetch');
    await page.locator('.model-hub-source-detail').screenshot({ path: info.outputPath('inventory.png') });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  });

  test('unread Source reconciliation cannot publish a model verdict: ' + lang, async ({ page }, info) => {
    const text = (key: string) => hub(key, undefined, lang);
    await page.route('**/api/**', (route) => route.abort());
    await page.goto('/e2e/model-provider/fixture.html?reconcile=failed&lang=' + lang);
    await page.getByRole('button', { name: text('sourceTest.open'), exact: true }).click();
    await page.getByRole('button', { name: text('sourceTest.run'), exact: true }).click();
    await expect(page.getByRole('alert')).toHaveText(text('sourceTest.requestFailed'));
    await expect(page.getByRole('status')).toHaveCount(0);
    await page.screenshot({ path: info.outputPath('unconfirmed-result.png') });
    expect(await page.getByTestId('calls').textContent()).toBe('["test:src_fixture001/gpt-5.6-luna"]');
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  });
}
