import { expect, test, type Page } from '@playwright/test';
import en from '../../src/i18n/en.json' with { type: 'json' };
import zh from '../../src/i18n/zh.json' with { type: 'json' };

const file = { name: '发布说明 中文.txt', mimeType: 'text/plain', buffer: Buffer.from('版本内容 ✓') };
const input = (page: Page) => page.getByPlaceholder(en.workbench.home.inputPlaceholder);
const send = (page: Page) => page.getByRole('button', { name: en.chat.compose.send, exact: true });
const writes = (page: Page, suffix: string) => page.evaluate((suffix) => window.homeMedia.writes.filter((write) => write.path.endsWith(suffix)), suffix);
const attach = async (page: Page) => page.locator('input[type=file]').setInputFiles(file);
async function open(page: Page, query = '') {
  await page.route('**/*', (route) => {
    const url = new URL(route.request().url());
    if (url.origin !== 'http://127.0.0.1:5217') return route.abort();
    return route.continue();
  });
  await page.goto(`/e2e/home-media/fixture.html${query}`);
  await expect(page.getByRole('button', { name: en.chat.compose.attach })).toBeEnabled();
}

test('draft files follow final project and Agent selection through picker cancellation and Settings', async ({ page }) => {
  await open(page);
  await input(page).fill('请阅读这份中文文件');
  await attach(page);
  await expect(page.getByText(file.name, { exact: true })).toBeVisible();
  expect(await writes(page, '/api/sessions')).toEqual([]);
  expect(await writes(page, '/attachments')).toEqual([]);
  const workspace = () => page.getByRole('button', { name: /^Workspace:/ });
  await workspace().click();
  expect((await writes(page, '/api/browse')).at(-1)?.body.path).toBe('/fixture/中文项目');
  await page.keyboard.press('Escape');
  await expect(input(page)).toHaveValue('请阅读这份中文文件');
  await workspace().click();
  await page.getByRole('button', { name: '另一个项目', exact: true }).click();
  await page.getByRole('button', { name: en.directoryBrowser.select, exact: true }).click();
  await page.getByRole('button', { name: en.workbench.newProjectDialog.pickFolder, exact: false }).click();
  expect((await writes(page, '/api/browse')).at(-1)?.body.path).toBe('/fixture/另一个项目');
  await page.getByRole('button', { name: en.directoryBrowser.select, exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: en.workbench.newProjectDialog.create, exact: true }).click();
  await expect(workspace()).toContainText('另一个项目');
  await page.getByRole('button', { name: /codex/ }).first().click();
  await page.getByRole('button', { name: /^claude/ }).click();
  await page.keyboard.press('Escape');
  await page.getByTestId('settings-entry').click();
  await expect(page.getByText('Fixture Settings')).toBeVisible();
  await expect(input(page)).toHaveValue('请阅读这份中文文件');
  expect(await input(page).evaluate((node) => Boolean(node.closest('[inert]')))).toBe(true);
  await page.getByRole('button', { name: 'Back to app' }).click();
  await expect(page.getByText(file.name, { exact: true })).toBeVisible();
  await send(page).dblclick();
  await expect(page.getByTestId('conversation')).toContainText('ses-1');
  expect(await writes(page, '/api/sessions')).toHaveLength(1);
  expect((await writes(page, '/api/sessions'))[0].body).toMatchObject({ project_id: 'project-1', agent_name: 'claude' });
  expect(await writes(page, '/messages')).toHaveLength(1);
  expect(await page.getByTestId('handoff').textContent()).toBe('null');
});

test('attachment-only send retries definite failures using the same scoped upload and keeps partial progress', async ({ page }) => {
  await open(page);
  await attach(page);
  await page.evaluate(() => { window.homeMedia.uploadFailures = 1; });
  await send(page).click();
  await expect(page.getByText(file.name, { exact: true })).toBeVisible();
  await expect(send(page)).toBeEnabled();
  expect(await writes(page, '/messages')).toEqual([]);
  await page.evaluate(() => { window.homeMedia.messageMode = 'rejected'; });
  await send(page).click();
  await expect(send(page)).toBeEnabled();
  await expect(page.getByText(file.name, { exact: true })).toBeVisible();
  await page.evaluate(() => { window.homeMedia.messageMode = 'success'; });
  await send(page).click();
  await expect(page.getByTestId('conversation')).toBeVisible();
  expect(await writes(page, '/api/sessions')).toHaveLength(1);
  expect(await writes(page, '/attachments')).toHaveLength(2);
  const submissions = await writes(page, '/messages');
  expect(submissions).toHaveLength(2);
  expect(submissions[1].body.text).toBe('');
  expect(submissions[1].body.content).toMatchObject({ attachments: [{ name: file.name, size: file.buffer.length }] });
});

