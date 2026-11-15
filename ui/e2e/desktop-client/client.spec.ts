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
      state.local.slug = 'authoritative-local';
      // A page this computer serves: the version opens the shared update panel (ui/design: update/panel)
      // over the desktop IPC. Remote presentation-only behavior has its own footer test and never
      // receives this native updater IPC fixture.
      await page.route('**/api/version**', (route) => route.fulfill({ json: { current: state.desktop.version, latest: '3.2.7', has_update: true, managed_by: 'desktop' } }));
      state.remote_auxiliary = 'disabled_incomplete';
      const requests = await installBridge(page, state, { projection: true });
      await language(page, lang);
      await page.goto(`/?theme=${theme}`);
      await expect(page.locator('[data-account-label]')).toHaveText('authoritative-local');
      const avatar = page.locator('[data-account-avatar]');
      await expect(avatar).toBeVisible();
      const footer = page.locator('[data-sidebar-footer]');
      const measurements = await footer.evaluate((node) => {
        const rect = (selector: string) => node.querySelector(selector)!.getBoundingClientRect();
        return { avatar: rect('[data-account-avatar]').x, tools: node.firstElementChild!.getBoundingClientRect().x,
          dotGap: rect('[data-status-indicator]').x - rect('[data-account-label]').right,
          versionRight: rect('[data-version-trigger]').right, footerRight: node.getBoundingClientRect().right };
      });
      expect(measurements.avatar).toBe(24);
      expect(measurements.tools).toBe(16);
      expect(measurements.dotGap).toBeLessThanOrEqual(6);
      await expect(page.locator('[data-account-chevron]')).toHaveCount(0);
      expect(measurements.versionRight).toBe(measurements.footerRight);
      await expect(footer).not.toContainText('max@example.test');
      await page.locator('[data-version-trigger] button').click();
      const popup = page.getByRole('dialog', { name: lang === 'en' ? 'Version & Update' : '版本与更新' });
      await expect(popup).toBeVisible();
      const box = await popup.boundingBox();
      expect(box!.x).toBeGreaterThanOrEqual(0); expect(box!.y).toBeGreaterThanOrEqual(0); expect(box!.x + box!.width).toBeLessThanOrEqual(viewport.width);
      await expect(popup.locator('[data-update-panel]')).toBeVisible();
      await expect(popup).toContainText('3.2.6rc9'); await expect(popup).toContainText('3.2.7'); await expect(popup).not.toContainText('2.4.0');
      await page.screenshot({ animations: 'disabled', path: info.outputPath('update.png') });
      await page.keyboard.press('Escape');
      await expect(page.locator('[data-version-trigger] button')).toBeFocused();
      await page.locator('[data-account-trigger]').click();
      await expect(page.locator('[data-instance-menu]')).toHaveCount(0);
      await expect(popup).toHaveCount(0);
      await expect(page.locator('[data-update-menu]')).toHaveCount(0);
      await page.screenshot({ animations: 'disabled', path: info.outputPath('account-entry.png') });
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
      await expect.poll(() => requests.at(-1)?.action.type).toBe('verify_otp');
      // Open the separate instance presentation after login; native owns the Workbench return.
      await page.goto(`${ACCOUNT}?view=instances&theme=${theme}`);
      await expect(page.getByText('工作室 Studio', { exact: true })).toBeVisible();
      await expect(page.getByText('共享团队', { exact: true })).toBeVisible();
      await page.screenshot({ animations: 'disabled', path: info.outputPath('instances.png') });
      await page.getByRole('button', { name: /工作室 Studio/ }).click();
      await expect(page.getByRole('heading', { name: /工作室 Studio/ })).toBeVisible();
      await page.screenshot({ animations: 'disabled', path: info.outputPath('consent.png') });
      await page.getByRole('button', { name: lang === 'en' ? 'Allow and connect' : '允许并连接' }).click();
      await expect(page.getByRole('dialog')).toHaveCount(0);
      await expect(page.locator('[data-instance-mask]')).toBeVisible();
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
  state.connection = { status: 'remote_connected', pending: null, committed: { kind: 'remote', name: 'Retained remote', instance_id: 'remote-retained', canonical_origin: 'https://retained.example.test' } };
  const requests = await installBridge(page, state);
  await page.goto(ACCOUNT);
  await page.getByRole('button', { name: 'Add authenticator' }).click();
  await expect(page.getByText('JBSWY3DPEHPK3PXP', { exact: true })).toBeVisible();
  await page.screenshot({ animations: 'disabled', path: info.outputPath('totp-enrollment.png') });
  await page.getByLabel('Authenticator code', { exact: true }).fill('654321');
  await page.getByRole('button', { name: 'Verify and enable' }).click();
  await expect(page.getByRole('heading', { name: 'Save your recovery codes' })).toBeVisible();
  await page.screenshot({ animations: 'disabled', path: info.outputPath('recovery-codes.png') });
  expect(requests.some((request) => request.action.type === 'cancel')).toBe(false);
  await page.getByRole('button', { name: 'I have saved these codes' }).click();
  await expect.poll(() => requests.at(-1)?.action.type).toBe('cancel');
  expect(state.connection.committed.kind).toBe('remote');
  await expect(page.getByText('fixture-recovery-one')).toHaveCount(0);
  expect(await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage }))).not.toContain('fixture-recovery');
  expect(JSON.stringify(state)).not.toContain('JBSWY');
  expect(requests.find((request) => request.action.type === 'finish_totp_enrollment')?.action).toEqual({ type: 'finish_totp_enrollment', code: '654321' });
});

