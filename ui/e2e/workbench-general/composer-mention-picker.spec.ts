import { expect, test, type Page } from '@playwright/test';
import { open, serveProduct } from './support';

// The `#` and `@` pickers open at the caret. Typed near the composer's right
// edge on a phone, the list must still open whole on screen: shifted left as
// far as it needs, never past either edge, and still above the caret.

const SESSION_ID = 'mention-picker-fixture';
const MARGIN = 8;

async function serveChat(page: Page) {
  const denied = await serveProduct(page);
  const session = {
    id: SESSION_ID, scope_id: 'fixture', project_id: 'proj-1', title: 'Mention picker',
    agent_id: 'agt-pm', agent_name: 'pm-claude', agent_backend: 'claude', model: 'claude-opus-4-7',
    reasoning_effort: null, speed: null, status: 'active', agent_status: 'idle', pinned: false,
    workdir: '/fixture/work', native_session_id: 'native-1', metadata: {},
    created_at: '2026-11-01T00:00:00Z', updated_at: '2026-11-01T00:00:00Z',
  };
  const turnState = {
    in_flight: false, foreground: 'idle', native_turn_started: false,
    pending_input_count: 0, background_activities: [], pending_activity_output_count: 0, connection: 'connected',
  };
  const agents = [
    { id: 'agt-pm', name: 'pm-claude', backend: 'claude', enabled: true, model: 'claude-opus-4-7' },
    { id: 'agt-research', name: 'research-assistant-with-a-long-name', backend: 'codex', enabled: true, model: 'gpt-6-astra' },
    { id: 'agt-ops', name: 'ops', backend: 'vibey', enabled: true, model: 'claude-sonnet-5-5' },
  ];
  // Long titles: the list is as wide as it gets, not only its minimum.
  const others = [
    'Review the release checklist before the Friday cut',
    'Reproduce the Windows first-run bootstrap failure on a clean VM',
    'Benchmark relay latency',
    'PR watch for the usage ledger',
  ].map((title, index) => ({ ...session, id: `other-${index}`, title }));
  await page.route(`**/api/sessions/${SESSION_ID}/bootstrap`, (route) => route.fulfill({ json: {
    session, capabilities: { can_chat: true }, agents, default_agent_name: 'pm-claude', config: { ui: {} },
    messages: [], queued: [], next_after_id: null, next_before_id: null,
    draft: { text: '', updated_at: null }, turn_state: turnState,
  } }));
  await page.route((url) => url.pathname === '/api/sessions', (route) => route.fulfill({ json: { sessions: [session, ...others] } }));
  await page.route(`**/api/sessions/${SESSION_ID}`, (route) => route.fulfill({ json: session }));
  await page.route(`**/api/sessions/${SESSION_ID}/queue`, (route) => route.fulfill({ json: [] }));
  // Typing saves the draft; answered here so the guard records no write.
  await page.route(`**/api/sessions/${SESSION_ID}/draft`, (route) => route.fulfill({ json: { ok: true } }));
  await page.route(`**/api/sessions/${SESSION_ID}/turn-state`, (route) => route.fulfill({ json: turnState }));
  await page.route(`**/api/sessions/${SESSION_ID}/activity**`, (route) => route.fulfill({ json: { groups: [] } }));
  await page.route(`**/api/inbox/${SESSION_ID}/read`, (route) => route.fulfill({ json: { ok: true } }));
  await page.route('**/api/asr/status', (route) => route.fulfill({ json: { available: false } }));
  await page.route('**/api/models/agents/**', (route) => route.fulfill({ json: { ok: false } }));
  await page.route('**/api/workbench/prefs', (route) => route.fulfill({ json: {
    ok: true, background_work_banner_enabled: true, fast_mode_default: false,
  } }));
  return denied;
}

async function typeToTheRightEdge(page: Page) {
  const editor = page.locator('main [contenteditable="true"]').last();
  await editor.click();
  // Grow the first line until the caret sits within a few characters of the edge.
  const box = (await editor.boundingBox())!;
  for (let i = 0; i < 80; i += 1) {
    await page.keyboard.type('m');
    const caretX = await page.evaluate(() => {
      const range = window.getSelection()!.getRangeAt(0).cloneRange();
      return range.getBoundingClientRect().right;
    });
    if (caretX > box.x + box.width - 24) break;
  }
  await page.keyboard.type(' ');
}

for (const { trigger, width } of [
  { trigger: '#', width: 390 },
  { trigger: '#', width: 320 },
  { trigger: '@', width: 390 },
]) {
  test(`the ${trigger} picker opens whole on a ${width}px phone when typed at the right edge`, async ({ page }) => {
    const denied = await serveChat(page);
    await page.setViewportSize({ width, height: 760 });
    await open(page, `/chat/${SESSION_ID}`);
    await typeToTheRightEdge(page);
    await page.keyboard.type(trigger);

    const picker = page.locator('[data-mention-picker]');
    await expect(picker.locator('li').first()).toBeVisible();
    // Results arrive after the list opens; read the settled place.
    const span = async () => {
      const box = (await picker.boundingBox())!;
      return [Math.round(box.x), Math.round(box.x + box.width)];
    };
    await expect.poll(async () => {
      const [left, right] = await span();
      return left >= MARGIN - 1 && right <= width - MARGIN + 1;
    }, { message: `picker spans ${JSON.stringify(await span())} of a ${width}px screen` }).toBe(true);
    // Still opens above the caret, clear of the line being typed.
    const caret = await page.evaluate(() => {
      const rect = window.getSelection()!.getRangeAt(0).getBoundingClientRect();
      return rect.top + rect.height / 2;
    });
    const pickerBox = (await picker.boundingBox())!;
    expect(pickerBox.y + pickerBox.height).toBeLessThan(caret);
    await page.screenshot({ path: `e2e/.artifacts/workbench-general/shots/composer-mention-picker-${trigger === '#' ? 'session' : 'agent'}-${width}.png` });

    await page.keyboard.press('Escape');
    await expect(picker).toHaveCount(0);
    expect(denied).toEqual([]);
  });
}
