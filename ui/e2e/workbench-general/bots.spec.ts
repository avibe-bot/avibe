import { expect, test, type Page } from '@playwright/test';

import { DESKTOP, open, serveProduct, type Lang } from './support';

// The Bots surfaces (docs/plans/bots.md D3, D5) against a hermetic fixture: the
// strip in the Inbox's old place with the Inbox as the brand row's icon, the
// hover card and context menu, the add dialog, a Bot's conversation, Bot rows in
// the Inbox. The typed `@<bot:handle>` marker is MentionEditor's own unit test.

const SHOTS = 'e2e/.artifacts/workbench-general/shots';

type FixtureBot = {
  handle: string;
  display_name: string;
  avatar: { kind: 'mascot' } | { kind: 'emoji'; emoji: string; hue: number } | null;
  role: string;
  running?: boolean;
};

const MAIN: FixtureBot = { handle: 'main', display_name: '', avatar: { kind: 'mascot' }, role: "The user's main Bot and first point of contact." };
const CSO: FixtureBot = { handle: 'cso', display_name: '首席安全官', avatar: { kind: 'emoji', emoji: '🛡️', hue: 200 }, role: '依赖与权限的安全审计，每周汇报' };
const PM: FixtureBot = { handle: 'pm', display_name: 'Product Lead', avatar: { kind: 'emoji', emoji: '🧭', hue: 42 }, role: 'Owns the backlog and its priorities', running: true };
const EXTRA: FixtureBot[] = [
  { handle: 'finance', display_name: '财务助理', avatar: null, role: '账单、报销与预算' },
  { handle: 'designer', display_name: 'Designer', avatar: { kind: 'emoji', emoji: '🎨', hue: 330 }, role: 'Interface and visual design' },
  { handle: 'research', display_name: '市场研究员', avatar: { kind: 'emoji', emoji: '🔭', hue: 265 }, role: '竞品与市场动态' },
];

const payload = (bot: FixtureBot, position: number) => ({
  handle: bot.handle,
  display_name: bot.display_name,
  avatar: bot.avatar,
  role: bot.role,
  agent_name: bot.handle,
  session_id: `ses_${bot.handle}`,
  position,
  builtin: bot.handle === 'main',
});

const AGENTS = [
  { id: 'agt-main', name: 'main', backend: 'vibey', enabled: true, archived: false, source: 'builtin', model: 'gpt-6-astra', reasoning_effort: 'high', description: MAIN.role, bot: { handle: 'main', display_name: '', builtin: true } },
  { id: 'agt-vibey', name: 'vibey', backend: 'vibey', enabled: true, archived: false, source: 'builtin', model: 'gpt-6-astra', reasoning_effort: 'high', description: 'Default Agent for the Vibey backend.', bot: null },
  { id: 'agt-auditor', name: 'security-auditor', backend: 'vibey', enabled: true, archived: false, source: 'user', model: 'gpt-6-astra', reasoning_effort: 'medium', description: 'Dependency and permission audits', bot: null },
  { id: 'agt-cso', name: 'cso', backend: 'vibey', enabled: true, archived: false, source: 'user', model: 'gpt-6-astra', reasoning_effort: 'medium', description: CSO.role, bot: { handle: 'cso', display_name: CSO.display_name, builtin: false } },
  { id: 'agt-reviewer', name: 'reviewer', backend: 'claude', enabled: true, archived: false, source: 'user', model: 'claude-opus-4-7', reasoning_effort: 'high', description: 'Reviews pull requests', bot: null },
];

const NOW = new Date('2026-11-01T08:00:00Z').getTime();
const ago = (minutes: number) => new Date(NOW - minutes * 60_000).toISOString();

