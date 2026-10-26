import { expect, test, type Locator, type Page } from '@playwright/test';
import { open, serveProduct } from './support';

// The composer's route trigger names who answers the next message. When the
// action row runs short — a phone with Queue and Stop showing beside Voice —
// detail gives way before the Agent: the effort first, then the model, each
// either whole or gone. The Agent's name is never cut.

const SESSION_ID = 'route-trigger-fixture';
const EFFORTS = ['low', 'medium', 'high', 'xhigh'].map((value) => ({ value, label: value }));

async function serveChat(page: Page, { running }: { running: boolean }) {
  const denied = await serveProduct(page);
  const session = {
    id: SESSION_ID, scope_id: 'fixture', project_id: 'proj-1', title: 'Route trigger',
    agent_id: 'agt-pm', agent_name: 'pm-claude', agent_backend: 'claude', model: 'claude-opus-4-7',
    reasoning_effort: 'xhigh', status: 'active', agent_status: running ? 'running' : 'idle', pinned: false,
    workdir: '/fixture/work', native_session_id: 'native-1', metadata: {},
    created_at: '2026-10-26T00:00:00Z', updated_at: '2026-10-26T00:00:00Z',
  };
  const turnState = {
    in_flight: running, foreground: running ? 'running' : 'idle', native_turn_started: running,
    pending_input_count: 0, background_activities: [], pending_activity_output_count: 0, connection: 'connected',
  };
  const agents = [{ id: 'agt-pm', name: 'pm-claude', backend: 'claude', enabled: true, model: 'claude-opus-4-7', reasoning_effort: 'xhigh' }];
  await page.route(`**/api/sessions/${SESSION_ID}/bootstrap`, (route) => route.fulfill({ json: {
    session, capabilities: { can_chat: true }, agents, default_agent_name: 'pm-claude', config: { ui: {} },
    messages: [], queued: [], next_after_id: null, next_before_id: null,
    draft: { text: '', updated_at: null }, turn_state: turnState,
  } }));
  await page.route(`**/api/sessions/${SESSION_ID}`, (route) => route.fulfill({ json: session }));
  await page.route(`**/api/sessions/${SESSION_ID}/queue`, (route) => route.fulfill({ json: [] }));
  await page.route(`**/api/sessions/${SESSION_ID}/turn-state`, (route) => route.fulfill({ json: turnState }));
  await page.route(`**/api/sessions/${SESSION_ID}/activity**`, (route) => route.fulfill({ json: { groups: [] } }));
  await page.route(`**/api/inbox/${SESSION_ID}/read`, (route) => route.fulfill({ json: { ok: true } }));
  await page.route('**/api/asr/status', (route) => route.fulfill({ json: { available: true } }));
  // The closed trigger names the model by its catalog label.
  await page.route('**/api/models/agents/**', (route) => route.fulfill({ json: { ok: false } }));
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

async function expectAgentWhole(parts: Locator) {
  const agent = parts.locator(':scope > span:not([data-route-detail])');
  await expect(agent).toHaveText('pm-claude');
  expect(await agent.evaluate((node) => node.scrollWidth <= node.clientWidth)).toBe(true);
  expect(inside((await agent.boundingBox())!, (await parts.boundingBox())!)).toBe(true);
}

for (const width of [320, 390]) {
  test(`a ${width}px phone with Queue and Stop keeps the Agent's name whole and drops detail whole`, async ({ page }) => {
    const denied = await serveChat(page, { running: true });
    await page.setViewportSize({ width, height: 760 });
    await open(page, `/chat/${SESSION_ID}`);
    await expect(page.getByRole('button', { name: 'Stop', exact: true })).toBeVisible();
    await page.locator('main [contenteditable="true"]').last().click();
    await page.keyboard.type('Run the next step after this one');
    await expect(page.getByRole('button', { name: 'Send to queue', exact: true })).toBeVisible();

    const trigger = page.locator('main').getByRole('button', { name: /pm-claude/ });
    const parts = trigger.locator('[data-route-trigger-parts]');
    await expectAgentWhole(parts);
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
