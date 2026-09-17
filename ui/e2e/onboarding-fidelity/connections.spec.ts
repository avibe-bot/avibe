import { expect, test, type Page } from '@playwright/test';
import { openOnboarding, openSetup, serveProduct, settleEffects } from './support';

async function authFixtures(page: Page) {
  const cancellations: string[] = [];
  await page.route('**/api/csrf-token', (route) => route.fulfill({ json: { csrf_token: 'fixture-token' } }));
  await page.route('**/api/backend/*/auth', (route) => route.fulfill({ json: { ok: true, active_auth_mode: 'none', auth_mode: 'oauth', has_api_key: false, base_url: null } }));
  await page.route('**/api/backend/opencode/providers', (route) => route.fulfill({ json: { ok: true, providers: [
    { id: 'openai', name: 'OpenAI', description: 'ChatGPT subscription or API Key', oauth_available: true, configured: false, local: false, models: [] },
    { id: 'github-copilot', name: 'GitHub Copilot', description: 'GitHub subscription', oauth_available: true, configured: false, local: false, models: [] },
    { id: 'poe', name: 'Poe', description: 'Browser authorization', oauth_available: true, configured: false, local: false, models: [] },
  ] } }));
  await page.route('**/api/backend/*/auth/oauth/start', (route) => route.fulfill({ json: { ok: true, flow_id: 'fixture-flow', state: 'awaiting_code', url: 'https://auth.openai.com/codex/device', device_code: 'ABCD-1234' } }));
  await page.route('**/api/backend/*/auth/oauth/status/*', (route) => route.fulfill({ json: { ok: true, flow_id: 'fixture-flow', state: 'awaiting_code', url: 'https://auth.openai.com/codex/device', device_code: 'ABCD-1234' } }));
  await page.route('**/api/backend/*/auth/oauth/cancel', (route) => { cancellations.push(route.request().postData() || ''); return route.fulfill({ json: { ok: true } }); });
  return cancellations;
}

for (const lang of ['en', 'zh']) for (const theme of ['dark', 'light']) {
  test(`connection family ${lang} ${theme}: native desktop + narrow fields/device/focus`, async ({ page }, info) => {
    const denied = await serveProduct(page);
    const cancellations = await authFixtures(page);
    await page.setViewportSize({ width: 1200, height: 800 });
    await openOnboarding(page, { lang, theme });
    await openSetup(page, lang);
    const subscription = lang === 'zh' ? '添加订阅' : 'Add subscription';
    const key = lang === 'zh' ? '添加 API Key' : 'Add API Key';
    await page.getByLabel('Claude Code', { exact: true }).getByRole('button', { name: subscription }).click();
    const dialog = page.getByRole('dialog');
    await expect(dialog.getByRole('button', { name: lang === 'zh' ? '使用 Claude 登录' : 'Sign in with Claude' })).toBeVisible();
    await settleEffects(page);
    const geometry = await dialog.evaluate((node) => { const rect = node.getBoundingClientRect(); const style = getComputedStyle(node); return { width: rect.width, padding: style.padding, gap: style.gap, radius: style.borderRadius }; });
    expect(geometry).toEqual({ width: 568, padding: '24px', gap: '20px', radius: '16px' });
    await settleEffects(page);
    await dialog.screenshot({ path: info.outputPath(`claude-start-${lang}-${theme}-desktop.png`) });
    await page.keyboard.press('Escape'); await expect(dialog).toHaveCount(0);

    await page.setViewportSize({ width: 390, height: 640 });
    await page.getByLabel('Claude Code', { exact: true }).getByRole('button', { name: key }).click();
    const save = dialog.getByRole('button', { name: lang === 'zh' ? '保存并连接' : 'Save and connect' });
    await expect(save).toBeDisabled();
    await save.scrollIntoViewIfNeeded(); await expect(save).toBeInViewport();
    const overflow = await dialog.evaluate((node) => ({ width: node.getBoundingClientRect().width, extra: node.scrollWidth - node.clientWidth }));
    expect(overflow.width).toBeLessThan(390); expect(overflow.extra).toBeLessThanOrEqual(1);
    for (let index = 0; index < 12; index++) { await page.keyboard.press('Tab'); expect(await dialog.evaluate((node) => node.contains(document.activeElement))).toBe(true); }
    await settleEffects(page);
    await dialog.screenshot({ path: info.outputPath(`claude-key-${lang}-${theme}-narrow.png`) });
    await page.keyboard.press('Escape'); await expect(dialog).toHaveCount(0);

    await page.setViewportSize({ width: 1200, height: 800 });
    await page.getByLabel('Codex', { exact: true }).getByRole('button', { name: subscription }).click();
    await dialog.getByRole('button', { name: lang === 'zh' ? '使用 ChatGPT 登录' : 'Sign in with ChatGPT' }).click();
    await expect(dialog.getByText('ABCD-1234')).toBeVisible();
    await expect(dialog.getByRole('radio').first()).toBeDisabled();
    await settleEffects(page);
    await dialog.screenshot({ path: info.outputPath(`codex-device-${lang}-${theme}-desktop.png`) });
    await page.keyboard.press('Escape'); await expect.poll(() => cancellations.length).toBe(1);

    await page.setViewportSize({ width: 390, height: 640 });
    await page.getByLabel('OpenCode', { exact: true }).getByRole('button', { name: key }).click();
    await dialog.getByRole('button', { name: /OpenAI/ }).click();
    await expect(dialog.getByLabel(lang === 'zh' ? 'Base URL（可选）' : 'Base URL (optional)')).toBeVisible();
    await settleEffects(page);
    await dialog.screenshot({ path: info.outputPath(`opencode-key-${lang}-${theme}-narrow.png`) });
    await page.keyboard.press('Escape');
    expect(denied).toEqual([]);
  });
}