const INBOX = {
  sessions: [
    { session_id: 'ses_cso', scope_id: null, project_id: null, project_name: null, title: CSO.display_name, last_activity_at: ago(12), last_message_author: 'agent', replied: false, preview_text: 'avault 依赖里发现 2 个高危漏洞，已整理修复建议和影响范围。', preview_at: ago(12), unread_count: 2, unread: true },
    { session_id: 'ses4project1', scope_id: 'scope-1', project_id: 'proj-1', project_name: '中文项目', title: 'Fix the cache break', last_activity_at: ago(21), last_message_author: 'agent', replied: false, preview_text: 'The prefix broke when old tool results were cleared; the PR is open.', preview_at: ago(21), unread_count: 3, unread: true },
    { session_id: 'ses_main', scope_id: null, project_id: null, project_name: null, title: 'Vibey', last_activity_at: ago(110), last_message_author: 'agent', replied: false, preview_text: 'The weekly report draft is in Feishu, waiting for your go-ahead.', preview_at: ago(110), unread_count: 0, unread: false },
  ],
  next_cursor: null,
  unread_by_session: { ses_cso: 2, ses4project1: 3 },
  unread_total: 5,
  unread_sessions: 2,
};

async function serveBots(page: Page, lang: Lang, bots: FixtureBot[]) {
  const denied = await serveProduct(page, lang);
  await page.clock.setFixedTime(NOW);
  await page.route('**/api/bots', (route) => route.fulfill({ json: { ok: true, bots: bots.map(payload) } }));
  await page.route('**/api/agents**', (route) => route.fulfill({ json: { ok: true, agents: AGENTS, default_agent_name: 'vibey' } }));
  await page.route('**/api/inbox**', (route) => {
    const only = new URL(route.request().url()).searchParams.get('session');
    return route.fulfill({ json: only ? { ...INBOX, sessions: INBOX.sessions.filter((row) => row.session_id === only) } : INBOX });
  });
  await page.route('**/api/bots/handle-suggestion', async (route) => {
    const body = route.request().postDataJSON() as { display_name: string };
    // The model's answer takes a moment; the dialog shows that it is coming.
    await new Promise((resolve) => setTimeout(resolve, 900));
    return route.fulfill({ json: { ok: true, handle: /[^ -~]/.test(body.display_name) ? 'cso-2' : body.display_name.toLowerCase(), source: 'model' } });
  });
  for (const bot of bots) {
    const sessionId = `ses_${bot.handle}`;
    const session = {
      id: sessionId, scope_id: null, project_id: null, title: bot.display_name || 'Vibey',
      agent_id: `agt-${bot.handle}`, agent_name: bot.handle, agent_backend: 'vibey', agent_variant: 'vibey',
      model: 'gpt-6-astra', reasoning_effort: 'high', speed: null, status: 'active',
      agent_status: bot.running ? 'running' : 'idle', pinned: false, visibility: 'foreground',
      workdir: `/fixture/.avibe/bots/${bot.handle}`, native_session_id: null, metadata: { kind: bot.handle === 'main' ? 'main' : 'bot' },
      created_at: ago(600), updated_at: ago(12),
    };
    const turnState = {
      in_flight: false, foreground: 'idle', native_turn_started: false, pending_input_count: 0,
      background_activities: [], pending_activity_output_count: 0, connection: 'connected',
    };
    await page.route(`**/api/sessions/${sessionId}/bootstrap`, (route) => route.fulfill({ json: {
      session, capabilities: { can_chat: true }, agents: AGENTS, default_agent_name: 'vibey', config: { ui: {} },
      messages: [], queued: [], next_after_id: null, next_before_id: null,
      draft: { text: '', updated_at: null }, turn_state: turnState,
    } }));
    await page.route(`**/api/sessions/${sessionId}`, (route) => route.fulfill({ json: session }));
    await page.route(`**/api/sessions/${sessionId}/queue`, (route) => route.fulfill({ json: [] }));
    await page.route(`**/api/sessions/${sessionId}/turn-state`, (route) => route.fulfill({ json: turnState }));
    await page.route(`**/api/sessions/${sessionId}/activity**`, (route) => route.fulfill({ json: { groups: [] } }));
    await page.route(`**/api/sessions/${sessionId}/mark-read`, (route) => route.fulfill({ json: { ok: true } }));
    await page.route(`**/api/sessions/${sessionId}/draft`, (route) => route.fulfill({ json: { ok: true, draft: { text: '', updated_at: null } } }));
  }
  await page.route('**/api/asr/status', (route) => route.fulfill({ json: { available: false } }));
  return { denied };
}

