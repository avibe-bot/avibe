import { expect, test, type Page } from '@playwright/test';
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

for (const desktop of [false, true]) {
  test(`${desktop ? 'native' : 'web'} chat retains the complete toolbar and confines native drag targets to the top`, async ({ page }) => {
    const denied = await serveChat(page);
    if (desktop) {
      await page.addInitScript(() => {
        Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { value: true });
        Object.defineProperty(window, '__AVIBE_DESKTOP_DRAG__', { value: true });
        document.addEventListener('DOMContentLoaded', () => document.documentElement.style.setProperty('--shell-titlebar-inset', '28px'));
      });
    }
    await page.setViewportSize({ width: 1200, height: 800 });
    await open(page, '/chat/chrome-fixture');
    const header = page.locator('[data-chat-header]');
    await expect(header).toBeVisible();
    await expect(header.getByRole('button', { name: '中文窗口会话' })).toBeVisible();
    await expect(header.getByRole('button', { name: /gpt-fixture/ })).toBeVisible();
    await expect(header.getByRole('button', { name: 'Visualize', exact: true })).toBeVisible();
    await expect(header.getByRole('button', { name: 'Back', exact: true })).toBeVisible();
    await expect(header).toHaveCount(1);

    if (desktop) {
      await expect(page.locator('[data-desktop-window-chrome] [data-chat-header]')).toHaveCount(1);
      await expect(page.locator('main [data-chat-header]')).toHaveCount(0);
      const bounds = await page.locator('[data-desktop-window-chrome]').boundingBox();
      expect(bounds).toEqual({ x: 0, y: 0, width: 1200, height: 48 });
      // Every rendered drag surface is confined to the same top row, including
      // the sidebar clearance. The transcript and composer never join it.
      for (const region of await page.locator('[data-tauri-drag-region]').all()) {
        const rect = await region.boundingBox();
        if (rect) {
          expect(rect.y).toBeGreaterThanOrEqual(0);
          expect(rect.y + rect.height).toBeLessThanOrEqual(48);
        }
        await expect(region).toHaveAttribute('data-tauri-drag-region', '');
      }
    } else {
      await expect(page.locator('[data-desktop-window-chrome]')).toHaveCount(0);
      await expect(page.locator('main [data-chat-header]')).toHaveCount(1);
      await expect(page.locator('[data-tauri-drag-region]')).toHaveCount(0);
    }
    // Native relocation keeps the web toolbar's content bounds, including at
    // widths where both the transcript and composer stop growing.
    for (const width of [880, 2000, 1200]) {
      await page.setViewportSize({ width, height: 800 });
      const toolbar = await header.locator(':scope > div').boundingBox();
      const composer = await page.locator('main div.mx-auto').filter({ has: page.getByRole('textbox') }).last().boundingBox();
      expect(toolbar).not.toBeNull();
      expect(composer).not.toBeNull();
      expect(toolbar!.x).toBeCloseTo(composer!.x, 0);
      expect(toolbar!.width).toBeCloseTo(composer!.width, 0);
      await expect(header.getByRole('button', { name: /gpt-fixture/ })).toBeVisible();
      await expect(header.getByRole('button', { name: 'Visualize', exact: true })).toBeVisible();
      if (width === 2000) {
        await page.screenshot({ path: `e2e/.artifacts/workbench-general/shots/chat-chrome-${desktop ? 'native' : 'web'}-wide.png` });
      }
    }
    // Relocation must preserve live title editing and the real route picker.
    await header.getByRole('button', { name: '中文窗口会话' }).click();
    await expect(header.locator('input')).toBeFocused();
    await header.locator('input').press('Escape');
    await header.getByRole('button', { name: /gpt-fixture/ }).click();
    await expect(page.getByRole('dialog')).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(page.getByRole('dialog')).toBeHidden();
    await page.mouse.move(1000, 400);
    await page.screenshot({ path: `e2e/.artifacts/workbench-general/shots/chat-chrome-${desktop ? 'native' : 'web'}.png` });

    // The retained ChatPage behind Settings must not leak a live portaled header.
    await page.locator('[data-settings-toggle]').click();
    await expect(page.locator('nav[aria-label="Settings sections"]')).toBeVisible();
    if (desktop) await expect(page.locator('[data-desktop-window-chrome]')).toBeHidden();
    await page.keyboard.press('Escape');
    await expect(header).toBeVisible();

    // The shell owns sidebar clearance independently of the route's toolbar.
    // Navigate through the actual brand link and browser history: the logo and
    // navigation must stay in place when the chat portal unmounts and returns.
    const sidebar = page.locator('aside').filter({ has: page.locator('#workbench-capability-nav') });
    const brand = sidebar.locator('a[href="/"]');
    const inbox = sidebar.getByRole('link', { name: 'Inbox', exact: true });
    const nav = sidebar.locator('#workbench-capability-nav');
    const chatSidebar = {
      brand: await brand.boundingBox(),
      inbox: await inbox.boundingBox(),
      nav: await nav.boundingBox(),
    };
    expect(chatSidebar.brand).not.toBeNull();
    expect(chatSidebar.inbox).not.toBeNull();
    expect(chatSidebar.nav).not.toBeNull();
    await brand.click();
    await expect(page).toHaveURL(/\/$/);
    await expect(header).toHaveCount(0);
    expect(await brand.boundingBox()).toEqual(chatSidebar.brand);
    expect(await inbox.boundingBox()).toEqual(chatSidebar.inbox);
    expect(await nav.boundingBox()).toEqual(chatSidebar.nav);
    if (desktop) {
      const topRow = sidebar.locator(':scope > [data-tauri-drag-region]');
      const topBounds = await topRow.boundingBox();
      expect(topBounds?.y).toBe(0);
      expect(topBounds?.height).toBe(48);
      expect(topBounds?.width).toBe((await sidebar.boundingBox())?.width);
      await expect(topRow).toHaveCSS('border-bottom-width', '1px');
      const brandMark = brand.locator('img');
      const brandMarkBounds = await brandMark.boundingBox();
      expect(brandMarkBounds?.x).toBeCloseTo((await sidebar.boundingBox())!.x + 16, 0);
      expect(brandMarkBounds?.y).toBeCloseTo(topBounds!.y + topBounds!.height + 16, 0);
      // There is no whole-window empty chrome added to the home page.
      await expect(page.locator('[data-desktop-window-chrome]')).toHaveCount(0);
    } else {
      await expect(sidebar.locator('[data-tauri-drag-region]')).toHaveCount(0);
      expect(chatSidebar.brand!.y).toBe(10);
    }
    await page.mouse.move(1000, 400);
    await page.screenshot({ path: `e2e/.artifacts/workbench-general/shots/home-chrome-${desktop ? 'native' : 'web'}.png` });
    await page.goBack();
    await expect(header).toBeVisible();
    expect(await brand.boundingBox()).toEqual(chatSidebar.brand);
    expect(await inbox.boundingBox()).toEqual(chatSidebar.inbox);
    expect(await nav.boundingBox()).toEqual(chatSidebar.nav);
    expect(denied).toEqual([]);
  });
}
