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
const fixtureImages = {
  med_1: '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="48"><rect width="64" height="48" fill="#10b981"/></svg>',
  med_2: '<svg xmlns="http://www.w3.org/2000/svg" width="48" height="64"><rect width="48" height="64" fill="#2563eb"/></svg>',
};

const settledScale = (style: string | null): number => {
  const match = style?.match(/scale\(([-+]?\d*\.?\d+)\)/);
  return match ? Number(match[1]) : 1;
};

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

test('C-SETTINGS-06: retained image viewer releases keys while Settings is foreground', async ({ page }) => {
  const denied = await memberFixture(page);
  const pageErrors: string[] = [];
  page.on('pageerror', (error) => pageErrors.push(error.message));
  const sessionId = 'ses-c-image-viewer';
  const session = {
    id: sessionId,
    scope_id: 'fixture',
    project_id: 'proj-picked',
    title: '图片会话',
    agent_id: null,
    agent_name: 'claude',
    agent_backend: 'claude',
    agent_variant: null,
    model: null,
    reasoning_effort: null,
    status: 'active',
    pinned: false,
    agent_status: 'idle',
    workdir: '/fixture/星河项目',
    native_session_id: null,
    created_at: '2026-09-01T00:00:00Z',
    updated_at: '2026-09-01T00:00:00Z',
    last_active_at: null,
    metadata: {},
  };
  const message = {
    id: 'msg-image',
    scope_id: 'fixture',
    session_id: sessionId,
    platform: 'avibe',
    author: 'user',
    type: 'user',
    source: 'user',
    author_id: null,
    author_name: null,
    native_message_id: null,
    parent_native_message_id: null,
    projection: null,
    text: '附件',
    content: { attachments: [
      { url: '/api/media/med_1', name: '星河.svg', mime: 'image/svg+xml', width: 64, height: 48 },
      { url: '/api/media/med_2', name: '山海.svg', mime: 'image/svg+xml', width: 48, height: 64 },
    ] },
    metadata: {},
    created_at: '2026-09-01T00:00:00Z',
    updated_at: '2026-09-01T00:00:00Z',
    delivered_at: '2026-09-01T00:00:01Z',
    read_at: null,
  };
  await page.route(`**/api/sessions/${sessionId}/bootstrap`, (route) => route.fulfill({ json: {
    session,
    capabilities: { can_chat: true },
    agents: [{ id: 'agent-claude', name: 'claude', display_name: 'claude', description: null,
      backend: 'claude', model: null, reasoning_effort: null, enabled: true, archived: false,
      archived_at: null, source: 'builtin', updated_at: '2026-09-01T00:00:00Z' }],
    default_agent_name: 'claude',
    config: { ui: {} },
    messages: [message],
    next_after_id: null,
    next_before_id: null,
    queued: [],
    draft: { text: '', updated_at: null },
    turn_state: { in_flight: false, foreground: 'idle', native_turn_started: false,
      pending_input_count: 0, background_activities: [], pending_activity_output_count: 0,
    connection: 'connected' },
  } }));
  await page.route(`**/api/sessions/${sessionId}`, (route) => route.fulfill({ json: session }));
  await page.route(`**/api/sessions/${sessionId}/activity**`, (route) => route.fulfill({ json: { groups: [] } }));
  await page.route(`**/api/sessions/${sessionId}/turn-state`, (route) => route.fulfill({ json: {
    in_flight: false, foreground: 'idle', native_turn_started: false, pending_input_count: 0,
    background_activities: [], pending_activity_output_count: 0, connection: 'connected',
  } }));
  await page.route(`**/api/sessions/${sessionId}/queue`, (route) => route.fulfill({ json: [] }));
  await page.route('**/api/media/med_*', (route) => {
    const body = route.request().url().endsWith('/med_2') ? fixtureImages.med_2 : fixtureImages.med_1;
    return route.fulfill({ status: 200, contentType: 'image/svg+xml', body });
  });

  await page.setViewportSize({ width: 1366, height: 768 });
  await open(page, `/chat/${sessionId}`);
  // Seed a real forward Settings entry before opening the viewer. Browser
  // Forward then suspends the retained Chat route without inventing a control
  // inside the hidden lightbox.
  await toggle(page).click();
  await expect(settings(page)).toBeVisible();
  await settings(page).getByRole('button', { name: 'Close Settings' }).click();
  await expect(settings(page)).toBeHidden();
  const inlineImage = page.locator('span img[src="/api/media/med_1"]');
  await expect(inlineImage).toBeVisible();
  await expect(inlineImage).toHaveJSProperty('naturalWidth', 64);
  await expect(inlineImage).toHaveJSProperty('naturalHeight', 48);
  // Click the image body away from its hover download affordance so this is the
  // same pointer path a user takes on the media itself.
  await inlineImage.click({ position: { x: 8, y: 8 } });
  const viewer = page.locator('[role="dialog"][aria-modal="true"]');
  await expect(viewer).toBeVisible();
  const viewerImage = viewer.locator('img');
  const originalSrc = await viewerImage.getAttribute('src');
  const originalImageHandle = await viewerImage.elementHandle();
  expect(originalImageHandle).not.toBeNull();
  await expect(viewerImage).toHaveJSProperty('naturalWidth', 64);
  await expect(viewerImage).toHaveJSProperty('naturalHeight', 48);
  const originalTransform = viewer.locator('.react-transform-component');
  await viewer.getByRole('button', { name: 'Zoom in' }).click();
  let previousScale = 1;
  let stableSamples = 0;
  await expect.poll(async () => {
    const scale = settledScale(await originalTransform.getAttribute('style'));
    stableSamples = scale > 1 && Math.abs(scale - previousScale) < 0.01 ? stableSamples + 1 : 0;
    previousScale = scale;
    return stableSamples >= 3;
  }).toBe(true);
  const originalScale = previousScale;
  expect(Number.isFinite(originalScale)).toBe(true);

  await page.evaluate(() => {
    (window as Window & { __viewerLowerEscape?: number }).__viewerLowerEscape = 0;
    window.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') {
        const state = window as Window & { __viewerLowerEscape?: number };
        state.__viewerLowerEscape = (state.__viewerLowerEscape ?? 0) + 1;
      }
    });
  });

  // Forward returns to a real retained Settings entry. The viewer stays mounted
  // in the hidden origin, but its capture listener must withdraw so Settings
  // receives Escape and the viewer's image/zoom state remains intact.
  await page.goForward();
  await expect(settings(page)).toBeVisible();
  await expect(viewer).toBeHidden();
  await page.keyboard.press('ArrowRight');
  await expect(viewerImage).toHaveAttribute('src', originalSrc!);
  await page.keyboard.press('Escape');
  await expect(settings(page)).toBeHidden();
  await expect(viewer).toBeVisible();
  await expect(viewerImage).toHaveAttribute('src', originalSrc!);
  await expect(viewerImage).toHaveJSProperty('naturalWidth', 64);
  await expect.poll(async () => settledScale(await originalTransform.getAttribute('style'))).toBeCloseTo(originalScale);
  expect(await page.evaluate((node) => node === document.querySelector('[role="dialog"][aria-modal="true"] img'), originalImageHandle)).toBe(true);

  await page.evaluate(() => {
    (window as Window & { __viewerLowerEscape?: number }).__viewerLowerEscape = 0;
  });
  await page.keyboard.press('ArrowRight');
  await expect(viewerImage).toHaveAttribute('src', '/api/media/med_2');
  await expect(viewerImage).toHaveJSProperty('naturalWidth', 48);
  await page.keyboard.press('Escape');
  await expect(viewer).toHaveCount(0);
  expect(await page.evaluate(() => (window as Window & { __viewerLowerEscape?: number }).__viewerLowerEscape)).toBe(0);

  // Repeat the history/suspension path from the other end of the gallery so an
  // inactive ArrowLeft cannot wrap the retained viewer back to med_1.
  await toggle(page).click();
  await expect(settings(page)).toBeVisible();
  await settings(page).getByRole('button', { name: 'Close Settings' }).click();
  await expect(settings(page)).toBeHidden();
  await page.locator('span img[src="/api/media/med_2"]').click({ position: { x: 8, y: 8 } });
  await expect(viewer).toBeVisible();
  await expect(viewerImage).toHaveAttribute('src', '/api/media/med_2');
  await page.goForward();
  await expect(settings(page)).toBeVisible();
  await expect(viewer).toBeHidden();
  await page.keyboard.press('ArrowLeft');
  await expect(viewerImage).toHaveAttribute('src', '/api/media/med_2');
  await page.keyboard.press('Escape');
  await expect(settings(page)).toBeHidden();
  await expect(viewer).toBeVisible();
  await expect(viewerImage).toHaveAttribute('src', '/api/media/med_2');
  await page.keyboard.press('Escape');
  await expect(viewer).toHaveCount(0);
  expect(denied).toEqual([]);
  expect(pageErrors).toEqual([]);
});
