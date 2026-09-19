import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';
import { open, serveProduct } from './support';

// Lane C: actual App/router/Composer/AgentRoutePicker with API responses confined
// to this Playwright context. No component, navigation, or persistence mocks.
const origin = 'http://127.0.0.1:5213';
const draft = '你好，整理项目「星河」的交付计划 🌱';
const projects = [
  { id: 'proj-recent', display_name: '近期项目', folder_path: '/fixture/近期项目' },
  { id: 'proj-picked', display_name: '星河项目', folder_path: '/fixture/星河项目' },
].map((project) => ({
  ...project, scope_id: 'fixture', archived: false,
  created_at: '2026-09-01T00:00:00Z', last_active_at: null,
  capabilities: { can_chat: true, has_folder: true },
}));
const caps = {
  is_instance_owner: false, can_read_instance: true, can_chat: true,
  can_manage_projects: false, can_manage_agents: false, can_manage_instance: false,
  can_manage_access_members: false, can_use_agents: true, can_use_skills: true,
  can_use_vault_secrets: true, can_use_show_pages: true, can_use_terminal_files: true,
  can_use_terminal: true, can_use_files: true, can_use_system: false,
};
const editor = { remote: true, authenticated: true, authorization_state: 'current',
  instance_kind: 'organization', instance_role: 'editor', capabilities: caps };
const textarea = (page: Page) => page.getByPlaceholder('Describe a task or ask a question...');
const settings = (page: Page) => page.locator('[data-settings-overlay="true"]');
const rail = (page: Page) => page.getByRole('navigation', { name: 'Settings sections' });
const toggle = (page: Page) => page.locator('aside [data-settings-toggle="true"]');

async function memberFixture(page: Page) {
  const denied = await serveProduct(page);
  await page.route('**/api/session', (route) => route.fulfill({ json: editor }));
  await page.route('**/api/projects**', (route) => route.fulfill({ json: { projects, sessions: {} } }));
  await page.route('**/api/workbench/projects-bootstrap**', (route) => route.fulfill({ json: { projects, sessions: {} } }));
  return denied;
}

async function frame(page: Page) {
  const nav = await rail(page).boundingBox();
  const pane = page.locator('nav[aria-label="Settings sections"] + section');
  return { rail: nav, content: await pane.locator(':scope > div').boundingBox() };
}

for (const width of [1200, 1366, 1920]) {
  test(`C-SETTINGS-01: direct and retained Settings have identical frame at ${width}`, async ({ page }) => {
    const denied = await serveProduct(page);
    await page.setViewportSize({ width, height: 768 });
    await open(page, '/');
    await page.getByRole('separator').focus();
    await page.keyboard.press('End');
    await textarea(page).fill(draft);
    await toggle(page).click();
    await expect(settings(page)).toBeVisible();
    expect(await settings(page).boundingBox()).toMatchObject({ x: 0, y: 0, width, height: 768 });
    await expect(page.getByRole('button', { name: 'Apps', exact: true })).toHaveCount(0);
    await expect(page.getByRole('separator')).toHaveCount(0);
    await expect(textarea(page)).toBeHidden();
    expect(await textarea(page).evaluate((node) => Boolean(node.closest('[inert][aria-hidden="true"]')))).toBe(true);
    const retained = await frame(page);
    expect(retained.rail?.width).toBe(196);
    expect(retained.content?.width).toBe(944);
    // Keyboard focus may only visit foreground controls; Ctrl+K may not awaken
    // the Workbench palette behind Settings or change its state for the return.
    for (let i = 0; i < 24; i += 1) {
      await page.keyboard.press('Tab');
      expect(await settings(page).evaluate((node) => node.contains(document.activeElement))).toBe(true);
    }
    await page.keyboard.press('Control+k');
    await settings(page).getByRole('button', { name: 'Close Settings' }).click();
    await expect(textarea(page)).toHaveValue(draft);
    await expect(toggle(page)).toBeFocused();
    await expect(page.getByRole('separator')).toHaveAttribute('aria-valuenow', '496');
    await open(page, '/settings/general');
    await expect(rail(page)).toBeVisible();
    expect(await frame(page)).toEqual(retained);
    expect(await page.locator('main#app-shell-scroll').boundingBox()).toMatchObject({ x: 0, width });
    await expect(page.locator('aside.fixed')).toBeHidden();
    expect(denied).toEqual([]);
  });
}

