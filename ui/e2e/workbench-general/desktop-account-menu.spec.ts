import { expect, test } from '../support/browserTest';
import { open, serveProduct } from './support';
import { clientState, signIn } from '../desktop-client/bridge-fixture';
import en from '../../src/i18n/en.json' with { type: 'json' };
import zh from '../../src/i18n/zh.json' with { type: 'json' };

// Production assets, a nonsecret shell projection and isolated protocol dispatch.
// The real URL producer/navigation guard run; no installed shell receives an intent.
for (const lang of ['en', 'zh'] as const) for (const theme of ['light', 'dark'] as const) {
  test(`AUTH-SETUP-178 production sidebar account menu ${lang} ${theme}`, async ({ page }, info) => {
    await page.setViewportSize({ width: 1200, height: 800 });
    const denied = await serveProduct(page, lang);
    const state = clientState(); signIn(state);
    state.local.slug = 'max';
    const presentations: string[] = [];
    await page.exposeFunction('__fixturePresent', (url: string) => presentations.push(url));
    let presentationBoundary = false;
    await page.route('**/assets/*.js', async (route) => {
      const response = await route.fetch();
      const body = await response.text();
      if (body.includes('avibe://desktop/') && body.includes('window.location.assign(')) {
        presentationBoundary = true;
        return route.fulfill({ response, body: body.replaceAll('window.location.assign(', 'window.__fixturePresent(') });
      }
      return route.fulfill({ response });
    });
    await page.addInitScript((snapshot) => {
      Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { value: true });
      Object.defineProperty(window, '__AVIBE_DESKTOP_CLIENT_VERSION__', { value: 1 });
      Object.defineProperty(window, '__AVIBE_DESKTOP_CLIENT_STATE__', { value: snapshot });
    }, state);
    await open(page, '/', { lang, theme });
    const copy = lang === 'en' ? en : zh;
    const trigger = page.locator('[data-account-trigger]');
    await expect(trigger).toContainText('max');
    await trigger.click();
    const menu = page.getByRole('menu').filter({ has: page.locator('[data-menu-instance]') });
    const change = menu.getByRole('menuitem', { name: copy.desktopClient.switchInstance });
    const signOut = menu.getByRole('menuitem', { name: copy.desktopClient.signOut });
    await expect(menu.locator('[data-menu-email]')).toHaveText('max@example.test');
    await expect(menu.locator('[data-menu-instance-status]')).toHaveText(copy.devices.reach.connected);
    await expect(change).toBeFocused();
    // Layout is measured after the opening animation; match the approved frame's geometry.
    await expect(menu).toHaveCSS('opacity', '1');
    const geometry = await menu.evaluate((node) => {
      const rect = node.getBoundingClientRect();
      const row = document.querySelector('[data-sidebar-status-row]')!.getBoundingClientRect();
      const instance = node.querySelector('[data-menu-instance]')!;
      const dot = instance.querySelector('[data-status-indicator]')!.getBoundingClientRect();
      const name = instance.children[1].children[0].getBoundingClientRect();
      const status = instance.children[1].children[1].getBoundingClientRect();
      const action = node.querySelector('[role="menuitem"]')!.children[1].getBoundingClientRect();
      return { width: rect.width, rowWidth: row.width, gap: row.top - rect.bottom,
        nameX: name.x, actionX: action.x, dotGap: name.x - dot.right, statusGap: status.x - name.right };
    });
    expect(geometry.width).toBe(geometry.rowWidth);
    expect(geometry.gap).toBeCloseTo(6, 0);
    expect(geometry.nameX).toBe(geometry.actionX);
    expect(geometry.dotGap).toBe(14);
    expect(geometry.statusGap).toBe(6);
    await page.mouse.move(700, 400);
    await page.screenshot({ path: info.outputPath('online.png'), animations: 'disabled' });
    await page.keyboard.press('ArrowUp');
    await expect(signOut).toBeFocused();
    await page.keyboard.press('ArrowDown');
    await expect(change).toBeFocused();
    await page.keyboard.press('Escape');
    await expect(menu).toBeHidden();
    await expect(trigger).toBeFocused();
    await trigger.press('Space');
    await expect(change).toBeFocused();
    await expect(menu).toHaveCSS('opacity', '1');
    await page.mouse.click(700, 400);
    await expect(menu).toBeHidden();
    await expect(trigger).toBeFocused();
    await trigger.press('Enter');
    await change.click();
    await expect(menu).toBeHidden();
    expect(presentationBoundary).toBe(true);
    expect(presentations).toEqual([`avibe://desktop/instances?locale=${lang}`]);
    await trigger.click();
    await signOut.click();
    await expect(menu).toBeHidden();
    expect(presentations).toEqual([`avibe://desktop/instances?locale=${lang}`, `avibe://desktop/sign-out?locale=${lang}`]);

    // One open menu follows live shell state, including a remote instance and long labels.
    await trigger.click();
    for (const connection of ['switching', 'remote_unreachable', 'remote_connected'] as const) {
      state.revision += 1;
      state.connection = { status: connection, committed: { kind: 'remote', name: 'Studio', instance_id: 'studio', canonical_origin: 'https://studio.example.test' }, pending: null };
      await page.evaluate((next) => window.dispatchEvent(new CustomEvent('avibe:desktop-client-state', { detail: next })), state);
      await expect(menu.locator('[data-menu-instance-status]')).toHaveText(connection === 'switching' ? copy.desktopClient.connecting : connection === 'remote_unreachable' ? copy.common.offline : copy.devices.reach.connected);
      await expect(menu.locator('[data-menu-instance]')).toContainText('Studio');
      await page.screenshot({ path: info.outputPath(`${connection}.png`), animations: 'disabled' });
    }
    await page.setViewportSize({ width: 880, height: 800 });
    state.revision += 1;
    state.connection.committed.name = 'research-and-product-planning-workstation';
    state.account.identity!.email = 'maximilian.product-research@example-company.test';
    await page.evaluate((next) => window.dispatchEvent(new CustomEvent('avibe:desktop-client-state', { detail: next })), state);
    await expect(menu.locator('[data-menu-email]')).toHaveText(state.account.identity!.email);
    const overflow = await menu.locator('[data-menu-instance]').evaluate((node) => {
      const name = node.children[1].children[0] as HTMLElement;
      const status = node.children[1].children[1] as HTMLElement;
      return { name: name.scrollWidth > name.clientWidth, ellipsis: getComputedStyle(name).textOverflow,
        status: status.scrollWidth <= status.clientWidth, fits: status.getBoundingClientRect().right <= node.getBoundingClientRect().right };
    });
    expect(overflow).toEqual({ name: true, ellipsis: 'ellipsis', status: true, fits: true });
    await page.screenshot({ path: info.outputPath('long-narrow.png'), animations: 'disabled' });
    await page.keyboard.press('Escape');
    await trigger.click();
    await change.click();
    expect(presentations.at(-1)).toBe(`avibe://desktop/instances?locale=${lang}`);
    state.revision += 1; state.account.status = 'signed_out'; state.account.identity = null;
    await page.evaluate((next) => window.dispatchEvent(new CustomEvent('avibe:desktop-client-state', { detail: next })), state);
    await expect(trigger).toContainText(copy.desktopClient.signIn);
    await trigger.click();
    await expect(menu).toBeHidden();
    expect(presentations.at(-1)).toBe(`avibe://desktop/account?locale=${lang}`);
    expect(denied).toEqual([]);
  });
}