for (const status of ['conflict', 'failed', 'paired_tunnel_unavailable'] as const) test(`AUTH-SETUP-173 ${status} keeps local escape and other instances`, async ({ page }, info) => {
  await isolateAccount(page); await language(page, 'en');
  const state = clientState(); signIn(state); state.enrollment.status = status;
  const requests = await installBridge(page, state); await page.goto(ACCOUNT);
  await expect(page.getByRole('button', { name: 'Other instances' })).toBeVisible();
  if (status === 'conflict') { await page.getByRole('button', { name: 'Review account change' }).click(); await expect(page.getByText(/Ask the current owner/)).toBeVisible(); }
  await page.screenshot({ animations: 'disabled', path: info.outputPath(`${status}.png`) });
  await page.getByRole('button', { name: 'Other instances' }).click();
  await expect(page.getByText('工作室 Studio', { exact: true })).toBeVisible();
  expect(requests.map((request) => request.action.type)).toEqual(['get_state', 'refresh_instances']);
});

test('AUTH-SETUP-176 unreachable target retains local escape; setup invitation never initializes', async ({ page }, info) => {
  await isolateAccount(page); await language(page, 'en');
  const state = clientState(); signIn(state);
  state.connection = { status: 'remote_unreachable', committed: { kind: 'remote', name: '工作室 Studio', instance_id: 'studio', canonical_origin: 'https://studio-app.avibe.bot' }, pending: null };
  state.remote_auxiliary = 'disabled_incomplete';
  const requests = await installBridge(page, state); await page.goto(`${ACCOUNT}?view=instances`);
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
  await expect(page.getByRole('heading', { name: 'This Mac is now in your account' })).toHaveCount(0);
  await expect.poll(() => requests.at(-1)?.action).toEqual(method === 'passkey' ? { type: 'verify_passkey' } : { type: 'verify_second_factor', method, code: method === 'totp' ? '654321' : 'fixture-recovery' });
});