for (const mode of ['unknown', 'network'] as const) test(`ambiguous ${mode} result retains input and blocks a duplicate first turn`, async ({ page }) => {
  await open(page);
  await input(page).fill('只发送一次');
  await attach(page);
  await page.evaluate((mode) => { window.homeMedia.messageMode = mode; }, mode);
  await send(page).click();
  await expect(input(page)).toHaveValue('只发送一次');
  await expect(page.getByText(file.name, { exact: true })).toBeVisible();
  await expect(page.getByRole('link', { name: en.newSession.inspectSession })).toHaveAttribute('href', '#/chat/ses-1');
  await expect(send(page)).toBeDisabled();
  expect(await writes(page, '/messages')).toHaveLength(1);
});

test('real microphone capture before any session supports cancel, final transcription, Settings and Send', async ({ page }) => {
  await open(page);
  await input(page).fill('原始草稿');
  await page.getByRole('button', { name: en.chat.compose.voice, exact: true }).click();
  await expect(page.getByRole('button', { name: en.chat.compose.stopRecording, exact: true })).toBeVisible();
  await page.getByRole('button', { name: en.chat.compose.cancelRecording, exact: true }).click();
  await expect(input(page)).toHaveValue('原始草稿');
  await page.getByRole('button', { name: en.chat.compose.voice, exact: true }).click();
  await expect(page.getByRole('button', { name: en.chat.compose.stopRecording, exact: true })).toBeVisible();
  await expect.poll(async () => page.locator('span').filter({ hasText: /^0:01$/ }).count()).toBeGreaterThan(0);
  await page.getByTestId('settings-entry').click();
  await page.getByRole('button', { name: 'Back to app' }).click();
  await page.getByRole('button', { name: en.chat.compose.stopRecording, exact: true }).click();
  await expect(input(page)).toHaveValue(/请整理中文发布说明。/);
  expect(await writes(page, '/api/sessions')).toEqual([]);
  expect(await writes(page, '/messages')).toEqual([]);
  expect(await writes(page, '/api/asr/transcribe')).not.toEqual([]);
  await send(page).click();
  await expect(page.getByTestId('conversation')).toBeVisible();
  expect(await writes(page, '/messages')).toHaveLength(1);
});

test('Settings opened during upload suspends submission and preserves the draft for explicit retry', async ({ page }) => {
  await open(page);
  await input(page).fill('保留到返回');
  await attach(page);
  await page.evaluate(() => { window.homeMedia.holdUpload = true; });
  await send(page).click();
  await expect.poll(async () => (await writes(page, '/attachments')).length).toBe(1);
  await page.getByTestId('settings-entry').click();
  await page.evaluate(() => window.homeMedia.releaseUpload());
  await expect(input(page)).toHaveValue('保留到返回');
  expect(await writes(page, '/messages')).toEqual([]);
  await page.getByRole('button', { name: 'Back to app' }).click();
  await send(page).click();
  await expect(page.getByTestId('conversation')).toBeVisible();
  expect(await writes(page, '/attachments')).toHaveLength(1);
  expect(await writes(page, '/messages')).toHaveLength(1);
});

for (const width of [320, 375, 390, 1366, 1600]) {
  for (const [lang, theme] of [['zh', 'dark'], ['en', 'dark'], ['zh', 'light'], ['en', 'light']]) {
    test(`home layout ${width} ${lang} ${theme}`, async ({ page }, testInfo) => {
      await page.setViewportSize({ width, height: width < 500 ? 700 : 768 });
      await page.route('**/*', (route) => new URL(route.request().url()).origin === 'http://127.0.0.1:5217' ? route.continue() : route.abort());
      await page.goto(`/e2e/home-media/fixture.html?lang=${lang}&theme=${theme}`);
      const copy = lang === 'zh' ? zh : en;
      await expect(page.getByRole('heading', { name: copy.workbench.home.heroTitle })).toBeVisible();
      await expect(page.getByRole('button', { name: copy.chat.compose.voice, exact: true })).toBeVisible();
      expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false);
      const attachBox = await page.getByRole('button', { name: copy.chat.compose.attach }).boundingBox();
      expect(attachBox?.width).toBe(28);
      expect(attachBox?.height).toBe(28);
      await page.getByRole('button', { name: copy.chat.compose.attach }).focus();
      await expect(page.getByRole('button', { name: copy.chat.compose.attach })).toBeFocused();
      await page.screenshot({ path: testInfo.outputPath('home.png'), fullPage: true });
    });
  }
}

