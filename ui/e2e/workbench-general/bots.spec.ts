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
      // The main Bot's conversation has one reply; the others start empty.
      messages: bot.handle === 'main' ? [{
        id: 'msg-main-reply', scope_id: null, session_id: sessionId, platform: 'avibe', author: 'agent', type: 'result',
        source: 'agent', author_id: null, author_name: null, native_message_id: null, parent_native_message_id: null,
        text: 'The launch checklist is on track.', content: { text: 'The launch checklist is on track.' },
        metadata: { author_agent_id: 'agt-main', author_backend: 'vibey' }, created_at: ago(5), updated_at: ago(5),
      }] : [],
      queued: [], next_after_id: null, next_before_id: null,
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
  // Model Hub's catalog for Vibey, which every Bot runs on.
  const catalogModel = (id: string, displayName: string) => ({
    id, origin: 'builtin', display_name: displayName, models_dev_id: null, context_window: null, max_output_tokens: null,
    input_modalities: [], output_modalities: [], supports_tools: null, supports_reasoning: true,
    reasoning_efforts: ['low', 'medium', 'high'], locked: false, routeable: true, effort_menu: { omitted: [], not_on: {} },
    fast_mode: false, fast_mode_confirmed: false,
  });
  await page.route('**/api/models/agents/vibey/models', (route) => route.fulfill({ json: { ok: true, agent: {
    backend: 'vibey', mode: 'hub',
    catalog_models: [catalogModel('gpt-6-astra', 'GPT-6 Astra'), catalogModel('claude-opus-4-7', 'Claude Opus 4.7')],
  } } }));
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
    await page.screenshot({ animations: 'disabled', path: `${SHOTS}/bots-menu-${theme}.png`, clip: { x: 0, y: 0, width: 620, height: 460 } });
    // Removing asks first and says what stays (ui/design: agents/bots-redesign/remove-bot).
    await menu.getByRole('menuitem', { name: '移出伙伴' }).click();
    const confirm = page.getByRole('dialog', { name: '把首席安全官移出伙伴？' });
    await expect(confirm).toContainText('对话和记忆都会保留');
    await page.screenshot({ animations: 'disabled', path: `${SHOTS}/bots-remove-confirm-${theme}.png` });
    await confirm.getByRole('button', { name: '取消' }).click();
    await expect(confirm).toHaveCount(0);
    await expect(strip.getByRole('button', { name: '首席安全官，2 条未读' })).toBeVisible();

    // The main Bot has no remove.
    await slots.first().getByRole('button').click({ button: 'right' });
    await expect(page.getByRole('menu').getByRole('menuitem', { name: '编辑资料' })).toBeVisible();
    await expect(page.getByRole('menu').getByRole('menuitem', { name: '移出伙伴' })).toHaveCount(0);
    await page.keyboard.press('Escape');

    // The Inbox popover: Bots above Sessions, and a Bot row is the Bot
    // (docs/plans/workbench-refinements.md §1).
    await inbox.hover();
    const popover = page.getByRole('dialog', { name: '收件箱' });
    await expect(popover).toBeVisible();
    const botsHeader = popover.getByText('伙伴', { exact: true });
    const sessionsHeader = popover.getByText('会话', { exact: true });
    await expect(botsHeader).toBeVisible();
    await expect(sessionsHeader).toBeVisible();
    expect((await botsHeader.boundingBox())!.y).toBeLessThan((await sessionsHeader.boundingBox())!.y);
    await expect(popover.getByRole('button').filter({ hasText: '首席安全官' })).toContainText('avault');
    await expect(popover.getByRole('button').filter({ hasText: '中文项目' })).toContainText('Fix the cache break');
    await page.screenshot({ animations: 'disabled', path: `${SHOTS}/bots-inbox-${theme}.png`, clip: { x: 0, y: 0, width: 680, height: 560 } });
    expect(denied).toEqual([]);
  });

  test(`muting Session messages leaves the Inbox counting Bots, and the switches are one click away (${theme})`, async ({ page }) => {
    const { denied } = await serveBots(page, 'zh', [MAIN, CSO, PM]);
    let preferences = { enabled: true, bots: true, sessions: false };
    const saved: unknown[] = [];
    await page.route('**/api/notifications/preferences', async (route) => {
      if (route.request().method() === 'PUT') {
        const changes = route.request().postDataJSON();
        saved.push(changes);
        preferences = { ...preferences, ...changes };
      }
      return route.fulfill({ json: { ok: true, preferences } });
    });
    await page.setViewportSize(DESKTOP);
    await open(page, '/', { lang: 'zh', theme });
    const sidebar = sidebarOf(page);

    // The Bot's 2 count; the Session's 3 is quiet (docs/plans/workbench-refinements.md §1).
    const inbox = sidebar.getByRole('link', { name: '收件箱，2 条未读' });
    await expect(inbox).toHaveText('2');
    await inbox.hover();
    const popover = page.getByRole('dialog', { name: '收件箱' });
    await expect(popover.getByText('免打扰')).toHaveCount(1);
    const bell = popover.getByRole('button', { name: '通知设置' });
    await expect(bell).toContainText('仅伙伴');
    await page.screenshot({ animations: 'disabled', path: `${SHOTS}/notifications-inbox-${theme}.png`, clip: { x: 0, y: 0, width: 680, height: 560 } });

    await bell.click();
    const sessions = popover.getByRole('switch', { name: '会话消息' });
    await expect(sessions).toHaveAttribute('aria-checked', 'false');
    await expect(popover).toContainText('会话消息照常进收件箱');
    await page.screenshot({ animations: 'disabled', path: `${SHOTS}/notifications-menu-${theme}.png`, clip: { x: 0, y: 0, width: 680, height: 560 } });
    // The click resolves once it is dispatched; the save reaches the route afterwards.
    await sessions.click();
    await expect.poll(() => saved).toEqual([{ sessions: true }]);
    await expect(sidebar.getByRole('link', { name: '收件箱，5 条未读' })).toHaveText('5');

    await popover.getByRole('button', { name: '更多通知设置' }).click();
    await expect(page).toHaveURL(/\/settings\/notifications/);
    await expect(popover).toBeHidden();
    await expect(page.getByRole('heading', { name: '通知', level: 1 })).toBeVisible();
    await expect(page.getByRole('switch', { name: '会话消息' })).toHaveAttribute('aria-checked', 'true');
    await page.screenshot({ animations: 'disabled', path: `${SHOTS}/notifications-settings-${theme}.png` });

    // The full Inbox: the same two sections, and the switches behind one button.
    await open(page, '/inbox', { lang: 'zh', theme });
    const main = page.getByRole('main');
    await expect(main.getByRole('button', { name: /^通知：全部/ })).toBeVisible();
    await expect(main.locator('section').first()).toContainText('首席安全官');
    await expect(main.locator('section').nth(1)).toContainText('Fix the cache break');
    await page.screenshot({ animations: 'disabled', path: `${SHOTS}/notifications-inbox-page-${theme}.png` });
    // That one switch was the only write.
    expect(saved).toEqual([{ sessions: true }]);
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
    await expect(dialog.getByRole('status')).toContainText('正在生成代号');
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
    // The header is back, avatar, name, and ⋯: the profile lives in the menu.
    await expect(header.getByRole('button', { name: 'Edit profile' })).toHaveCount(0);
    // No rename: the name is not a click-to-edit title, and the menu offers no
    // rename, archive, pin, hide, or fork.
    await expect(header.locator('input')).toHaveCount(0);
    await header.getByRole('button', { name: 'Session actions' }).first().click();
    const actions = page.getByRole('group', { name: 'Session actions' });
    await expect(actions.getByRole('button')).toHaveText(['Edit profile']);
    await expect(actions.getByRole('switch', { name: 'Hide details' })).toHaveAttribute('aria-checked', 'true');
    await expect(actions).not.toContainText('Fork');
    await page.screenshot({ animations: 'disabled', path: `${SHOTS}/bots-conversation-menu-${theme}.png`, clip: { x: 0, y: 0, width: 900, height: 320 } });
    await page.keyboard.press('Escape');
    // The empty conversation shows the Bot's role; the open Bot carries the ring.
    await expect(page.locator('main')).toContainText('依赖与权限的安全审计，每周汇报');
    await expect(page.getByRole('button', { name: '首席安全官, 2 unread', exact: true })).toHaveAttribute('aria-current', 'page');
    // The composer's route is the model and effort alone: no Bot name, no Agent level.
    const route = page.locator('main').getByRole('button', { name: /^(gpt-6-astra|GPT-6 Astra)/ });
    await expect(route).not.toContainText('首席安全官');
    await route.click();
    await expect(page.getByText('Model', { exact: true })).toBeVisible();
    await expect(page.getByText('Agent', { exact: true })).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'New agent' })).toHaveCount(0);
    await page.screenshot({ animations: 'disabled', path: `${SHOTS}/bots-conversation-route-${theme}.png` });
    await page.keyboard.press('Escape');
    await page.mouse.move(900, 300);
    await page.screenshot({ animations: 'disabled', path: `${SHOTS}/bots-conversation-${theme}.png` });

    await header.getByRole('button', { name: 'Session actions' }).first().click();
    await page.getByRole('group', { name: 'Session actions' }).getByRole('button', { name: 'Edit profile' }).click();
    const dialog = page.getByRole('dialog', { name: 'Edit profile' });
    await expect(dialog.getByRole('textbox').first()).toHaveValue('首席安全官');
    await expect(dialog.getByText('@cso')).toBeVisible();
    await expect(dialog.getByText('Fixed once the Bot exists')).toBeVisible();
    await page.screenshot({ animations: 'disabled', path: `${SHOTS}/bots-edit-${theme}.png` });
    expect(denied).toEqual([]);
  });

  // The user's message to a Bot is P1 (docs/plans/bots.md D4, MESSAGE-DELIVERY-339):
  // the server takes it into the Turn the Bot is on, so the conversation names who
  // is working and promises no queue. Steered, it is simply the user's message;
  // refused by that Turn, it says it has not reached the Bot yet, never "queued".
  test(`a working Bot takes the user's message, and a refused one says it has not arrived (${theme})`, async ({ page }) => {
    const { denied } = await serveBots(page, 'zh', [MAIN, { ...CSO, running: true }, PM]);
    const sessionId = 'ses_cso';
    const turnState = {
      in_flight: true, foreground: 'running', native_turn_started: true, pending_input_count: 0,
      background_activities: [], pending_activity_output_count: 0, connection: 'connected',
    };
    const session = {
      id: sessionId, scope_id: null, project_id: null, title: CSO.display_name, agent_id: 'agt-cso', agent_name: 'cso',
      agent_backend: 'vibey', agent_variant: 'vibey', model: 'gpt-6-astra', reasoning_effort: 'high', speed: null, status: 'active',
      agent_status: 'running', pinned: false, visibility: 'foreground', workdir: '/fixture/.avibe/bots/cso',
      native_session_id: 'native-cso', metadata: { kind: 'bot' }, created_at: ago(600), updated_at: ago(1),
    };
    const ask = {
      id: 'msg-ask', scope_id: null, session_id: sessionId, platform: 'avibe', author: 'user', type: 'user', source: 'user',
      author_id: null, author_name: null, native_message_id: null, parent_native_message_id: null,
      text: '帮我审一下 avault 这周的依赖升级。', content: {}, metadata: {}, created_at: ago(3), updated_at: ago(3),
    };
    await page.route(`**/api/sessions/${sessionId}/bootstrap`, (route) => route.fulfill({ json: {
      session, capabilities: { can_chat: true }, agents: AGENTS, default_agent_name: 'vibey', config: { ui: {} },
      messages: [ask], queued: [], next_after_id: null, next_before_id: null, draft: { text: '', updated_at: null }, turn_state: turnState,
    } }));
    await page.route(`**/api/sessions/${sessionId}`, (route) => route.fulfill({ json: session }));
    await page.route(`**/api/sessions/${sessionId}/turn-state`, (route) => route.fulfill({ json: turnState }));
    let queued: unknown[] = [];
    await page.route(`**/api/sessions/${sessionId}/queue`, (route) => route.fulfill({ json: { queued } }));
    const sent: string[] = [];
    await page.route(`**/api/sessions/${sessionId}/messages`, (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      const { text } = route.request().postDataJSON() as { text: string };
      sent.push(text);
      const row = { ...ask, id: `msg-${sent.length}`, text, created_at: ago(0), updated_at: ago(0) };
      // The first message steers into the Turn; the Turn refuses the second, which waits.
      if (sent.length === 1) return route.fulfill({ status: 201, json: { ...row, state: 'accepted', delivery_state: 'accepted' } });
      queued = [{ ...row, type: 'queued', state: 'queued', priority: 'p3' }];
      return route.fulfill({ status: 202, json: { ...row, type: 'queued', state: 'queued', queued: true, delivery_state: 'queued' } });
    });
    await page.setViewportSize(DESKTOP);
    await open(page, `/chat/${sessionId}`, { lang: 'zh', theme });
    const main = page.locator('main');
    const composer = main.getByRole('textbox').last();

    // Working: the composer names the Bot and promises no queue.
    await expect(main.getByText('首席安全官 正在处理…')).toBeVisible();
    await expect(main).not.toContainText('排队');

    await composer.click();
    await page.keyboard.type('先看 avault-core，别的放一放。');
    await page.keyboard.press('Enter');
    await expect(main.getByText('先看 avault-core，别的放一放。')).toBeVisible();
    await expect(page.locator('[data-queue-row]')).toHaveCount(0);
    await page.mouse.move(900, 300);
    await page.screenshot({ animations: 'disabled', path: `${SHOTS}/bots-steer-accepted-${theme}.png` });

    await composer.click();
    await page.keyboard.type('顺便把结论发我邮箱。');
    await page.keyboard.press('Enter');
    const strip = page.locator('[data-queue-batch]').locator('..');
    await expect(strip).toContainText('还没送达 · 1');
    await expect(strip).toContainText('没能插进首席安全官正在处理的这一轮，这一轮结束后就会收到。');
    await expect(page.locator('[data-queue-row]')).toContainText('顺便把结论发我邮箱。');
    await expect(page.locator('[data-queue-row]').getByRole('button', { name: '不发了' })).toBeVisible();
    // It is not in the conversation: it has not reached the Bot.
    await expect(page.locator('[data-message-id="msg-2"]')).toHaveCount(0);
    await expect(main).not.toContainText('排队');
    await page.mouse.move(900, 300);
    await page.screenshot({ animations: 'disabled', path: `${SHOTS}/bots-steer-refused-${theme}.png` });
    expect(sent).toEqual(['先看 avault-core，别的放一放。', '顺便把结论发我邮箱。']);
    expect(denied).toEqual([]);
  });

  // "Hide details" (docs/plans/bots.md D5): on by default, a Bot's conversation
  // reads like talking to a person. The Watch that woke the Bot, the execution
  // process, and the background-task banner stay out; the Bot's report on the
  // Watch, and the thinking bubble while it works, stay in. Off, they return, the
  // execution process because Settings shows Agent activity here.
  test(`"Hide details" keeps a Bot's conversation to what it says (${theme})`, async ({ page }) => {
    const { denied } = await serveBots(page, 'zh', [MAIN, { ...CSO, running: true }, PM]);
    const sessionId = 'ses_cso';
    const row = (id: string, minutes: number, fields: Record<string, unknown>) => ({
      id, scope_id: null, session_id: sessionId, platform: 'avibe', source: 'agent', author_id: null, author_name: null,
      native_message_id: null, parent_native_message_id: null, content: {}, metadata: {}, created_at: ago(minutes), updated_at: ago(minutes),
      ...fields,
    });
    const messages = [
      row('msg-ask', 30, { author: 'user', type: 'user', source: 'user', text: '盯一下 avault 的依赖升级 PR，CI 过了告诉我。' }),
      row('msg-watch', 6, {
        author: 'harness', type: 'harness', source: 'harness', author_name: 'watch', author_id: 'wat-1',
        text: '[Harness: Watch wat-1] avault#412 CI completed: 18 checks passed.',
      }),
      row('msg-report', 5, {
        author: 'agent', type: 'result', text: 'avault 的依赖升级 PR 的 CI 全部通过了，没有新的高危依赖，可以合并。',
        metadata: { author_agent_id: 'agt-cso', author_backend: 'vibey' },
      }),
    ];
    const turnState = {
      in_flight: true, foreground: 'running', native_turn_started: true, pending_input_count: 0,
      background_activities: [{
        id: 'watch:wat-2', backend: 'vibey', runtime_key: 'vibey', session_id: sessionId, kind: 'watch', status: 'running',
        description: 'avault#413 review', started_at: ago(4), updated_at: ago(1), item_kind: 'watch', label: 'avault#413 review',
      }],
      pending_activity_output_count: 0, connection: 'connected',
    };
    const session = {
      id: sessionId, scope_id: null, project_id: null, title: CSO.display_name, agent_id: 'agt-cso', agent_name: 'cso',
      agent_backend: 'vibey', agent_variant: 'vibey', model: 'gpt-6-astra', reasoning_effort: 'high', speed: null, status: 'active',
      agent_status: 'running', pinned: false, visibility: 'foreground', workdir: '/fixture/.avibe/bots/cso',
      native_session_id: 'native-cso', metadata: { kind: 'bot' }, created_at: ago(600), updated_at: ago(1),
    };
    await page.route(`**/api/sessions/${sessionId}/bootstrap`, (route) => route.fulfill({ json: {
      session, capabilities: { can_chat: true }, agents: AGENTS, default_agent_name: 'vibey',
      // Settings shows Agent activity everywhere.
      config: { ui: { show_agent_activity: true } },
      messages, queued: [], next_after_id: null, next_before_id: null, draft: { text: '', updated_at: null }, turn_state: turnState,
    } }));
    await page.route(`**/api/sessions/${sessionId}`, (route) => route.fulfill({ json: session }));
    await page.route(`**/api/sessions/${sessionId}/turn-state`, (route) => route.fulfill({ json: turnState }));
    await page.route(`**/api/sessions/${sessionId}/activity**`, (route) => route.fulfill({ json: { groups: [{
      id: 'grp-report', anchor_message_id: 'msg-report', anchor_position: 'before', open: false, status: 'done',
      steps: 4, duration_ms: 42_000, started_at: ago(6), ended_at: ago(5),
    }] } }));
    let hideDetails = true;
    const patches: unknown[] = [];
    await page.route('**/api/bots', (route) => route.fulfill({ json: { ok: true, bots: [MAIN, CSO, PM].map((bot, index) => ({
      ...payload(bot, index), ...(bot.handle === 'cso' ? { hide_details: hideDetails } : {}),
    })) } }));
    await page.route('**/api/bots/cso', (route) => {
      const body = route.request().postDataJSON() as { hide_details: boolean };
      patches.push(body);
      hideDetails = body.hide_details;
      return route.fulfill({ json: { ok: true, bot: { ...payload(CSO, 1), hide_details: hideDetails } } });
    });
    await page.setViewportSize(DESKTOP);
    await open(page, `/chat/${sessionId}`, { lang: 'zh', theme });
    const main = page.locator('main');
    const report = page.locator('[data-message-id="msg-report"]');

    // On, the default: the Bot's report stands as its own message.
    await expect(report).toContainText('CI 全部通过了');
    await expect(report).toContainText('首席安全官');
    await expect(page.locator('[data-message-id="msg-ask"]')).toBeVisible();
    await expect(page.locator('[data-message-id="msg-watch"]')).toHaveCount(0);
    await expect(main.getByText('执行过程', { exact: false })).toHaveCount(0);
    await expect(main.getByRole('status').filter({ hasText: '后台任务' })).toHaveCount(0);
    // It is working: it shows as the Bot typing, not as its steps.
    const typing = main.locator('.vr-typing-dot').first();
    await expect(typing).toBeVisible();
    await page.mouse.move(900, 300);
    await page.screenshot({ animations: 'disabled', path: `${SHOTS}/bots-hide-details-on-${theme}.png` });

    const header = page.locator('[data-chat-header]');
    await header.getByRole('button', { name: '会话操作' }).first().click();
    const toggle = page.getByRole('group', { name: '会话操作' }).getByRole('switch', { name: '隐藏细节' });
    await toggle.click();
    await expect.poll(() => patches).toEqual([{ hide_details: false }]);
    await expect(toggle).toHaveAttribute('aria-checked', 'false');
    await page.keyboard.press('Escape');

    // Off: the Watch, the execution process, and the background task return.
    await expect(page.locator('[data-message-id="msg-watch"]')).toBeVisible();
    await expect(main.getByText('执行过程', { exact: false }).first()).toBeVisible();
    await expect(main.getByRole('status').filter({ hasText: '后台任务' })).toBeVisible();
    await expect(report).toContainText('CI 全部通过了');
    await page.mouse.move(900, 300);
    await page.screenshot({ animations: 'disabled', path: `${SHOTS}/bots-hide-details-off-${theme}.png` });
    expect(denied).toEqual([]);
  });
}

