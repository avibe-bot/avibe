import { mkdir } from 'node:fs/promises';
import { expect, test, type Page } from '../support/browserTest';
import { open, serveProduct } from './support';

async function serveChat(page: Page) {
  const denied = await serveProduct(page);
  const session = {
    id: 'chrome-fixture', scope_id: 'fixture', project_id: 'proj-1', title: '中文窗口会话',
    agent_id: null, agent_name: 'codex', agent_backend: 'codex', model: 'gpt-fixture',
    reasoning_effort: 'high', status: 'active', agent_status: 'idle', pinned: false,
    workdir: '/fixture/work', native_session_id: null, metadata: {},
    created_at: '2026-09-29T00:00:00Z', updated_at: '2026-09-29T00:00:00Z',
  };
  const turnState = { in_flight: false, foreground: 'idle', native_turn_started: false,
    pending_input_count: 0, background_activities: [], pending_activity_output_count: 0, connection: 'connected' };
  await page.route('**/api/sessions/chrome-fixture/bootstrap', route => route.fulfill({ json: {
    session, capabilities: { can_chat: true }, agents: [{ id: 'codex', name: 'codex', backend: 'codex', enabled: true }],
    default_agent_name: 'codex', config: { ui: {} }, messages: [], queued: [],
    next_after_id: null, next_before_id: null, draft: { text: '', updated_at: null }, turn_state: turnState,
  } }));
  await page.route('**/api/sessions/chrome-fixture', route => route.fulfill({ json: session }));
  await page.route('**/api/sessions/chrome-fixture/queue', route => route.fulfill({ json: [] }));
  await page.route('**/api/sessions/chrome-fixture/turn-state', route => route.fulfill({ json: turnState }));
  await page.route('**/api/sessions/chrome-fixture/activity**', route => route.fulfill({ json: { groups: [] } }));
  // The unread acknowledgement belongs to this fixture only.
  await page.route('**/api/inbox/chrome-fixture/read', route => route.fulfill({ json: { ok: true } }));
  return denied;
}

const sid = 'chrome-fixture';
const frameHtml = `<!doctype html><html><body><h1>Authored Show</h1><script>
function report(enabled = false) {
  document.body.dataset.annotating = String(enabled);
  parent.postMessage({ type: 'avibe:annotation:state', enabled, mode: 'smart', available: true }, location.origin);
}
addEventListener('message', event => {
  if (event.origin !== location.origin) return;
  if (event.data.type === 'avibe:annotation:query') report();
  if (event.data.type === 'avibe:annotation:control') report(event.data.action === 'enable');
});
report();
</script></body></html>`;

for (const device of ['iPhone', 'Android', 'iOS App', 'narrow desktop', 'desktop']) {
  test(`${device}: Show controls preserve chat actions and desktop menus`, async ({ page }) => {
    const denied = await serveChat(page);
    await page.addInitScript((device) => {
      const ua = device === 'iPhone' ? 'Mozilla/5.0 (iPhone) Version/18.0 Mobile Safari/605.1.15'
        : device === 'Android' ? 'Mozilla/5.0 (Linux; Android 14) Chrome/130.0 Mobile Safari/537.36'
        : 'Mozilla/5.0 (Macintosh) Version/18.0 Safari/605.1.15';
      Object.defineProperty(navigator, 'userAgent', { value: ua });
      Object.defineProperty(navigator, 'platform', { value: device === 'iPhone' ? 'iPhone' : device === 'Android' ? 'Linux armv8l' : 'MacIntel' });
      Object.defineProperty(navigator, 'maxTouchPoints', { value: device.includes('desktop') || device === 'iOS App' ? 0 : 5 });
      if (device === 'iOS App') {
        // The phone bridge, with optional native features unavailable in this fixture.
        Object.defineProperty(window, 'avibeNative', { value: {
          contract: 1, platform: 'ios', capabilities: {},
          call: async () => ({}), on: () => () => {},
        } });
      }
    }, device);
    const payload = { session_id: sid, title: 'Authored Show', visibility: 'private', active_url: `/show/${sid}/`, url_available: true, offline: false, icon_version: null };
    await page.route(`**/api/show-pages/${sid}/ensure`, route => route.fulfill({ json: { ok: true, existed: true, ...payload } }));
    await page.route(`**/api/show-pages/${sid}`, route => route.fulfill({ json: payload }));
    await page.route(`**/api/show-pages/${sid}/access`, route => route.fulfill({ json: {
      ok: true, mode: 'unmanaged', ownership_status: 'unmanaged', access_level: 'private',
      can_use: true, can_manage: true, can_publish_public: true, group_ids: [],
    } }));
    await page.route(`**/show/${sid}/**`, route => route.fulfill({ contentType: 'text/html', body: frameHtml }));
    await page.setViewportSize({ width: device === 'desktop' ? 1200 : 390, height: 844 });
    await open(page, `/chat/${sid}`);
    const header = page.locator('[data-chat-header]');
    const menu = header.getByRole('button', { name: 'Session actions', exact: true });
    await expect(menu).toBeVisible();
    await menu.click();
    await expect(page.getByRole('button', { name: 'Pin to top', exact: true })).toBeVisible();
    await page.keyboard.press('Escape');
    await header.getByRole('button', { name: 'Visualize', exact: true }).click();
    await expect(page.frameLocator('iframe[title="Show Page"]').getByRole('heading', { name: 'Authored Show' })).toBeVisible();
    if (device.includes('desktop')) {
      await expect(menu).toBeVisible();
      await menu.click();
      await expect(page.getByRole('button', { name: 'Pin to top', exact: true })).toBeVisible();
      await page.keyboard.press('Escape');
    } else {
      await expect(menu).toHaveCount(0);
      await expect(header.locator('.md\\:hidden').getByRole('button', { name: 'Session actions', includeHidden: true })).toHaveCount(0);
    }
    await expect(header.getByRole('button', { name: 'Annotate', exact: true })).toBeVisible();
    await expect(header.getByRole('button', { name: 'Share', exact: true })).toBeVisible();
    if (process.env.SHOW_CONTROLS_SHOTS) {
      await mkdir(process.env.SHOW_CONTROLS_SHOTS, { recursive: true });
      await page.screenshot({ path: `${process.env.SHOW_CONTROLS_SHOTS}/${device}-show.png` });
    }
    await header.getByRole('button', { name: 'Annotate', exact: true }).click();
    await expect(page.frameLocator('iframe[title="Show Page"]').locator('body')).toHaveAttribute('data-annotating', 'true');
    await page.getByRole('button', { name: 'Turn off annotation', exact: true }).click();
    await expect(page.frameLocator('iframe[title="Show Page"]').locator('body')).toHaveAttribute('data-annotating', 'false');
    await header.getByRole('button', { name: 'Back to chat', exact: true }).click();
    await expect(menu).toBeVisible();
    await menu.click();
    await expect(page.getByRole('button', { name: 'Pin to top', exact: true })).toBeVisible();
    expect(denied).toEqual([]);
  });
}
