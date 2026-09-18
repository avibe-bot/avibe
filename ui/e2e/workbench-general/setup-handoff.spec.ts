import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';

import { DESKTOP, config, open, serveProduct } from './support';

/**
 * The handoff the wizard makes to the home, driven end to end through the real
 * product: the setup screen's own Enter, the real router, the real
 * `ApiProvider`, and the home's own readiness read.
 *
 * What is under test is a claim about the running system. The wizard says setup
 * finished; only the backend can say it is ready; and the banner may only speak
 * once BOTH have, about the backend this home would actually run. So the
 * fixture holds the home's read open — a state no unit test can stage, because
 * the pending window only exists between a real navigation and a real response —
 * and the banner must not appear until that read answers.
 *
 * The other half of the claim is that the banner is only a banner. The user is
 * already working while it arrives, so the route they picked — Agent, and the
 * MODEL on it, which is the field a re-seeded default would quietly overwrite —
 * has to read the same before it appears, after it appears, and after it is
 * waved off.
 *
 * Hermetic like the rest of the suite: every request is answered or refused by
 * `serveProduct`, nothing reaches a running service, and the one write the flow
 * makes (setup completion) is answered from the same fixture instance the reads
 * come from. Each test asserts nothing was refused.
 */

const COMPOSER = 'Describe a task or ask a question...';
const DRAFT = '整理一下这周的发布说明';

/**
 * One installed, connected assistant and two that are neither. The home's
 * default Agent runs on Codex, so Codex is the only backend that can
 * corroborate anything here — and a fixture where all three were ready could
 * not tell "asked about the right one" from "asked about any of them".
 */
const CONNECTIONS: Record<string, Record<string, unknown>> = {
  codex: { installed: true, enabled: true, auth: 'api_key', application: 'applied', ready: true, entry_eligible: true },
  claude: { installed: false, enabled: false, auth: 'none', application: 'stopped', ready: false, entry_eligible: false },
  opencode: { installed: false, enabled: false, auth: 'none', application: 'stopped', ready: false, entry_eligible: false },
};

/**
 * The default Agent's own model, and the other model its backend offers.
 *
 * `PICKED_MODEL` is deliberately NOT the default: a route still showing the
 * Agent's default after the banner arrives proves nothing, because that is also
 * what a route reset back to defaults would show. Only a model the user chose
 * can tell "the pick survived" from "the default was re-applied".
 */
const AGENT_MODEL = 'gpt-5';
const PICKED_MODEL = 'gpt-5-codex';

const backendOf = (url: string) => new URL(url).pathname.split('/').at(-2) ?? '';

/**
 * The endpoints the setup flow needs, on top of the shared harness.
 *
 * Connection reads answer at once until setup is persisted; every read AFTER
 * that is the home's own — the wizard is gone by then — and is held until the
 * test lets it answer. That boundary is the product's, not the harness's: the
 * completion write is exactly what separates the two.
 */