const sidebarOf = (page: Page) => page.locator('aside').filter({ has: page.locator('#workbench-capability-nav') });

for (const theme of ['light', 'dark'] as const) {
  test(`the Bots strip takes the Inbox's place, with its hover card and menu (${theme})`, async ({ page }) => {
    const { denied } = await serveBots(page, 'zh', [MAIN, CSO, PM]);
    await page.setViewportSize(DESKTOP);
    await open(page, '/', { lang: 'zh', theme });
    const sidebar = sidebarOf(page);
    const strip = sidebar.getByRole('region', { name: '伙伴' });
    await expect(strip).toBeVisible();

    // The brand reads "Agent OS" alone, and the Inbox is the icon at its right.
    await expect(sidebar.getByText('Agent OS', { exact: true })).toBeVisible();
    const inbox = sidebar.getByRole('link', { name: '收件箱，5 条未读' });
    await expect(inbox).toHaveText('5');
    const brand = (await sidebar.locator('a[href="/"]').boundingBox())!;
    const inboxBox = (await inbox.boundingBox())!;
    expect(Math.abs(inboxBox.y + inboxBox.height / 2 - (brand.y + brand.height / 2))).toBeLessThan(2);
    expect((await strip.boundingBox())!.y).toBeGreaterThan(brand.y + brand.height);

    // The main Bot first, under its localized default name; Add follows inline.
    const slots = strip.locator('[data-bot-handle]');
    await expect(slots).toHaveCount(3);
    await expect(slots.first()).toHaveAttribute('data-bot-handle', 'main');
    await expect(slots.first()).toContainText('歪比');
    await expect(strip.getByRole('button', { name: '首席安全官，2 条未读' })).toContainText('2');
    const add = strip.getByRole('button', { name: '添加伙伴' });
    expect((await add.boundingBox())!.x).toBeGreaterThan((await slots.last().boundingBox())!.x);
    // Three Bots and Add fit the default column: nothing scrolls or fades.
    const row = slots.first().locator('xpath=..');
    expect(await row.evaluate((node) => node.scrollWidth <= node.clientWidth)).toBe(true);
    await expect(row).not.toHaveClass(/bot-strip-fade/);
    await page.mouse.move(900, 600);
    await sidebar.screenshot({ animations: 'disabled', path: `${SHOTS}/bots-strip-${theme}.png` });

    await strip.getByRole('button', { name: 'Product Lead' }).hover();
    const card = page.getByRole('dialog').filter({ hasText: '@pm' });
    await expect(card).toBeVisible();
    await expect(card).toContainText('Owns the backlog and its priorities');
    await expect(card).toContainText('运行中');
    await page.screenshot({ animations: 'disabled', path: `${SHOTS}/bots-hover-${theme}.png`, clip: { x: 0, y: 0, width: 620, height: 460 } });

    await strip.getByRole('button', { name: '首席安全官，2 条未读' }).click({ button: 'right' });
    const menu = page.getByRole('menu');
    await expect(menu.getByRole('menuitem', { name: '编辑资料' })).toBeVisible();
    await expect(menu.getByRole('menuitem', { name: '移出伙伴' })).toBeVisible();
    await expect(menu).toContainText('对话和记忆都会保留');
    await page.screenshot({ animations: 'disabled', path: `${SHOTS}/bots-menu-${theme}.png`, clip: { x: 0, y: 0, width: 620, height: 460 } });
    await page.keyboard.press('Escape');

    // The main Bot has no remove.
    await slots.first().getByRole('button').click({ button: 'right' });
    await expect(page.getByRole('menu').getByRole('menuitem', { name: '编辑资料' })).toBeVisible();
    await expect(page.getByRole('menu').getByRole('menuitem', { name: '移出伙伴' })).toHaveCount(0);
    await page.keyboard.press('Escape');

    // The Inbox popover: a Bot row is the Bot, with its tag.
    await inbox.hover();
    const popover = page.getByRole('dialog', { name: '收件箱' });
    await expect(popover).toBeVisible();
    await expect(popover.getByRole('button').filter({ hasText: '首席安全官' })).toContainText('伙伴');
    await expect(popover.getByRole('button').filter({ hasText: '中文项目' })).toContainText('Fix the cache break');
    await page.screenshot({ animations: 'disabled', path: `${SHOTS}/bots-inbox-${theme}.png`, clip: { x: 0, y: 0, width: 680, height: 560 } });
    expect(denied).toEqual([]);
  });

  test(`the add dialog generates a handle and offers existing Agents (${theme})`, async ({ page }) => {
    const { denied } = await serveBots(page, 'zh', [MAIN, CSO, PM]);
    await page.setViewportSize(DESKTOP);
    await open(page, '/', { lang: 'zh', theme });
    await sidebarOf(page).getByRole('button', { name: '添加伙伴' }).click();
    const dialog = page.getByRole('dialog', { name: '添加伙伴' });
    await expect(dialog).toBeVisible();

    await dialog.getByRole('textbox').first().fill('首席安全官');
    await expect(dialog.getByRole('status')).toContainText('正在生成英文代号');
    await page.screenshot({ animations: 'disabled', path: `${SHOTS}/bots-add-generating-${theme}.png` });
    await expect(dialog.getByText('@cso-2')).toBeVisible();
    await dialog.getByPlaceholder('例如：负责依赖与权限的安全审计，每周一汇报一次').fill('负责依赖与权限的安全审计，每周一早上汇报一次。');
    await expect(dialog).toContainText('也可以直接跟歪比说');
    await page.screenshot({ animations: 'disabled', path: `${SHOTS}/bots-add-ready-${theme}.png` });

    await dialog.getByRole('button', { name: '选择头像' }).click();
    await page.getByRole('radio', { name: '🛡️' }).click();
    await page.screenshot({ animations: 'disabled', path: `${SHOTS}/bots-add-avatar-${theme}.png` });
    await page.getByRole('radio', { name: '上传图片' }).click();
    await expect(page.getByText('拖入图片，或点击选择')).toBeVisible();
    await page.screenshot({ animations: 'disabled', path: `${SHOTS}/bots-add-upload-${theme}.png` });
    await page.keyboard.press('Escape');

    await dialog.getByRole('radio', { name: '从已有 Agent 设为伙伴' }).click();
    // Only Vibey Agents that are not Bots: never the main Bot, a Bot, a Claude
    // Agent, or the built-in `vibey` worker.
    const choices = dialog.getByRole('radiogroup', { name: '从已有 Agent 设为伙伴' }).getByRole('radio');
    await expect(choices).toHaveText([/^security-auditor/]);
    await choices.filter({ hasText: 'security-auditor' }).click();
    await expect(dialog.getByText('@security-auditor')).toBeVisible();
    await expect(dialog.getByRole('button', { name: '设为伙伴' })).toBeEnabled();
    await page.screenshot({ animations: 'disabled', path: `${SHOTS}/bots-add-agent-${theme}.png` });
    expect(denied).toEqual([]);
  });

  test(`a Bot's conversation shows the Bot, its role, and no rename (${theme})`, async ({ page }) => {
    const { denied } = await serveBots(page, 'en', [MAIN, CSO, PM]);
    await page.setViewportSize(DESKTOP);
    await open(page, '/chat/ses_cso', { lang: 'en', theme });
    const header = page.locator('[data-chat-header]');
    await expect(header).toContainText('首席安全官');
    await expect(header.getByRole('button', { name: 'Edit profile' })).toBeVisible();
    // No rename: the name is not a click-to-edit title, and the menu offers no
    // rename, archive, pin, or hide.
    await expect(header.locator('input')).toHaveCount(0);
    await header.getByRole('button', { name: 'Session actions' }).first().click();
    const actions = page.getByRole('group', { name: 'Session actions' });
    await expect(actions.getByRole('button')).toHaveText(['Fork session']);
    await page.keyboard.press('Escape');
    // The empty conversation shows the Bot's role; the open Bot carries the ring.
    await expect(page.locator('main')).toContainText('依赖与权限的安全审计，每周汇报');
    await expect(page.getByRole('button', { name: '首席安全官, 2 unread', exact: true })).toHaveAttribute('aria-current', 'page');
    // The composer moves the model only: the Agent is the Bot's.
    await page.mouse.move(900, 300);
    await page.screenshot({ animations: 'disabled', path: `${SHOTS}/bots-conversation-${theme}.png` });

    await header.getByRole('button', { name: 'Edit profile' }).click();
    const dialog = page.getByRole('dialog', { name: 'Edit profile' });
    await expect(dialog.getByRole('textbox').first()).toHaveValue('首席安全官');
    await expect(dialog.getByText('@cso')).toBeVisible();
    await expect(dialog.getByText('Fixed once the Bot exists')).toBeVisible();
    await page.screenshot({ animations: 'disabled', path: `${SHOTS}/bots-edit-${theme}.png` });
    expect(denied).toEqual([]);
  });
}

