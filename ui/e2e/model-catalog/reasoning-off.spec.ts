import { expect, test, type Locator, type Page } from '@playwright/test';
import { hub } from '../support/copy';
import type { BackendModel, BackendModelsPut } from '../../src/components/settings/models/types';

const savedState = async (page: Page): Promise<{ saved: BackendModel[]; writes: BackendModelsPut[] }> =>
  JSON.parse(await page.getByTestId('saved').innerText());

for (const backend of ['claude', 'codex', 'opencode']) {
  for (const lang of ['en', 'zh'] as const) {
    test(`MH-MENU-COMPOSE-001: explicit model Off preserves old tiers (${backend}/${lang})`, async ({ page, isMobile }, testInfo) => {
      const text = (key: string, vars?: Record<string, string | number>) => hub(`gateway.${key}`, vars, lang);
      const activate = (locator: Locator) => isMobile ? locator.tap() : locator.click();
      const offLabel = lang === 'zh' ? '关闭' : 'Off';
      await page.route('**/api/**', (route) => route.abort());
      await page.goto(`/e2e/model-catalog/fixture.html?backend=${backend}&lang=${lang}&reasoning-off=1`);
      const initial = await savedState(page);

      await activate(page.getByRole('button', { name: 'Manage models', exact: true }));
      const edit = () => page.getByRole('button', { name: text('catalog.edit', { model: 'Existing model' }), exact: true });
      await activate(edit());
      await expect(page.getByRole('checkbox', { name: offLabel, exact: true })).not.toBeChecked();
      for (const effort of initial.saved[0].reasoning_efforts) {
        await expect(page.getByRole('checkbox', { name: effort, exact: true })).toBeChecked();
      }
      await activate(page.getByRole('button', { name: text('modelEditor.apply'), exact: true }));
      expect(await savedState(page)).toEqual(initial);

      await activate(edit());
      await activate(page.getByRole('checkbox', { name: offLabel, exact: true }));
      await expect(page.getByRole('checkbox', { name: offLabel, exact: true })).toBeChecked();
      await page.screenshot({ path: testInfo.outputPath('model-declared-off.png'), scale: 'css' });
      await activate(page.getByRole('button', { name: text('modelEditor.apply'), exact: true }));
      expect(await savedState(page)).toEqual(initial);
      await activate(page.getByRole('button', { name: text('catalog.save'), exact: true }));

      const result = await savedState(page);
      expect(result.writes).toHaveLength(1);
      expect(result.writes[0].baseline).toEqual(initial.saved);
      expect(result.saved).toEqual([{
        ...initial.saved[0],
        reasoning_efforts: [...initial.saved[0].reasoning_efforts, 'none'],
      }]);

      await activate(page.getByRole('button', { name: 'Manage models', exact: true }));
      await activate(edit());
      await expect(page.getByRole('checkbox', { name: offLabel, exact: true })).toBeChecked();
      await activate(page.getByRole('checkbox', { name: offLabel, exact: true }));
      await activate(page.getByRole('button', { name: text('modelEditor.cancel'), exact: true }));
      await activate(page.getByRole('button', { name: text('catalog.cancel'), exact: true }));
      expect(await savedState(page)).toEqual(result);
    });
  }
}
