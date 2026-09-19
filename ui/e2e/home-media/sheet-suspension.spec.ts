import { expect, test, type Page } from '@playwright/test';
import en from '../../src/i18n/en.json' with { type: 'json' };

const draft = (page: Page) => page.getByPlaceholder(en.newSession.placeholder);
const send = (page: Page) => page.getByRole('button', { name: en.chat.compose.send, exact: true });
const writes = (page: Page, suffix: string) => page.evaluate((suffix) => window.homeMedia.writes.filter((write) => write.path.endsWith(suffix)), suffix);
async function open(page: Page, query = '') {
  await page.route('**/*', (route) => new URL(route.request().url()).origin === 'http://127.0.0.1:5217' ? route.continue() : route.abort());
  await page.goto(`/e2e/home-media/fixture.html?surface=sheet${query}`);
  await page.getByRole('button', { name: 'Open new session' }).click();
  await expect(draft(page)).toBeEnabled();
}
async function settings(page: Page) {
  // A test-owned foreground navigation shortcut models shell/external entry;
  // B has no Settings button inside the real mobile sheet.
  await page.keyboard.press('Alt+s');
  await expect(page.getByTestId('settings-foreground')).toBeVisible();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  const input = page.getByRole('textbox', { name: 'Settings value' });
  await input.fill('设置不应被后台抢走');
  await expect(input).toBeFocused();
  await page.keyboard.press('Alt+z');
  await page.keyboard.press('Control+n');
  await page.keyboard.press('Meta+n');
  await expect(input).toBeFocused();
  expect(await page.evaluate(() => document.body.style.pointerEvents)).not.toBe('none');
  await page.keyboard.press('Tab');
  await expect(page.getByRole('button', { name: 'Back to app' })).toBeFocused();
  await page.keyboard.press('Tab');
  await expect(page.getByRole('button', { name: 'Discard suspended sheet' })).toBeFocused();
}
test.afterEach(async ({ page }) => {
  expect(await page.evaluate(() => window.homeMedia.unexpectedRequests)).toEqual([]);
});
const back = (page: Page) => page.getByRole('button', { name: 'Back to app' }).click();

for (const width of [390, 1366]) test(`sheet ${width} keeps Unicode draft and selected project/Agent while its open picker loses all modal effects`, async ({ page }) => {
  await page.setViewportSize({ width, height: 768 });
  await open(page, '&twoProjects');
  await draft(page).fill('项目乙：请检查发布 ✓');
  await page.getByRole('button', { name: '第二个项目', exact: true }).click();
  await page.getByRole('button', { name: /codex/ }).first().click();
  await page.getByRole('button', { name: /^claude/ }).click();
  await settings(page);
  await expect(page.getByTestId('sheet-logical-open')).toHaveText('true');
  expect(await writes(page, '/messages')).toHaveLength(0);
  await back(page);
  await expect(draft(page)).toHaveValue('项目乙：请检查发布 ✓');
  await expect(page.getByRole('button', { name: /claude/ }).first()).toBeVisible();
  await send(page).click();
  await expect(page.getByTestId('conversation')).toContainText('ses-1');
  expect((await writes(page, '/api/sessions'))[0].body).toMatchObject({ project_id: 'project-second', agent_name: 'claude' });
  expect(await writes(page, '/messages')).toHaveLength(1);
});