test('AUTH-SETUP-177 a bare sign-out view never authorizes logout; Back clears setup material', async ({ page }) => {
  await isolateAccount(page); await language(page, 'en');
  const state = clientState(); signIn(state);
  const requests = await installBridge(page, state); await page.goto(`${ACCOUNT}?view=sign-out`);
  await expect(page.getByRole('heading', { name: 'Sign out' })).toBeVisible();
  expect(requests.map((request) => request.action.type)).toEqual(['get_state']);
  await expect(page.getByRole('button', { name: 'Sign out', exact: true })).toHaveCount(0);
  state.account = { ...state.account, status: 'second_factor_required', gate: 'enroll' };
  await page.goto(ACCOUNT);
  await page.getByRole('button', { name: 'Add authenticator' }).click();
  await expect(page.getByText('JBSWY3DPEHPK3PXP', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Back to Workbench' }).click();
  await expect.poll(() => requests.at(-1)?.action.type).toBe('cancel');
  await expect(page.getByText('JBSWY3DPEHPK3PXP', { exact: true })).toHaveCount(0);
});

for (const lang of ['en', 'zh'] as const) test(`feedback2 inline failure, disabled offline and wrapping ${lang}`, async ({ page }, info) => {
  await page.setViewportSize({ width: 880, height: 600 });
  const denied = await isolateAccount(page); await language(page, lang);
  const state = clientState(); signIn(state);
  state.catalog.instances[0].name = '工作室 Studio — a long name for the shared research and product development computer';
  state.catalog.instances[1].owner_email = 'a-very-long-shared-account-name-for-the-research-team@example.test';
  const requests = await installBridge(page, state, { selectError: 'instance_upgrade_required' });
  await page.goto(`${ACCOUNT}?view=instances`);
  const row = page.locator('[data-instance-id="studio"]');
  await row.getByRole('button').click();
  await expect(row.getByRole('alert')).toContainText(lang === 'en' ? 'This instance needs to upgrade Avibe' : '这个实例需要先升级 Avibe');
  await expect(page.locator('[data-instance-switcher] > [role="alert"]')).toHaveCount(0);
  await row.getByRole('button', { name: lang === 'en' ? 'Retry' : '重试', exact: true }).click();
  await expect.poll(() => requests.filter((request) => request.action.type === 'select_instance').length).toBe(2);
  const offline = page.locator('[data-instance-id="team"]');
  await expect(offline.getByRole('button')).toBeDisabled();
  await expect(offline).toContainText(lang === 'en' ? 'Offline' : '离线');
  const errorBox = await row.getByRole('alert').boundingBox();
  const rowBox = await row.boundingBox();
  expect(errorBox!.y + errorBox!.height).toBeLessThanOrEqual(rowBox!.y + rowBox!.height);
  const retryBox = await row.getByRole('button').boundingBox();
  expect(Math.abs(retryBox!.y + retryBox!.height / 2 - rowBox!.y - rowBox!.height / 2)).toBeLessThanOrEqual(2);
  expect(requests.filter((request) => request.action.type === 'select_instance')).toHaveLength(2);
  await page.screenshot({ animations: 'disabled', path: info.outputPath('row-failure-offline.png') });
  expect(await page.locator('main').evaluate((node) => node.scrollWidth <= node.clientWidth)).toBe(true);
  await page.getByRole('button', { name: lang === 'en' ? 'Refresh instances' : '刷新实例' }).click();
  await expect.poll(() => requests.at(-1)?.action.type).toBe('refresh_instances');
  expect(denied).toEqual([]);
});

test('rc19 pending target remains in the masked list', async ({ page }, info) => {
  await isolateAccount(page); await language(page, 'en');
  const state = clientState(); signIn(state);
  state.connection.status = 'switching';
  state.connection.pending = { kind: 'remote', name: '工作室 Studio', instance_id: 'studio', canonical_origin: 'https://studio-app.avibe.bot' };
  await installBridge(page, state);
  await page.goto(`${ACCOUNT}?view=instances`);
  await expect(page.locator('[data-instance-id="studio"]')).toContainText('Opening…');
  await expect(page.locator('[data-instance-mask]')).toBeVisible();
  for (const button of await page.locator('[data-instance-list] button').all()) await expect(button).toBeDisabled();
  await expect(page.getByRole('button', { name: 'Back to Workbench' })).toBeEnabled();
  await page.screenshot({ animations: 'disabled', path: info.outputPath('pending-list.png') });
});

for (const lang of ['en', 'zh'] as const) test(`rc19 password preserves email, clears secret, and continues through 2FA ${lang}`, async ({ page }, info) => {
  const denied = await isolateAccount(page); await language(page, lang);
  const state = clientState();
  const requests = await installBridge(page, state, { factor: 'challenge' });
  await page.goto(ACCOUNT);
  await page.locator('#desktop-auth-field').fill('long-research-account@example.test');
  await page.getByRole('button', { name: lang === 'en' ? 'Use password instead' : '使用密码登录' }).click();
  await expect(page.locator('#desktop-auth-field')).toHaveValue('long-research-account@example.test');
  await page.locator('#desktop-password').fill('synthetic-password-fixture');
  await page.screenshot({ animations: 'disabled', path: info.outputPath('password.png') });
  await page.getByRole('button', { name: lang === 'en' ? 'Sign in' : '登录', exact: true }).click();
  await expect(page.locator('[data-account-factor]')).toBeVisible();
  expect(requests.find((request) => request.action.type === 'sign_in_password')?.action).toEqual({ type: 'sign_in_password', email: 'long-research-account@example.test', password: 'synthetic-password-fixture' });
  expect(JSON.stringify(state)).not.toContain('synthetic-password');
  expect(await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage }))).not.toContain('synthetic-password');
  await page.getByRole('button', { name: lang === 'en' ? 'Use a recovery code' : '使用恢复码' }).click();
  await page.locator('#factor-code').fill('fixture-recovery');
  await page.getByRole('button', { name: lang === 'en' ? 'Verify' : '验证', exact: true }).click();
  await expect.poll(() => requests.at(-1)?.action.type).toBe('verify_second_factor');
  expect(denied).toEqual([]);
});

