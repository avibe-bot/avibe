// The setup lineup in a real browser: the built-in assistant stands first in every
// screen's row, the row keeps the reference's tracks, and the built-in card offering its
// models never has the shell's aside drawn over it while the action stays where every
// screen puts it. Hermetic: every read is answered here and every write is refused.
import { expect, test, type Page } from '@playwright/test';
import { PHASES, freezeAt, openOnboarding, openSetup, serveProduct } from './support';
import { CONTRACT_VERSION } from '../../src/components/settings/models/types';

const LINEUP = ['vibey', 'claude', 'codex', 'opencode'];
const NATIVE_MODELS: Record<string, string> = { claude: 'claude-opus-5-5', codex: 'gpt-5.6-sol', opencode: 'openai/gpt-5.6-sol' };

/** Every backend on the Hub with a runnable route, except the built-in one unless it is
 *  given a model. `ready: false` leaves every connection unready, so entry is held and
 *  the caption has something to say. */
async function serveLineup(page: Page, { permitted = true, vibey = null as string | null, ready = true } = {}) {
  const models: Record<string, string> = { ...NATIVE_MODELS, ...(vibey ? { vibey } : {}) };
  const supply = (backend: string) => ({
    backend, cli_present: backend !== 'vibey', mode: 'hub', menu_kind: 'fixed',
    named_agents: [{ name: backend, effective_model_id: models[backend] ?? null, supply_status: models[backend] ? 'ok' : null }],
    catalog_models: models[backend] ? [{ id: models[backend] }] : [],
  });
  const agent = (backend: string) => ({
    id: `${backend}-${backend}`, name: backend, display_name: backend, description: null, backend, model: models[backend] ?? null,
    reasoning_effort: null, enabled: true, archived: false, archived_at: null, source: 'builtin', updated_at: '',
    system_prompt: null, created_at: '', metadata: { builtin_default: true },
  });
  await page.route('**/api/models/agents', (route) => route.fulfill({ json: { ok: true, agents: LINEUP.map(supply) } }));
  await page.route('**/api/models/agents?**', (route) => route.fulfill({ json: { ok: true, agents: LINEUP.map(supply) } }));
  await page.route(/\/api\/agents(\?.*)?$/, (route) => route.fulfill({ json: { ok: true, agents: LINEUP.map(agent), default_agent_name: 'claude' } }));
  await page.route(/\/api\/agents\/[a-z]+$/, (route) => {
    const name = new URL(route.request().url()).pathname.split('/').at(-1)!;
    return route.fulfill({ json: { ok: true, agent: agent(name), default_agent_name: 'claude' } });
  });
  await page.route('**/api/models/agents/*/chains', (route) => {
    const backend = new URL(route.request().url()).pathname.split('/')[4];
    const model = models[backend];
    return route.fulfill({ json: { chains: model ? [{
      contract_version: CONTRACT_VERSION, backend, model_id: model, manual_override: null, route_origin: 'automatic',
      current: { source_id: 'src_setup_fixture', model_id: model },
      chain: [{ source_id: 'src_setup_fixture', model_id: model, channel: 'hub', health: 'healthy', runnable: true, reason: null, retry_at: null }],
      supply_state: 'ok',
    }] : [] } });
  });
  const candidate = (id: string) => ({ id, display_name: null, reasoning_efforts: [], origin: 'provider',
    suppliers: [{ source_id: 'src_setup_fixture', source_name: 'OpenAI', model_id: id }] });
  await page.route('**/api/models/agents/vibey/models/candidates', (route) => route.fulfill({ json: { candidates: {
    builtin: [], providers: ['gpt-5.6-sol', 'gpt-5.6-mini', 'gpt-5.6-nano', 'gpt-5.6-pro'].map(candidate), in_list: [],
  } } }));
  await page.route('**/api/backend/*/connection', (route) => route.fulfill({ json: {
    ok: true, backend: new URL(route.request().url()).pathname.split('/').at(-2), installed: true, enabled: true,
    auth: 'api_key', application: 'applied', ready, entry_eligible: ready, supply_mode: 'hub',
  } }));
  await page.route('**/api/opencode/permission-status', (route) => route.fulfill({ json: { ok: true, permission_allowed: permitted } }));
}