test('voice failure retains the recording for explicit retry while preserving the surrounding draft', async ({ page }) => {
  await open(page);
  await input(page).fill('上下文：');
  await page.evaluate(() => { window.homeMedia.asrFailures = 1; });
  await input(page).focus();
  await page.keyboard.press('Alt+z');
  await expect(page.getByRole('button', { name: en.chat.compose.stopRecording, exact: true })).toBeVisible();
  await expect.poll(async () => page.locator('span').filter({ hasText: /^0:01$/ }).count()).toBeGreaterThan(0);
  await page.keyboard.press('Alt+z');
  await expect(page.getByRole('button', { name: en.chat.compose.voiceRetry, exact: true })).toBeVisible();
  await expect(input(page)).toHaveValue('上下文：');
  expect(await writes(page, '/api/sessions')).toEqual([]);
  await page.getByRole('button', { name: en.chat.compose.voiceRetry, exact: true }).click();
  await expect(input(page)).toHaveValue(/上下文：.*请整理中文发布说明。/);
  await expect(send(page)).toBeEnabled();
});

test('directory picker remains keyboard-contained and its footer reachable on a short narrow viewport', async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 440 });
  await open(page);
  await page.getByRole('button', { name: /^Workspace:/ }).click();
  const dialog = page.getByRole('dialog');
  const select = dialog.getByRole('button', { name: en.directoryBrowser.select, exact: true });
  await expect(select).toBeInViewport();
  await select.focus();
  for (let i = 0; i < 18; i++) await page.keyboard.press('Tab');
  expect(await dialog.evaluate((node) => node.contains(document.activeElement))).toBe(true);
  expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false);
  await page.keyboard.press('Escape');
  await expect(dialog).toHaveCount(0);
  await expect(input(page)).toBeVisible();
});

test('global new-session sheet can be reopened after uncertain admission without carrying a send lock or old draft', async ({ page }) => {
  await page.route('**/*', (route) => new URL(route.request().url()).origin === 'http://127.0.0.1:5217' ? route.continue() : route.abort());
  await page.goto('/e2e/home-media/fixture.html?surface=sheet');
  await page.getByRole('button', { name: 'Open new session' }).click();
  const draft = page.getByPlaceholder(en.newSession.placeholder);
  await draft.fill('结果未知的第一条');
  await page.evaluate(() => { window.homeMedia.messageMode = 'unknown'; });
  await send(page).click();
  await expect(draft).toHaveValue('结果未知的第一条');
  await expect(send(page)).toBeDisabled();
  await page.keyboard.press('Escape');
  await page.getByRole('button', { name: 'Open new session' }).click();
  await expect(draft).toHaveValue('');
  await expect(page.getByRole('link', { name: en.newSession.inspectSession })).toHaveCount(0);
  await draft.fill('独立的新对话');
  await page.evaluate(() => { window.homeMedia.messageMode = 'success'; });
  await send(page).click();
  await expect(page.getByTestId('conversation')).toContainText('ses-2');
  expect(await writes(page, '/api/sessions')).toHaveLength(2);
  expect((await writes(page, '/messages')).map((write) => write.body.text)).toEqual(['结果未知的第一条', '独立的新对话']);
});

for (const failure of ['upload', 'message'] as const) test(`terminal ${failure} scope is replaced on explicit retry, with the same original file`, async ({ page }) => {
  await open(page);
  await attach(page);
  await page.evaluate((failure) => {
    window.homeMedia.uploadTerminal = failure === 'upload';
    window.homeMedia.messageTerminal = failure === 'message' ? 409 : 0;
  }, failure);
  await send(page).click();
  await expect(page.getByText(file.name, { exact: true })).toBeVisible();
  await expect(send(page)).toBeEnabled();
  await page.evaluate(() => { window.homeMedia.uploadTerminal = false; window.homeMedia.messageTerminal = 0; });
  await send(page).click();
  await expect(page.getByTestId('conversation')).toContainText('ses-2');
  expect(await writes(page, '/api/sessions')).toHaveLength(2);
  expect((await writes(page, '/attachments')).map((write) => write.body)).toEqual([
    expect.objectContaining({ name: file.name, sessionId: 'ses-1', size: file.buffer.length }),
    expect.objectContaining({ name: file.name, sessionId: 'ses-2', size: file.buffer.length }),
  ]);
});
