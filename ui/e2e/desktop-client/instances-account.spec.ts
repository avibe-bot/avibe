import { test, expect } from '../support/browserTest';
import { clientState, signIn, installBridge } from './bridge-fixture';
for (const lang of ['en','zh']) for (const theme of ['light','dark']) for (const connection of ['local','remote']) {
  test(`AUTH-SETUP-177 instances inline account card ${lang} ${theme} ${connection}`, async ({ page }, info) => {
    await page.setViewportSize({width:880,height:820});
    await page.route('**/*', r => new URL(r.request().url()).origin === 'http://127.0.0.1:5214' ? r.continue() : r.abort());
    await page.addInitScript(lng => localStorage.setItem('i18nextLng', lng), lang);
    const state = clientState(); signIn(state);
    state.local.slug = 'max'; state.local.instance_id = 'local-fixture';
    if (connection === 'remote') {
      const item = state.catalog.instances[0]; state.current_instance = item;
      state.connection = {status:'remote_connected',committed:{kind:'remote',name:item.name,instance_id:item.id,canonical_origin:item.url},pending:null};
    }
    const requests = await installBridge(page, state);
    await page.goto(`http://127.0.0.1:5214/account/index.html?view=instances&theme=${theme}`);
    await expect(page.locator('[data-instance-account]')).toBeVisible();
    await page.evaluate(() => document.fonts.ready);
    const card = page.locator('[data-instance-account]');
    await expect(card.getByText(lang === 'en' ? 'Currently signed in' : '当前登录', { exact: true })).toBeVisible();
    await expect(card.locator('.lucide-chevron-right')).toHaveCount(0);
    await expect(card.getByRole('button')).toHaveCount(1);
    await card.getByText('max@example.test', { exact: true }).click();
    await expect(page.locator('[data-account-details]')).toHaveCount(0);
    expect(requests.map(request => request.action.type)).toEqual(['get_state']);
    await page.mouse.move(0, 0);
    await page.screenshot({path:info.outputPath(`${lang}-${theme}-${connection}.png`),animations:'disabled'});
    await card.getByRole('button').click();
    await expect.poll(() => requests.filter(request => request.action.type === 'sign_out').length).toBe(1);
    expect(requests.at(-1)?.action).toEqual({type:'sign_out'});
    await expect(page.locator('[data-account-sign-out]')).toBeVisible();
    await expect(page.getByRole('dialog')).toHaveCount(0);
    expect(state.account.status).toBe('signed_out');
    expect(state.connection.committed.kind).toBe('local');
  });
}