const withSetupApi = async (page: Page) => {
  const homeReads: string[] = [];
  const writes: unknown[] = [];
  let completed = false;
  let release = () => {};
  const held = new Promise<void>((resolve) => { release = resolve; });

  await page.route('**/api/backend/*/connection', async (route) => {
    const backend = backendOf(route.request().url());
    if (completed) {
      homeReads.push(backend);
      await held;
    }
    await route.fulfill({ json: { ok: true, backend, ...CONNECTIONS[backend] } });
  });
  await page.route('**/api/backend/*/runtime', (route) => {
    const backend = backendOf(route.request().url());
    return route.fulfill({
      json: {
        ok: true,
        name: backend,
        enabled: Boolean(CONNECTIONS[backend]?.enabled),
        cli_path: backend,
        resolved_path: `/fixture/bin/${backend}`,
        installed: Boolean(CONNECTIONS[backend]?.installed),
        current_version: '1.0.0',
        latest_version: '1.0.0',
        has_update: false,
        supports_restart: true,
        process_status: CONNECTIONS[backend]?.ready ? 'running' : 'stopped',
      },
    });
  });
  await page.route('**/api/opencode/permission-status', (route) => route.fulfill({ json: { ok: true, permission_allowed: true } }));
  // The Agent catalog with a model on the default Agent, and the model list its
  // backend answers with — the product's own two reads for this, not a new
  // contract. Stated here rather than in the shared harness: the capture specs
  // assert against that fixture, and this is the one test about the model field.
  await page.route('**/api/agents**', (route) => route.fulfill({
    json: {
      ok: true,
      agents: [
        { id: 'agent-codex', name: 'codex', backend: 'codex', enabled: true, model: AGENT_MODEL },
        { id: 'agent-claude', name: 'claude', backend: 'claude', enabled: true },
      ],
      default_agent_name: 'codex',
    },
  }));
  await page.route('**/api/codex/models', (route) => route.fulfill({
    json: { ok: true, models: [AGENT_MODEL, PICKED_MODEL] },
  }));
  await page.route('**/api/cli/detect**', (route) => {
    const binary = new URL(route.request().url()).searchParams.get('binary') ?? '';
    const name = binary.split('/').pop() ?? '';
    return route.fulfill({
      json: CONNECTIONS[name]?.installed ? { found: true, path: `/fixture/bin/${name}` } : { found: false },
    });
  });
  // The ONE write this flow makes. Answering it here rather than letting the
  // harness refuse it is what makes the completion real — and what it answers
  // with is the same instance every read comes from.
  await page.route('**/api/config', async (route) => {
    const request = route.request();
    if (request.method() !== 'POST') return route.fallback();
    writes.push(request.postDataJSON());
    completed = true;
    return route.fulfill({ json: { ...config('en'), setup_completed: true } });
  });

  return { homeReads, writes, release: () => release() };
};

const composer = (page: Page) => page.getByPlaceholder(COMPOSER);
const agentTrigger = (page: Page) => page.getByRole('button', { name: /codex|claude/ }).first();
const workspaceChip = (page: Page) => page.getByRole('button', { name: /^Workspace: / });
const banner = (page: Page) => page.getByText('Codex is ready to go');
const heroTitle = (page: Page) => page.getByRole('heading', { name: 'What would you like to do?' });

/** `gpt-5` is a prefix of `gpt-5-codex`, so "the trigger shows the Agent's own
 *  default model" has to include the picked one being absent from it. */
const expectDefaultModel = async (page: Page) => {
  await expect(agentTrigger(page)).toContainText(AGENT_MODEL);
  await expect(agentTrigger(page)).not.toContainText(PICKED_MODEL);
};

/** Walks the real wizard to the point where entering is the user's to do. */
async function reachEntry(page: Page) {
  await open(page, '/setup');
  await page.getByRole('button', { name: 'Get started' }).click();
  await expect(page.getByRole('heading', { name: 'Connect AI. Get to work' })).toBeVisible();
  const enter = page.getByRole('button', { name: 'Enter workspace' });
  await expect(enter).toBeEnabled();
  return enter;
}