type Box = { x: number; y: number; width: number; height: number };
const boxes = (page: Page, selector: string): Promise<Box[]> => page.locator(selector).evaluateAll((nodes) =>
  nodes.map((node) => { const { x, y, width, height } = node.getBoundingClientRect(); return { x, y, width, height }; }));
/** Where the action sits in the shell's own scroll space: a click Playwright makes may
 *  scroll the shell, and that moves the viewport, not the layout. */
const actionTop = (page: Page) => page.locator('.onboarding-primary-action').evaluate((node) =>
  node.getBoundingClientRect().top + (node.closest('.onboarding-shell') as HTMLElement).scrollTop);
const overlaps = (a: Box, b: Box) => a.x < b.x + b.width && b.x < a.x + a.width && a.y < b.y + b.height && b.y < a.y + a.height;

test('the lineup stands the built-in assistant first, on the reference tracks of every screen', async ({ page }) => {
  await page.setViewportSize({ width: 1200, height: 800 });
  await serveProduct(page);
  await serveLineup(page);
  await openOnboarding(page);
  await freezeAt(page, PHASES['codex-working']);
  const intro = await boxes(page, '.onboarding-collaboration-card');
  expect(await page.locator('.onboarding-collaboration-card').evaluateAll((nodes) => nodes.map((node) => node.getAttribute('data-backend'))))
    .toEqual(LINEUP);
  // 242-wide cards with 24 between them, in the 1040 column from 80 to 1120.
  expect(intro.map((box) => Math.round(box.x))).toEqual([80, 346, 612, 878]);
  for (const box of intro) expect(box.width).toBeCloseTo(242, 0);
  const action = (await boxes(page, '.onboarding-primary-action'))[0];
  expect(action.width).toBeCloseTo(299, 0);
  expect(action.x + action.width / 2).toBeCloseTo(600, 0);

  await openSetup(page, 'en');
  await page.clock.runFor(2000);
  // The built-in card grows to offer its models; the others keep the card's own height.
  const vibey = page.locator('.onboarding-assistant[data-backend="vibey"]');
  await expect(vibey.getByRole('button', { name: /gpt-5\.6-sol/ })).toBeVisible();
  const assistants = await boxes(page, '[data-setup-screen="assistants"] .onboarding-assistant');
  expect(assistants.map((box) => Math.round(box.x))).toEqual(intro.map((box) => Math.round(box.x)));
  expect(assistants[0].height).toBeGreaterThan(assistants[1].height);
  expect(new Set(assistants.slice(1).map((box) => Math.round(box.height))).size).toBe(1);
  expect(new Set(assistants.map((box) => Math.round(box.y))).size).toBe(1);

  // The providers screen's destinations are the same row, the built-in one tagged.
  await page.getByRole('button', { name: 'Back to providers' }).click();
  await page.clock.runFor(950);
  const destinations = await boxes(page, '[data-setup-screen="providers"] .setup-destination');
  expect(destinations.map((box) => Math.round(box.x))).toEqual(intro.map((box) => Math.round(box.x)));
  await expect(page.locator('.setup-destination').first()).toContainText('Built-in');
});

/**
 * The caption is a row of the assistants' own grid and OpenCode asks for its permission in
 * its own card, so nothing is drawn over a card at any band; every band reserves both and
 * the offer, so the action keeps the introduction's y. Walked with the caption speaking
 * beside the open offer, and silent beside a Vibey that already has its model.
 */
