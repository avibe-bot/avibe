import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';

import { NARROW, open, serveProduct } from './support';

/**
 * The Workbench home's two Settings continuations, on a phone.
 *
 * What is under test is a lifecycle, not a layout: the home holds an unsent
 * draft, an Agent pick and a workspace target that live nowhere but in the
 * component, so following either continuation used to throw them away. These
 * specs drive the real product through the whole round trip — type, pick, open a
 * workspace through the directory browser, follow, navigate inside Settings,
 * come back the way the phone actually offers — and then read that state back
 * off the page.
 *
 * The preservation claims are paired with a control that leaves the home by an
 * ordinary tab and returns with browser Back — the same return path the
 * chat-apps case below uses — where the identical assertions must FAIL. Without
 * it, a reading could be re-derived rather than preserved. The phone-continuation
 * case returns through the in-app control instead, which is the other way the
 * phone offers out of Settings.
 *
 * Hermetic like the rest of the suite: every request is answered or refused by
 * `serveProduct`, and each test asserts nothing was refused. Unlike the rest of
 * the suite this file runs against the built app — the directory browser the
 * workspace pick goes through cannot be driven under the dev server's StrictMode
 * double-mount; `playwright.workbench-general-build.config.ts` explains why.
 */

const COMPOSER = 'Tell the agent what you want…';
const DRAFT = '把「中文项目」的日志整理成周报，并附上待办清单';

// Two workspaces, because one cannot tell a preserved selection from the
// fallback: `useNewSession` resolves an explicit pick first and the most recent
// project second, so only a SECOND project makes the two readings differ. The
// draft above is about the older one, so opening it is what a user would do.
// Names and paths are non-ASCII for the same reason the shared fixture's are.
const HOME_DIR = '/Users/max';
const WORKSPACES_DIR = '/Users/max/工作区';
const RECENT_WORKSPACE = {
  id: 'proj-2',
  scope_id: 'scope-1',
  display_name: '设计稿归档',
  folder_path: `${WORKSPACES_DIR}/设计稿归档`,
  created_at: '2026-01-02T00:00:00Z',
  last_active_at: '2026-02-02T00:00:00Z',
  archived: false,
  capabilities: { can_chat: true, has_folder: true },
};
const PICKED_WORKSPACE = {
  id: 'proj-1',
  scope_id: 'scope-1',
  display_name: '中文项目',
  folder_path: `${WORKSPACES_DIR}/中文项目`,
  created_at: '2026-01-01T00:00:00Z',
  last_active_at: '2026-01-10T00:00:00Z',
  archived: false,
  capabilities: { can_chat: true, has_folder: true },
};
const PROJECTS = { projects: [PICKED_WORKSPACE, RECENT_WORKSPACE], sessions: {} };

/** The folders the browse endpoint answers with — test-owned, never the real disk. */
const BROWSE: Record<string, { path: string; parent: string | null; dirs: { name: string; path: string }[] }> = {
  '~': { path: HOME_DIR, parent: '/Users', dirs: [{ name: '工作区', path: WORKSPACES_DIR }] },
  [HOME_DIR]: { path: HOME_DIR, parent: '/Users', dirs: [{ name: '工作区', path: WORKSPACES_DIR }] },
  [WORKSPACES_DIR]: {
    path: WORKSPACES_DIR,
    parent: HOME_DIR,
    dirs: [
      { name: PICKED_WORKSPACE.display_name, path: PICKED_WORKSPACE.folder_path },
      { name: RECENT_WORKSPACE.display_name, path: RECENT_WORKSPACE.folder_path },
    ],
  },
  // The browser opens on the current target's folder, so the walk starts at
  // the recent workspace and goes up through the breadcrumb — the same route a
  // user takes. Without this entry the very first listing fails and no folder
  // is reachable at all.
  [RECENT_WORKSPACE.folder_path]: { path: RECENT_WORKSPACE.folder_path, parent: WORKSPACES_DIR, dirs: [] },
  [PICKED_WORKSPACE.folder_path]: { path: PICKED_WORKSPACE.folder_path, parent: WORKSPACES_DIR, dirs: [] },
};

