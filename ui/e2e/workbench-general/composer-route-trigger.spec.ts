import { expect, test, type Locator, type Page } from '@playwright/test';
import { open, serveProduct } from './support';

// The composer's route trigger names who answers the next message. When the
// action row runs short — a phone with Queue and Stop showing beside Voice —
// detail gives way before the Agent: the effort first, then the model, each
// either whole or gone. The Agent's name is never cut.

const SESSION_ID = 'route-trigger-fixture';
const EFFORTS = ['low', 'medium', 'high', 'xhigh'].map((value) => ({ value, label: value }));

async function serveChat(
  page: Page,
  { running, agentName = 'pm-claude', fast = false }: { running: boolean; agentName?: string; fast?: boolean },
) {
  const denied = await serveProduct(page);
  const session = {
    id: SESSION_ID, scope_id: 'fixture', project_id: 'proj-1', title: 'Route trigger',
    agent_id: 'agt-pm', agent_name: agentName, agent_backend: 'claude', model: 'claude-opus-4-7',
    reasoning_effort: 'xhigh', speed: fast ? 'fast' : null, status: 'active', agent_status: running ? 'running' : 'idle', pinned: false,
    workdir: '/fixture/work', native_session_id: 'native-1', metadata: {},
    created_at: '2026-10-26T00:00:00Z', updated_at: '2026-10-26T00:00:00Z',
  };
  const turnState = {
    in_flight: running, foreground: running ? 'running' : 'idle', native_turn_started: running,
    pending_input_count: 0, background_activities: [], pending_activity_output_count: 0, connection: 'connected',
  };
  const agents = [{ id: 'agt-pm', name: agentName, backend: 'claude', enabled: true, model: 'claude-opus-4-7', reasoning_effort: 'xhigh' }];
  await page.route(`**/api/sessions/${SESSION_ID}/bootstrap`, (route) => route.fulfill({ json: {
    session, capabilities: { can_chat: true }, agents, default_agent_name: agentName, config: { ui: {} },
    messages: [], queued: [], next_after_id: null, next_before_id: null,
    draft: { text: '', updated_at: null }, turn_state: turnState,
  } }));
  await page.route(`**/api/sessions/${SESSION_ID}`, (route) => route.fulfill({ json: session }));
  await page.route(`**/api/sessions/${SESSION_ID}/queue`, (route) => route.fulfill({ json: [] }));
  await page.route(`**/api/sessions/${SESSION_ID}/turn-state`, (route) => route.fulfill({ json: turnState }));
  await page.route(`**/api/sessions/${SESSION_ID}/activity**`, (route) => route.fulfill({ json: { groups: [] } }));
  await page.route(`**/api/inbox/${SESSION_ID}/read`, (route) => route.fulfill({ json: { ok: true } }));
  await page.route('**/api/asr/status', (route) => route.fulfill({ json: { available: true } }));
  // The closed trigger names the model by its catalog label. A fast route reads
  // Model Hub's catalog, which says the model reaches its vendor's fast tier.
  await page.route('**/api/models/agents/**', (route) => route.fulfill({ json: fast
    ? { ok: true, agent: { backend: 'claude', mode: 'hub', catalog_models: [{
      id: 'claude-opus-4-7', origin: 'builtin', display_name: 'Claude Opus 4.7', models_dev_id: null,
      context_window: null, max_output_tokens: null, input_modalities: [], output_modalities: [],
      supports_tools: null, supports_reasoning: true, reasoning_efforts: ['low', 'medium', 'high', 'xhigh'],
      locked: false, routeable: true, effort_menu: { omitted: [], not_on: {} }, fast_mode: true,
    }] } }
    : { ok: false } }));
  await page.route('**/api/claude/models', (route) => route.fulfill({ json: {
    ok: true,
    models: ['claude-opus-4-7', 'claude-sonnet-4-6'],
    model_labels: { 'claude-opus-4-7': 'Claude Opus 4.7', 'claude-sonnet-4-6': 'Claude Sonnet 4.6' },
    reasoning_options: { 'claude-opus-4-7': EFFORTS, 'claude-sonnet-4-6': EFFORTS },
  } }));
  return denied;
}