test('with more Bots than fit, the strip scrolls and Add stays pinned at the right', async ({ page }) => {
  const { denied } = await serveBots(page, 'en', [MAIN, CSO, PM, ...EXTRA]);
  await page.setViewportSize(DESKTOP);
  await open(page, '/', { lang: 'en', theme: 'light' });
  const strip = sidebarOf(page).getByRole('region', { name: 'Bots' });
  const add = strip.getByRole('button', { name: 'Add a Bot' });
  const stripBox = (await strip.boundingBox())!;
  const addBox = (await add.boundingBox())!;
  expect(addBox.x + addBox.width).toBeGreaterThan(stripBox.x + stripBox.width - 12);
  const scroller = strip.locator('[data-bot-handle]').first().locator('xpath=..');
  expect(await scroller.evaluate((node) => node.scrollWidth > node.clientWidth)).toBe(true);
  await expect(scroller).toHaveClass(/bot-strip-fade/);
  await page.mouse.move(900, 600);
  await sidebarOf(page).screenshot({ animations: 'disabled', path: `${SHOTS}/bots-strip-overflow-light.png` });
  expect(denied).toEqual([]);
});

test('only the Owner sees the Bots strip', async ({ page }) => {
  const { denied } = await serveBots(page, 'en', [MAIN, CSO]);
  await page.route('**/api/session', (route) => route.fulfill({ json: {
    remote: true, authenticated: true, authorization_state: 'current', instance_kind: 'organization', instance_role: 'member',
    capabilities: { is_instance_owner: false, can_read_instance: true, can_chat: true, can_use_agents: true },
  } }));
  await page.setViewportSize(DESKTOP);
  await open(page, '/', { lang: 'en' });
  const sidebar = sidebarOf(page);
  await expect(sidebar.getByText('Agent OS', { exact: true })).toBeVisible();
  await expect(sidebar.getByRole('region', { name: 'Bots' })).toHaveCount(0);
  expect(denied.filter((request) => request.includes('/api/bots'))).toEqual([]);
});