test('adding a Bot sends the generated handle, its role, and the emoji avatar, then opens its conversation', async ({ page }) => {
  const { denied } = await serveBots(page, 'en', [MAIN, CSO, PM]);
  const created: unknown[] = [];
  await page.route('**/api/bots', (route) => {
    if (route.request().method() !== 'POST') return route.fallback();
    const body = route.request().postDataJSON() as { display_name: string; handle: string };
    created.push(body);
    return route.fulfill({ status: 201, json: { ok: true, bot: payload({ handle: body.handle, display_name: body.display_name, avatar: null, role: '' }, 3) } });
  });
  await page.setViewportSize(DESKTOP);
  await open(page, '/', { lang: 'en' });
  await sidebarOf(page).getByRole('button', { name: 'Add a Bot' }).click();
  const dialog = page.getByRole('dialog', { name: 'Add a Bot' });
  await dialog.getByRole('textbox').first().fill('首席安全官');
  await dialog.getByPlaceholder('e.g. Audits dependencies and permissions, and reports every Monday').fill('Audits dependencies weekly.');
  await dialog.getByRole('button', { name: 'Choose an avatar' }).click();
  await page.getByRole('radio', { name: '🛡️' }).click();
  await page.keyboard.press('Escape');
  await expect(dialog.getByText('@cso-2')).toBeVisible();
  await dialog.getByRole('button', { name: 'Add Bot' }).click();
  await expect(page).toHaveURL(/\/chat\/ses_cso-2$/);
  // The model follows the main Bot unless Advanced chose one, so none is sent.
  expect(created).toEqual([{
    display_name: '首席安全官', handle: 'cso-2', role: 'Audits dependencies weekly.', avatar: { emoji: '🛡️', hue: 200 },
  }]);
  expect(denied).toEqual([]);
});