type Box = { x: number; y: number; width: number; height: number };
const inside = (inner: Box, outer: Box) =>
  inner.x >= outer.x - 0.5 && inner.x + inner.width <= outer.x + outer.width + 0.5
  && inner.y >= outer.y - 0.5 && inner.y + inner.height <= outer.y + outer.height + 0.5;

/** Which detail parts show on the trigger's one visible line; fails on a part shown only in part. */
async function shownDetails(parts: Locator): Promise<string[]> {
  const clip = (await parts.boundingBox())!;
  const shown: string[] = [];
  for (const detail of await parts.locator('[data-route-detail]').all()) {
    const box = (await detail.boundingBox())!;
    const name = (await detail.getAttribute('data-route-detail'))!;
    if (box.y >= clip.y + clip.height - 0.5) continue; // wrapped onto the clipped line
    expect(inside(box, clip), `${name} is shown whole`).toBe(true);
    shown.push(name);
  }
  return shown;
}

const agentOf = (parts: Locator) => parts.locator(':scope > span:not([data-route-detail])');

/** The Agent outranks detail: its start is always shown, and it is cut only once no detail is left. */
async function expectAgentFirst(parts: Locator, name = 'pm-claude') {
  const agent = agentOf(parts);
  await expect(agent).toHaveText(name);
  expect(inside((await agent.boundingBox())!, (await parts.boundingBox())!)).toBe(true);
  const cut = await agent.evaluate((node) => node.scrollWidth > node.clientWidth);
  if (cut) expect(await shownDetails(parts)).toEqual([]);
  return cut;
}

async function expectAgentWhole(parts: Locator, name = 'pm-claude') {
  const agent = agentOf(parts);
  await expect(agent).toHaveText(name);
  expect(await agent.evaluate((node) => node.scrollWidth <= node.clientWidth)).toBe(true);
  expect(inside((await agent.boundingBox())!, (await parts.boundingBox())!)).toBe(true);
}

for (const width of [320, 390]) {
  test(`a ${width}px phone with Queue and Stop drops detail whole before touching the Agent's name`, async ({ page }) => {
    const denied = await serveChat(page, { running: true });
    await page.setViewportSize({ width, height: 760 });
    await open(page, `/chat/${SESSION_ID}`);
    await expect(page.getByRole('button', { name: 'Stop', exact: true })).toBeVisible();
    await page.locator('main [contenteditable="true"]').last().click();
    await page.keyboard.type('Run the next step after this one');
    await expect(page.getByRole('button', { name: 'Send to queue', exact: true })).toBeVisible();

    const trigger = page.locator('main').getByRole('button', { name: /pm-claude/ });
    const parts = trigger.locator('[data-route-trigger-parts]');
    await expectAgentFirst(parts);
    // A phone this size holds a short name whole; 320px is left to font metrics.
    if (width >= 390) await expectAgentWhole(parts);
    const shown = await shownDetails(parts);
    // Detail leaves in order: the effort never outlasts the model.
    expect(shown.includes('effort') ? shown.includes('model') : true).toBe(true);
    // The trigger shares the row without covering its neighbours.
    const triggerBox = (await trigger.boundingBox())!;
    const voiceBox = (await page.getByRole('button', { name: 'Voice input', exact: true }).boundingBox())!;
    expect(triggerBox.x + triggerBox.width).toBeLessThanOrEqual(voiceBox.x + 0.5);
    await page.screenshot({ path: `e2e/.artifacts/workbench-general/shots/composer-route-trigger-${width}.png` });
    expect(denied).toEqual([]);
  });
}

test('a roomy row shows the Agent, model and effort together', async ({ page }) => {
  const denied = await serveChat(page, { running: false });
  await page.setViewportSize({ width: 1200, height: 800 });
  await open(page, `/chat/${SESSION_ID}`);
  const parts = page.locator('main').getByRole('button', { name: /pm-claude/ }).locator('[data-route-trigger-parts]');
  await expect(parts.locator('[data-route-detail="model"]')).toHaveText('Claude Opus 4.7');
  await expectAgentWhole(parts);
  expect(await shownDetails(parts)).toEqual(['model', 'effort']);
  expect(denied).toEqual([]);
});

