import { expect, test } from '@playwright/test';
import { open, serveProduct } from './support';

// Real AppShell -> Dock -> WindowLayer -> AppWindow -> ShowPageApp, with only
// HTTP replies replaced. The frame is test-owned unsaved data, not a real file.
const sid = 'ses-c-foreground';
const title = '星河未保存页面';
const draft = '保留这段未保存内容 🌱';
const pagePayload = {
  session_id: sid, title, visibility: 'private', active_url: `/show/${sid}/`,
  url_available: true, offline: false, icon_version: null,
};
const frameHtml = `<!doctype html><html><head><meta charset="utf-8"></head><body>
<label>Unsaved buffer<textarea id="buffer"></textarea></label>
<script>
window.instanceId = crypto.randomUUID();
window.commands = [];
let enabled = false;
function report() {
  parent.postMessage({ type: 'avibe:annotation:state', enabled, mode: 'smart', available: true }, location.origin);
}
addEventListener('message', (event) => {
  if (event.origin !== location.origin) return;
  const data = event.data;
  if (data.type === 'avibe:annotation:query') report();
  if (data.type === 'avibe:annotation:control') {
    window.commands.push(data.action);
    if (data.action === 'enable') enabled = true;
    if (data.action === 'disable') enabled = false;
    report();
  }
});
report();
</script></body></html>`;

for (const chrome of ['Annotate', 'Share']) {
  test(`C-SETTINGS-04: retains the real app frame and suspends ${chrome} portals and chords`, async ({ page }) => {
    const denied = await serveProduct(page);
    await page.route('**/api/dock', (route) => route.fulfill({ json: {
      ok: true, dock: { order: [`show:${sid}`, 'library'], pins: [{
        session_id: sid, title_snapshot: title, pinned_at: '2026-09-19T00:00:00Z',
      }] },
    } }));
    await page.route('**/api/show-pages', (route) => route.fulfill({ json: { pages: [pagePayload] } }));
    await page.route(`**/api/show-pages/${sid}`, (route) => route.fulfill({ json: pagePayload }));
    await page.route(`**/api/show-pages/${sid}/access`, (route) => route.fulfill({ json: {
      ok: true, mode: 'unmanaged', ownership_status: 'unmanaged', access_level: 'private',
      can_use: true, can_manage: false, can_publish_public: false, group_ids: [],
    } }));
    await page.route(`**/api/sessions/${sid}`, (route) => route.fulfill({ json: {
      id: sid, title, status: 'active', project_id: 'proj-1',
    } }));
    await page.route(`**/show/${sid}/**`, (route) => route.fulfill({ contentType: 'text/html', body: frameHtml }));
    await page.setViewportSize({ width: 1366, height: 900 });
    await open(page, '/');
    const apps = page.getByRole('button', { name: 'Apps', exact: true });
    await apps.click();
    await page.getByRole('button', { name: title, exact: true }).click();
    const app = page.locator('[data-window-id]').first();
    await expect(app).toBeVisible();
    const frame = page.frameLocator('iframe[title="Show Page"]');
    await frame.getByRole('textbox', { name: 'Unsaved buffer' }).fill(draft);
    const iframe = page.locator('iframe[title="Show Page"]');
    const frameState = () => iframe.evaluate((node) => {
      const win = (node as HTMLIFrameElement).contentWindow as Window & { instanceId: string; commands: string[] };
      return { id: win.instanceId, commands: win.commands };
    });
    const original = await frameState();
    await app.getByRole('button', { name: chrome, exact: true }).click();
    const portal = page.locator('[data-window-owner-id]');
    await expect(portal).toBeVisible();
    const id = await app.getAttribute('data-window-id');
    await page.locator('aside [data-settings-toggle]').click();
    const settings = page.locator('[data-settings-overlay]');
    await expect(settings).toBeVisible();
    await expect(app).toBeHidden();
    await expect(app).toHaveAttribute('inert', '');
    await expect(portal).toHaveCount(0);
    await expect(app).toHaveAttribute('aria-hidden', 'true');
    await expect(apps).toHaveCount(0);
    const before = await frameState();
    // Programmatic frame delivery verifies the listener itself is suspended;
    // real user input cannot reach this hidden/inert frame in the first place.
    await iframe.evaluate((node) => {
      const win = (node as HTMLIFrameElement).contentWindow!;
      for (const code of ['KeyX', 'KeyW']) {
        win.dispatchEvent(new KeyboardEvent('keydown', { code, altKey: true, bubbles: true, cancelable: true }));
      }
    });
    for (const chord of ['Alt+2', 'Alt+w', 'Control+m', 'Control+w']) {
      // dispatchEvent avoids invoking the browser's own tab-close reservation.
      await settings.dispatchEvent('keydown', {
        key: chord.slice(-1), code: chord.endsWith('2') ? 'Digit2' : `Key${chord.slice(-1).toUpperCase()}`,
        altKey: chord.startsWith('Alt'), ctrlKey: chord.startsWith('Control'), bubbles: true,
      });
    }
    expect(await frameState()).toEqual(before);
    expect(before.id).toBe(original.id);
    await expect(page.locator('[data-window-id]')).toHaveCount(1);
    const unloadGuard = await page.evaluate(() => !window.dispatchEvent(new Event('beforeunload', { cancelable: true })));
    expect(unloadGuard).toBe(true);
    await settings.getByRole('button', { name: 'Close Settings' }).click();
    await expect(app).toBeVisible();
    await expect(app).toHaveAttribute('data-window-id', id!);
    await expect(frame.getByRole('textbox', { name: 'Unsaved buffer' })).toHaveValue(draft);
    expect((await frameState()).id).toBe(original.id);
    await expect(page.locator('aside [data-settings-toggle]')).toBeFocused();
    await app.getByRole('button', { name: chrome, exact: true }).click();
    await expect(portal).toBeVisible();
    expect(denied).toEqual([]);
  });
}