/**
 * Capabilities are the boundary the product reads; the role name is only how
 * the server spells one out. `vibe/authorization.py` projects `can_chat` and
 * `can_use_files` from editor upward and `can_manage_instance` and
 * `can_manage_projects` from member upward, so an editor is the real role that
 * can work here and cannot reach either continuation destination. It is also the
 * one that gets the workspace PICKER: opening a local folder needs
 * `can_manage_projects && can_use_files`, and without it the same chip lists the
 * projects that already exist instead. The manager branch of that same chip —
 * the directory browser — is what the continuation tests above exercise.
 */
const EDITOR_SESSION = {
  remote: true,
  authenticated: true,
  email: 'editor@example.com',
  authorization_state: 'current',
  instance_kind: 'organization',
  instance_role: 'editor',
  capabilities: {
    is_instance_owner: false,
    can_read_instance: true,
    can_chat: true,
    can_manage_projects: false,
    can_manage_agents: false,
    can_manage_instance: false,
    can_manage_access_members: false,
    can_use_agents: true,
    can_use_skills: true,
    can_use_vault_secrets: true,
    can_use_show_pages: true,
    can_use_terminal_files: true,
    can_use_terminal: true,
    can_use_files: true,
    can_use_system: false,
  },
};

/**
 * Routes registered after `serveProduct` win, so each of these replaces only
 * what it names. Two writes are answered here rather than refused — the
 * directory browse and the find-or-create the manager path actually makes.
 * Both are fulfilled from the fixtures above, so no real folder is ever listed
 * and no project is ever created; the returned array records every create
 * payload, so a second creation cannot pass unnoticed.
 */
const withWorkspaceApi = async (page: Page) => {
  const creates: unknown[] = [];
  await page.route('**/api/projects**', (route) => {
    const request = route.request();
    if (request.method() !== 'POST') return route.fulfill({ json: PROJECTS });
    // create_project is find-or-create by folder path, so opening a folder that
    // is already a project answers with that project instead of a new row.
    const payload = request.postDataJSON() as { folder_path?: string };
    creates.push(payload);
    const existing = PROJECTS.projects.find((project) => project.folder_path === payload.folder_path);
    return existing
      ? route.fulfill({ json: existing })
      : route.fulfill({ status: 404, json: { error: `no project fixture for ${payload.folder_path}` } });
  });
  await page.route('**/api/workbench/projects-bootstrap**', (route) => route.fulfill({ json: PROJECTS }));
  await page.route('**/api/browse', (route) => {
    const { path } = route.request().postDataJSON() as { path: string };
    const answer = BROWSE[path];
    return route.fulfill(
      answer ? { json: { ok: true, ...answer } } : { json: { ok: false, error: `no folder fixture for ${path}` } },
    );
  });
  return creates;
};

const asEditor = (page: Page) =>
  page.route('**/api/session', (route) => route.fulfill({ json: EDITOR_SESSION }));

const composer = (page: Page) => page.getByPlaceholder(COMPOSER);
const agentTrigger = (page: Page) => page.getByRole('button', { name: /codex|claude/ }).first();
// The home's own project row, not the sidebar tree. The mint fill is how the
// shared picker marks the resolved target, so "which workspace is selected" is
// read off the named chip rather than off a separate label.
const homeMain = (page: Page) => page.locator('main#app-shell-scroll');
const projectChip = (page: Page, name: string) => homeMain(page).getByRole('button', { name, exact: true });
const expectSelectedProject = async (page: Page, name: string) => {
  await expect(projectChip(page, name)).toHaveClass(/bg-mint-soft/);
};
// Opening a folder is the card's own action now, not a chip beside the input.
const openProjectPill = (page: Page) => homeMain(page).getByRole('button', { name: 'Open project', exact: true });
const popover = (page: Page) => page.locator('[data-radix-popper-content-wrapper]');
const settingsSurface = (page: Page) => page.locator('[data-settings-overlay="true"]');
const inboxTab = (page: Page) => page.locator('nav.fixed.bottom-0 a[href="/inbox"]');
const brandHome = (page: Page) => page.locator('header a[href="/"]');
// By label, not by role: what the phone's root back must do is leave Settings
// behind, and asserting that through the control's element type would pass on a
// link that merely pushed another home. Whether it is a close action at all is
// the unit test's job.
const backToWorkbench = (page: Page) => page.getByLabel('Back to Workbench');

/**
 * Where the current document sits in the session history — the number
 * `closeSettingsOverlay` unwinds to. A push moves it forward, so a return that
 * lands back on the recorded one proves the Settings entries were left behind
 * rather than stacked in front of the home.
 */