test('rc19 password failure and forgotten-password guidance keep email but clear the password', async ({ page }, info) => {
  await isolateAccount(page); await language(page, 'en');
  await installBridge(page, clientState(), { passwordError: 'invalid_credentials' });
  await page.goto(ACCOUNT);
  await page.locator('#desktop-auth-field').fill('max@example.test');
  await page.getByRole('button', { name: 'Use password instead' }).click();
  await page.locator('#desktop-password').fill('synthetic-wrong-password');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('The email or password is incorrect');
  await expect(page.locator('#desktop-password')).toHaveValue('');
  await page.screenshot({ animations: 'disabled', path: info.outputPath('password-error.png') });
  await page.getByRole('button', { name: 'Forgot password?' }).click();
  await expect(page.locator('#desktop-password')).toHaveCount(0);
  await expect(page.locator('#desktop-auth-field')).toHaveValue('max@example.test');
  await expect(page.getByRole('status')).toContainText('Account security at avibe.bot');
});

test('rc19 only a current native direct intent consumes sign-out once', async ({ page }) => {
  await isolateAccount(page); await language(page, 'en');
  const state = clientState(); signIn(state);
  const requests = await installBridge(page, state, { signOutIntent: 'synthetic-intent' });
  await page.goto(`${ACCOUNT}?view=sign-out`);
  await expect.poll(() => requests.filter((request) => request.action.type === 'sign_out').length).toBe(1);
  expect(requests.find((request) => request.action.type === 'sign_out')?.action).toEqual({ type: 'sign_out', intent_id: 'synthetic-intent' });
  expect(JSON.stringify({ ...await page.evaluate(() => ({ ...localStorage, ...sessionStorage })) })).not.toContain('synthetic-intent');
});

// URL locale must win before any visible heading, even over the opposite stored preference.
for (const locale of ['en', 'zh'] as const) test(`rc19 admitted locale controls first account render ${locale}`, async ({ page }) => {
  await isolateAccount(page); await language(page, locale === 'en' ? 'zh' : 'en');
  await installBridge(page, clientState());
  await page.addInitScript(() => {
    const seen: string[] = [];
    (window as unknown as { __headings: string[] }).__headings = seen;
    new MutationObserver(() => {
      const title = document.querySelector('h1')?.textContent;
      if (title && seen.at(-1) !== title) seen.push(title);
    }).observe(document, { childList: true, subtree: true });
  });
  await page.goto(`${ACCOUNT}?view=account&locale=${locale}`);
  const title = locale === 'en' ? 'Start with an email.' : '从一封邮件开始。';
  await expect(page.getByRole('heading', { name: title, exact: true })).toBeVisible();
  expect(await page.evaluate(() => (window as unknown as { __headings: string[] }).__headings)).toEqual([title]);
});

test('rc19 stale receipt cannot auto-logout and a failed current intent needs explicit retry', async ({ page }) => {
  await isolateAccount(page); await language(page, 'en');
  const state = clientState(); signIn(state);
  const options = { signOutIntent: 'synthetic-failed-intent', signOutGeneration: 0, signOutError: 'cleanup_failed' };
  const requests = await installBridge(page, state, options);
  await page.goto(`${ACCOUNT}?view=sign-out`);
  await expect.poll(() => requests.filter((request) => request.action.type === 'get_state').length).toBe(1);
  await expect(page.getByRole('button', { name: 'Back to Workbench' }).last()).toBeVisible();
  expect(requests.filter((request) => request.action.type === 'sign_out')).toHaveLength(0);
  options.signOutGeneration = state.generation;
  await page.reload();
  await expect(page.getByRole('alert')).toBeVisible();
  expect(requests.filter((request) => request.action.type === 'sign_out')).toHaveLength(1);
  await page.getByRole('button', { name: 'Retry', exact: true }).click();
  await expect.poll(() => requests.filter((request) => request.action.type === 'sign_out').length).toBe(2);
  expect(requests.at(-1)?.action).toEqual({ type: 'sign_out' });
});