test('C-SETTINGS-02: preserves picked Agent/workspace, Unicode draft and history through internal navigation', async ({ page }) => {
  const denied = await memberFixture(page);
  await page.setViewportSize({ width: 1366, height: 768 });
  await open(page, '/');
  await textarea(page).fill(draft);
  const workspace = page.getByRole('button', { name: /^Workspace: / });
  await workspace.click();
  await page.locator('[data-radix-popper-content-wrapper]').getByRole('button', { name: '星河项目', exact: true }).click();
  const agent = page.getByRole('button', { name: /codex|claude/ }).first();
  await agent.click();
  await page.locator('[data-radix-popper-content-wrapper]').getByRole('button', { name: 'claude', exact: true }).click();
  await expect(agent).toContainText('claude');
  await textarea(page).evaluate((node) => { node.setAttribute('data-origin-instance', 'preserved'); });
  const index = await page.evaluate(() => window.history.state.idx);
  await toggle(page).click();
    await expect(rail(page).getByRole('link', { name: 'Backends', exact: true })).toHaveCount(0);
  await rail(page).getByRole('link', { name: 'Shortcuts', exact: true }).click();
  await expect(page).toHaveURL(/\/settings\/shortcuts$/);
  await page.goBack();
  await expect(page).toHaveURL(/\/settings\/general$/);
  await settings(page).getByRole('button', { name: 'Back to Avibe' }).click();
  await expect(page).toHaveURL(`${origin}/`);
  await expect(textarea(page)).toHaveValue(draft);
  await expect(textarea(page)).toHaveAttribute('data-origin-instance', 'preserved');
  await expect(workspace).toHaveAccessibleName('Workspace: /fixture/星河项目');
  await expect(agent).toContainText('claude');
  await expect(toggle(page)).toBeFocused();
  expect(await page.evaluate(() => window.history.state.idx)).toBe(index);
  // Browser Back also restores the same retained instance.
  await toggle(page).click();
  await expect(settings(page)).toBeVisible();
  await page.goBack();
  await expect(textarea(page)).toHaveValue(draft);
  await expect(toggle(page)).toBeFocused();
  expect(denied).toEqual([]);
});

for (const width of [320, 375, 390]) {
  test(`C-SETTINGS-03: direct alias and section navigation fit phone ${width}`, async ({ page }) => {
    const denied = await serveProduct(page);
    await page.setViewportSize({ width, height: 568 });
    await open(page, '/settings/appearance');
    await expect(page).toHaveURL(/\/settings\/general$/);
    await expect(page.getByRole('radiogroup')).toBeVisible();
    await expect(page.locator('aside.fixed')).toBeHidden();
    expect(await page.locator('main#app-shell-scroll').boundingBox()).toMatchObject({ x: 0, width });
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(width);
    await page.getByRole('link', { name: 'All settings' }).click();
    await expect(rail(page)).toBeVisible();
    await rail(page).getByRole('link', { name: 'General', exact: true }).click();
    await expect(page.getByRole('radiogroup')).toBeVisible();
    await page.getByRole('link', { name: 'All settings' }).click();
    await page.getByRole('link', { name: 'Back to Workbench' }).click();
    await expect(textarea(page)).toBeVisible();
    expect(denied).toEqual([]);
  });
}

test('C-SETTINGS-05: retains sidebar editor draft while withdrawing modal effects', async ({ page }) => {
  const denied = await serveProduct(page);
  await page.route('**/api/projects/proj-1/agents-md', (route) => route.fulfill({ json: {
    content: '# 项目约定', source: 'agents', symlinked: true, claude_is_regular_file: false,
  } }));
  await page.setViewportSize({ width: 1366, height: 768 });
  await open(page, '/');
  // Keep a real Settings history entry so browser Forward can suspend an open
  // modal without bypassing its pointer/focus trap or inventing a router mock.
  await toggle(page).click();
  await settings(page).getByRole('button', { name: 'Close Settings' }).click();
  await page.getByRole('button', { name: 'Project actions' }).click();
  await page.getByRole('button', { name: 'Edit AGENTS.md', exact: true }).click();
  const editorDraft = page.getByPlaceholder("Write this project's AGENTS.md in Markdown…");
  await editorDraft.fill('# 未保存的约定\n保留星河项目 🌱');
  await expect(page.locator('body')).toHaveCSS('pointer-events', 'none');
  await page.goForward();
  await expect(settings(page)).toBeVisible();
  await expect(editorDraft).toHaveCount(0);
  await expect(page.locator('body')).not.toHaveCSS('pointer-events', 'none');
  await expect(page.locator('body')).not.toHaveAttribute('data-scroll-locked');
  await rail(page).getByRole('link', { name: 'Shortcuts', exact: true }).click();
  await expect(page).toHaveURL(/\/settings\/shortcuts$/);
  await settings(page).getByRole('button', { name: 'Close Settings' }).click();
  await expect(editorDraft).toBeVisible();
  await expect(editorDraft).toHaveValue('# 未保存的约定\n保留星河项目 🌱');
  await page.getByRole('button', { name: 'Cancel', exact: true }).click();
  await expect(editorDraft).toHaveCount(0);
  await expect(page.locator('body')).not.toHaveCSS('pointer-events', 'none');
  expect(denied).toEqual([]);
});

test('C-SETTINGS-05: sidebar rename suspension keeps typed input without an implicit save', async ({ page }) => {
  const denied = await serveProduct(page);
  await page.setViewportSize({ width: 1366, height: 768 });
  await open(page, '/');
  await toggle(page).click();
  await settings(page).getByRole('button', { name: 'Close Settings' }).click();
  await page.getByRole('button', { name: 'Project actions' }).click();
  await page.getByRole('button', { name: 'Rename', exact: true }).click();
  const rename = page.locator('aside.fixed input');
  await rename.fill('星河项目未保存');
  await page.goForward();
  await expect(settings(page)).toBeVisible();
  await expect(rename).toBeHidden();
  await expect(rename).toHaveValue('星河项目未保存');
  await settings(page).getByRole('button', { name: 'Close Settings' }).click();
  await expect(rename).toBeVisible();
  await expect(rename).toHaveValue('星河项目未保存');
  expect(denied).toEqual([]);
});