for (const outcome of ['success', 'rejected', 'unknown', 'network'] as const) {
  test(`admitted POST ${outcome} during Settings preserves its result without hidden navigation or automatic resend`, async ({ page }) => {
    await open(page);
    await draft(page).fill('只执行一次，保留结果');
    await page.evaluate((outcome) => { window.homeMedia.holdMessage = true; window.homeMedia.messageMode = outcome; }, outcome);
    await send(page).click();
    await expect.poll(async () => (await writes(page, '/messages')).length).toBe(1);
    await settings(page);
    await page.evaluate(() => window.homeMedia.releaseMessage());
    await expect.poll(() => page.evaluate(() => window.homeMedia.messageCompletions)).toBe(1);
    await expect(page.getByTestId('settings-foreground')).toBeVisible();
    await expect(page.getByTestId('sheet-logical-open')).toHaveText('true');
    expect(await writes(page, '/messages')).toHaveLength(1);
    await back(page);
    if (outcome === 'success') {
      await expect(page.getByTestId('conversation')).toContainText('ses-1');
      await expect(page.getByTestId('sheet-logical-open')).toHaveText('false');
      expect(await page.getByTestId('handoff').textContent()).toBe('null');
    } else {
      await expect(draft(page)).toHaveValue('只执行一次，保留结果');
      if (outcome === 'rejected') {
        await expect(send(page)).toBeEnabled();
        await page.evaluate(() => { window.homeMedia.holdMessage = false; window.homeMedia.messageMode = 'success'; });
        await send(page).click();
        await expect(page.getByTestId('conversation')).toContainText('ses-1');
        expect(await writes(page, '/api/sessions')).toHaveLength(1);
        expect(await writes(page, '/messages')).toHaveLength(2);
      } else {
        await expect(send(page)).toBeDisabled();
        await expect(page.getByRole('link', { name: en.newSession.inspectSession })).toBeVisible();
        await settings(page);
        await back(page);
        await expect(send(page)).toBeDisabled();
        await page.keyboard.press('Escape');
        await page.getByRole('button', { name: 'Open new session' }).click();
        await expect(draft(page)).toHaveValue('');
        await expect(page.getByRole('link', { name: en.newSession.inspectSession })).toHaveCount(0);
        await draft(page).fill('明确新会话');
        await page.evaluate(() => { window.homeMedia.holdMessage = false; window.homeMedia.messageMode = 'success'; });
        await send(page).click();
        await expect(page.getByTestId('conversation')).toContainText('ses-2');
      }
    }
  });
}

test('create completing under Settings retains its scope and waits for explicit send before POST', async ({ page }) => {
  await open(page);
  await draft(page).fill('创建后暂停');
  await page.evaluate(() => { window.homeMedia.holdCreate = true; });
  await send(page).click();
  await expect.poll(async () => (await writes(page, '/api/sessions')).length).toBe(1);
  await settings(page);
  await page.evaluate(() => window.homeMedia.releaseCreate());
  await back(page);
  await expect(draft(page)).toHaveValue('创建后暂停');
  await expect(send(page)).toBeEnabled();
  expect(await writes(page, '/messages')).toHaveLength(0);
  await send(page).click();
  await expect(page.getByTestId('conversation')).toContainText('ses-1');
  expect(await writes(page, '/api/sessions')).toHaveLength(1);
});

for (const timing of ['pending', 'completed'] as const) test(`actual close discards ${timing} suspended send without letting its old Composer overwrite a new draft`, async ({ page }) => {
  await open(page);
  await draft(page).fill('旧的发送');
  await page.evaluate(() => { window.homeMedia.holdMessage = true; });
  await send(page).click();
  await expect.poll(async () => (await writes(page, '/messages')).length).toBe(1);
  await settings(page);
  if (timing === 'completed') {
    await page.evaluate(() => window.homeMedia.releaseMessage());
    await expect.poll(() => page.evaluate(() => window.homeMedia.messageCompletions)).toBe(1);
  }
  await page.getByRole('button', { name: 'Discard suspended sheet' }).click();
  await back(page);
  await page.getByRole('button', { name: 'Open new session' }).click();
  await expect(draft(page)).toHaveValue('');
  await draft(page).fill('新草稿不能被旧回调覆盖');
  if (timing === 'pending') {
    await page.evaluate(() => { window.homeMedia.messageMode = 'unknown'; window.homeMedia.releaseMessage(); });
    await expect.poll(() => page.evaluate(() => window.homeMedia.messageCompletions)).toBe(1);
  }
  await expect(draft(page)).toHaveValue('新草稿不能被旧回调覆盖');
  await expect(page.getByRole('link', { name: en.newSession.inspectSession })).toHaveCount(0);
  await expect(send(page)).toBeEnabled();
});

async function pickFolder(page: Page) {
  await page.getByRole('button', { name: '另一个项目', exact: true }).click();
  await page.getByRole('button', { name: en.directoryBrowser.select, exact: true }).click();
}

