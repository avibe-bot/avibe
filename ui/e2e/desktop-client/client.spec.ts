import { expect, test } from '../support/browserTest';
import { serveProduct } from '../workbench-general/support';
import { clientState, installBridge, signIn } from './bridge-fixture';
import type { Page } from '@playwright/test';

const ACCOUNT = 'http://127.0.0.1:5214/account/index.html';
async function isolateAccount(page: Page) {
  const denied: string[] = [];
  await page.route('**/*', (route) => {
    const url = new URL(route.request().url());
    if (url.origin === 'http://127.0.0.1:5214' && route.request().method() === 'GET') return route.continue();
    denied.push(url.origin + url.pathname);
    return route.abort();
  });
  return denied;
}
async function language(page: Page, lang: string) {
  await page.addInitScript((lng) => localStorage.setItem('i18nextLng', lng), lang);
}

for (const viewport of [{ width: 1200, height: 800 }, { width: 880, height: 600 }]) {
  for (const lang of ['en', 'zh'] as const) for (const theme of ['light', 'dark']) {
    test(`AUTH-SETUP-178 real AppShell ${viewport.width} ${lang} ${theme}`, async ({ page }, info) => {
      await page.setViewportSize(viewport);
      const denied = await serveProduct(page, lang);
      const state = clientState(); signIn(state);
      // Exercise the original local updater. Remote presentation-only behavior has
      // its own footer test and never receives this native updater IPC fixture.
      await page.route('**/api/version**', (route) => route.fulfill({ json: { current: state.desktop.version, latest: '3.2.7', has_update: true, managed_by: 'desktop' } }));
      state.remote_auxiliary = 'disabled_incomplete';
      const requests = await installBridge(page, state, { projection: true });
      await language(page, lang);
      await page.goto(`/?theme=${theme}`);
      const avatar = page.locator('[data-account-avatar]');
      await expect(avatar).toBeVisible();
      const footer = page.locator('[data-sidebar-footer]');
      const measurements = await footer.evaluate((node) => {
        const rect = (selector: string) => node.querySelector(selector)!.getBoundingClientRect();
        return { avatar: rect('[data-account-avatar]').x, tools: node.firstElementChild!.getBoundingClientRect().x,
          dotGap: rect('[data-status-indicator]').x - rect('[data-account-label]').right,
          arrowGap: rect('[data-account-chevron]').x - rect('[data-status-indicator]').right,
          versionRight: rect('[data-version-trigger]').right, footerRight: node.getBoundingClientRect().right };
      });
      expect(measurements.avatar).toBe(16);
      expect(measurements.tools).toBe(16);
      expect(measurements.dotGap).toBeLessThanOrEqual(6);
      expect(measurements.arrowGap).toBeLessThanOrEqual(6);
      expect(measurements.versionRight).toBe(measurements.footerRight);
      await expect(footer).not.toContainText('max@example.test');
      await page.locator('[data-version-trigger] button').click();
      const popup = page.getByRole('dialog', { name: lang === 'en' ? 'Version & Update' : '版本与更新' });
      await expect(popup).toBeVisible();
      const box = await popup.boundingBox();
      expect(box!.x).toBeGreaterThanOrEqual(0); expect(box!.y).toBeGreaterThanOrEqual(0); expect(box!.x + box!.width).toBeLessThanOrEqual(viewport.width);
      await expect(popup).toContainText('3.2.6rc9'); await expect(popup).not.toContainText('2.4.0');
      await page.screenshot({ animations: 'disabled', path: info.outputPath('update.png') });
      await page.keyboard.press('Escape');
      await expect(page.locator('[data-version-trigger] button')).toBeFocused();
      await page.locator('[data-account-trigger]').click();
      await expect(page.locator('[data-instance-menu]')).toContainText('max@example.test');
      await expect(page.locator('[data-instance-menu]')).toContainText(lang === 'en' ? 'Phase 1' : '第一阶段');
      await expect(popup).toHaveCount(0);
      await expect(page.locator('[data-update-menu]')).toHaveCount(0);
      await expect(page.locator('[data-instance-menu]')).toHaveCSS('opacity', '1');
      await page.screenshot({ animations: 'disabled', path: info.outputPath('account-menu.png') });
      await page.keyboard.press('Escape');
      await expect(page.locator('[data-account-trigger]')).toBeFocused();
      expect(requests).toEqual([]); expect(denied).toEqual([]);
    });
    test(`AUTH-SETUP-177 bundled login ${viewport.width} ${lang} ${theme}`, async ({ page }, info) => {
      await page.setViewportSize(viewport);
      const denied = await isolateAccount(page); await language(page, lang);
      const requests = await installBridge(page, clientState());
      await page.goto(`${ACCOUNT}?theme=${theme}`);
      const field = page.locator('#desktop-auth-field');
      await expect(field).toBeVisible();
      await page.screenshot({ animations: 'disabled', path: info.outputPath('email.png') });
      await field.fill('max@example.test');
      await page.getByRole('button', { name: lang === 'en' ? 'Send verification code' : '发送验证码' }).click();
      await expect(page.getByRole('heading', { name: lang === 'en' ? 'Check your inbox' : '查看你的邮箱' })).toBeVisible();
      await field.fill('123456');
      await page.getByRole('button', { name: lang === 'en' ? 'Verify and sign in' : '验证并登录' }).click();
      await expect(page.getByRole('heading', { name: lang === 'en' ? 'This Mac is now in your account' : '本机已加入你的账号' })).toBeVisible();
      await page.getByRole('button', { name: lang === 'en' ? 'View instances' : '查看实例', exact: true }).click();
      await expect(page.getByText('工作室 Studio', { exact: true })).toBeVisible();
      await expect(page.getByText('共享团队', { exact: true })).toBeVisible();
      await page.screenshot({ animations: 'disabled', path: info.outputPath('instances.png') });
      await page.getByRole('button', { name: /工作室 Studio/ }).click();
      await expect(page.getByRole('heading', { name: /工作室 Studio/ })).toBeVisible();
      await page.screenshot({ animations: 'disabled', path: info.outputPath('consent.png') });
      await page.getByRole('button', { name: lang === 'en' ? 'Allow and connect' : '允许并连接' }).click();
      await page.keyboard.press('Escape');
      await expect.poll(() => requests.at(-1)?.action.type).toBe('cancel');
      expect(requests.some((request) => request.action.type === 'select_instance' && Object.keys(request.action).join(',') === 'type,instance_id')).toBe(true);
      expect(denied).toEqual([]);
    });
  }
}

