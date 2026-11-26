import { expect, test } from '../support/browserTest';
import { clientState, installBridge, signIn } from './bridge-fixture';

const ACCOUNT = 'http://127.0.0.1:5214/account/index.html';
for (const [lang, theme, width, height] of [['en', 'light', 880, 600], ['en', 'dark', 880, 600], ['zh', 'light', 880, 600], ['zh', 'dark', 880, 600]] as const) {
  test(`feedback2 current shared group, offline keyboard, account and guide ${lang} ${theme}`, async ({ page }, info) => {
    await page.setViewportSize({ width, height });
    const denied: string[] = [];
    await page.route('**/*', (route) => {
      if (new URL(route.request().url()).origin === new URL(ACCOUNT).origin && route.request().method() === 'GET') return route.continue();
      denied.push(route.request().url()); return route.abort();
    });
    await page.addInitScript((lng) => localStorage.setItem('i18nextLng', lng), lang);
    const state = clientState(); signIn(state);
    state.remote_auxiliary = 'disabled_incomplete';
    state.account.identity!.email = 'international-design-and-engineering-account@cross-region-research-and-development-workstations.example.test';
    state.local.remote_access = 'on'; state.local.remote_access_url = 'https://local-fixture-app.avibe.bot';
    state.catalog.instances[1].available = true;
    state.catalog.instances[1].name = 'Shared design and engineering workstation · 跨时区产品研发团队';
    state.catalog.instances.push({ ...state.catalog.instances[1], id: 'offline-team', available: false, name: 'Offline shared lab', host: 'a-long-shared-workstation-domain-app.avibe.bot' });
    state.connection = { status: 'remote_connected', committed: { kind: 'remote', name: state.catalog.instances[1].name, instance_id: 'team', canonical_origin: state.catalog.instances[1].url }, pending: null };
    state.current_instance = { ...state.catalog.instances[1] };
    const requests = await installBridge(page, state, { signOutError: 'cleanup_error' });
    await page.goto(`${ACCOUNT}?view=instances&theme=${theme}`);
    const current = page.getByRole('region', { name: lang === 'en' ? 'Currently using' : '正在使用' });
    await expect(current.locator('[data-instance-icon="shared"]')).toBeVisible();
    await expect(page.getByText(state.catalog.instances[1].name, { exact: true })).toHaveCount(1);
    const others = page.locator('[data-other-instances]');
    await expect(others.locator('[data-instance-id]')).toHaveCount(3);
    const offline = page.locator('[data-instance-id="offline-team"]');
    await expect(offline.getByRole('button')).toBeDisabled();
    await expect(offline).toContainText(lang === 'en' ? 'Shared by' : '共享');
    await expect(offline).toContainText(lang === 'en' ? 'Offline' : '离线');
    await expect(offline.locator('.lucide-chevron-right')).toHaveCount(0);
    await page.getByRole('button', { name: lang === 'en' ? 'Refresh instances' : '刷新实例' }).focus();
    for (let i = 0; i < 8; i += 1) { await page.keyboard.press('Tab'); await expect(offline.getByRole('button')).not.toBeFocused(); }
    expect(requests.filter((r) => r.action.type === 'select_instance')).toEqual([]);
    await expect(page.getByText(/Mini and native notifications are unavailable|第一阶段暂不支持远程实例/)).toHaveCount(0);
    await expect(page.locator('footer')).toHaveCount(0);
    await page.screenshot({ animations: 'disabled', path: info.outputPath('shared-current.png') });
    const entry = page.getByRole('region', { name: lang === 'en' ? 'Account' : '账号', exact: true });
    const email = entry.getByText(state.account.identity!.email, { exact: true });
    await entry.scrollIntoViewIfNeeded();
    await expect(email).toBeVisible();
    await expect(entry.locator('.lucide-chevron-right')).toHaveCount(0);
    const geometry = await email.evaluate((text) => ({
      textHeight: text.getBoundingClientRect().height, lineHeight: parseFloat(getComputedStyle(text).lineHeight),
      overflow: text.scrollWidth > text.clientWidth,
    }));
    expect(geometry.textHeight).toBeGreaterThan(geometry.lineHeight * 1.5);
    expect(geometry.overflow).toBe(false);
    await email.click();
    await expect(page.locator('[data-account-details]')).toHaveCount(0);
    await page.screenshot({ animations: 'disabled', path: info.outputPath('account-card-email.png') });
    // Account remains an admitted native route with its binding information and Back action.
    await page.goto(`${ACCOUNT}?view=account&theme=${theme}`);
    await expect(page.locator('[data-account-details]')).toBeVisible();
    // A legacy projection URL is not a native binding observation.
    await expect(page.getByRole('link', { name: state.local.remote_access_url })).toHaveCount(0);
    await page.getByRole('button', { name: lang === 'en' ? 'Instances' : '实例', exact: true }).click();
    await page.getByRole('button', { name: lang === 'en' ? 'Add instance' : '添加实例' }).click();
    await expect(page.getByText('vibe remote')).toBeVisible();
    await expect(page.getByRole('link', { name: lang === 'en' ? 'Open installation guide' : '打开安装指南' })).toHaveAttribute('href', 'https://docs.avibe.bot/get-started/install');
    await page.screenshot({ animations: 'disabled', path: info.outputPath('add-guide.png') });
    await page.getByRole('button', { name: lang === 'en' ? 'Return and refresh' : '返回并刷新' }).click();
    await expect.poll(() => requests.at(-1)?.action.type).toBe('refresh_instances');
    expect(requests.filter((r) => r.action.type === 'sign_out')).toHaveLength(0);
    await page.getByRole('button', { name: lang === 'en' ? 'Sign out on this device' : '在此设备退出' }).click();
    await expect(page.getByRole('alert')).toBeVisible();
    await page.getByRole('button', { name: lang === 'en' ? 'Retry' : '重试' }).click();
    await expect.poll(() => requests.filter((r) => r.action.type === 'sign_out').length).toBe(2);
    expect(denied).toEqual([]);
  });
}

for (const [lang, theme, width, height] of [['en', 'light', 1200, 800], ['zh', 'dark', 880, 600]] as const) {
  test(`feedback2 password link has themed underline and real icon spacing ${lang} ${theme}`, async ({ page }, info) => {
    await page.setViewportSize({ width, height });
    await page.route('**/*', (route) => new URL(route.request().url()).origin === new URL(ACCOUNT).origin ? route.continue() : route.abort());
    await page.addInitScript((lng) => localStorage.setItem('i18nextLng', lng), lang);
    await installBridge(page, clientState());
    await page.goto(`${ACCOUNT}?theme=${theme}`);
    await page.getByRole('button', { name: lang === 'en' ? 'Continue with email' : '使用邮箱继续' }).click();
    const link = page.getByRole('button', { name: lang === 'en' ? 'Sign in with password' : '使用密码登录' });
    await expect(link).toHaveCSS('text-decoration-line', 'underline');
    const style = await link.evaluate((node) => ({ gap: parseFloat(getComputedStyle(node).columnGap), color: getComputedStyle(node).color, foreground: getComputedStyle(document.querySelector('main')!).color }));
    expect(style.gap).toBeGreaterThanOrEqual(8); await expect(link).toHaveCSS('color', style.foreground);
    await link.click(); await expect(page.locator('#mobile-login-password')).toBeVisible();
    await page.screenshot({ animations: 'disabled', path: info.outputPath('password-link.png') });
  });
}
