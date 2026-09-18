import { expect, test, type Locator, type Page } from '@playwright/test';
import { hub } from '../support/copy';

const text = (key: string, vars?: Record<string, string | number>) => hub(`gateway.${key}`, vars, 'en');

// The expected painted properties are independent of the declarations being
// checked. A token's presence, or an equally broken reference copy, is no oracle.
const guardMetrics = [
  ['.model-hub-guard-body', { padding: '20px', gap: '14px' }],
  ['.model-hub-guard-label', { gap: '6px', fontSize: '10.5px' }],
  ['.model-hub-guard-label > span', { padding: '3px 8px', fontSize: '10px' }],
  ['.model-hub-guard-list', { padding: '8px', gap: '6px', borderRadius: '10px' }],
  ['.model-hub-guard-hop', { minHeight: '52px', padding: '0px 10px', gap: '10px', borderRadius: '8px' }],
  ['.model-hub-guard-hop strong', { fontSize: '12px' }],
  ['.model-hub-guard-hop span span', { fontSize: '10.5px', marginTop: '3px' }],
  ['.model-hub-guard-hint', { gap: '7px', fontSize: '11.5px', lineHeight: '17px' }],
] as const;

async function metrics(locator: Locator, expected: Readonly<Record<string, string>>) {
  await expect(locator.first()).toBeVisible();
  const actual = await locator.evaluateAll((nodes, properties) => nodes.map((node) => {
    const style = getComputedStyle(node);
    return Object.fromEntries(properties.map((property) => [property, style[property as keyof CSSStyleDeclaration]]));
  }), Object.keys(expected));
  expect(actual.length).toBeGreaterThan(0);
  for (const value of actual) expect(value).toEqual(expected);
}

async function guardContract(page: Page) {
  for (const [selector, expected] of guardMetrics) {
    await metrics(page.getByTestId('token-scope-body').locator(selector), expected);
  }
}

async function inkContract(locator: Locator, light: boolean) {
  const expected = await locator.first().evaluate((node, isLight) => {
    const probe = document.createElement('span');
    probe.style.color = isLight
      ? 'color-mix(in srgb, var(--muted) 80%, transparent)'
      : 'rgb(255 255 255 / 45.1%)';
    node.append(probe);
    const ink = getComputedStyle(probe).color;
    probe.remove();
    return ink;
  }, light);
  await expect(locator.first()).toHaveCSS('color', expected);
}

async function openCatalog(page: Page) {
  await page.getByRole('button', { name: 'Manage models', exact: true }).click();
  await expect(page.getByRole('button', { name: text('catalog.edit', { model: 'Existing model' }), exact: true })).toBeVisible();
}

async function openEditor(page: Page) {
  await openCatalog(page);
  await page.getByRole('button', { name: text('catalog.edit', { model: 'Existing model' }), exact: true }).click();
  const editor = page.locator('.model-hub-model-editor');
  await expect(editor).toBeVisible();
  // React ownership does not imply DOM inheritance through the Radix portal.
  expect(await editor.evaluate((node) => node.closest('.model-hub-catalog-dialog'))).toBeNull();
  return editor;
}

async function editorContract(editor: Locator) {
  await metrics(editor.locator('input.model-hub-model-control'), { height: '40px' });
  // Field forwards labelClassName to its own label element.
  await metrics(editor.locator('.model-hub-model-field-label'), { fontSize: '12px' });
  await metrics(editor.locator('.model-hub-model-editor-body'), { gap: '20px', padding: '18px 24px' });
}

test.beforeEach(async ({ page }) => {
  await page.route('**/api/**', (route) => route.abort());
});

for (const mode of ['dark', 'light', 'system-dark', 'system-light'] as const) {
  test(`MH-MENU-COMPOSE-001: token scope survives portalled consumers in ${mode}`, async ({ page }, testInfo) => {
    const light = mode.endsWith('light');
    await page.emulateMedia({ colorScheme: mode.startsWith('system') ? (light ? 'light' : 'dark') : (light ? 'dark' : 'light') });
    await page.goto('/e2e/model-catalog/fixture.html?backend=codex&tokens');
    await page.evaluate((theme) => {
      if (theme.startsWith('system')) document.documentElement.removeAttribute('data-theme');
      else document.documentElement.setAttribute('data-theme', theme);
    }, mode);
    await guardContract(page);
    await inkContract(page.getByTestId('token-scope-body').locator('.model-hub-guard-label'), light);

    await openCatalog(page);
    await metrics(page.locator('.model-hub-catalog-name'), { fontSize: '13.5px' });
    await page.getByRole('button', { name: text('catalog.add'), exact: true }).click();
    const row = page.getByRole('checkbox', { name: /claude-candidate-alpha/ });
    await metrics(row, { minHeight: '36px', borderRadius: '8px', columnGap: '10px', padding: '0px 12px' });
    await metrics(row.locator('.model-hub-picker-name'), { fontSize: '12.5px' });
    await page.getByRole('button', { name: text('picker.custom'), exact: true }).click();
    const editor = page.locator('.model-hub-model-editor');
    await editorContract(editor);
    expect(await editor.evaluate((node) => node.closest('.model-hub-catalog-dialog'))).toBeNull();
    await editor.evaluate(async (node) => {
      await Promise.all(node.getAnimations().map((animation) => animation.finished));
    });
    await page.screenshot({ path: testInfo.outputPath(`token-scope-${mode}.png`), scale: 'css' });
  });
}