test('adding before the handle arrives waits for it and creates with it', async ({ page }) => {
  const { denied } = await serveBots(page, 'en', [MAIN, CSO, PM]);
  const created: { handle?: string }[] = [];
  await page.route('**/api/bots', (route) => {
    if (route.request().method() !== 'POST') return route.fallback();
    const body = route.request().postDataJSON() as { display_name: string; handle: string };
    created.push(body);
    return route.fulfill({ status: 201, json: { ok: true, bot: payload({ handle: body.handle, display_name: body.display_name, avatar: null, role: '' }, 3) } });
  });
  await page.setViewportSize(DESKTOP);
  await open(page, '/', { lang: 'en' });
  await sidebarOf(page).getByRole('button', { name: 'Add a Bot' }).click();
  const dialog = page.getByRole('dialog', { name: 'Add a Bot' });
  await dialog.getByRole('textbox').first().fill('首席安全官');
  await dialog.getByRole('button', { name: 'Add Bot' }).click();
  await expect(page).toHaveURL(/\/chat\/ses_cso-2$/);
  expect(created.map((body) => body.handle)).toEqual(['cso-2']);
  expect(denied).toEqual([]);
});

test('closing the dialog while a create waits for its handle sends nothing', async ({ page }) => {
  const { denied } = await serveBots(page, 'en', [MAIN, CSO, PM]);
  const created: unknown[] = [];
  await page.route('**/api/bots', (route) => {
    if (route.request().method() !== 'POST') return route.fallback();
    created.push(route.request().postDataJSON());
    return route.fulfill({ status: 201, json: { ok: true, bot: payload(CSO, 3) } });
  });
  await page.setViewportSize(DESKTOP);
  await open(page, '/', { lang: 'en' });
  await sidebarOf(page).getByRole('button', { name: 'Add a Bot' }).click();
  const dialog = page.getByRole('dialog', { name: 'Add a Bot' });
  await dialog.getByRole('textbox').first().fill('首席安全官');
  // Add before the handle exists: the create waits for one, then the user leaves.
  await dialog.getByRole('button', { name: 'Add Bot' }).click();
  await page.keyboard.press('Escape');
  await expect(dialog).toBeHidden();
  await page.waitForTimeout(2000);
  expect(created).toEqual([]);
  await expect(page).toHaveURL(/\/$/);
  expect(denied).toEqual([]);
});

