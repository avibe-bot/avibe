import { expect, test } from '@playwright/test';
import { hub } from '../support/copy';

for (const lang of ['en', 'zh'] as const) {
  test('save, optional selected-model test and manual inventory: ' + lang, async ({ page }, info) => {
    const text = (key: string) => hub(key, undefined, lang);
    await page.route('**/api/**', (route) => route.abort());
    await page.goto('/e2e/model-provider/fixture.html?lang=' + lang);
    await expect(page.getByText('Example relay', { exact: true })).toBeVisible();
    await expect(page.getByText('high', { exact: true })).toHaveCount(0);
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
    await page.getByRole('button', { name: text('sourceDetail.action.addModel'), exact: true }).last().click();
    await expect(page.getByText('manual-model', { exact: true })).toBeVisible();
    expect(await page.getByTestId('calls').textContent()).not.toContain('refetch');
    await page.screenshot({ path: info.outputPath('inventory.png') });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  });
}