for (const lang of ['en', 'zh']) {
  test(`corrected compact labels and Light action ${lang}`, async ({ page }, info) => {
    const denied = await serveProduct(page); await authFixtures(page);
    await page.setViewportSize({ width: 390, height: 640 });
    await openOnboarding(page, { lang, theme: 'light' }); await openSetup(page, lang);
    const addKey = lang === 'zh' ? '添加 API Key' : 'Add API Key';
    await page.getByLabel('Claude Code', { exact: true }).getByRole('button', { name: addKey }).click();
    const dialog = page.getByRole('dialog');
    const credential = dialog.getByRole('radiogroup', { name: lang === 'zh' ? '凭据类型' : 'Credential type' });
    await expect(credential.getByRole('radio', { name: 'API Key', exact: true })).toBeVisible();
    await expect(credential.getByRole('radio', { name: 'Auth Token', exact: true })).toBeVisible();
    const bounds = await credential.evaluate((node) => [...node.querySelectorAll('button')].map((button) => {
      const range = document.createRange(); range.selectNodeContents(button);
      const text = range.getBoundingClientRect(); const box = button.getBoundingClientRect();
      return text.left >= box.left && text.right <= box.right && text.top >= box.top && text.bottom <= box.bottom;
    }));
    expect(bounds).toEqual([true, true]);
    await page.keyboard.press('Tab');
    await credential.getByRole('radio', { name: 'Auth Token' }).focus();
    const focus = await credential.getByRole('radio', { name: 'Auth Token' }).evaluate((node) => {
      const style = getComputedStyle(node); return { width: style.outlineWidth, style: style.outlineStyle, color: style.outlineColor };
    });
    expect(focus.width).toBe('2px'); expect(focus.style).toBe('solid');
    const save = dialog.getByRole('button', { name: lang === 'zh' ? '保存并连接' : 'Save and connect' });
    const disabled = await save.evaluate((node) => { const style = getComputedStyle(node); return { opacity: style.opacity, fill: style.backgroundColor, text: style.color }; });
    expect(disabled.opacity).toBe('0.4'); expect(disabled.text).toBe('rgb(255, 255, 255)');
    await settleEffects(page); await dialog.screenshot({ path: info.outputPath(`corrected-claude-key-${lang}-light-disabled.png`) });
    await dialog.getByLabel('API Key', { exact: true }).fill('nonsecret-fixture-value');
    await expect(save).toBeEnabled();
    await settleEffects(page);
    const enabled = await save.evaluate((node) => { const style = getComputedStyle(node); return { opacity: style.opacity, fill: style.backgroundColor, text: style.color }; });
    expect(enabled).toEqual({ ...disabled, opacity: '1' });
    await settleEffects(page); await dialog.screenshot({ path: info.outputPath(`corrected-claude-key-${lang}-light-enabled.png`) });
    await page.keyboard.press('Escape');
    await page.getByLabel('OpenCode', { exact: true }).getByRole('button', { name: addKey }).click();
    await dialog.getByRole('button', { name: /OpenAI/ }).click();
    await expect(dialog.getByLabel('API Key', { exact: true })).toBeVisible();
    await expect(dialog.getByText(/ANTHROPIC_/)).toHaveCount(0);
    await settleEffects(page); await dialog.screenshot({ path: info.outputPath(`corrected-opencode-key-${lang}-light-narrow.png`) });
    await page.keyboard.press('Escape'); expect(denied).toEqual([]);
  });
}

