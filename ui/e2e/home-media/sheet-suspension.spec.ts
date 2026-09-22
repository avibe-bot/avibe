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
async function settings(page: Page, history = false) {
  // A test-owned foreground navigation shortcut models shell/external entry;
  // B has no Settings button inside the real mobile sheet.
  if (history) await page.goForward();
  else await page.keyboard.press('Alt+s');
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
const picker = (page: Page) => page.getByRole('dialog', { name: en.directoryBrowser.title });
const manualPath = (page: Page) => page.getByPlaceholder(en.directoryBrowser.editPathPlaceholder);
const resolvedPath = (page: Page) => picker(page).locator('code');
const initialDirectory = '/fixture/中文项目';
async function openProjectPicker(page: Page) {
  await open(page);
  await page.getByRole('button', { name: en.newSession.newProject, exact: true }).click();
  await expect(resolvedPath(page)).toHaveText(initialDirectory);
  await expect(picker(page).getByRole('button', { name: en.directoryBrowser.select, exact: true })).toBeEnabled();
}
const holdBrowse = (page: Page, path: string) => page.evaluate((path) => { window.homeMedia.heldBrowsePaths.push(path); }, path);
const pendingBrowse = (page: Page, path: string) => expect.poll(() => page.evaluate((path) => window.homeMedia.pendingBrowses.some((request) => request.path === path), path)).toBe(true);
const releaseBrowse = (page: Page, path: string) => page.evaluate((path) => {
  const index = window.homeMedia.pendingBrowses.findIndex((request) => request.path === path);
  if (index < 0) throw new Error(`No held browse for ${path}`);
  window.homeMedia.pendingBrowses.splice(index, 1)[0].release();
  window.homeMedia.heldBrowsePaths = window.homeMedia.heldBrowsePaths.filter((held) => held !== path);
}, path);
const selection = (page: Page) => manualPath(page).evaluate((input: HTMLInputElement) => ({ start: input.selectionStart, end: input.selectionEnd, direction: input.selectionDirection }));

for (const width of [390, 1366]) test(`Files picker ${width} searches, refreshes hidden entries and creates a folder through the real API client`, async ({ page }, testInfo) => {
  await page.setViewportSize({ width, height: 768 });
  await openProjectPicker(page);
  const search = picker(page).getByPlaceholder(en.apps.fileBrowser.searchPlaceholder);
  await search.fill('另一个');
  await expect(picker(page).getByRole('button', { name: /^另一个项目(?: |$)/ })).toBeVisible();
  await picker(page).getByRole('checkbox', { name: en.apps.fileBrowser.showHidden, exact: true }).check();
  await picker(page).getByRole('button', { name: en.apps.fileBrowser.refresh, exact: true }).click();
  await expect(search).toHaveValue('另一个');
  await search.fill('');
  await expect(picker(page).getByRole('button', { name: /^\.hidden(?: |$)/ })).toBeVisible();
  expect((await page.evaluate(() => window.homeMedia.listRequests)).at(-1)).toEqual({ path: initialDirectory, showHidden: true });
  await picker(page).getByRole('button', { name: en.apps.fileBrowser.newFolder, exact: true }).click();
  const name = picker(page).getByPlaceholder(en.apps.fileBrowser.newFolderPlaceholder);
  await name.fill('新建文件夹🌱');
  await name.press('Enter');
  await expect(picker(page).getByRole('button', { name: /^新建文件夹🌱(?: |$)/ })).toBeVisible();
  expect((await writes(page, '/api/files/mkdir')).at(-1)?.body.path).toBe(`${initialDirectory}/新建文件夹🌱`);
  await expect(picker(page).getByRole('button', { name: en.directoryBrowser.back, exact: true })).toBeDisabled();
  await picker(page).screenshot({ path: testInfo.outputPath('files-picker.png') });
});

for (const width of [390, 1366]) test(`folder creation shortcut ${width} follows route activity and preserves the draft`, async ({ page }) => {
  await page.setViewportSize({ width, height: 768 });
  await openProjectPicker(page);
  await expect(picker(page).getByRole('button', { name: en.directoryBrowser.favoritesHome, exact: true })).toBeVisible();
  await page.keyboard.press('Control+n');
  const folderName = page.getByPlaceholder(en.apps.fileBrowser.newFolderPlaceholder);
  await expect(folderName).toBeFocused();
  await folderName.fill('快捷键文件夹');
  await page.keyboard.press('Meta+n');
  await expect(folderName).toHaveValue('快捷键文件夹');
  await settings(page);
  await back(page);
  await expect(folderName).toHaveValue('快捷键文件夹');
  await expect(folderName).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(picker(page).getByRole('button', { name: /^快捷键文件夹(?: |$)/ })).toBeVisible();
  expect((await writes(page, '/api/files/mkdir')).at(-1)?.body.path).toBe(`${initialDirectory}/快捷键文件夹`);
});

for (const width of [390, 1366]) for (const suspended of [false, true]) {
  test(`directory draft ${width}: pending browse preserves text and selection ${suspended ? 'through Settings' : 'in foreground'}`, async ({ page }) => {
    await page.setViewportSize({ width, height: 768 });
    await openProjectPicker(page);
    const destination = `${initialDirectory}/另一个项目`;
    await holdBrowse(page, destination);
    await picker(page).getByRole('button', { name: /^另一个项目(?: |$)/ }).click();
    await pendingBrowse(page, destination);
    await picker(page).getByRole('button', { name: en.directoryBrowser.editPath, exact: true }).click();
    const text = '/未确认的路径/草稿🌱';
    await manualPath(page).fill(text);
    for (let index = 0; index <= text.length; index++) await page.keyboard.press('ArrowLeft');
    await page.keyboard.press('ArrowRight');
    await page.keyboard.press('Shift+ArrowRight');
    await page.keyboard.press('Shift+ArrowRight');
    const selected = await selection(page);
    expect(selected).toMatchObject({ start: 1, end: 3 });
    const settingsInput = page.getByRole('textbox', { name: 'Settings value' });
    if (suspended) {
      await settings(page);
      await settingsInput.focus();
    }
    await releaseBrowse(page, destination);
    await expect.poll(() => page.evaluate((path) => window.homeMedia.browseCompletions.includes(path), destination)).toBe(true);
    if (suspended) {
      await expect(settingsInput).toBeFocused();
      await expect(picker(page)).toHaveCount(0);
      await back(page);
    }
    await expect(resolvedPath(page)).toHaveText(destination);
    await expect(manualPath(page)).toHaveValue(text);
    await expect(manualPath(page)).toBeFocused();
    expect(await selection(page)).toEqual(selected);
    // An ordinary key must land at the retained selection, without refocusing
    // or manually restoring the caret after the browse response.
    await page.keyboard.press('x');
    await expect(manualPath(page)).toHaveValue('/x认的路径/草稿🌱');

    await page.keyboard.press('Escape');
    await picker(page).getByRole('button', { name: en.directoryBrowser.editPath, exact: true }).click();
    await expect(manualPath(page)).toHaveValue(destination);
    const target = '/手动选择/最终目录';
    await manualPath(page).fill(target);
    await page.keyboard.press('Enter');
    await expect(manualPath(page)).toHaveCount(0);
    await expect(resolvedPath(page)).toHaveText(target);
    expect((await writes(page, '/api/browse')).at(-1)?.body.path).toBe(target);
    await picker(page).getByRole('button', { name: en.directoryBrowser.back, exact: true }).click();
    await expect(resolvedPath(page)).toHaveText(destination);
    await picker(page).getByRole('button', { name: en.directoryBrowser.forward, exact: true }).click();
    await expect(resolvedPath(page)).toHaveText(target);
  });
}

test('directory draft survives history and hidden refresh while an obsolete browse cannot replace a manual target', async ({ page }) => {
  await openProjectPicker(page);
  const start = initialDirectory;
  const destination = `${initialDirectory}/另一个项目`;
  await picker(page).getByRole('button', { name: /^另一个项目(?: |$)/ }).click();
  await expect(resolvedPath(page)).toHaveText(destination);
  await holdBrowse(page, start);
  await picker(page).getByRole('button', { name: en.directoryBrowser.back, exact: true }).click();
  await pendingBrowse(page, start);
  await picker(page).getByRole('button', { name: en.directoryBrowser.editPath, exact: true }).click();
  await manualPath(page).fill('/历史期间草稿');
  await releaseBrowse(page, start);
  await expect(resolvedPath(page)).toHaveText(start);
  await expect(manualPath(page)).toHaveValue('/历史期间草稿');
  await holdBrowse(page, destination);
  await picker(page).getByRole('button', { name: en.directoryBrowser.forward, exact: true }).click();
  await pendingBrowse(page, destination);
  await manualPath(page).fill('/隐藏文件刷新草稿');
  await releaseBrowse(page, destination);
  await expect(resolvedPath(page)).toHaveText(destination);
  await expect(manualPath(page)).toHaveValue('/隐藏文件刷新草稿');
  await holdBrowse(page, destination);
  await picker(page).getByRole('checkbox', { name: en.apps.fileBrowser.showHidden, exact: true }).click();
  await pendingBrowse(page, destination);
  const target = '/用户明确提交的路径';
  await manualPath(page).fill(target);
  await page.keyboard.press('Enter');
  await expect(manualPath(page)).toHaveCount(0);
  await expect(resolvedPath(page)).toHaveText(target);
  await releaseBrowse(page, destination);
  await expect(resolvedPath(page)).toHaveText(target);
  await picker(page).getByRole('button', { name: en.directoryBrowser.back, exact: true }).click();
  await expect(resolvedPath(page)).toHaveText(destination);
  await picker(page).getByRole('button', { name: en.directoryBrowser.forward, exact: true }).click();
  await expect(resolvedPath(page)).toHaveText(target);
  await picker(page).getByRole('button', { name: en.directoryBrowser.select, exact: true }).click();
  await page.getByRole('button', { name: en.workbench.newProjectDialog.create, exact: true }).click();
  await expect(draft(page)).toBeVisible();
  expect((await writes(page, '/api/projects')).at(-1)?.body.folder_path).toBe(target);
});

for (const width of [390, 1366]) for (const reopen of [false, true]) test(`directory manual submit ${width} retains the ${reopen ? 'reopened' : 'newer'} draft and caret through Settings`, async ({ page }) => {
  await page.setViewportSize({ width, height: 768 });
  await openProjectPicker(page);
  await settings(page);
  await back(page);
  const edit = picker(page).getByRole('button', { name: en.directoryBrowser.editPath, exact: true });
  await edit.click();
  const submitted = '/已提交的目录';
  await manualPath(page).fill(submitted);
  await holdBrowse(page, submitted);
  await page.keyboard.press('Enter');
  await pendingBrowse(page, submitted);
  if (reopen) {
    await page.keyboard.press('Escape');
    await expect(manualPath(page)).toHaveCount(0);
    await edit.click();
    await expect(manualPath(page)).toHaveValue(initialDirectory);
  }
  await page.keyboard.press('ControlOrMeta+a');
  await page.keyboard.insertText('/提交后的新草稿🌱');
  expect(await selection(page)).toMatchObject({ start: 10, end: 10 });
  // History entry adds no key event to refresh a stale React onSelect record.
  await settings(page, true);
  await releaseBrowse(page, submitted);
  await expect.poll(() => page.evaluate((path) => window.homeMedia.browseCompletions.includes(path), submitted)).toBe(true);
  await expect(picker(page)).toHaveCount(0);
  await back(page);
  // Escape is an explicit cancellation; typing an unsubmitted newer draft is
  // not. Both cases retain the new draft/caret through route withdrawal.
  const committed = reopen ? initialDirectory : submitted;
  await expect(resolvedPath(page)).toHaveText(committed);
  await expect(manualPath(page)).toHaveValue('/提交后的新草稿🌱');
  // Observe after deferred modal cleanup, before a key could repair focus.
  await page.waitForTimeout(300);
  await expect(manualPath(page)).toBeFocused();
  expect(await selection(page)).toMatchObject({ start: 10, end: 10 });
  await page.keyboard.press('x');
  await expect(manualPath(page)).toHaveValue('/提交后的新草稿🌱x');
  await page.keyboard.insertText('续写🌿');
  await expect(manualPath(page)).toHaveValue('/提交后的新草稿🌱x续写🌿');
  await page.keyboard.press('Escape');
  await edit.click();
  await expect(manualPath(page)).toHaveValue(committed);
  expect(await selection(page)).toMatchObject({ start: 0, end: committed.length });
  await page.keyboard.press('Escape');
  const backButton = picker(page).getByRole('button', { name: en.directoryBrowser.back, exact: true });
  if (reopen) {
    await expect(backButton).toBeDisabled();
  } else {
    await backButton.click();
    await expect(resolvedPath(page)).toHaveText(initialDirectory);
    await picker(page).getByRole('button', { name: en.directoryBrowser.forward, exact: true }).click();
    await expect(resolvedPath(page)).toHaveText(submitted);
  }
});

for (const width of [390, 1366]) test(`directory selection ${width} follows text insertion, deletion and caret-only changes through repeated withdrawal`, async ({ page }) => {
  await page.setViewportSize({ width, height: 768 });
  await openProjectPicker(page);
  await settings(page);
  await back(page);
  await picker(page).getByRole('button', { name: en.directoryBrowser.editPath, exact: true }).click();
  await page.keyboard.press('ControlOrMeta+a');
  await page.keyboard.insertText('/粘贴式输入🌱');
  for (const edit of ['insert', 'delete', 'move', 'range'] as const) {
    if (edit === 'delete') await page.keyboard.press('Backspace');
    if (edit === 'move' || edit === 'range') await page.keyboard.press('ArrowLeft');
    if (edit === 'range') await page.keyboard.press('Shift+ArrowLeft');
    const value = await manualPath(page).inputValue();
    const before = await selection(page);
    expect(before.start).not.toBeNull();
    expect(before.end).not.toBeNull();
    if (edit === 'range') expect(before.end! - before.start!).toBeGreaterThan(0);
    else expect(before.start).toBe(before.end);
    await settings(page, true);
    await back(page);
    await page.waitForTimeout(300);
    await expect(manualPath(page)).toBeFocused();
    await expect(manualPath(page)).toHaveValue(value);
    expect(await selection(page)).toEqual(before);
    await page.keyboard.press('x');
    const continued = `${value.slice(0, before.start!)}x${value.slice(before.end!)}`;
    await expect(manualPath(page)).toHaveValue(continued);
    await page.keyboard.insertText('续');
    await expect(manualPath(page)).toHaveValue(`${value.slice(0, before.start!)}x续${value.slice(before.end!)}`);
  }
});

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
  await page.getByRole('button', { name: /^另一个项目(?: |$)/ }).click();
  await page.getByRole('button', { name: en.directoryBrowser.select, exact: true }).click();
}

test('no-project handoff preserves picker path/history/manual input and confirmation name across Settings', async ({ page }) => {
  await open(page, '&empty');
  await draft(page).fill('没有项目时也保留的中文任务');
  await send(page).click();
  await expect(page.getByRole('dialog', { name: en.directoryBrowser.title })).toBeVisible();
  await page.getByRole('button', { name: /^另一个项目(?: |$)/ }).click();
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
  await expect(page.getByPlaceholder(en.apps.fileBrowser.newFolderPlaceholder)).toHaveCount(0);
  await page.keyboard.press('Escape');
  await page.getByRole('button', { name: en.apps.fileBrowser.newFolder, exact: true }).click();
  const folderName = page.getByPlaceholder(en.apps.fileBrowser.newFolderPlaceholder);
  await folderName.fill('未创建文件夹');
  await settings(page);
  await back(page);
  await expect(folderName).toHaveValue('未创建文件夹');
  await expect(folderName).toBeFocused();
  expect(await writes(page, '/api/files/mkdir')).toHaveLength(0);
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
