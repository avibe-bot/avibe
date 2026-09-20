import { expect, test } from '@playwright/test';
import { ORIGIN, serveProduct } from './support';

// Exercise the actual App/AuthGuard/Settings overlay/Wizard boundary. All
// server responses are fixture-owned; the Vite server has a dead backend.
// The Python connection tests separately establish the readiness producer.
for (const width of [1200, 390]) {
  test(`Hub takeover returns to the same setup step and permits completion at ${width}px`, async ({ page }, info) => {
    await page.setViewportSize({ width, height: 844 });
    const denied = await serveProduct(page);
    const nativeRequests: string[] = [];
    const writes: string[] = [];
    const applied: string[][] = [];
    const observedReady: boolean[] = [];
    const pageErrors: string[] = [];
    page.on('pageerror', (error) => pageErrors.push(error.message));
    let migrated = false;
    let completed = false;
    const source = {
      id: 'src_fixture_claude', kind: 'api_key', vendor: 'anthropic',
      display_name: 'Migrated Claude', protocol: 'anthropic', base_url: null,
      supply_channel: 'hub', billing: 'metered', masked_credential: 'sk-…fixture',
      state: { status: 'standby', retry_at: null, detail_key: null },
      models: [], last_discovered_at: null,
    };
    const config = () => ({
      mode: 'v2', setup_completed: completed, setup_state: { needs_setup: !completed },
      capabilities: { model_hub: { enabled: true } },
      platforms: { enabled: [] },
      agents: Object.fromEntries(['claude', 'codex', 'opencode'].map((backend) => [
        backend, { enabled: backend === 'claude', cli_path: backend },
      ])),
      agent: { default_cwd: '/fixture/work' },
      model_hub: { enabled: true, runtime_default_applied: true },
    });
    const supply = () => ({
      backend: 'claude', cli_present: true, mode: 'hub', menu_kind: 'fixed',
      sources: { order: migrated ? [source.id] : [], eligibility: [] },
      routes: {}, builtin_models: [], catalog_models: [], named_agents: [], menu: null,
      model_supply: [], supply_status: migrated ? 'ok' : 'unavailable',
    });
    const items = () => migrated ? [] : [{
      id: 'mig_claude_fixture', backend: 'claude', kind: 'api_key',
      masked_detail: 'sk-…fixture', proposed_action: 'import', selected: true,
      notes_key: null, vendor: 'anthropic', display_name: 'Anthropic',
      masked_credential: 'sk-…fixture',
    }];
    await page.addInitScript(() => localStorage.setItem('i18nextLng', 'en'));
    await page.route(`${ORIGIN}/api/**`, async (route) => {
      const request = route.request();
      const path = new URL(request.url()).pathname;
      const answer = (json: unknown) => route.fulfill({ json });
      if (request.method() !== 'GET') writes.push(`${request.method()} ${path}`);
      if (/\/backend\/[^/]+\/auth(?:\/|$)/.test(path)) {
        nativeRequests.push(`${request.method()} ${path}`);
        return answer({ ok: false, error: 'native_auth_hub_owned', reauth_channel: 'hub' });
      }
      if (path === '/api/config') {
        if (request.method() === 'POST') {
          const body = request.postDataJSON();
          if (body.setup_completed === true) completed = true;
        }
        return answer(config());
      }
      if (path === '/api/session') return answer({ remote: false, instance_kind: 'personal' });
      if (path === '/api/csrf-token') return answer({ csrf_token: 'fixture-token' });
      if (path === '/api/version') return answer({
        current: '1.0.0', latest: '1.0.0', has_update: false, error: null,
      });
      if (path === '/api/models/sources') return answer({ ok: true, sources: migrated ? [source] : [] });
      if (path === '/api/models/agents') return answer({ ok: true, agents: [supply()] });
      if (path === '/api/models/agents/claude/sources') return answer({ ok: true, agent: supply() });
      if (path === '/api/models/agents/claude/chains') return answer({ ok: true, chains: [] });
      if (path === '/api/models/runtime/status') return answer({
        ok: true,
        runtime: {
          contract_version: 10, enabled: true, host_platform: 'linux',
          manifest: { name: 'cliproxyapi', resolution: 'resolved', version: 'fixture', source_sha: 'a'.repeat(40), assets: [] },
          status: { installed_version: 'fixture', verified: true, health: 'ok' },
        },
      });
      if (path === '/api/models/events') return answer({ ok: true, events: [] });
      if (path === '/api/models/migration/scan') return answer({ ok: true, scan: { items: items() } });
      if (path === '/api/models/migration/apply') {
        const ids = request.postDataJSON().item_ids;
        applied.push(ids);
        migrated = true;
        return answer({ ok: true, applied: ids.length, sources: [source] });
      }
      if (/\/api\/backend\/[^/]+\/connection$/.test(path)) {
        const backend = path.split('/')[3];
        const ready = backend === 'claude' && migrated;
        if (backend === 'claude') observedReady.push(ready);
        return answer({
          ok: true, backend, installed: true, enabled: backend === 'claude',
          supply_mode: 'hub', auth: ready ? 'api_key' : 'none',
          application: 'applied', ready, entry_eligible: ready,
        });
      }
      if (path === '/api/agents') return answer({
        ok: true, default_agent_name: 'claude', agents: [{
          id: 'fixture-agent', name: 'claude', backend: 'claude', enabled: true,
          archived: false, description: '', model: null,
        }],
      });
      // These unrelated app-shell reads are empty, not live-service fallbacks.
      if (path === '/api/memory/status') return answer({ ok: true, enabled: false });
      if (path === '/api/projects') return answer({ ok: true, projects: [] });
      if (path === '/api/scopes') return answer({ ok: true, scopes: [] });
      if (path === '/api/inbox') return answer({
        sessions: [], next_cursor: null, unread_by_session: {},
        unread_total: 0, unread_sessions: 0,
      });
      if (path === '/api/events') return route.fulfill({ status: 204 });
      return route.fallback();
    });

    await page.goto('/setup');
    await page.getByRole('button', { name: 'Get started', exact: true }).click();
    const enter = page.getByRole('button', { name: 'Enter workspace', exact: true });
    await expect(enter).toBeDisabled();
    await expect.poll(() => observedReady.length).toBeGreaterThan(0);
    const wizard = await page.locator('.onboarding-assistants').elementHandle();
    const claude = page.getByLabel('Claude Code', { exact: true });
    await expect(claude.getByRole('button', { name: 'Add API Key', exact: true })).toHaveCount(0);
    await claude.getByRole('button', { name: 'Open Model Hub', exact: true }).click();
    await expect(page).toHaveURL(/\/settings\/models$/);
    await page.getByRole('button', { name: 'Migrate configuration', exact: true }).click();
    const migration = page.getByRole('dialog').filter({ has: page.getByRole('heading', { name: 'Migrate to the Model Hub', exact: true }) });
    await expect(migration).toBeVisible();
    await migration.getByRole('button', { name: 'Start migration', exact: true }).click();
    await expect.poll(() => applied).toEqual([['mig_claude_fixture']]);
    await expect(migration).toHaveCount(0);
    await page.screenshot({ path: info.outputPath(`hub-migrated-${width}.png`) });

    // Actual browser history closes both the desktop sheet and mobile surface.
    await page.goBack();
    await expect(page).toHaveURL(/\/setup$/);
    await expect(page.locator('.onboarding-assistants')).toBeVisible();
    expect(await wizard!.evaluate((node) => node.isConnected)).toBe(true);
    await expect(enter).toBeEnabled();
    await expect(page.getByRole('button', { name: 'Review and migrate', exact: true })).toHaveCount(0);
    expect(observedReady).toContain(true);
    await page.screenshot({ path: info.outputPath(`setup-hub-ready-${width}.png`), fullPage: true });
    await enter.click();
    await expect.poll(() => completed).toBe(true);
    await expect(page).toHaveURL(`${ORIGIN}/`);
    expect(pageErrors).toEqual([]);
    expect(nativeRequests).toEqual([]);
    expect(writes.filter((write) => !write.endsWith('/api/models/migration/scan'))).toEqual([
      'POST /api/models/migration/apply',
      'POST /api/config',
    ]);
    // Initial/return flow must never need a running backend. App-shell reads
    // after completion are not part of this test's connection contract.
    expect(denied.filter((request) => /\/backend\/.*\/auth/.test(request))).toEqual([]);
  });
}