for (const [width, height] of [[1200, 800], [1440, 900], [1920, 1080], [1280, 1024], [1024, 768], [800, 1000], [390, 844]] as const) {
  for (const state of ['held', 'ready'] as const) {
    test(`${width}x${height} ${state}: the caption is in flow under the cards, and the action holds still`, async ({ page }) => {
      await page.setViewportSize({ width, height });
      await serveProduct(page);
      await serveLineup(page, state === 'held' ? { permitted: false, ready: false } : { permitted: false, vibey: 'gpt-5.6-sol' });
      await openOnboarding(page);
      await freezeAt(page, PHASES['codex-working']);
      const introAction = await actionTop(page);
      await openSetup(page, 'en');
      await page.clock.runFor(2000);
      const opencode = page.locator('.onboarding-assistant[data-backend="opencode"]');
      await expect(opencode.getByRole('button', { name: 'Allow tool calls' })).toBeVisible();
      const caption = page.locator('[data-setup-screen="assistants"] .onboarding-setup-aside');
      if (state === 'held') {
        await expect(page.locator('.onboarding-assistant[data-backend="vibey"] .onboarding-model-offer')).toBeVisible();
        await expect(caption).toContainText('Pick a model for Vibey');
      }
      const captionBox = (await boxes(page, '[data-setup-screen="assistants"] .onboarding-setup-aside'))[0];
      const footer = (await boxes(page, '.onboarding-setup-footer'))[0];
      for (const card of await boxes(page, '[data-setup-screen="assistants"] .onboarding-assistant')) {
        expect(overlaps(card, captionBox)).toBe(false);
      }
      expect(overlaps(footer, captionBox)).toBe(false);
      expect(await actionTop(page)).toBeCloseTo(introAction, 0);
    });
  }
}

/** OpenCode's permission write, held open so the card's loading, refusal and success are
 *  three observable moments rather than one settled render. */
async function deferredPermission(page: Page) {
  const waiting: (() => void)[] = [];
  let failing = true;
  await page.route('**/api/opencode/setup-permission', async (route) => {
    await new Promise<void>((resolve) => { waiting.push(resolve); });
    return route.fulfill({ json: failing
      ? { ok: false, message: 'opencode.json could not be written: the configuration directory is owned by another user', config_path: '/fixture/opencode.json' }
      : { ok: true, message: 'Allowed', config_path: '/fixture/opencode.json' } });
  });
  return {
    settle: async () => { await expect.poll(() => waiting.length).toBeGreaterThan(0); waiting.shift()!(); },
    succeed: () => { failing = false; },
  };
}

for (const [width, height] of [[1366, 768], [1920, 1080]] as const) {
  test(`${width}x${height}: OpenCode asks for its permission in its card, and the pair holds through the write`, async ({ page }) => {
    await page.setViewportSize({ width, height });
    await serveProduct(page);
    await serveLineup(page, { permitted: false });
    const permission = await deferredPermission(page);
    await openOnboarding(page);
    const introAction = await actionTop(page);
    await openSetup(page, 'en');
    await page.clock.runFor(2000);
    const card = page.locator('.onboarding-assistant[data-backend="opencode"]');
    await expect(card.getByText('OpenCode stops to approve every tool call')).toBeVisible();
    const allow = card.getByRole('button', { name: 'Allow tool calls' });
    const holds = async () => {
      expect(await actionTop(page)).toBeCloseTo(introAction, 0);
      const caption = (await boxes(page, '[data-setup-screen="assistants"] .onboarding-setup-aside'))[0];
      for (const one of await boxes(page, '[data-setup-screen="assistants"] .onboarding-assistant')) {
        expect(overlaps(one, caption)).toBe(false);
      }
    };
    await holds();
    // Unanswered: the row says so and nothing moves.
    await allow.click();
    await expect(allow).toBeDisabled();
    await holds();
    // Refused: in the note's place; the server's sentence is a click away.
    await permission.settle();
    const refusal = card.locator('details.onboarding-assistant-note');
    await expect(refusal).toContainText('Could not allow tool calls');
    await holds();
    // Granted: the request is not a cleared message but an absent one.
    permission.succeed();
    await allow.click();
    await permission.settle();
    await expect(allow).toHaveCount(0);
    await expect(refusal).toHaveCount(0);
    await holds();
  });
}