test('Settings opening over the dialog suspends it with the form intact', async ({ page }) => {
  const { denied } = await serveBots(page, 'en', [MAIN, CSO, PM]);
  await page.setViewportSize(DESKTOP);
  await open(page, '/', { lang: 'en' });
  await sidebarOf(page).getByRole('button', { name: 'Add a Bot' }).click();
  const dialog = page.getByRole('dialog', { name: 'Add a Bot' });
  await dialog.getByRole('textbox').first().fill('Researcher');
  // The model picker's Add model exit is an in-app route change to Settings; the
  // sidebar's Settings link makes the same one (clicked through the modal's inert layer).
  await page.locator('aside [data-settings-toggle="true"]').evaluate((node: HTMLElement) => node.click());
  await expect(page.locator('[data-settings-overlay="true"]')).toBeVisible();
  await expect(dialog).toBeHidden();
  await page.keyboard.press('Escape');
  await expect(page.locator('[data-settings-overlay="true"]')).toBeHidden();
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole('textbox').first()).toHaveValue('Researcher');
  expect(denied).toEqual([]);
});

test('on a short window the dialog scrolls to its footer', async ({ page }) => {
  const { denied } = await serveBots(page, 'en', [MAIN, CSO, PM]);
  await page.setViewportSize({ width: 1000, height: 420 });
  await open(page, '/', { lang: 'en' });
  await sidebarOf(page).getByRole('button', { name: 'Add a Bot' }).click();
  const dialog = page.getByRole('dialog', { name: 'Add a Bot' });
  await expect(dialog).toBeVisible();
  expect(await dialog.evaluate((node) => node.scrollHeight > node.clientHeight && getComputedStyle(node).overflowY === 'auto')).toBe(true);
  const add = dialog.getByRole('button', { name: 'Add Bot' });
  await add.scrollIntoViewIfNeeded();
  await expect(add).toBeInViewport();
  expect(denied).toEqual([]);
});