const historyIndex = (page: Page) =>
  page.evaluate(() => (window.history.state as { idx?: number } | null)?.idx ?? null);

/**
 * Marks the live composer element. If the home is torn down and rebuilt, the
 * replacement carries no mark — so this proves the SAME instance survived,
 * which is what keeps every other piece of its state alive too.
 */
const markInstance = (page: Page) =>
  composer(page).evaluate((node) => {
    (node as HTMLTextAreaElement & { __avibeMark?: string }).__avibeMark = 'home-1';
  });

const instanceSurvived = (page: Page) =>
  composer(page).evaluate(
    (node) => (node as HTMLTextAreaElement & { __avibeMark?: string }).__avibeMark === 'home-1',
  );

/** Picks the non-default Agent through the real picker. */
async function pickClaude(page: Page) {
  await expect(agentTrigger(page)).toHaveText(/codex/);
  await agentTrigger(page).click();
  await popover(page).getByRole('button', { name: 'claude', exact: true }).click();
  await expect(agentTrigger(page)).toHaveText(/claude/);
  // The picker keeps its panel open after a pick so the model and effort rows
  // stay reachable. It is anchored above the input column, so leaving it open
  // would cover the project row; close it the way a user does.
  await page.keyboard.press('Escape');
  await expect(popover(page)).toHaveCount(0);
}

/**
 * Opens the non-default workspace the way someone who can manage projects
 * actually does it: the card's Open project starts the directory browser on the
 * current target's folder, the browser walks up and into a real one, and the
 * confirm card fires create_project — which is
 * find-or-create by path, so opening a folder that is already a project selects
 * that project. No product code is aware of this test; only the endpoints are.
 */
async function openPickedWorkspace(page: Page) {
  await openProjectPill(page).click();
  const browser = page.getByRole('dialog', { name: 'Select Project Folder' });
  await browser.getByRole('button', { name: '工作区' }).click();
  await browser.getByRole('button', { name: PICKED_WORKSPACE.display_name }).click();
  await expect(browser.locator('code')).toHaveText(PICKED_WORKSPACE.folder_path);
  await browser.getByRole('button', { name: 'Select' }).click();

  const confirm = page.getByRole('dialog', { name: 'Open project' });
  await expect(confirm).toContainText(PICKED_WORKSPACE.folder_path);
  await confirm.getByRole('button', { name: 'Open project' }).click();

  await expectSelectedProject(page, PICKED_WORKSPACE.display_name);
}

/**
 * Types the draft, opens the non-default workspace, picks the non-default Agent,
 * then marks the instance. Workspace before Agent because switching projects
 * deliberately drops a stale Agent pick so the new project's default applies —
 * doing it the other way round would be testing that rule, not the round trip.
 */
async function primeHome(page: Page) {
  await composer(page).fill(DRAFT);
  // The home resolves the most recent project on its own; the open below is
  // what makes a preserved reading distinguishable from a re-derived one.
  await expectSelectedProject(page, RECENT_WORKSPACE.display_name);
  await openPickedWorkspace(page);
  await pickClaude(page);
  await markInstance(page);
}

/**
 * Leaves the home by the ordinary Inbox tab, and waits for the departure to
 * have actually happened. React Router writes history synchronously but commits
 * the new route inside a transition, so the URL reads `/inbox` while the home is
 * still the mounted tree; turning round inside that window supersedes the
 * pending transition and the home is never torn down at all. The control is
 * about what a real teardown costs, so it waits for the destination to be on
 * screen and the home's composer to be gone before going Back.
 */
async function leaveByInboxTab(page: Page) {
  await inboxTab(page).click();
  await expect(page).toHaveURL(/\/inbox$/);
  await expect(page.getByRole('heading', { level: 1, name: 'Inbox' })).toBeVisible();
  await expect(composer(page)).toHaveCount(0);
}

async function expectHomeIntact(page: Page, creates: unknown[]) {
  await expect(composer(page)).toHaveValue(DRAFT);
  await expect(agentTrigger(page)).toHaveText(/claude/);
  await expectSelectedProject(page, PICKED_WORKSPACE.display_name);
  expect(await instanceSurvived(page)).toBe(true);
  // Coming back must not re-run the open: the same home is still there, so the
  // one create the user made is the only one the server ever sees.
  expect(creates).toEqual([{ folder_path: PICKED_WORKSPACE.folder_path }]);
}