test('corrected locked Codex Light has one opacity layer and visible selected method', async ({ page }, info) => {
  const denied = await serveProduct(page); await authFixtures(page);
  await page.setViewportSize({ width: 1200, height: 800 });
  await openOnboarding(page, { theme: 'light' }); await openSetup(page, 'en');
  await page.getByLabel('Codex', { exact: true }).getByRole('button', { name: 'Add subscription' }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByRole('button', { name: 'Sign in with ChatGPT' }).click();
  await expect(dialog.getByText('ABCD-1234')).toBeVisible();
  const radio = dialog.getByRole('radio', { name: 'Sign in with ChatGPT' });
  await expect(radio).toBeDisabled(); await expect(radio).toHaveAttribute('aria-checked', 'true');
  const styles = await radio.evaluate((node) => {
    const style = getComputedStyle(node); const parent = getComputedStyle(node.parentElement!);
    return { opacity: style.opacity, parentOpacity: parent.opacity, color: style.color, fill: style.backgroundColor, border: style.borderColor };
  });
  expect(styles.opacity).toBe('1'); expect(styles.parentOpacity).toBe('0.6');
  expect(styles.fill).not.toBe('rgba(0, 0, 0, 0)'); expect(styles.border).not.toBe('rgba(0, 0, 0, 0)');
  await settleEffects(page); await dialog.screenshot({ path: info.outputPath('corrected-codex-locked-light.png') });
  await page.keyboard.press('Escape'); expect(denied).toEqual([]);
});

for (const width of [1200, 390]) {
  test(`OpenCode inline model recovery ${width}`, async ({ page }, info) => {
    const denied = await serveProduct(page); await authFixtures(page);
    let model = 'openai/gpt-5.6-sol'; let writes = 0;
    const agent = () => ({ id: 'fixture-agent', name: 'opencode', display_name: 'OpenCode', backend: 'opencode', enabled: true, model, reasoning_effort: null, source: 'builtin', archived: false });
    await page.route('**/api/backend/*/connection', (route) => { const backend = new URL(route.request().url()).pathname.split('/').at(-2); return route.fulfill({ json: { ok: true, backend, ready: backend === 'opencode', entry_eligible: backend === 'opencode', installed: true, enabled: true, auth: 'api_key', application: 'applied' } }); });
    await page.route('**/api/agents', (route) => route.fulfill({ json: { ok: true, default_agent_name: 'opencode', agents: [agent()] } }));
    await page.route('**/api/agents/opencode', (route) => { if (route.request().method() === 'PATCH') { model = route.request().postDataJSON().model; writes++; } return route.fulfill({ json: { ok: true, agent: agent() } }); });
    await page.route('**/api/backend/opencode/providers', (route) => route.fulfill({ json: { ok: true, default_provider: 'openai', providers: [{ id: 'anthropic', name: 'Anthropic', active_auth_type: 'api', configured: true }] } }));
    await page.route('**/api/models/agents/opencode/models', (route) => route.fulfill({ json: { ok: true, agent: { backend: 'opencode', mode: 'direct' } } }));
    await page.route('**/api/opencode/options', (route) => route.fulfill({ json: { ok: true, data: { models: { providers: [{ id: 'anthropic', models: { 'chosen-model': {} } }] } } } }));
    await page.setViewportSize({ width, height: 800 });
    await openOnboarding(page); await openSetup(page, 'en');
    await page.getByRole('button', { name: 'Enter workspace' }).click();
    const recovery = page.getByRole('region', { name: 'Choose a model for this connection' });
    await expect(recovery).toBeVisible(); expect(writes).toBe(0);
    const picker = recovery.getByRole('combobox'); await expect(picker).toBeEnabled(); await picker.click();
    await page.getByRole('option', { name: 'anthropic/chosen-model' }).click();
    const apply = recovery.getByRole('button', { name: 'Apply model and enter' });
    await apply.scrollIntoViewIfNeeded(); await expect(apply).toBeInViewport();
    expect(await recovery.evaluate((node) => node.scrollWidth - node.clientWidth)).toBeLessThanOrEqual(1);
    await settleEffects(page); await recovery.screenshot({ path: info.outputPath(`model-recovery-${width}.png`) });
    await recovery.getByRole('button', { name: 'Cancel' }).click(); expect(writes).toBe(0);
    await expect(recovery).toHaveCount(0); expect(denied).toEqual([]);
  });
}

test('saved Slack recovery narrow uses the existing form only after explicit repair', async ({ page }, info) => {
  const denied = await serveProduct(page); await authFixtures(page);
  let manifests = 0;
  await page.route('**/api/config', (route) => route.fulfill({ json: { platforms: { enabled: ['slack'] }, platform_catalog: [{ id: 'slack', config_key: 'slack', credential_fields: ['bot_token'] }], slack: { has_app_token: true, bot_token: '' }, agents: { claude: { enabled: true }, codex: { enabled: true }, opencode: { enabled: true } } } }));
  await page.route('**/api/backend/claude/connection', (route) => route.fulfill({ json: { ok: true, backend: 'claude', ready: true, entry_eligible: true, enabled: true, installed: true, application: 'applied', auth: 'api_key' } }));
  await page.route('**/api/slack/manifest', (route) => { manifests++; return route.fulfill({ json: { ok: true, manifest: '{}' } }); });
  await page.setViewportSize({ width: 390, height: 640 });
  await openOnboarding(page); await openSetup(page, 'en');
  await page.getByRole('button', { name: 'Enter workspace' }).click();
  const recovery = page.getByRole('region', { name: 'Repair saved messaging configuration' });
  await expect(recovery).toBeVisible(); expect(manifests).toBe(0);
  await recovery.getByRole('button', { name: 'Repair saved messaging configuration' }).click();
  await expect.poll(() => manifests).toBe(1);
  await recovery.getByRole('button', { name: /Get Bot Token/ }).click();
  await recovery.getByPlaceholder('xoxb-... (paste here)').fill('xoxb-fixture-only');
  const apply = recovery.getByRole('button', { name: /Apply/ });
  await expect(apply).toBeEnabled(); await apply.scrollIntoViewIfNeeded(); await expect(apply).toBeInViewport();
  expect(await recovery.evaluate((node) => node.scrollWidth - node.clientWidth)).toBeLessThanOrEqual(1);
  await settleEffects(page); await recovery.screenshot({ path: info.outputPath('saved-slack-recovery-narrow.png') });
  await recovery.getByRole('button', { name: 'Cancel' }).click();
  expect(denied).toEqual([]);
});
