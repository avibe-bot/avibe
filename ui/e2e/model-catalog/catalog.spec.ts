import { expect, test, type Locator, type Page } from '@playwright/test';
import { hub } from '../support/copy';
import type { BackendModel, BackendModelsPut } from '../../src/components/settings/models/types';

const savedState = async (page: Page): Promise<{ saved: BackendModel[]; writes: BackendModelsPut[] }> =>
  JSON.parse(await page.getByTestId('saved').innerText());

for (const backend of ['claude', 'codex', 'opencode']) {
  for (const lang of ['en', 'zh'] as const) {
    test.describe(`${backend}/${lang}`, () => {
      const text = (key: string, vars?: Record<string, string | number>) => hub(`gateway.${key}`, vars, lang);
      const grip = (page: Page, model: string) => page.getByRole('button', { name: text('catalog.reorder', { model }), exact: true });
      test.beforeEach(async ({ page }) => {
        await page.route('**/api/**', (route) => route.abort());
        await page.goto(`/e2e/model-catalog/fixture.html?backend=${backend}&lang=${lang}`);
        await page.getByRole('button', { name: 'Manage models', exact: true }).click();
        await expect(grip(page, 'Existing model')).toBeVisible();
      });

      test('keeps picker additions until the catalog is saved', async ({ page, isMobile }, testInfo) => {
        const activate = (locator: Locator) => isMobile ? locator.tap() : locator.click();
        const initial = await savedState(page);
        await activate(page.getByRole('button', { name: text('catalog.add'), exact: true }));
        await activate(page.getByRole('checkbox', { name: /claude-candidate-alpha/ }));
        await activate(page.getByRole('checkbox', { name: /claude-candidate-beta/ }));
        await activate(page.getByRole('button', { name: text('picker.confirm', { count: 2 }), exact: true }));

        await expect(page.getByRole('dialog')).toHaveCount(1);
        await expect(grip(page, 'claude-candidate-alpha')).toBeVisible();
        await expect(grip(page, 'claude-candidate-beta')).toBeVisible();
        expect(await savedState(page)).toEqual(initial);
        await page.screenshot({ path: testInfo.outputPath('added-draft.png'), scale: 'css' });
        await activate(page.getByRole('button', { name: text('catalog.save'), exact: true }));
        await expect(page.getByRole('dialog')).toHaveCount(0);
        const result = await savedState(page);
        expect(result.saved.map((model) => model.id)).toEqual(['claude-existing-model', 'claude-candidate-alpha', 'claude-candidate-beta']);
        expect(result.saved[0]).toEqual(initial.saved[0]);
        expect(result.writes).toHaveLength(1);
        expect(result.writes[0].baseline).toEqual(initial.saved);
        expect(result.writes[0].expected_suppliers).toEqual({ 'claude-candidate-alpha': [], 'claude-candidate-beta': [] });
        await activate(page.getByRole('button', { name: 'Manage models', exact: true }));
        await expect(grip(page, 'claude-candidate-beta')).toBeVisible();
      });

      test('dismisses only the picker or editor and preserves the unsaved draft', async ({ page, isMobile }) => {
        const activate = (locator: Locator) => isMobile ? locator.tap() : locator.click();
        const initial = await savedState(page);
        await activate(page.getByRole('button', { name: text('catalog.add'), exact: true }));
        await activate(page.getByRole('checkbox', { name: /claude-candidate-alpha/ }));
        await activate(page.getByRole('button', { name: text('picker.confirm', { count: 1 }), exact: true }));
        await expect(grip(page, 'claude-candidate-alpha')).toBeVisible();

        for (const exit of ['cancel', 'escape', 'outside'] as const) {
          await activate(page.getByRole('button', { name: text('catalog.add'), exact: true }));
          await expect(page.getByRole('checkbox', { name: /claude-candidate-beta/ })).toBeVisible();
          // Raw Escape/coordinate input has no locator actionability wait.
          await page.getByRole('dialog').filter({ has: page.getByRole('checkbox', { name: /claude-candidate-beta/ }) })
            .evaluate(async (node) => { await Promise.all(node.getAnimations().map((animation) => animation.finished)); });
          if (exit === 'cancel') await activate(page.getByRole('button', { name: text('catalog.cancel'), exact: true }));
          else if (exit === 'escape') await page.keyboard.press('Escape');
          else if (isMobile) await page.touchscreen.tap(5, 5);
          else await page.mouse.click(5, 5);
          await expect(grip(page, 'claude-candidate-alpha')).toBeVisible();
          await expect(page.getByRole('dialog')).toHaveCount(1);
        }

        await activate(page.getByRole('button', { name: text('catalog.edit', { model: 'Existing model' }), exact: true }));
        await activate(page.getByRole('button', { name: text('modelEditor.cancel'), exact: true }));
        await expect(grip(page, 'claude-candidate-alpha')).toBeVisible();
        await activate(page.getByRole('button', { name: text('catalog.edit', { model: 'Existing model' }), exact: true }));
        await page.getByLabel(text('modelEditor.displayName.label'), { exact: true }).fill('Edited name');
        await activate(page.getByRole('button', { name: text('modelEditor.apply'), exact: true }));
        await expect(grip(page, 'Edited name')).toBeVisible();
        await expect(grip(page, 'claude-candidate-alpha')).toBeVisible();
        expect(await savedState(page)).toEqual(initial);
        await activate(page.getByRole('button', { name: text('catalog.cancel'), exact: true }));
        await expect(page.getByRole('dialog')).toHaveCount(0);
        expect(await savedState(page)).toEqual(initial);
      });

      test('hands the picker to the custom editor without dismissing the catalog', async ({ page, isMobile }) => {
        const activate = (locator: Locator) => isMobile ? locator.tap() : locator.click();
        const initial = await savedState(page);
        await activate(page.getByRole('button', { name: text('catalog.add'), exact: true }));
        await activate(page.getByRole('button', { name: text('picker.custom'), exact: true }));
        await page.getByLabel(text('modelEditor.id.label'), { exact: true }).fill('claude-custom-model');
        await activate(page.getByRole('button', { name: text('modelEditor.add'), exact: true }));
        await expect(grip(page, 'claude-custom-model')).toBeVisible();
        expect(await savedState(page)).toEqual(initial);
        await activate(page.getByRole('button', { name: text('catalog.save'), exact: true }));
        await expect(page.getByRole('dialog')).toHaveCount(0);
        const result = await savedState(page);
        expect(result.saved.map((model) => model.id)).toEqual(['claude-existing-model', 'claude-custom-model']);
        expect(result.writes).toHaveLength(1);
        expect(result.writes[0].expected_suppliers).toBeUndefined();
      });
    });
  }
}