test('AUTH-SETUP-171 direct TOTP enrollment and recovery material stays transient', async ({ page }, info) => {
  await isolateAccount(page); await language(page, 'en');
  const state = clientState();
  state.account = { ...state.account, status: 'second_factor_required', gate: 'enroll' };
  const requests = await installBridge(page, state);
  await page.goto(ACCOUNT);
  await page.getByRole('button', { name: 'Add authenticator' }).click();
  await expect(page.getByText('JBSWY3DPEHPK3PXP', { exact: true })).toBeVisible();
  await page.screenshot({ animations: 'disabled', path: info.outputPath('totp-enrollment.png') });
  await page.getByLabel('Authenticator code', { exact: true }).fill('654321');
  await page.getByRole('button', { name: 'Verify and enable' }).click();
  await expect(page.getByRole('heading', { name: 'Save your recovery codes' })).toBeVisible();
  await page.screenshot({ animations: 'disabled', path: info.outputPath('recovery-codes.png') });
  await page.getByRole('button', { name: 'I have saved these codes' }).click();
  await expect(page.getByText('fixture-recovery-one')).toHaveCount(0);
  expect(await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage }))).not.toContain('fixture-recovery');
  expect(JSON.stringify(state)).not.toContain('JBSWY');
  expect(requests.find((request) => request.action.type === 'finish_totp_enrollment')?.action).toEqual({ type: 'finish_totp_enrollment', code: '654321' });
});

for (const status of ['conflict', 'failed', 'paired_tunnel_unavailable'] as const) test(`AUTH-SETUP-173 ${status} keeps local escape and other instances`, async ({ page }, info) => {
  await isolateAccount(page); await language(page, 'en');
  const state = clientState(); signIn(state); state.enrollment.status = status;
  const requests = await installBridge(page, state); await page.goto(ACCOUNT);
  await expect(page.getByRole('button', { name: 'View other instances' })).toBeVisible();
  if (status === 'conflict') { await page.getByRole('button', { name: 'Review account change' }).click(); await expect(page.getByText(/Ask the current owner/)).toBeVisible(); }
  await page.screenshot({ animations: 'disabled', path: info.outputPath(`${status}.png`) });
  await page.getByRole('button', { name: 'View other instances' }).click();
  await expect(page.getByText('工作室 Studio', { exact: true })).toBeVisible();
  expect(requests.map((request) => request.action.type)).toEqual(['get_state', 'refresh_instances']);
});

