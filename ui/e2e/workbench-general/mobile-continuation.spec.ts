import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';

import { NARROW, open, serveProduct } from './support';

/**
 * The Workbench home's two Settings continuations, on a phone.
 *
 * What is under test is a lifecycle, not a layout: the home holds an unsent
 * draft, an Agent pick and a workspace target that live nowhere but in the
 * component, so following either continuation used to throw them away. These
 * specs drive the real product through the whole round trip — type, pick,
 * follow, navigate inside Settings, come back the way the phone actually offers
 * — and then read that state back off the page.
 *
 * Every preservation claim is paired with a control that leaves the home by an
 * ordinary route and returns the same way, where the identical assertions must
 * FAIL. Without it, a reading could be re-derived rather than preserved.
 *
 * Hermetic like the rest of the suite: every request is answered or refused by
 * `serveProduct`, and each test asserts nothing was refused.
 */

const COMPOSER = 'Describe a task or ask a question...';
const DRAFT = '把「中文项目」的日志整理成周报，并附上待办清单';

// Two workspaces, because one cannot tell a preserved selection from the
// fallback: `useNewSession` resolves an explicit pick first and the most recent
// project second, so only a SECOND project makes the two readings differ. The
// draft above is about the older one, so picking it is what a user would do.
// Names and paths are non-ASCII for the same reason the shared fixture's are.
const RECENT_WORKSPACE = {
  id: 'proj-2',
  scope_id: 'scope-1',
  display_name: '设计稿归档',
  folder_path: '/Users/max/工作区/设计稿归档',
  created_at: '2026-01-02T00:00:00Z',
  last_active_at: '2026-02-02T00:00:00Z',
  archived: false,
  capabilities: { can_chat: true, has_folder: true },
};
const PICKED_WORKSPACE = {
  id: 'proj-1',
  scope_id: 'scope-1',
  display_name: '中文项目',
  folder_path: '/Users/max/工作区/中文项目',
  created_at: '2026-01-01T00:00:00Z',
  last_active_at: '2026-01-10T00:00:00Z',
  archived: false,
  capabilities: { can_chat: true, has_folder: true },
};
const PROJECTS = { projects: [PICKED_WORKSPACE, RECENT_WORKSPACE], sessions: {} };

/**
 * Capabilities are the boundary the product reads; the role name is only how
 * the server spells one out. `vibe/authorization.py` projects `can_chat` from
 * editor upward and `can_manage_instance` from member upward, so an editor is
 * the real role that can work here and cannot reach either continuation
 * destination. It is also the one that gets the workspace PICKER: creating a
 * local project needs `can_manage_projects && can_use_files`, and without it
 * the same chip lists the projects that already exist instead.
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

/** Routes registered after `serveProduct` win, so each of these replaces only what it names. */
const withWorkspaces = async (page: Page) => {
  await page.route('**/api/projects**', (route) => route.fulfill({ json: PROJECTS }));
  await page.route('**/api/workbench/projects-bootstrap**', (route) => route.fulfill({ json: PROJECTS }));
};
const asEditor = (page: Page) =>
  page.route('**/api/session', (route) => route.fulfill({ json: EDITOR_SESSION }));

const composer = (page: Page) => page.getByPlaceholder(COMPOSER);
const agentTrigger = (page: Page) => page.getByRole('button', { name: /codex|claude/ }).first();
// One accessible name for both chip branches: the workspace path, which is what
// actually distinguishes the two fixtures.
const workspaceChip = (page: Page) => page.getByRole('button', { name: /^Workspace: / });
const popover = (page: Page) => page.locator('[data-radix-popper-content-wrapper]');
const settingsSurface = (page: Page) => page.locator('[data-settings-overlay="true"]');
const inboxTab = (page: Page) => page.locator('nav.fixed.bottom-0 a[href="/inbox"]');

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
}