test('dragging a Bot along the strip moves it, and the main Bot stays first', async ({ page }) => {
  const { denied } = await serveBots(page, 'en', [MAIN, CSO, PM]);
  const moves: unknown[] = [];
  await page.route('**/api/bots/pm', async (route) => {
    moves.push(route.request().postDataJSON());
    // The server now holds the new order, and the refetch reads it back.
    await page.route('**/api/bots', (list) => list.fulfill({ json: { ok: true, bots: [payload(MAIN, 0), payload(PM, 1), payload(CSO, 2)] } }));
    return route.fulfill({ json: { ok: true, bot: payload(PM, 1) } });
  });
  await page.setViewportSize(DESKTOP);
  await open(page, '/', { lang: 'en' });
  const strip = sidebarOf(page).getByRole('region', { name: 'Bots' });
  const from = (await strip.locator('[data-bot-handle="pm"] button').boundingBox())!;
  const to = (await strip.locator('[data-bot-handle="cso"] button').boundingBox())!;
  await page.mouse.move(from.x + from.width / 2, from.y + from.height / 2);
  await page.mouse.down();
  await page.mouse.move(from.x + from.width / 2 - 10, from.y + from.height / 2, { steps: 4 });
  await page.mouse.move(to.x + to.width / 2 - 8, to.y + to.height / 2, { steps: 8 });
  await page.mouse.up();
  await expect.poll(() => moves).toEqual([{ position: 1 }]);
  await expect(strip.locator('[data-bot-handle]')).toHaveCount(3);
  expect(await strip.locator('[data-bot-handle]').evaluateAll((nodes) => nodes.map((node) => node.getAttribute('data-bot-handle'))))
    .toEqual(['main', 'pm', 'cso']);
  expect(denied).toEqual([]);
});