// The real bridge promise fails after native has broadcast a new pending generation.
// The row must recover its Retry control without changing the projected native state.
for (const failure of ['reject', 'malformed'] as const) test(`rc19 IPC ${failure} stays on the selected row and can retry`, async ({ page }, info) => {
  const denied = await isolateAccount(page); await language(page, 'en');
  const state = clientState(); signIn(state);
  const options: { selectTransportFailure?: 'reject' | 'malformed' } = { selectTransportFailure: failure };
  const requests = await installBridge(page, state, options);
  await page.goto(`${ACCOUNT}?view=instances`);
  const row = page.locator('[data-instance-id="studio"]');
  await row.getByRole('button').click();
  await expect(row.getByRole('alert')).toContainText('The Desktop connection is unavailable');
  await expect(row.getByRole('button', { name: 'Retry', exact: true })).toBeEnabled();
  await expect(page.locator('[data-instance-mask]')).toHaveCount(0);
  await page.screenshot({ animations: 'disabled', path: info.outputPath('ipc-row-error.png') });
  options.selectTransportFailure = undefined;
  await row.getByRole('button', { name: 'Retry', exact: true }).click();
  await expect(page.getByRole('dialog')).toBeVisible();
  expect(requests.filter((request) => request.action.type === 'select_instance').map((request) => request.expected_generation)).toEqual([1, 8]);
  expect(denied).toEqual([]);
});

for (const lang of ['en', 'zh'] as const) test(`A5 current shared snapshot and restored account ${lang}`, async ({ page }, info) => {
  await isolateAccount(page); await language(page, lang);
  await page.setViewportSize({ width: 880, height: 600 });
  const state = clientState(); signIn(state);
  const current = { ...state.catalog.instances[1]!, available: false };
  state.current_instance = current;
  state.catalog.instances = state.catalog.instances.filter((item) => item.id !== current.id);
  state.local.slug = null;
  state.connection = { status: lang === 'zh' ? 'remote_unreachable' : 'remote_connected', pending: null, committed: { kind: 'remote', name: current.name, instance_id: current.id, canonical_origin: current.url } };
  const requests = await installBridge(page, state);
  await page.goto(`${ACCOUNT}?view=instances&theme=${lang === 'en' ? 'light' : 'dark'}`);
  const row = page.locator(`[data-instance-id="${current.id}"]`);
  await expect(row).toHaveCount(1);
  await expect(row.locator('[data-instance-icon="shared"]')).toBeVisible();
  await expect(row).toContainText(current.owner_email!);
  await expect(row.getByRole('button')).toBeDisabled();
  await expect(page.locator('[data-other-instances]')).not.toContainText(current.name);
  await expect(page.locator('[data-instance-id="local"]')).toContainText(lang === 'en' ? 'This computer' : '本机');
  await page.screenshot({ animations: 'disabled', path: info.outputPath('a5-current-snapshot.png') });
  expect(requests.map((request) => request.action.type)).toEqual(['get_state']);
  await page.getByRole('button', { name: state.account.identity!.email, exact: true }).click();
  await expect(page.locator('[data-account-details]')).toBeVisible();
  expect(requests.map((request) => request.action.type)).toEqual(['get_state']);
  // Reloading an already signed-in account is not a newly completed login.
  await page.goto(`${ACCOUNT}?view=account`);
  await expect(page.locator('[data-account-details]')).toBeVisible();
  expect(requests.map((request) => request.action.type)).toEqual(['get_state', 'get_state']);
  await page.getByRole('button', { name: lang === 'en' ? 'Back to Workbench' : '返回工作台' }).click();
  await expect.poll(() => requests.at(-1)?.action.type).toBe('cancel');
  expect(state.connection.committed.instance_id).toBe(current.id);
});