test.describe('Setup handoff to the Workbench home', () => {
  test.use({ viewport: DESKTOP });

  test('announces the backend only once it has answered for itself', async ({ page }) => {
    const denied = await serveProduct(page);
    const setup = await withSetupApi(page);

    const enter = await reachEntry(page);
    await enter.click();

    // The home, reached by the wizard's own navigation.
    await expect(page).toHaveURL(/127\.0\.0\.1:5213\/$/);
    await expect(heroTitle(page)).toBeVisible();
    expect(setup.writes).toEqual([{ setup_completed: true }]);

    // The home asked the backend its Agent route actually runs — and nothing
    // else. While that read is open there is nothing corroborated, so the
    // wizard's word alone draws no banner.
    await expect.poll(() => setup.homeReads).toEqual(['codex']);
    await expect(banner(page)).toHaveCount(0);

    // The user starts working before anything is announced, and picks a model
    // through the composer's own picker — the route owner the home already has,
    // no second store and no write of its own.
    await expectDefaultModel(page);
    await agentTrigger(page).click();
    await page.getByRole('button', { name: PICKED_MODEL, exact: true }).click();
    await page.keyboard.press('Escape');
    await expect(agentTrigger(page)).toContainText(PICKED_MODEL);
    // Choosing a model is not choosing a backend, so there is nothing new to
    // corroborate and the home asks nothing again.
    expect(setup.homeReads).toEqual(['codex']);
    await expect(banner(page)).toHaveCount(0);

    setup.release();
    await expect(banner(page)).toBeVisible();

    // The Agent the setup left selected is the one the home offers, the model
    // the user picked is untouched by the announcement arriving over it, and
    // setup completion did not rewrite either: the completion write is still
    // the only one this flow has made.
    await expect(agentTrigger(page)).toHaveText(/codex/);
    await expect(agentTrigger(page)).toContainText(PICKED_MODEL);
    await expect(workspaceChip(page)).toContainText('中文项目');
    expect(setup.writes).toEqual([{ setup_completed: true }]);

    // Dismissing is about the banner and nothing else: the home the user has
    // already started using is untouched.
    await composer(page).fill(DRAFT);
    await page.getByRole('button', { name: 'Dismiss' }).click();
    await expect(banner(page)).toHaveCount(0);
    await expect(composer(page)).toHaveValue(DRAFT);
    await expect(agentTrigger(page)).toHaveText(/codex/);
    await expect(agentTrigger(page)).toContainText(PICKED_MODEL);
    await expect(workspaceChip(page)).toContainText('中文项目');

    // The completion was consumed, not read: reloading restores the entry as it
    // now stands, which no longer claims a setup just finished. So the home says
    // nothing about one — and does not even ask, which is why the read count is
    // the assertion rather than the banner alone. (A read here would hang on the
    // still-held fixture; it never happens.)
    const readsBeforeReload = setup.homeReads.length;
    await page.reload();
    await expect(heroTitle(page)).toBeVisible();
    await expect(banner(page)).toHaveCount(0);
    expect(setup.homeReads).toHaveLength(readsBeforeReload);
    // And the model pick was a pick for this composer, never a write: the new
    // page inherits the Agent's own default again, which is the same reason the
    // completion write is still the only request this flow has sent.
    await expectDefaultModel(page);
    expect(setup.writes).toEqual([{ setup_completed: true }]);

    expect(denied).toEqual([]);
  });

  /**
   * The announcement is about the visit the wizard handed off to, so leaving
   * that visit ends it — and only the route can say a departure happened. The
   * home is also unmounted and remounted in place by the setup route guard
   * re-validating, which is NOT a departure and must still be able to announce;
   * a lifetime read from the component's own cleanup cannot tell the two apart.
   *
   * Driven through the shell's own navigation rather than a harness router, so
   * what is observed is the real one: real links, real guard, real surface.
   */
  test('forgets the announcement once the user leaves the home', async ({ page }) => {
    const denied = await serveProduct(page);
    const setup = await withSetupApi(page);

    const enter = await reachEntry(page);
    await enter.click();
    await expect(heroTitle(page)).toBeVisible();
    setup.release();
    await expect(banner(page)).toBeVisible();

    // Away, through the shell's own nav — and really away: the home's composer
    // is gone, not covered.
    await page.getByRole('link', { name: 'Agents' }).click();
    await expect(page).toHaveURL(/\/agents$/);
    await expect(composer(page)).toHaveCount(0);

    // Back to the home as any other visit: nothing is owed, so nothing is
    // announced — and nothing is asked either, which is the assertion that
    // separates "said nothing" from "said nothing yet".
    const readsBeforeReturn = setup.homeReads.length;
    await page.getByRole('link', { name: 'New chat' }).click();
    await expect(heroTitle(page)).toBeVisible();
    await expect(composer(page)).toBeVisible();
    await expect(banner(page)).toHaveCount(0);
    expect(setup.homeReads).toHaveLength(readsBeforeReturn);

    expect(denied).toEqual([]);
  });
});