test("a Bot's replies read as the Bot, and its route as its model", async ({ page }) => {
  const { denied } = await serveBots(page, 'en', [MAIN, CSO, PM]);
  await page.setViewportSize(DESKTOP);
  await open(page, '/chat/ses_main', { lang: 'en' });
  const reply = page.locator('[data-message-id="msg-main-reply"]');
  await expect(reply).toContainText('The launch checklist is on track.');
  // D2: chat shows the display name (the main Bot's default, here) with its avatar.
  await expect(reply).toContainText('Vibey');
  await expect(reply).not.toContainText('main');
  await expect(reply.locator('.bot-avatar-mascot')).toHaveCount(1);
  // The composer's route names the model, never the Bot's Agent.
  await expect(page.locator('main').getByRole('button', { name: /^(gpt-6-astra|GPT-6 Astra)/ })).toBeVisible();
  await expect(page.locator('main').getByRole('button', { name: /^main/ })).toHaveCount(0);
  expect(denied).toEqual([]);
});

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

// The card sits to the right of its avatar, over the rest of the strip, so a
// scroll takes it away at once; avatars sliding under a still pointer open
// nothing, and the next avatar the pointer moves onto opens its own card.
test('scrolling the strip closes the hover card until the pointer moves onto an avatar', async ({ page }) => {
  const { denied } = await serveBots(page, 'en', [MAIN, CSO, PM, ...EXTRA]);
  await page.setViewportSize(DESKTOP);
  await open(page, '/', { lang: 'en' });
  const strip = sidebarOf(page).getByRole('region', { name: 'Bots' });
  const scroller = strip.locator('[data-bot-handle]').first().locator('xpath=..');
  const cards = page.getByRole('dialog');
  const centre = async (handle: string) => {
    const box = (await strip.locator(`[data-bot-handle="${handle}"] button`).boundingBox())!;
    return { x: box.x + box.width / 2, y: box.y + box.height / 2 };
  };

  for (const scroll of ['wheel', 'drag or touch'] as const) {
    const start = await centre('cso');
    await page.mouse.move(start.x, start.y, { steps: 3 });
    await expect(cards.filter({ hasText: '@cso' })).toBeVisible();
    if (scroll === 'wheel') await page.mouse.wheel(120, 0);
    // Any other scroll reaches the strip as the same scroll event.
    else await scroller.evaluate((node) => node.scrollBy({ left: 120 }));
    await expect(cards).toHaveCount(0);
    // Another avatar now sits under the still pointer: well past the open delay, no card.
    await page.waitForTimeout(800);
    await expect(cards).toHaveCount(0);
    // Moving within that avatar is not moving onto one.
    await page.mouse.move(start.x + 3, start.y + 1);
    await page.waitForTimeout(500);
    await expect(cards).toHaveCount(0);
    // The next avatar the pointer moves onto opens its card.
    const next = await scroller.evaluate((node, x) => {
      const slot = [...node.querySelectorAll<HTMLElement>('[data-bot-handle]')]
        .find((item) => item.getBoundingClientRect().left > x + 10);
      return slot!.dataset.botHandle!;
    }, start.x);
    const target = await centre(next);
    await page.mouse.move(target.x, target.y, { steps: 3 });
    await expect(cards.filter({ hasText: `@${next}` })).toBeVisible();
    await page.mouse.move(900, 600);
    await expect(cards).toHaveCount(0);
    await scroller.evaluate((node) => node.scrollTo({ left: 0 }));
  }
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
  test(`the Agents page shows Bots as Bots, and offers "Set as Bot" on eligible Vibey Agents (${theme})`, async ({ page }) => {
    const { denied } = await serveBots(page, 'en', [MAIN, CSO, PM]);
    for (const agent of AGENTS) {
      await page.route(`**/api/agents/${agent.name}`, (route) => route.fulfill({ json: {
        ok: true,
        agent: { ...agent, display_name: agent.name, archived_at: null, updated_at: ago(60), system_prompt: null, metadata: {}, created_at: ago(600) },
      } }));
    }
    await page.setViewportSize(DESKTOP);
    await open(page, '/agents', { lang: 'en', theme });
    const content = page.getByRole('main');
    const panel = content.locator('aside');
    // Bots come first, by name; a Bot's Agent is never listed as an Agent, the main Bot's included.
    const bot = content.getByRole('button', { name: /^首席安全官/ });
    await expect(bot).toBeVisible();
    await expect(content.getByRole('button', { name: /^Product Lead/ })).toBeVisible();
    await expect(content.getByRole('button', { name: /^reviewer/ })).toBeVisible();
    await expect(content.getByRole('button', { name: /^cso/ })).toHaveCount(0);
    await expect(content.getByRole('button', { name: /^main/ })).toHaveCount(0);
    // The built-in worker is never offered as a Bot (D9).
    await content.getByRole('button', { name: /^vibey/ }).click();
    await expect(panel.getByText('Default', { exact: true })).toBeVisible();
    await expect(panel.getByRole('button', { name: 'Set as Bot' })).toHaveCount(0);

    // A Bot opens as itself: its conversation and removal, none of an Agent's controls.
    await bot.click();
    await expect(panel.getByRole('button', { name: 'Open conversation' })).toBeVisible();
    await expect(panel.getByRole('button', { name: 'Edit profile' })).toBeVisible();
    await expect(panel.getByText('@cso')).toBeVisible();
    await expect(panel.getByRole('switch')).toHaveCount(0);
    await expect(panel.getByRole('button', { name: 'Delete' })).toHaveCount(0);
    await expect(panel.getByRole('button', { name: 'Run' })).toHaveCount(0);
    await expect(panel.locator('input[value="cso"]')).toHaveCount(0);
    await page.screenshot({ animations: 'disabled', path: `${SHOTS}/bots-agents-bot-${theme}.png` });

    await content.getByRole('button', { name: /^security-auditor/ }).click();
    const set = panel.getByRole('button', { name: 'Set as Bot' });
    await expect(set).toBeEnabled();
    await page.screenshot({ animations: 'disabled', path: `${SHOTS}/bots-agents-set-${theme}.png` });
    await set.click();
    const dialog = page.getByRole('dialog', { name: 'Add a Bot' });
    await expect(dialog.getByRole('radio', { name: /security-auditor/ })).toHaveAttribute('aria-checked', 'true');
    await expect(dialog.getByText('@security-auditor')).toBeVisible();
    expect(denied).toEqual([]);
  });
}

test('the default Agent is offered as a Bot nowhere, and the add dialog says what to do first', async ({ page }) => {
  const { denied } = await serveBots(page, 'en', [MAIN, CSO, PM]);
  // The user made their own Agent the default; the server refuses it as a Bot
  // (`bot_agent_default`) until another Agent is the default.
  const auditor = AGENTS[2];
  await page.route(
    (url) => url.pathname === '/api/agents',
    (route) => route.fulfill({ json: { ok: true, agents: AGENTS, default_agent_name: auditor.name } }),
  );
  await page.route(`**/api/agents/${auditor.name}`, (route) => route.fulfill({ json: {
    ok: true,
    agent: { ...auditor, display_name: auditor.name, archived_at: null, updated_at: ago(60), system_prompt: null, metadata: {}, created_at: ago(600) },
  } }));
  await page.setViewportSize(DESKTOP);
  const reason = 'It is the default Agent. Make another Agent the default first, then set it as a Bot.';

  await open(page, '/agents', { lang: 'en', theme: 'light' });
  await page.getByRole('main').getByRole('button', { name: /^security-auditor/ }).click();
  // The Agents page draws only what applies: the default Agent has no "Set as Bot".
  await expect(page.getByRole('main').locator('aside').getByText('Default', { exact: true })).toBeVisible();
  await expect(page.getByRole('main').getByRole('button', { name: 'Set as Bot' })).toHaveCount(0);

  await sidebarOf(page).getByRole('button', { name: 'Add a Bot' }).click();
  const dialog = page.getByRole('dialog', { name: 'Add a Bot' });
  await dialog.getByRole('radio', { name: 'From an existing Agent' }).click();
  const choice = dialog.getByRole('radiogroup', { name: 'From an existing Agent' }).getByRole('radio', { name: /security-auditor/ });
  await expect(choice).toBeDisabled();
  await expect(choice).toContainText(reason);
  await expect(dialog.getByRole('button', { name: 'Set as Bot' })).toBeDisabled();
  expect(denied).toEqual([]);
});

// Long copy never widens the add/edit dialog past its own width: an Agent's
// description, a name, a role, or the main Bot's name is truncated or wrapped,
// and every control stays inside the dialog (regression pass 5, 1365×900).
test('long names, roles, and Agent descriptions keep the Bot dialog at its width', async ({ page }) => {
  const longName = '负责依赖、权限、密钥轮换与供应链安全审计的首席安全与合规官员兼发布前最终把关人';
  const longRole = 'Audits dependencies, permissions, secret rotation, and the supply chain before every release, and reports a weekly summary with the open risks, owners, and deadlines';
  const longBot: FixtureBot = { handle: 'compliance', display_name: longName, avatar: null, role: longRole };
  const mainLong: FixtureBot = { ...MAIN, display_name: 'VibeyTheFirstPointOfContactForEverythingThatHappensAroundHere' };
  const { denied } = await serveBots(page, 'zh', [mainLong, CSO, PM, longBot]);
  const longAgent = {
    ...AGENTS[2],
    id: 'agt-roster', name: 'e2e-roster-3117',
    description: 'Temporary regression Agent for prompt-tier verification; no autonomous work, only explicit requests during a regression pass',
  };
  await page.route(
    (url) => url.pathname === '/api/agents',
    (route) => route.fulfill({ json: { ok: true, agents: [...AGENTS, longAgent], default_agent_name: 'vibey' } }),
  );
  await page.setViewportSize({ width: 1365, height: 900 });
  await open(page, '/', { lang: 'zh', theme: 'light' });

  const fits = async (name: string) => {
    const dialog = page.getByRole('dialog', { name });
    await expect(dialog).toBeVisible();
    const box = await dialog.evaluate((element) => {
      const edge = element.getBoundingClientRect();
      const controls = [...element.querySelectorAll('button, input, textarea')]
        .map((control) => control.getBoundingClientRect())
        .filter((rect) => rect.width > 0);
      // Text that runs out of its own box (no ellipsis, nothing scrolling it) overlaps its neighbours.
      const spilled = [...element.querySelectorAll<HTMLElement>('*')]
        .filter((node) => node.clientWidth > 0 && [...node.childNodes].some((child) => child.nodeType === Node.TEXT_NODE && child.textContent?.trim()))
        .filter((node) => {
          const style = getComputedStyle(node);
          return style.textOverflow !== 'ellipsis' && style.overflowX === 'visible' && node.scrollWidth > node.clientWidth + 1;
        })
        .map((node) => node.textContent?.slice(0, 40));
      return {
        scrollWidth: element.scrollWidth,
        clientWidth: element.clientWidth,
        right: edge.right,
        controlsRight: Math.max(...controls.map((rect) => rect.right)),
        spilled,
      };
    });
    expect(box.scrollWidth).toBeLessThanOrEqual(box.clientWidth);
    expect(box.controlsRight).toBeLessThanOrEqual(box.right);
    expect(box.spilled).toEqual([]);
    return dialog;
  };

  await sidebarOf(page).getByRole('button', { name: '添加伙伴' }).click();
  let dialog = await fits('添加伙伴');
  await dialog.getByRole('textbox').first().fill(longName);
  await dialog.getByRole('textbox').nth(1).fill(longRole);
  await fits('添加伙伴');
  await dialog.getByRole('radio', { name: '从已有 Agent 设为伙伴' }).click();
  await expect(dialog.getByRole('radio', { name: /e2e-roster-3117/ })).toBeVisible();
  dialog = await fits('添加伙伴');
  await dialog.getByRole('radio', { name: /e2e-roster-3117/ }).click();
  await expect(dialog.getByRole('button', { name: '设为伙伴' })).toBeEnabled();
  await fits('添加伙伴');
  await page.keyboard.press('Escape');

  await page.locator('[data-bot-handle="compliance"] button').first().click({ button: 'right' });
  await page.getByRole('menuitem', { name: '编辑资料' }).click();
  await fits('编辑资料');
  expect(denied).toEqual([]);
});

// The browser half of the server's Bot-removal contract (docs/plans/bots.md D1):
// once a Bot is removed its Session is hidden, so every open tab drops it from
// the Inbox and badges the server's number, without a reload. The fixture is the
// server: one state behind both tabs, publishing what the real one publishes.
test('two open tabs follow a Bot removal and a switch change to the server\'s badge', async ({ page }) => {
  const tabs = [page, await page.context().newPage()];
  const server = {
    bots: [MAIN, CSO, PM],
    sessions: INBOX.sessions,
    preferences: { enabled: true, bots: true, sessions: false },
  };
  const unread = () => Object.fromEntries(server.sessions.filter((row) => row.unread_count > 0).map((row) => [row.session_id, row.unread_count]));
  const badgeTotal = () => server.sessions.reduce((sum, row) => {
    const bot = server.bots.some((candidate) => `ses_${candidate.handle}` === row.session_id);
    const loud = server.preferences.enabled && (bot ? server.preferences.bots : server.preferences.sessions);
    return sum + (loud ? row.unread_count : 0);
  }, 0);
  // What the server publishes reaches every open stream.
  const publish = (type: string, data: object) => Promise.all(tabs.map((tab) => tab.evaluate(
    ([name, body]) => (window as unknown as { __serverEvent: (type: string, data: object) => void }).__serverEvent(name, body),
    [type, data] as const,
  )));

  const denials: string[][] = [];
  for (const tab of tabs) {
    denials.push((await serveBots(tab, 'zh', server.bots)).denied);
    await tab.addInitScript(() => {
      const streams = new Set<EventTarget>();
      class FixtureEvents extends EventTarget {
        readyState = 1;
        constructor() { super(); streams.add(this); }
        close() { streams.delete(this); this.readyState = 2; }
      }
      Object.defineProperty(window, 'EventSource', { value: FixtureEvents });
      const target = window as unknown as { __serverEvent: (type: string, data: object) => void; __appBadges: number[] };
      target.__serverEvent = (type, data) => streams.forEach((stream) => stream.dispatchEvent(new MessageEvent(type, {
        data: JSON.stringify({ type, data }),
      })));
      target.__appBadges = [];
      Object.defineProperty(navigator, 'setAppBadge', { configurable: true, value: async (count: number) => { target.__appBadges.push(count); } });
      Object.defineProperty(navigator, 'clearAppBadge', { configurable: true, value: async () => { target.__appBadges.push(0); } });
    });
    await tab.route('**/api/bots', (route) => (route.request().method() === 'GET'
      ? route.fulfill({ json: { ok: true, bots: server.bots.map(payload) } })
      : route.fallback()));
    await tab.route('**/api/inbox**', (route) => {
      const only = new URL(route.request().url()).searchParams.get('session');
      const sessions = only ? server.sessions.filter((row) => row.session_id === only) : server.sessions;
      const map = unread();
      return route.fulfill({ json: {
        sessions, next_cursor: null, unread_by_session: map,
        unread_total: Object.values(map).reduce((sum, n) => sum + n, 0), unread_sessions: Object.keys(map).length,
        badge_total: badgeTotal(),
      } });
    });
    await tab.route('**/api/notifications/preferences', async (route) => {
      if (route.request().method() === 'PUT') {
        server.preferences = { ...server.preferences, ...route.request().postDataJSON() };
        await route.fulfill({ json: { ok: true, preferences: server.preferences } });
        await publish('notifications.preferences.updated', { user_key: 'local', preferences: server.preferences });
        return;
      }
      return route.fulfill({ json: { ok: true, preferences: server.preferences } });
    });
    await tab.route('**/api/bots/cso', async (route) => {
      if (route.request().method() !== 'DELETE') return route.fallback();
      // Removing the Bot hides its Session everywhere the server answers.
      server.bots = server.bots.filter((bot) => bot.handle !== 'cso');
      server.sessions = server.sessions.filter((row) => row.session_id !== 'ses_cso');
      await route.fulfill({ json: { ok: true, bot: payload(CSO, 1) } });
      await publish('bots.changed', { handle: 'cso' });
    });
    await tab.setViewportSize(DESKTOP);
    await open(tab, '/', { lang: 'zh' });
  }
  const [a] = tabs;
  const lastBadge = (tab: Page) => tab.evaluate(() => (window as unknown as { __appBadges: number[] }).__appBadges.at(-1));

  // Sessions are muted: both tabs count the Bot's 2.
  for (const tab of tabs) await expect(sidebarOf(tab).getByRole('link', { name: '收件箱，2 条未读' })).toBeVisible();

  await sidebarOf(a).getByRole('button', { name: '首席安全官，2 条未读' }).click({ button: 'right' });
  await a.getByRole('menu').getByRole('menuitem', { name: '移出伙伴' }).click();
  await a.getByRole('dialog', { name: '把首席安全官移出伙伴？' }).getByRole('button', { name: '移出伙伴' }).click();
  // Its Session is gone, not moved to Sessions: only the muted Session's 3 remains.
  await expect.poll(() => badgeTotal()).toBe(0);
  for (const tab of tabs) {
    await expect(sidebarOf(tab).getByRole('button', { name: /^首席安全官/ })).toHaveCount(0);
    await expect(sidebarOf(tab).getByRole('link', { name: '收件箱：有未读，通知已关闭' })).toBeVisible();
    await expect.poll(() => lastBadge(tab)).toBe(0);
  }

  const inbox = sidebarOf(a).getByRole('link', { name: '收件箱：有未读，通知已关闭' });
  await inbox.hover();
  const popover = a.getByRole('dialog', { name: '收件箱' });
  await expect(popover).not.toContainText('首席安全官');
  await popover.getByRole('button', { name: '通知设置' }).click();
  await popover.getByRole('switch', { name: '会话消息' }).click();

  // Both tabs show the number the server gives every reader.
  await expect.poll(() => badgeTotal()).toBe(3);
  for (const tab of tabs) {
    await expect(sidebarOf(tab).getByRole('link', { name: '收件箱，3 条未读' })).toBeVisible();
    await expect.poll(() => lastBadge(tab)).toBe(3);
  }
  for (const denied of denials) expect(denied).toEqual([]);
});
