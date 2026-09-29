import { expect, test } from '@playwright/test';
import en from '../../src/i18n/en.json' with { type: 'json' };
import zh from '../../src/i18n/zh.json' with { type: 'json' };
import { openOnboarding, openSetup, serveModelHub, serveProduct } from './support';

for (const [lang, copy, width] of [['en', en, 1440], ['zh', zh, 390]] as const) {
  test(`AUTH-SETUP-126: subscription creation satisfies the setup provider gate (${lang})`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    const denied = await serveProduct(page);
    const vendor = lang === 'en' ? 'anthropic' : 'openai';
    const source = {
      id: 'src_subscription_fixture', vendor, display_name: '测试订阅', kind: 'subscription',
      protocol: vendor === 'anthropic' ? 'anthropic' : 'openai_responses',
      supply_channel: 'hub', billing: 'monthly', state: { status: 'standby' },
      models: [], last_discovered_at: null,
    };
    let sources: typeof source[] = [];
    const starts: { vendor: string; channel: string }[] = [];
    const flow = {
      flow_id: 'oaf_setup_fixture', vendor, channel: 'hub', state: 'success',
      presentation: { expects: 'none' }, expires_at: '2099-01-01T00:00:00Z',
    };
    await page.route('**/api/agents?*', (route) => route.fulfill({ json: { ok: true, agents: [], default_agent_name: null } }));
    await page.route('**/api/models/sources', (route) => route.fulfill({ json: { sources } }));
    await page.route('**/api/models/oauth/start', (route) => {
      const body = route.request().postDataJSON();
      starts.push({ vendor: body.vendor, channel: body.channel });
      return route.fulfill({ json: { flow } });
    });
    await page.route('**/api/models/oauth/status/oaf_setup_fixture', (route) => {
      sources = [source];
      return route.fulfill({ json: { flow, source, added_to: [], adopted_by: [] } });
    });
    await page.route('**/api/models/oauth/cancel', (route) => {
      expect(route.request().postDataJSON().flow_id).toBe(flow.flow_id);
      return route.fulfill({ json: { ok: true } });
    });
    await openOnboarding(page, { lang, realTime: true });
    await page.getByRole('button', { name: lang === 'zh' ? '立即开始' : 'Get started' }).click();
    const primary = page.locator('.onboarding-primary-action');
    await expect(primary).toHaveText(copy.onboarding.providers.actionAdd);
    await page.locator(`.setup-provider-card[data-provider="${vendor}"]`).click();
    await page.getByRole('dialog').getByRole('contentinfo').getByRole('button', {
      name: copy.onboarding.providers.addFooterSignInNamed.replace('{{name}}', vendor === 'anthropic' ? 'Claude' : 'ChatGPT'), exact: true,
    }).click();
    const chooser = page.getByRole('dialog').filter({ has: page.getByRole('heading', {
      name: copy.settings.models.addSub.title.replace('{{vendor}}', vendor === 'anthropic' ? 'Claude' : 'ChatGPT'), exact: true,
    }) });
    await expect(chooser.getByRole('radio')).toHaveCount(1);
    await expect(chooser.getByRole('radio')).toContainText(copy.settings.models.addSub.opt.hub.label);
    expect(starts).toEqual([]);
    await chooser.getByRole('button', { name: copy.settings.models.addSub.signIn, exact: true }).click();
    await expect(page.getByRole('dialog')).toHaveCount(0);
    expect(starts).toEqual([{ vendor, channel: 'hub' }]);
    await expect(page.locator(`.setup-provider-card[data-provider="${vendor}"]`)).toHaveAttribute('data-state', 'connected');
    await expect(primary).toHaveText(copy.onboarding.providers.actionContinue);
    await expect(primary).toBeEnabled();
    await primary.click();
    await expect(page.locator('[data-setup-screen]')).toHaveAttribute('data-setup-screen', 'assistants');
    expect(denied).toEqual([]);
  });

  test(`AUTH-SETUP-126: assistant fixture can decline migration with an existing source (${lang})`, async ({ page }) => {
    await serveProduct(page);
    const applied = await serveModelHub(page);
    await openOnboarding(page, { lang });
    await openSetup(page, lang);
    await expect(page.locator('[data-setup-screen]')).toHaveAttribute('data-setup-screen', 'assistants');
    expect(applied).toEqual([]);
  });

  test(`AUTH-SETUP-126: provider gate, add failure and lost-source recovery (${lang})`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    const denied = await serveProduct(page);
    const source = {
      id: 'src_fixture', vendor: 'custom', display_name: '测试模型来源', kind: 'api_key',
      protocol: 'openai_chat', supply_channel: 'hub', billing: 'metered',
      state: { status: 'active' }, models: [], last_discovered_at: null,
    };
    const initialSources = lang === 'zh'
      ? [{ ...source, kind: 'subscription', supply_channel: 'native_cli' }] : [];
    let sources: typeof source[] = initialSources;
    let creates = 0;
    await page.route('**/api/agents?*', (route) => route.fulfill({ json: { ok: true, agents: [], default_agent_name: null } }));
    await page.route('**/api/models/sources', (route) => {
      if (route.request().method() === 'GET') return route.fulfill({ json: { sources } });
      expect(route.request().method()).toBe('POST');
      creates += 1;
      if (creates === 1) return route.fulfill({ status: 422, json: { error: 'discovery_failed', message: 'Fixture authentication rejected' } });
      sources = [source];
      return route.fulfill({ status: 201, json: { source, added_to: [], adopted_by: [] } });
    });
    await openOnboarding(page, { lang, realTime: true });
    await page.getByRole('button', { name: lang === 'zh' ? '立即开始' : 'Get started' }).click();
    const step = page.locator('[data-setup-screen]');
    const primary = page.locator('.onboarding-primary-action');
    await expect(step).toHaveAttribute('data-setup-screen', 'providers');
    await expect(primary).toHaveText(copy.onboarding.providers.actionAdd);
    await expect(primary).toBeEnabled();
    await expect(page.getByRole('button', { name: copy.onboarding.providers.actionContinue })).toHaveCount(0);
    await primary.click();
    const dialog = page.getByRole('dialog');
    await expect(dialog).toBeVisible();
    await expect(step).toHaveAttribute('data-setup-screen', 'providers');
    await dialog.getByRole('button', { name: 'API Key', exact: true }).click();
    await dialog.getByLabel(copy.settings.models.addKey.field.baseUrl, { exact: true }).fill('https://fixture.example/v1');
    await dialog.getByLabel(copy.settings.models.addKey.field.apiKey, { exact: true }).fill('sk-fixture-only');
    await dialog.getByRole('button', { name: copy.onboarding.providers.addFooterAddKey, exact: true }).click();
    await expect(dialog.getByText(copy.onboarding.providers.addErrorPreserved)).toBeVisible();
    await expect(step).toHaveAttribute('data-setup-screen', 'providers');
    expect(sources).toEqual(initialSources);
    await dialog.getByRole('button', { name: copy.common.retry, exact: true }).click();
    await expect(dialog).toHaveCount(0);
    await expect(primary).toHaveText(copy.onboarding.providers.actionContinue);
    await expect(primary).toBeEnabled();
    expect(creates).toBe(2);

    // The source disappears elsewhere between steps. The assistant screen must
    // offer recovery even if a direct backend still reports itself entry-eligible.
    sources = [];
    await page.route('**/api/backend/*/connection', (route) => route.fulfill({ json: {
      ok: true, backend: new URL(route.request().url()).pathname.split('/').at(-2),
      installed: true, enabled: true, auth: 'api_key', application: 'applied',
      ready: true, entry_eligible: true, supply_mode: 'direct',
    } }));
    await primary.click();
    await expect(step).toHaveAttribute('data-setup-screen', 'assistants');
    await expect(primary).toBeDisabled();
    await expect(page.getByText(copy.onboarding.connection.sourceRequired)).toBeVisible();
    await page.getByRole('button', { name: copy.onboarding.connection.addSource }).click();
    await expect(step).toHaveAttribute('data-setup-screen', 'providers');
    await expect(primary).toHaveText(copy.onboarding.providers.actionAdd);
    await expect(primary).toBeEnabled();
    expect(denied).toEqual([]);
  });
}