test('a name wider than a crowded phone row keeps its beginning and ends in an ellipsis', async ({ page }) => {
  const name = 'pm-claude-research-lead';
  const denied = await serveChat(page, { running: true, agentName: name });
  await page.setViewportSize({ width: 320, height: 760 });
  await open(page, `/chat/${SESSION_ID}`);
  await expect(page.getByRole('button', { name: 'Stop', exact: true })).toBeVisible();
  await page.locator('main [contenteditable="true"]').last().click();
  await page.keyboard.type('Run the next step after this one');
  await expect(page.getByRole('button', { name: 'Send to queue', exact: true })).toBeVisible();

  const parts = page.locator('main').getByRole('button', { name: new RegExp(name) }).locator('[data-route-trigger-parts]');
  const agent = agentOf(parts);
  await expect(agent).toHaveText(name);
  // The label fits the line it holds, so its start is never clipped away.
  expect(inside((await agent.boundingBox())!, (await parts.boundingBox())!)).toBe(true);
  expect(await agent.evaluate((node) => getComputedStyle(node).textOverflow)).toBe('ellipsis');
  expect(await agent.evaluate((node) => node.scrollWidth > node.clientWidth)).toBe(true);
  expect(await shownDetails(parts)).toEqual([]);
  await page.screenshot({ path: 'e2e/.artifacts/workbench-general/shots/composer-route-trigger-long-name.png' });
  expect(denied).toEqual([]);
});

test('a route that runs fast keeps its mark beside the chevron when a crowded phone row drops detail', async ({ page }) => {
  const denied = await serveChat(page, { running: true, fast: true });
  await page.setViewportSize({ width: 320, height: 760 });
  await open(page, `/chat/${SESSION_ID}`);
  await expect(page.getByRole('button', { name: 'Stop', exact: true })).toBeVisible();
  await page.locator('main [contenteditable="true"]').last().click();
  await page.keyboard.type('Run the next step after this one');
  await expect(page.getByRole('button', { name: 'Send to queue', exact: true })).toBeVisible();

  const trigger = page.locator('main').getByRole('button', { name: /pm-claude/ });
  const mark = trigger.locator('[data-fast-mark]');
  await expect(mark).toBeVisible();
  // The mark says the route costs more: detail gives way, the mark does not.
  const parts = trigger.locator('[data-route-trigger-parts]');
  expect(await shownDetails(parts)).not.toContain('effort');
  expect(inside((await mark.boundingBox())!, (await trigger.boundingBox())!)).toBe(true);
  await page.screenshot({ path: 'e2e/.artifacts/workbench-general/shots/composer-route-trigger-fast.png' });
  expect(denied).toEqual([]);
});

test('the Fast tooltip explains on hover and keyboard focus, and does not linger after a click', async ({ page }) => {
  const denied = await serveChat(page, { running: false, fast: true });
  await page.route('**/api/workbench/prefs', (route) => route.fulfill({ json: {
    ok: true, background_work_banner_enabled: true, fast_mode_default: true,
  } }));
  await page.setViewportSize({ width: 1280, height: 800 });
  await open(page, `/chat/${SESSION_ID}`);
  await page.locator('main').getByRole('button', { name: /pm-claude/ }).click();
  const fast = page.getByRole('button', { name: 'Fast', exact: true });
  await expect(fast).toBeEnabled();
  const tip = page.getByRole('tooltip').filter({ hasText: 'Fast mode' });
  const opacity = () => tip.evaluate((node) => Number(getComputedStyle(node).opacity));

  await fast.hover();
  await expect.poll(opacity).toBe(1);
  // A click leaves the button focused; the tooltip must not stay over the model list.
  await fast.click();
  await page.mouse.move(0, 0);
  await expect(fast).toBeFocused();
  await expect.poll(opacity).toBe(0);
  // Reaching it from the keyboard still explains it.
  await page.keyboard.press('Shift+Tab');
  await page.keyboard.press('Tab');
  await expect(fast).toBeFocused();
  await expect.poll(opacity).toBe(1);
  expect(denied).toEqual([]);
});