for (const theme of ['light', 'dark'] as const) {
  test(`the Agents page marks Bots and offers "Set as Bot" on eligible Vibey Agents (${theme})`, async ({ page }) => {
    const { denied } = await serveBots(page, 'en', [MAIN, CSO, PM]);
    for (const agent of AGENTS) {
      await page.route(`**/api/agents/${agent.name}`, (route) => route.fulfill({ json: {
        ok: true,
        agent: { ...agent, display_name: agent.name, archived_at: null, updated_at: ago(60), system_prompt: null, metadata: {}, created_at: ago(600) },
      } }));
    }
    await page.setViewportSize(DESKTOP);
    await open(page, '/agents', { lang: 'en', theme });
    const row = page.getByRole('button', { name: /^cso/ });
    await expect(row.getByText('Bot', { exact: true })).toBeVisible();
    // The built-in worker is never offered as a Bot (D9).
    await page.getByRole('button', { name: /^vibey/ }).click();
    await expect(page.getByText('Vibe Agent · Vibey backend')).toBeVisible();
    await expect(page.getByRole('button', { name: 'Set as Bot' })).toHaveCount(0);
    await row.click();
    const detail = page.getByText('This Agent is the Bot 首席安全官');
    await expect(detail).toBeVisible();
    // While it is a Bot it keeps its name and stays enabled; no delete is offered.
    await expect(page.locator('input[value="cso"]')).toBeDisabled();
    await expect(page.getByRole('switch')).toBeDisabled();
    await expect(page.getByRole('button', { name: 'Delete' })).toHaveCount(0);
    await page.screenshot({ animations: 'disabled', path: `${SHOTS}/bots-agents-bot-${theme}.png` });

    await page.getByRole('button', { name: /^security-auditor/ }).click();
    const set = page.getByRole('button', { name: 'Set as Bot' });
    await expect(set).toBeEnabled();
    await page.screenshot({ animations: 'disabled', path: `${SHOTS}/bots-agents-set-${theme}.png` });
    await set.click();
    const dialog = page.getByRole('dialog', { name: 'Add a Bot' });
    await expect(dialog.getByRole('radio', { name: /security-auditor/ })).toHaveAttribute('aria-checked', 'true');
    await expect(dialog.getByText('@security-auditor')).toBeVisible();
    expect(denied).toEqual([]);
  });
}