test('MH-MENU-COMPOSE-001: shared label tracks a nested light boundary', async ({ page }) => {
  await page.goto('/e2e/model-catalog/fixture.html?backend=codex&tokens');
  await page.evaluate(() => {
    document.documentElement.setAttribute('data-theme', 'dark');
    document.querySelector('[data-testid="token-scope-body"]')!.setAttribute('data-theme', 'light');
  });
  await guardContract(page);
  await inkContract(page.getByTestId('token-scope-body').locator('.model-hub-guard-label'), true);
});

// These controls edit the emitted stylesheet in an isolated browser context.
// They do not modify the checkout or implement selector/cascade evaluation.
async function removeDeclaration(page: Page, property: string) {
  return page.evaluate((name) => {
    const removed: string[] = [];
    const visit = (rules: CSSRuleList) => {
      for (const rule of rules) {
        if (rule instanceof CSSStyleRule && rule.style.getPropertyValue(name)) {
          removed.push(rule.style.getPropertyValue(name));
          rule.style.removeProperty(name);
        }
        if ('cssRules' in rule) visit((rule as CSSGroupingRule).cssRules);
      }
    };
    for (const sheet of document.styleSheets) visit(sheet.cssRules);
    if (!removed.length) throw new Error(`Control could not find ${name}`);
    return removed;
  }, property);
}

test('MH-MENU-COMPOSE-001: misplaced editor metric is observable after portalling', async ({ page }) => {
  await page.goto('/e2e/model-catalog/fixture.html?backend=codex&tokens');
  const editor = await openEditor(page);
  await editorContract(editor);
  const values = await removeDeclaration(page, '--model-hub-model-control-height');
  expect(new Set(values)).toEqual(new Set(['40px']));
  await page.addStyleTag({ content: '.model-hub-catalog-dialog { --model-hub-model-control-height: 40px; }' });
  await expect(editor.locator('input.model-hub-model-control').first()).not.toHaveCSS('height', '40px');
});

test('MH-MENU-COMPOSE-001: missing guard declaration is observable at its consumer', async ({ page }) => {
  await page.goto('/e2e/model-catalog/fixture.html?backend=codex&tokens');
  await guardContract(page);
  await removeDeclaration(page, '--model-hub-guard-count-padding-x');
  await expect(page.getByTestId('token-scope-body').locator('.model-hub-guard-label > span').first()).toHaveCSS('padding', '0px');
});

for (const selector of [':where(.model-hub-model-control)', ':is(.model-hub-model-control, .unused-control)', '.model-hub-model-editor .model-hub-model-control']) {
  test(`MH-MENU-COMPOSE-001: equivalent declaration ${selector} retains rendered geometry`, async ({ page }) => {
    await page.goto('/e2e/model-catalog/fixture.html?backend=codex&tokens');
    const editor = await openEditor(page);
    await removeDeclaration(page, '--model-hub-model-control-height');
    await page.addStyleTag({ content: `${selector} { --model-hub-model-control-height: 40px; }` });
    await editorContract(editor);
  });
}

test('MH-MENU-COMPOSE-001: a metric must be declared in the active media condition', async ({ page }) => {
  await page.goto('/e2e/model-catalog/fixture.html?backend=codex&tokens');
  const editor = await openEditor(page);
  await editorContract(editor);
  await removeDeclaration(page, '--model-hub-model-control-height');
  const conditional = await page.addStyleTag({
    content: '@media print { .model-hub-model-control { --model-hub-model-control-height: 40px; } }',
  });
  await expect(editor.locator('input.model-hub-model-control').first()).not.toHaveCSS('height', '40px');
  await conditional.evaluate((node) => node.parentNode!.removeChild(node));
  await page.addStyleTag({
    content: '@media screen { @supports (height: 40px) { :where(.model-hub-model-control) { --model-hub-model-control-height: 40px; } } }',
  });
  await editorContract(editor);
});

test('MH-MENU-COMPOSE-001: inherited names with missing dependencies cannot supply a metric', async ({ page }) => {
  await page.goto('/e2e/model-catalog/fixture.html?backend=codex&tokens');
  const editor = await openEditor(page);
  await editorContract(editor);
  await removeDeclaration(page, '--model-hub-model-control-height');
  await page.addStyleTag({ content: ':root { --model-hub-model-control-height: var(--scope-missing-height); }' });
  await expect(editor.locator('input.model-hub-model-control').first()).not.toHaveCSS('height', '40px');
  await page.addStyleTag({ content: ':root { --scope-missing-height: 40px; }' });
  await editorContract(editor);
});

test('MH-MENU-COMPOSE-001: a root-frozen ink differs from the nested theme role', async ({ page }) => {
  await page.goto('/e2e/model-catalog/fixture.html?backend=codex&tokens');
  await page.evaluate(() => {
    document.documentElement.setAttribute('data-theme', 'dark');
    document.querySelector('[data-testid="token-scope-body"]')!.setAttribute('data-theme', 'light');
  });
  const label = page.getByTestId('token-scope-body').locator('.model-hub-guard-label').first();
  await inkContract(label, true);
  const before = await label.evaluate((node) => getComputedStyle(node).color);
  await page.addStyleTag({ content: ':root { --scope-control-ink: var(--model-hub-ink-73); } .model-hub-guard-label { color: var(--scope-control-ink); }' });
  await expect(label).not.toHaveCSS('color', before);
  await inkContract(label, false);
  // A local selector-list override restores the intended role.
  await page.addStyleTag({ content: '[data-theme="light"], .scope-light { --scope-control-ink: var(--model-hub-ink-73); }' });
  await inkContract(label, true);
});
