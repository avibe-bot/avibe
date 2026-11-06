// The phone's project list comes back the way the reader left it: the projects
// they opened stay open, and the list keeps its place, whether they come back
// from a conversation in the same document or in a new one (a reload, or a tab
// the browser discarded in the background, which reloads the conversation and
// then loads the list afresh on the way back).
import { devices, expect, test, type Page } from '@playwright/test';
import { open, serveProduct } from './support';

test.use({ viewport: { width: 390, height: 664 }, isMobile: true, hasTouch: true, userAgent: devices['iPhone 13'].userAgent });

const names = ['Avibe', 'chainbot', 'Research', 'Documents', 'Design'];
const projects = names.map((display_name, index) => ({
  id: `project-${index}`, scope_id: `scope-${index}`, display_name, folder_path: `/fixture/${index}`,
  created_at: `2026-01-0${index + 1}T00:00:00Z`, last_active_at: null, archived: false,
  capabilities: { can_chat: true, has_folder: true },
}));
const sessionsOf = (projectId: string) => Array.from({ length: 8 }, (_, index) => ({
  id: `${projectId}-s${index}`, title: `${projectId} conversation ${index + 1}`, project_id: projectId,
  scope_id: projects.find((p) => p.id === projectId)?.scope_id, status: 'active', agent_status: 'idle',
  pinned: false, visibility: 'foreground', created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z',
  last_active_at: '2026-01-01T00:00:00Z', metadata: {},
}));
const page0 = (projectId: string) => ({ sessions: sessionsOf(projectId), next_before_id: null });

/** The product against a server holding `projects`. While `held` is set, the
 *  tree's bootstrap is not answered, so only what the device stored can paint. */
async function serve(page: Page) {
  const bootstrap = { held: false };
  await serveProduct(page);
  // A reader the device can store for: what `/api/session` answers a local owner.
  await page.route('**/api/session', (route) => route.fulfill({
    json: { remote: false, authenticated: true, authorization_state: 'current', instance_kind: 'personal', instance_role: 'owner', author_id: 'local' },
  }));
  await page.route('**/api/projects**', (route) => route.fulfill({ json: { projects } }));
  await page.route('**/api/workbench/projects-bootstrap**', async (route) => {
    if (bootstrap.held) return;
    const asked = new URL(route.request().url()).searchParams.getAll('project_id');
    await route.fulfill({ json: { projects, sessions: Object.fromEntries(asked.map((id) => [id, page0(id)])) } });
  });
  await page.route('**/api/sessions**', (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === '/api/sessions') return route.fulfill({ json: page0(url.searchParams.get('project_id') ?? '') });
    const [, , , id, tail] = url.pathname.split('/');
    const projectId = id.replace(/-s\d+$/, '');
    const session = { ...sessionsOf(projectId).find((s) => s.id === id)!, agent_name: 'claude', agent_backend: 'claude', workdir: '/fixture', native_session_id: null };
    const turnState = { in_flight: false, foreground: 'idle', native_turn_started: false, pending_input_count: 0, background_activities: [], pending_activity_output_count: 0, connection: 'connected' };
    if (tail === 'bootstrap') return route.fulfill({ json: { session, capabilities: { can_chat: true }, agents: [], default_agent_name: null, config: { ui: {} }, messages: [], queued: [], next_after_id: null, next_before_id: null, draft: { text: '', updated_at: null }, turn_state: turnState } });
    if (tail === 'queue') return route.fulfill({ json: [] });
    if (tail === 'turn-state') return route.fulfill({ json: turnState });
    if (tail === 'activity') return route.fulfill({ json: { groups: [] } });
    return route.fulfill({ json: tail ? {} : session });
  });
  return bootstrap;
}

const scrollTop = (page: Page) => page.locator('#app-shell-scroll').evaluate((el) => el.scrollTop);
const header = (page: Page, name: string) => page.getByRole('main').getByText(name, { exact: true }).first();
const row = (page: Page, projectId: string, n: number) => page.getByText(`${projectId} conversation ${n}`, { exact: true });
const chatBack = (page: Page) => page.locator('[data-chat-header] button').first();