/** Types the draft and picks the non-default Agent, then marks the instance. */
async function primeHome(page: Page) {
  await composer(page).fill(DRAFT);
  await pickClaude(page);
  await expect(workspaceChip(page)).toHaveAccessibleName(`Workspace: ${RECENT_WORKSPACE.folder_path}`);
  await markInstance(page);
}

async function expectHomeIntact(page: Page) {
  await expect(composer(page)).toHaveValue(DRAFT);
  await expect(agentTrigger(page)).toHaveText(/claude/);
  await expect(workspaceChip(page)).toHaveAccessibleName(`Workspace: ${RECENT_WORKSPACE.folder_path}`);
  expect(await instanceSurvived(page)).toBe(true);
}

test.describe('Workbench continuations on a phone', () => {
  test.use({ viewport: NARROW });

  test('carries the composer through Settings and back on the phone continuation', async ({ page }) => {
    const denied = await serveProduct(page);
    await withWorkspaces(page);
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
    await page.getByRole('link', { name: 'Back to Workbench' }).click();
    await expect(page).toHaveURL(/127\.0\.0\.1:5213\/$/);

    await expectHomeIntact(page);
    expect(denied).toEqual([]);
  });

  test('carries the composer through the chat-apps continuation and browser Back', async ({ page }) => {
    const denied = await serveProduct(page);
    await withWorkspaces(page);
    await open(page, '/');
    await primeHome(page);

    await page.locator('main a[href="/settings/platforms"]').click();
    await expect(page).toHaveURL(/\/settings\/platforms$/);
    await expect(settingsSurface(page)).toBeVisible();

    // The OS/browser Back button, not an in-page control.
    await page.goBack();
    await expect(page).toHaveURL(/127\.0\.0\.1:5213\/$/);
    await expect(settingsSurface(page)).toHaveCount(0);

    await expectHomeIntact(page);
    expect(denied).toEqual([]);
  });

  test('starts a fresh home when the phone leaves by an ordinary route', async ({ page }) => {
    const denied = await serveProduct(page);
    await withWorkspaces(page);
    await open(page, '/');
    await primeHome(page);

    // The control for the two tests above: same phone, same fixture, same way
    // back — only the departure differs. An ordinary tab is not a continuation,
    // so the home is torn down and what comes back is a new one.
    await inboxTab(page).click();
    await expect(page).toHaveURL(/\/inbox$/);
    await page.goBack();
    await expect(page).toHaveURL(/127\.0\.0\.1:5213\/$/);

    await expect(composer(page)).toHaveValue('');
    await expect(agentTrigger(page)).toHaveText(/codex/);
    expect(await instanceSurvived(page)).toBe(false);
    expect(denied).toEqual([]);
  });

  test('keeps a picked workspace exactly as long as the home that holds it', async ({ page }) => {
    const denied = await serveProduct(page);
    await withWorkspaces(page);
    await asEditor(page);
    await open(page, '/');

    // The chip lists projects for someone who cannot create one, so this is a
    // real selection through the shipped UI rather than a seeded fixture value.
    await composer(page).fill(DRAFT);
    await pickClaude(page);
    await expect(workspaceChip(page)).toHaveAccessibleName(`Workspace: ${RECENT_WORKSPACE.folder_path}`);
    await workspaceChip(page).click();
    await popover(page).getByRole('button', { name: PICKED_WORKSPACE.display_name }).click();
    await expect(workspaceChip(page)).toHaveAccessibleName(`Workspace: ${PICKED_WORKSPACE.folder_path}`);
    await expect(workspaceChip(page)).toContainText(PICKED_WORKSPACE.display_name);

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
    await inboxTab(page).click();
    await expect(page).toHaveURL(/\/inbox$/);
    await page.goBack();
    await expect(page).toHaveURL(/127\.0\.0\.1:5213\/$/);
    await expect(composer(page)).toHaveValue('');
    await expect(workspaceChip(page)).toHaveAccessibleName(`Workspace: ${RECENT_WORKSPACE.folder_path}`);

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