test('no-project handoff preserves picker path/history/manual input and confirmation name across Settings', async ({ page }) => {
  await open(page, '&empty');
  await draft(page).fill('没有项目时也保留的中文任务');
  await send(page).click();
  await expect(page.getByRole('dialog', { name: en.directoryBrowser.title })).toBeVisible();
  await page.getByRole('button', { name: '另一个项目', exact: true }).click();
  await page.getByRole('button', { name: en.directoryBrowser.editPath, exact: true }).click();
  const path = page.getByPlaceholder(en.directoryBrowser.editPathPlaceholder);
  await path.fill('/未确认的路径/草稿');
  await settings(page);
  await back(page);
  await expect(path).toHaveValue('/未确认的路径/草稿');
  await expect(path).toBeFocused();
  await page.keyboard.press('End');
  await page.keyboard.insertText('/继续编辑');
  await expect(path).toHaveValue('/未确认的路径/草稿/继续编辑');
  await expect(page.getByPlaceholder(en.directoryBrowser.newFolderPlaceholder)).toHaveCount(0);
  await page.keyboard.press('Escape');
  await page.getByRole('button', { name: en.directoryBrowser.newFolder, exact: true }).click();
  const folderName = page.getByPlaceholder(en.directoryBrowser.newFolderPlaceholder);
  await folderName.fill('未创建文件夹');
  await settings(page);
  await back(page);
  await expect(folderName).toHaveValue('未创建文件夹');
  await expect(folderName).toBeFocused();
  expect(await writes(page, '/api/browse/mkdir')).toHaveLength(0);
  await page.keyboard.press('Escape');
  await page.getByRole('button', { name: en.directoryBrowser.back, exact: true }).click();
  await expect(page.getByRole('button', { name: en.directoryBrowser.forward, exact: true })).toBeEnabled();
  await page.getByRole('button', { name: en.directoryBrowser.forward, exact: true }).click();
  await page.getByRole('button', { name: en.directoryBrowser.select, exact: true }).click();
  const name = page.getByLabel(en.workbench.newProjectDialog.displayName, { exact: true });
  await name.fill('保留的项目名称');
  await settings(page);
  await back(page);
  await expect(name).toHaveValue('保留的项目名称');
  await page.getByRole('button', { name: en.workbench.newProjectDialog.create, exact: true }).click();
  await expect(draft(page)).toHaveValue('没有项目时也保留的中文任务');
  await send(page).click();
  await expect(page.getByTestId('conversation')).toBeVisible();
  expect((await writes(page, '/api/projects'))[0].body).toMatchObject({ folder_path: '/fixture/另一个项目', display_name: '保留的项目名称' });
  expect((await writes(page, '/messages'))[0].body.text).toBe('没有项目时也保留的中文任务');
});

for (const outcome of ['success', 'failure', 'cancel'] as const) test(`project create ${outcome} under Settings preserves the correct continuation`, async ({ page }) => {
  await open(page);
  await draft(page).fill('项目创建期间的草稿');
  await page.getByRole('button', { name: en.newSession.newProject, exact: true }).click();
  await pickFolder(page);
  await page.evaluate((outcome) => { window.homeMedia.holdProject = true; window.homeMedia.projectFailure = outcome === 'failure'; }, outcome);
  await page.getByRole('button', { name: en.workbench.newProjectDialog.create, exact: true }).click();
  await expect.poll(async () => (await writes(page, '/api/projects')).length).toBe(1);
  if (outcome === 'cancel') await page.getByRole('button', { name: en.workbench.newProjectDialog.cancel, exact: true }).first().click();
  await settings(page);
  await page.evaluate(() => window.homeMedia.releaseProject());
  await expect.poll(() => page.evaluate(() => window.homeMedia.projectCompletions)).toBe(1);
  await expect(page.getByTestId('sheet-logical-open')).toHaveText('false');
  await back(page);
  if (outcome === 'success') {
    await expect(draft(page)).toHaveValue('项目创建期间的草稿');
    await send(page).click();
    await expect(page.getByTestId('conversation')).toContainText('ses-1');
    expect((await writes(page, '/api/sessions'))[0].body.project_id).toBe('project-1');
  } else if (outcome === 'failure') {
    await expect(page.getByLabel(en.workbench.newProjectDialog.displayName, { exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: en.workbench.newProjectDialog.create, exact: true })).toBeEnabled();
  } else {
    await expect(page.getByRole('dialog')).toHaveCount(0);
    await expect(page.getByTestId('sheet-logical-open')).toHaveText('false');
  }
  expect(await writes(page, '/api/projects')).toHaveLength(1);
});
