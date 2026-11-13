import { expect, test } from '../support/browserTest';
import { serveProduct, config } from '../workbench-general/support';
import { clientState, installBridge } from './bridge-fixture';

for (const exit of ['normal', 'later'] as const) test(`AUTH-SETUP-170 browser ${exit} confirms completion before native presentation`, async ({ page }, info) => {
  await page.setViewportSize({ width: 1200, height: 800 });
  await serveProduct(page);
  await installBridge(page, clientState(), { projection: true });
  await page.addInitScript(() => localStorage.setItem('i18nextLng', 'en'));
  let completed = false;
  let confirmed = false;
  const writes: Record<string, unknown>[] = [];
  const presentations: string[] = [];
  await page.exposeFunction('__fixturePresent', (url: string) => {
    expect(completed).toBe(true); expect(confirmed).toBe(true); presentations.push(url);
  });
  // Replace only the browser's OS protocol dispatch, so this test cannot launch
  // the user's installed Avibe. Native integration exercises the real dispatch.
  await page.route('**/src/desktop/bridge.ts', async (route) => {
    const response = await route.fetch();
    const body = await response.text();
    expect(body).toContain('window.location.assign(');
    await route.fulfill({ response, body: body.replace('window.location.assign(', 'window.__fixturePresent(') });
  });
  const agent = { id: 'fixture-agent', name: 'vibey', backend: 'vibey', enabled: true, archived: false, source: 'builtin', metadata: { builtin_default: true }, model: 'gpt-5', display_name: 'Vibey', description: '', system_prompt: null, reasoning_effort: null };
  const source = { id: 'fixture-source', kind: 'api_key', vendor: 'openai', display_name: 'Fixture', protocol: 'openai_chat', supply_channel: 'hub', billing: 'metered', state: { status: 'active' }, models: [{ id: 'gpt-5', origin: 'discovered', reasoning_efforts: [], reasoning_efforts_source: null }], last_discovered_at: null };
  const supply = { backend: 'vibey', cli_present: true, mode: 'hub', menu_kind: 'fixed', sources: { order: [source.id], eligibility: [{ source_id: source.id, eligible: true }] }, routes: {}, builtin_models: [], catalog_models: [], menu: null, model_supply: [], named_agents: [{ name: 'vibey', effective_model_id: 'gpt-5', supply_status: 'ok' }], supply_status: 'ok' };
  await page.route('**/api/**', async (route) => {
    const request = route.request(), path = new URL(request.url()).pathname;
    const answer = (json: unknown) => route.fulfill({ json });
    if (path === '/api/config') {
      if (request.method() === 'POST') {
        const body = request.postDataJSON() as Record<string, unknown>; writes.push(body);
        if (body.setup_completed === true) completed = true;
      } else if (completed) confirmed = true;
      return answer({ ...config('en'), setup_completed: completed, setup_state: { needs_setup: !completed }, model_hub: { enabled: true, runtime_default_applied: true } });
    }
    if (path === '/api/models/sources') return answer({ ok: true, sources: exit === 'normal' ? [source] : [] });
    if (path === '/api/models/agents') return answer({ ok: true, agents: [supply] });
    if (path === '/api/agents') return answer({ ok: true, agents: [agent], default_agent_name: 'vibey' });
    if (path === '/api/agents/vibey') return answer({ ok: true, agent, default_agent_name: 'vibey' });
    if (path === '/api/models/runtime/status') return answer({ ok: true, runtime: { contract_version: 23, enabled: true, host_platform: 'darwin', manifest: { name: 'cliproxyapi', resolution: 'resolved', version: 'fixture', source_sha: 'a'.repeat(40), assets: [] }, status: { installed_version: 'fixture', verified: true, health: 'ok' } } });
    if (/\/api\/backend\/[^/]+\/connection$/.test(path)) return answer({ ok: true, backend: path.split('/')[3], enabled: true, installed: true, supply_mode: 'hub', auth: 'api_key', application: 'applied', ready: true, entry_eligible: true });
    if (path === '/api/cli/detect') return answer({ found: true, status: 'ok', path: '/fixture/bin/claude' });
    if (path === '/api/models/migration/scan') return answer({ ok: true, items: [] });
    if (/\/api\/models\/agents\/vibey\/chains$/.test(path)) return answer({ ok: true, chains: [{ backend: 'vibey', model_id: 'gpt-5', chain: [{ source_id: source.id, model_id: 'gpt-5' }] }] });
    return route.fallback();
  });
  await page.route('**/e2e/desktop-client/setup.html', (route) => route.continue());
  await page.goto('/e2e/desktop-client/setup.html');
  await page.getByRole('button', { name: 'Get started', exact: true }).click();
  if (exit === 'later') await page.getByRole('button', { name: 'Set up later', exact: true }).click();
  else {
    const primary = page.locator('.onboarding-primary-action');
    await expect(primary).toBeEnabled(); await primary.click();
    await page.getByRole('button', { name: 'Enter workspace', exact: true }).click();
  }
  await expect.poll(() => presentations).toEqual(['avibe://desktop/setup-complete']);
  expect(writes.filter((body) => body.setup_completed === true)).toHaveLength(1);
  expect(writes.filter((body) => Object.keys(body).length === 0)).toHaveLength(1);
  await expect(page.locator('[data-setup-completed]')).toBeAttached();
  await page.screenshot({ path: info.outputPath(`${exit}-completed.png`) });
});