test('AUTH-SETUP-176 unreachable target retains local escape; setup invitation never initializes', async ({ page }, info) => {
  await isolateAccount(page); await language(page, 'en');
  const state = clientState(); signIn(state);
  state.connection = { status: 'remote_unreachable', committed: { kind: 'remote', name: '工作室 Studio', instance_id: 'studio', canonical_origin: 'https://studio-app.avibe.bot' }, pending: null };
  state.remote_auxiliary = 'disabled_incomplete';
  const requests = await installBridge(page, state); await page.goto(ACCOUNT);
  await expect(page.getByRole('button', { name: 'Back to this Mac' })).toBeVisible();
  await page.screenshot({ animations: 'disabled', path: info.outputPath('offline.png') });
  await page.getByRole('button', { name: 'Back to this Mac' }).click();
  await expect.poll(() => requests.at(-1)?.action.type).toBe('return_local');
  await page.goto(`${ACCOUNT}?view=setup-complete`);
  await expect(page.getByRole('heading', { name: 'This Mac is ready' })).toBeVisible();
  await page.screenshot({ animations: 'disabled', path: info.outputPath('setup-complete.png') });
  await page.getByRole('button', { name: 'Start using this Mac' }).click();
  await expect.poll(() => requests.at(-1)?.action.type).toBe('return_local');
});

for (const method of ['totp', 'recovery', 'passkey'] as const) test(`AUTH-SETUP-171 second factor ${method} uses the frozen native action`, async ({ page }, info) => {
  await isolateAccount(page); await language(page, 'en');
  const state = clientState(); state.account = { ...state.account, status: 'second_factor_required', gate: 'challenge', methods: ['totp', 'passkey'] };
  const requests = await installBridge(page, state); await page.goto(ACCOUNT);
  if (method === 'passkey') await page.getByRole('button', { name: 'Use a passkey' }).click();
  else {
    if (method === 'recovery') await page.getByRole('button', { name: 'Use a recovery code' }).click();
    await page.getByLabel(method === 'totp' ? 'Authenticator code' : 'Recovery code', { exact: true }).fill(method === 'totp' ? '654321' : 'fixture-recovery');
    await page.screenshot({ animations: 'disabled', path: info.outputPath(`${method}.png`) });
    await page.getByRole('button', { name: 'Verify', exact: true }).click();
  }
  await expect(page.getByRole('heading', { name: 'This Mac is now in your account' })).toBeVisible();
  expect(requests.at(-1)?.action).toEqual(method === 'passkey' ? { type: 'verify_passkey' } : { type: 'verify_second_factor', method, code: method === 'totp' ? '654321' : 'fixture-recovery' });
});

test('AUTH-SETUP-177 sign-out is a confirmation page; Back cancels and clears setup material', async ({ page }) => {
  await isolateAccount(page); await language(page, 'en');
  const state = clientState(); signIn(state);
  const requests = await installBridge(page, state); await page.goto(`${ACCOUNT}?view=sign-out`);
  await expect(page.getByRole('heading', { name: 'Sign out' })).toBeVisible();
  expect(requests.map((request) => request.action.type)).toEqual(['get_state']);
  await page.getByRole('button', { name: 'Sign out', exact: true }).click();
  await expect.poll(() => requests.at(-1)?.action.type).toBe('sign_out');
  state.account = { ...state.account, status: 'second_factor_required', gate: 'enroll' };
  await page.goto(ACCOUNT);
  await page.getByRole('button', { name: 'Add authenticator' }).click();
  await expect(page.getByText('JBSWY3DPEHPK3PXP', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Back to Workbench' }).click();
  await expect.poll(() => requests.at(-1)?.action.type).toBe('cancel');
  await expect(page.getByText('JBSWY3DPEHPK3PXP', { exact: true })).toHaveCount(0);
});