test.describe('Workbench continuations on a phone', () => {
  test.use({ viewport: NARROW });

  test('carries the composer through Settings and back on the phone continuation', async ({ page }) => {
    const denied = await serveProduct(page);
    const creates = await withWorkspaceApi(page);
    await open(page, '/');
    await primeHome(page);

    await page.getByRole('link', { name: 'Continue on your phone' }).click();
    await expect(page).toHaveURL(/\/settings\/remote-access$/);

    // Full-screen: below md the Settings surface is the whole viewport, with no
    // sidebar offset and no shell chrome peeking out from under it.
    await expect(settingsSurface(page)).toBeVisible();
    expect(await settingsSurface(page).boundingBox()).toMatchObject({ x: 0, y: 0, width: NARROW.width });

    // Settings-internal navigation, including the page that absorbed the retired
    // Appearance section: the theme controls are now part of General.
    await page.getByRole('link', { name: 'All settings' }).click();
    await expect(page).toHaveURL(/\/settings$/);
    await page.getByRole('link', { name: 'General' }).click();
    await expect(page).toHaveURL(/\/settings\/general$/);
    await expect(page.getByRole('radiogroup', { name: 'Appearance' })).toBeVisible();
    await expect(page.getByRole('radio', { name: 'Dark' })).toBeVisible();

    // Out the way the phone offers: section list, then the Workbench.
    await page.getByRole('link', { name: 'All settings' }).click();
    await backToWorkbench(page).click();
    await expect(page).toHaveURL(/127\.0\.0\.1:5213\/$/);

    await expectHomeIntact(page, creates);
    expect(denied).toEqual([]);
  });

  test('carries the composer through the chat-apps continuation and browser Back', async ({ page }) => {
    const denied = await serveProduct(page);
    const creates = await withWorkspaceApi(page);
    await open(page, '/');
    await primeHome(page);

    await page.locator('main a[href="/settings/platforms"]').click();
    await expect(page).toHaveURL(/\/settings\/platforms$/);
    await expect(settingsSurface(page)).toBeVisible();

    // The OS/browser Back button, not an in-page control.
    await page.goBack();
    await expect(page).toHaveURL(/127\.0\.0\.1:5213\/$/);
    await expect(settingsSurface(page)).toHaveCount(0);

    await expectHomeIntact(page, creates);
    expect(denied).toEqual([]);
  });

  test('leaves no Settings entry behind when the phone returns to the Workbench', async ({ page }) => {
    const denied = await serveProduct(page);
    const creates = await withWorkspaceApi(page);

    // A genuine page before the home, so "where Back goes afterwards" has an
    // answer that is not the document load itself.
    await open(page, '/inbox');
    await expect(page.getByRole('heading', { level: 1, name: 'Inbox' })).toBeVisible();
    await brandHome(page).click();
    await expect(page).toHaveURL(/127\.0\.0\.1:5213\/$/);
    const homeIndex = await historyIndex(page);
    expect(homeIndex).toBe(1);

    await primeHome(page);

    // Everything the phone can stack in front of the home: a continuation, the
    // section list, a section, the list again.
    await page.getByRole('link', { name: 'Continue on your phone' }).click();
    await expect(page).toHaveURL(/\/settings\/remote-access$/);
    await page.getByRole('link', { name: 'All settings' }).click();
    await expect(page).toHaveURL(/\/settings$/);
    await page.getByRole('link', { name: 'General' }).click();
    await expect(page).toHaveURL(/\/settings\/general$/);
    await page.getByRole('link', { name: 'All settings' }).click();
    await expect(page).toHaveURL(/\/settings$/);
    expect(await historyIndex(page)).toBeGreaterThan(homeIndex!);

    // The real exit control, not a scripted history call.
    await backToWorkbench(page).click();
    await expect(page).toHaveURL(/127\.0\.0\.1:5213\/$/);
    await expect(settingsSurface(page)).toHaveCount(0);
    // The home the user left, at the entry they left it from: everything opened
    // over it is gone from the stack rather than sitting one Back away.
    expect(await historyIndex(page)).toBe(homeIndex);
    await expectHomeIntact(page, creates);

    // The other continuation, out through the same control. The chevron inside
    // Settings still steps up the rail — the origin rides along with it — and
    // only the root action closes.
    await page.locator('main a[href="/settings/platforms"]').click();
    await expect(page).toHaveURL(/\/settings\/platforms$/);
    await page.getByRole('link', { name: 'All settings' }).click();
    await expect(page).toHaveURL(/\/settings$/);
    await backToWorkbench(page).click();
    await expect(page).toHaveURL(/127\.0\.0\.1:5213\/$/);
    expect(await historyIndex(page)).toBe(homeIndex);
    await expectHomeIntact(page, creates);

    // And Back from there is the page before the home, not Settings again.
    await page.goBack();
    await expect(page).toHaveURL(/\/inbox$/);
    await expect(page.getByRole('heading', { level: 1, name: 'Inbox' })).toBeVisible();
    await expect(settingsSurface(page)).toHaveCount(0);
    expect(denied).toEqual([]);
  });

  test('starts a fresh home when the phone leaves by an ordinary route', async ({ page }) => {
    const denied = await serveProduct(page);
    const creates = await withWorkspaceApi(page);
    await open(page, '/');
    await primeHome(page);

    // The control for the two tests above: same phone, same fixture, same
    // browser Back as the chat-apps case — only the departure differs. An
    // ordinary tab is not a continuation, so the home is torn down and what
    // comes back is a new one that resolves the most recent project again.
    await leaveByInboxTab(page);
    await page.goBack();
    await expect(page).toHaveURL(/127\.0\.0\.1:5213\/$/);

    await expect(composer(page)).toHaveValue('');
    await expect(agentTrigger(page)).toHaveText(/codex/);
    await expectSelectedProject(page, RECENT_WORKSPACE.display_name);
    expect(await instanceSurvived(page)).toBe(false);
    expect(creates).toEqual([{ folder_path: PICKED_WORKSPACE.folder_path }]);
    expect(denied).toEqual([]);
  });

  test('keeps a picked workspace exactly as long as the home that holds it', async ({ page }) => {
    const denied = await serveProduct(page);
    const creates = await withWorkspaceApi(page);
    await asEditor(page);
    await open(page, '/');

    // The other half of the same row: someone who cannot open folders picks
    // among the projects they already have, with no create call at all — and
    // gets no folder-opening affordance to reach for.
    await composer(page).fill(DRAFT);
    await pickClaude(page);
    await expectSelectedProject(page, RECENT_WORKSPACE.display_name);
    await expect(openProjectPill(page)).toHaveCount(0);
    await projectChip(page, PICKED_WORKSPACE.display_name).click();
    await expectSelectedProject(page, PICKED_WORKSPACE.display_name);

    // No continuation to offer: both destinations sit behind `can_manage_instance`,
    // which this role does not have — so neither the links nor the sentence that
    // only exists to introduce them is drawn, and chat is untouched.
    await expect(page.getByText('Continue on your phone')).toHaveCount(0);
    await expect(page.getByText('Connect chat apps in')).toHaveCount(0);
    await expect(page.locator('main a[href^="/settings"]')).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Send' })).toBeEnabled();

    // The pick is component state of the same class as the draft: a home that is
    // torn down comes back on the most recent project, not the chosen one. That
    // is what a continuation has to preserve, and what a refetch cannot restore.
    await leaveByInboxTab(page);
    await page.goBack();
    await expect(page).toHaveURL(/127\.0\.0\.1:5213\/$/);
    await expect(composer(page)).toHaveValue('');
    await expectSelectedProject(page, RECENT_WORKSPACE.display_name);

    expect(creates).toEqual([]);
    expect(denied).toEqual([]);
  });

  test('lands a retired Settings link on the page that took its content over', async ({ page }) => {
    const denied = await serveProduct(page);

    // A stale bookmark is the only way into the alias — nothing in the product
    // links to it any more — so this is a document load, not an in-app hop.
    await open(page, '/settings/appearance');
    await expect(page).toHaveURL(/\/settings\/general$/);
    await expect(page.getByRole('radiogroup', { name: 'Appearance' })).toBeVisible();
    // Landed directly: there is no covered surface to retain, so Settings is the
    // primary route rather than an overlay over a home that was never rendered.
    await expect(settingsSurface(page)).toHaveCount(0);
    await page.getByRole('link', { name: 'All settings' }).click();
    await expect(page).toHaveURL(/\/settings$/);

    // Account did not move: it is still part of the Replies page.
    await open(page, '/settings/account');
    await expect(page).toHaveURL(/\/settings\/replies$/);
    await expect(settingsSurface(page)).toHaveCount(0);

    expect(denied).toEqual([]);
  });
});
