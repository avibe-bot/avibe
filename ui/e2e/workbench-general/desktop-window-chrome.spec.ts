import { expect, test, type Page } from '@playwright/test';
import { open, serveProduct } from './support';

// The neutral overlay and the aside's resizer used to paint two lines at
// different stacking levels. Unit gesture tests cannot detect chrome covering
// the top hit area or hiding the active border.
for (const theme of ['light', 'dark'] as const) {
test(`native sidebar divider owns full-height hit testing and all visual states (${theme})`, async ({ page }) => {
  const denied = await serveChat(page);
  await page.addInitScript(() => {
    Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { value: true });
    Object.defineProperty(window, '__AVIBE_DESKTOP_DRAG__', { value: true });
    document.addEventListener('DOMContentLoaded', () => document.documentElement.style.setProperty('--shell-titlebar-inset', '28px'));
  });
  await page.setViewportSize({ width: 1200, height: 800 });
  await open(page, '/agents', { theme });
  const agents = page.getByRole('link', { name: 'Agents', exact: true });
  await expect(agents).toBeVisible();
  const agentsBorder = await agents.evaluate((element) => getComputedStyle(element).borderRightColor);
  await open(page, '/chat/chrome-fixture', { theme });
  const edge = page.getByRole('separator', { name: 'Resize sidebar' });
  await expect(edge).toBeVisible();
  const sidebar = page.locator('aside').filter({ has: page.locator('#workbench-capability-nav') });
  const verifyEdge = async () => {
    const side = (await sidebar.boundingBox())!;
    const bounds = (await edge.boundingBox())!;
    expect(bounds.y).toBe(0);
    expect(bounds.height).toBe(800);
    expect(bounds.width).toBe(8);
    expect(await edge.evaluate((element) => {
      const r = element.getBoundingClientRect();
      return [1, 27, 47, 49, innerHeight - 1].map(y =>
        document.elementFromPoint(r.right - 0.5, y) === element);
    })).toEqual([true, true, true, true, true]);
    expect(bounds.x + bounds.width).toBe(side.x + side.width);
    return bounds;
  };
  let bounds = await verifyEdge();
  await expect(sidebar).toHaveCSS('border-right-width', '0px');
  await expect(page.locator('[data-sidebar-divider]')).toHaveCount(0);
  await expect(edge).toHaveCSS('border-right-width', '1px');
  const neutral = await edge.evaluate(el => getComputedStyle(el).borderRightColor);
  expect(neutral).not.toBe(agentsBorder);
  await page.mouse.move(bounds.x + 4, 400);
  await expect(edge).toHaveCSS('border-right-color', agentsBorder);
  await verifyEdge();
  await page.screenshot({ path: 'e2e/.artifacts/workbench-general/shots/divider-hover.png' });
  await page.mouse.move(bounds.x + 4, 1);
  await expect(edge).toHaveCSS('border-right-color', agentsBorder);
  await page.mouse.move(1000, 400);
  await expect(edge).toHaveCSS('border-right-color', neutral);

  // Keyboard focus colors the same border; blur restores it.
  await page.keyboard.press('Tab');
  await edge.focus();
  expect(await edge.evaluate(el => el.matches(':focus-visible'))).toBe(true);
  await expect(edge).toHaveCSS('border-right-color', agentsBorder);
  await page.keyboard.press('ArrowRight');
  bounds = await verifyEdge();
  await page.getByRole('button', { name: 'Back', exact: true }).focus();
  await expect(edge).toHaveCSS('border-right-color', neutral);

  // Start at the top chrome, then move beyond the width clamp: captured input
  // keeps the entire edge active even while the pointer is far outside it.
  await page.mouse.move(bounds.x + 4, 1);
  await page.mouse.down();
  await expect(edge).toHaveAttribute('data-dragging', 'true');
  await page.mouse.move(1000, 400);
  await expect(edge).toHaveCSS('border-right-color', agentsBorder);
  bounds = await verifyEdge();
  expect(bounds.x + bounds.width).toBe(496);
  await page.mouse.up();
  await expect(edge).not.toHaveAttribute('data-dragging');
  await expect(edge).toHaveCSS('border-right-color', neutral);

  // Cancellation uses existing gesture cleanup, not another hover-state writer.
  await page.mouse.move(bounds.x + 4, 400);
  await page.mouse.down();
  await page.mouse.move(1000, 400);
  await edge.dispatchEvent('pointercancel', { pointerId: 1 });
  await expect(edge).not.toHaveAttribute('data-dragging');
  await page.mouse.up();
  await page.mouse.move(1001, 400);
  await expect(edge).toHaveCSS('border-right-color', neutral);
  await page.screenshot({ path: 'e2e/.artifacts/workbench-general/shots/divider-neutral.png' });

  // Only the narrow resize strip sits above chrome, not controls or empty drag.
  expect(await page.evaluate(() => document.elementFromPoint(140, 1)?.hasAttribute('data-tauri-drag-region'))).toBe(true);
  for (const name of ['Back', 'Visualize']) {
    const button = page.getByRole('button', { name, exact: true });
    expect(await button.evaluate(el => {
      const r = el.getBoundingClientRect();
      return el.contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2));
    })).toBe(true);
  }
  await page.evaluate(() => {
    localStorage.setItem('avibe.settings.menu-placement.v1', 'inline');
    window.dispatchEvent(new Event('avibe:settings-menu-placement-changed'));
  });
  await page.locator('[data-settings-toggle]').click();
  await expect(page.locator('nav[aria-label="Settings sections"]')).toBeVisible();
  await page.mouse.move(bounds.x + 4, 1);
  await page.mouse.down();
  await page.mouse.move(bounds.x - 12, 100);
  await page.mouse.up();
  await expect(page.locator('nav[aria-label="Settings sections"]')).toBeVisible();
  await verifyEdge();
  await page.keyboard.press('Escape');
  await sidebar.locator('a[href="/"]').click();
  await expect(page.locator('[data-chat-header]')).toHaveCount(0);
  bounds = await verifyEdge();
  await page.mouse.move(bounds.x + 4, 1);
  await expect(edge).toHaveCSS('border-right-color', agentsBorder);
  await page.setViewportSize({ width: 700, height: 800 });
  await expect(edge).toHaveCount(0);
  await page.setViewportSize({ width: 1200, height: 800 });
  await page.evaluate(() => localStorage.setItem('avibe.settings.menu-placement.v1', 'standalone'));
  await open(page, '/settings/general', { theme: 'light' });
  await expect(page.locator('[data-sidebar-resizer]')).toBeHidden();
  expect(denied).toEqual([]);
});
}

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
    await expect(header.getByRole('button', { name: 'Visualize', exact: true })).toBeVisible();
    await expect(header.getByRole('button', { name: 'Back', exact: true })).toBeVisible();
    await expect(header).toHaveCount(1);
    // Desktop carries the session menu right after the title — not among the
    // page-level controls on the right.
    const sessionMenu = header.getByRole('button', { name: 'Session actions', exact: true });
    await expect(sessionMenu).toHaveCount(1);
    await expect(sessionMenu).toBeVisible();
    const titleBox = await header.getByRole('button', { name: '中文窗口会话' }).boundingBox();
    const menuBox = await sessionMenu.boundingBox();
    const visualizeBox = await header.getByRole('button', { name: 'Visualize', exact: true }).boundingBox();
    expect(menuBox!.x).toBeGreaterThanOrEqual(titleBox!.x + titleBox!.width - 1);
    expect(menuBox!.x + menuBox!.width).toBeLessThan(visualizeBox!.x);
    // The route picker answers "who replies to the next message", so it lives in
    // the composer's action row, below the text — never in the header.
    const routePicker = page.getByRole('button', { name: /gpt-fixture/ });
    await expect(header.getByRole('button', { name: /gpt-fixture/ })).toHaveCount(0);
    await expect(routePicker).toHaveCount(1);
    const textboxBox = await page.getByRole('textbox').last().boundingBox();
    const pickerBox = await routePicker.boundingBox();
    expect(pickerBox!.y).toBeGreaterThanOrEqual(textboxBox!.y + textboxBox!.height - 1);

    if (desktop) {
      await expect(page.locator('[data-desktop-window-chrome] [data-chat-header]')).toHaveCount(1);
      await expect(page.locator('main [data-chat-header]')).toHaveCount(0);
      const bounds = await page.locator('[data-desktop-window-chrome]').boundingBox();
      expect(bounds).toEqual({ x: 0, y: 0, width: 1200, height: 48 });
      // Native clearance remains 28px; toolbar empty surfaces use its independent
      // 48px height. The transcript and composer never join either drag region.
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
      await expect(routePicker).toBeVisible();
      await expect(header.getByRole('button', { name: 'Visualize', exact: true })).toBeVisible();
      if (width === 2000) {
        await page.screenshot({ path: `e2e/.artifacts/workbench-general/shots/chat-chrome-${desktop ? 'native' : 'web'}-wide.png` });
      }
    }
    // Relocation must preserve live title editing and the real route picker.
    await header.getByRole('button', { name: '中文窗口会话' }).click();
    await expect(header.locator('input')).toBeFocused();
    await header.locator('input').press('Escape');
    await routePicker.click();
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
      expect(topBounds?.height).toBe(28);
      expect(topBounds?.width).toBe((await sidebar.boundingBox())?.width);
      await expect(topRow).toHaveCSS('border-bottom-width', '0px');
      await expect(sidebar).toHaveCSS('border-right-width', '0px');
      await expect(page.getByRole('separator', { name: 'Resize sidebar' })).toHaveCSS('border-right-width', '1px');
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