/** Opens Avibe and Research, scrolls down, and opens a Research conversation:
 *  the place the list was left at. */
async function openTwoAndAConversation(page: Page) {
  await open(page, '/projects');
  await header(page, 'Avibe').click();
  await expect(row(page, 'project-0', 1)).toBeVisible();
  await header(page, 'Research').click();
  await expect(row(page, 'project-2', 1)).toBeVisible();
  await row(page, 'project-2', 4).scrollIntoViewIfNeeded();
  // Where the list is as the reader taps: the click may scroll the row out from
  // under the tab bar first, so the place is read at the press.
  await page.evaluate(() => {
    const scroller = document.getElementById('app-shell-scroll')!;
    document.addEventListener('pointerdown', () => {
      (window as unknown as { __left: number }).__left = scroller.scrollTop;
    }, { capture: true, once: true });
  });
  await row(page, 'project-2', 4).click();
  const left = await page.evaluate(() => (window as unknown as { __left: number }).__left);
  expect(left).toBeGreaterThan(100);
  await expect(page).toHaveURL(/\/chat\/project-2-s3$/);
  await expect(chatBack(page)).toBeVisible();
  return left;
}

async function expectListAsLeft(page: Page, left: number) {
  await expect(page).toHaveURL(/\/projects$/);
  await expect(row(page, 'project-0', 1)).toBeAttached();
  await expect(row(page, 'project-2', 1)).toBeAttached();
  await expect.poll(() => scrollTop(page)).toBe(left);
}

for (const back of ['history back (the iOS swipe)', 'the conversation\'s back button'] as const) {
  test(`${back} returns to the list as it was left`, async ({ page }) => {
    await serve(page);
    const left = await openTwoAndAConversation(page);
    if (back === 'history back (the iOS swipe)') await page.goBack();
    else await chatBack(page).click();
    await expectListAsLeft(page, left);
  });
}

test('a conversation the browser reloaded goes back to the list as it was left', async ({ page }) => {
  await serve(page);
  const left = await openTwoAndAConversation(page);
  // A tab discarded in the background reloads where it was.
  await page.reload();
  await expect(chatBack(page)).toBeVisible();
  await page.goBack();
  await expectListAsLeft(page, left);
});

test('a reload paints the open projects from the device, never closed for a frame', async ({ page }) => {
  const bootstrap = await serve(page);
  await open(page, '/projects');
  await header(page, 'Avibe').click();
  await expect(row(page, 'project-0', 1)).toBeVisible();
  await header(page, 'Research').click();
  await expect(row(page, 'project-2', 1)).toBeVisible();

  // Every frame of the next document: whether it draws the list, and which open
  // projects it draws with their rows.
  await page.addInitScript(() => {
    const frames: { cards: number; avibe: boolean; research: boolean }[] = [];
    (window as unknown as { __frames: typeof frames }).__frames = frames;
    const sample = () => {
      const text = document.body?.textContent ?? '';
      frames.push({
        cards: document.querySelectorAll('[data-flip-id^="project:"]').length,
        avibe: text.includes('project-0 conversation 1'),
        research: text.includes('project-2 conversation 1'),
      });
      requestAnimationFrame(sample);
    };
    requestAnimationFrame(sample);
  });
  // Only what this device stored can paint: the server does not answer.
  bootstrap.held = true;
  await page.reload();
  await expect(row(page, 'project-0', 1)).toBeVisible();
  await expect(row(page, 'project-2', 1)).toBeVisible();
  await expect(header(page, 'chainbot')).toBeVisible();
  const frames = await page.evaluate(() => (window as unknown as { __frames: { cards: number; avibe: boolean; research: boolean }[] }).__frames);
  const drawn = frames.filter((frame) => frame.cards > 0);
  expect(drawn.length).toBeGreaterThan(0);
  expect(drawn.filter((frame) => !frame.avibe || !frame.research)).toEqual([]);
});
