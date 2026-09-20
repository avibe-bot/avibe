import type { Page, Route } from '@playwright/test';

export const ORIGIN = 'http://127.0.0.1:5212';

/** Every phase of the authored 8.9s loop, addressed by the elapsed time it freezes at. */
export const PHASES = {
  'pm-working': 600,
  'handoff-to-codex': 1875,
  'codex-working': 3000,
  'handoff-to-opencode': 4125,
  'tests-running': 5200,
  'return-to-pm': 6500,
  'pm-summary': 8000,
} as const;

/**
 * The accepted responsive matrix, plus the two widths between its rows that the
 * composition actually changes at. 1920 and 1440 are the wide desktop that takes the
 * 1200 content box; 1366 and 1200 are the standard desktop on 976; 1024 and 768 are
 * where the diagram stops following its proportion; 390, 375 and 320 are the phones.
 */
export const VIEWPORTS = [
  { width: 1920, height: 1080 },
  { width: 1440, height: 900 },
  { width: 1366, height: 768 },
  { width: 1200, height: 800 },
  { width: 1024, height: 768 },
  { width: 768, height: 1024 },
  { width: 390, height: 844 },
  { width: 375, height: 812 },
  { width: 320, height: 568 },
] as const;

export const size = ({ width, height }: { width: number; height: number }) => `${width}x${height}`;

const CONFIG = {
  platforms: { enabled: [] },
  agents: {
    claude: { enabled: true, cli_path: 'claude' },
    codex: { enabled: true, cli_path: 'codex' },
    opencode: { enabled: true, cli_path: 'opencode' },
  },
  agent: { default_cwd: '/fixture/work' },
  show_duration: false,
};

const CATALOG = {
  platforms: ['slack', 'discord', 'telegram', 'lark', 'wechat'].map((id) => ({ id, enabled: false })),
};

/**
 * Answers the product's reads locally and refuses everything else. Any request that
 * is not a same-origin GET — every write verb, and anything addressed off this
 * fixture's origin — is aborted and recorded, so a capture can prove it never
 * touched a running service or real user state.
 */
export async function serveProduct(page: Page) {
  const denied: string[] = [];
  await page.route('**/*', async (route: Route) => {
    const request = route.request();
    const method = request.method();
    if (request.url().startsWith(ORIGIN) && (method === 'GET' || method === 'HEAD') && !new URL(request.url()).pathname.startsWith('/api/')) return route.fallback();
    denied.push(`${method} ${request.url()}`);
    return route.abort();
  });
  await page.route('**/status', (route) => route.fulfill({ json: { state: 'running' } }));
  await page.route('**/api/backend/*/connection', (route) => route.fulfill({ json: { ok: true, backend: new URL(route.request().url()).pathname.split('/').at(-2), installed: true, enabled: true, auth: 'none', application: 'applied', ready: false, entry_eligible: false } }));
  await page.route('**/api/config', (route) => route.fulfill({ json: CONFIG }));
  await page.route('**/api/platforms', (route) => route.fulfill({ json: CATALOG }));
  await page.route('**/api/settings**', (route) => route.fulfill({ json: { channels: {} } }));
  await page.route('**/api/cli/detect**', (route) => {
    const binary = new URL(route.request().url()).searchParams.get('binary') ?? '';
    return route.fulfill({ json: { found: true, path: `/fixture/bin/${binary.split('/').pop()}` } });
  });
  // The setup step reads each assistant's runtime and OpenCode's permission flag. They
  // are answered as an installed, permitted runtime so the screen renders the state the
  // design describes rather than the error state a missing backend would produce.
  await page.route('**/api/backend/*/runtime', (route) => {
    const name = new URL(route.request().url()).pathname.split('/').at(-2);
    return route.fulfill({
      json: {
        ok: true,
        name,
        enabled: true,
        cli_path: name,
        resolved_path: `/fixture/bin/${name}`,
        installed: true,
        current_version: '1.0.0',
        latest_version: '1.0.0',
        has_update: false,
        supports_restart: true,
        process_status: 'running',
      },
    });
  });
  await page.route('**/api/opencode/permission-status', (route) =>
    route.fulfill({ json: { ok: true, permission_allowed: true } }),
  );
  return denied;
}

/**
 * Loads the fixture with a frozen clock so a phase can be held still for comparison.
 *
 * `realTime` leaves the clock alone, which is what a recording of the loop needs: the
 * story is meant to be watched at the speed it was authored at, and a frozen clock can
 * only ever show it one still at a time.
 */
/** Any fixed instant; the story reads elapsed time, so only determinism matters. */
const FIXED_TIME = new Date('2026-09-17T09:00:00Z');

export async function openOnboarding(
  page: Page,
  options: { lang?: string; theme?: string | null; realTime?: boolean } = {},
) {
  if (!options.realTime) {
    // `install` alone replaces the timers but leaves the clock ticking with real time,
    // so the story still lands wherever wall-clock time carried it between two runs —
    // which is how a dark and a light capture of "the same phase" end up different.
    // `pauseAt` is what actually holds it: afterwards only `freezeAt` moves it.
    await page.clock.install({ time: FIXED_TIME });
    await page.clock.pauseAt(FIXED_TIME);
  }
  const query = new URLSearchParams({ lang: options.lang ?? 'en' });
  // `theme: null` omits the parameter, which is the only way to watch the provider
  // choose for itself — the query overrides the stored preference by design.
  const theme = options.theme === undefined ? 'dark' : options.theme;
  if (theme !== null) query.set('theme', theme);
  await page.goto(`/e2e/onboarding-fidelity/fixture.html?${query}`);
  await page.locator('.onboarding-collaboration').waitFor();
}

/** Seeds the Web UI's own theme key, the way a user who once picked one leaves it. */
export function storeThemePreference(page: Page, mode: 'system' | 'light' | 'dark') {
  return page.addInitScript((value) => {
    window.localStorage.setItem('vibe-remote-theme', value);
  }, mode);
}

/** Which card the story is drawing, and what it says — the phase, as the page shows it. */
export function renderedPhase(page: Page) {
  return page.evaluate(() => {
    const cards = [...document.querySelectorAll('.onboarding-collaboration-card')];
    return {
      states: cards.map((card) => card.getAttribute('data-state') ?? ''),
      captions: cards.map((card) => card.querySelector('.onboarding-status-text')?.textContent?.trim() ?? ''),
      written: cards.map((card) => card.querySelectorAll('.onboarding-write-visible').length),
      pulse: document.querySelectorAll('[data-testid="handoff-pulse"]').length,
    };
  });
}

/** Advances the frozen clock to an exact elapsed time inside the loop. */
export async function freezeAt(page: Page, elapsed: number) {
  await page.clock.runFor(elapsed);
  await page.locator('.onboarding-collaboration-card[data-state]').first().waitFor();
}

/**
 * Holds every CSS animation at one point of its OWN timeline. `page.clock` controls
 * timers, which is what the story's clock is built from; it does not control the
 * document timeline the shimmer and the test ticks run on. A screenshot taken after
 * `freezeAt` alone therefore catches those wherever wall-clock time had carried them,
 * and two runs of the same phase differ. Seeking past a `both`-filled animation's
 * duration holds its end state, which is what a settled phase looks like — 1000ms
 * clears the longest of them (750ms shimmer, 340ms ticks at a 480ms last delay).
 */
export async function settleEffects(page: Page, at = 1000) {
  await page.evaluate((time) => {
    for (const animation of document.getAnimations()) {
      animation.currentTime = time;
      animation.pause();
    }
  }, at);
}

/**
 * What the CSS ANIMATIONS are actually doing, which is the state a DOM diff cannot see.
 * Transitions are deliberately left out: `document.getAnimations()` returns those too,
 * but a transition is finite and settles by itself, while an animation loops — so it is
 * the animations that would otherwise play on unseen in a background tab, and only they
 * that `animation-play-state` can hold.
 */
export function cssEffects(page: Page) {
  return page.evaluate(async () => {
    const animations = document.getAnimations().filter((animation) => {
      if (!('animationName' in animation)) return false;
      // The onboarding's own motion, which is the whole of what its lifecycle owns.
      const target = (animation.effect as KeyframeEffect | null)?.target ?? null;
      return !!target?.closest('.onboarding-shell');
    });
    // playState can already say "paused" while the pause task is still pending.
    // Sample the committed timeline, not its one-frame-early hold time.
    await Promise.all(animations.map((animation) => animation.ready));
    return animations.map((animation) => ({
      name: String((animation as unknown as { animationName: string }).animationName),
      state: animation.playState,
      at: Math.round(Number(animation.currentTime ?? 0)),
    }))
      .sort((left, right) => `${left.name}${left.at}`.localeCompare(`${right.name}${right.at}`));
  });
}

/** Simulates the tab going to the background, which no Playwright API does directly. */
export async function setDocumentHidden(page: Page, hidden: boolean) {
  await page.evaluate((value) => {
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => value });
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => (value ? 'hidden' : 'visible') });
    document.dispatchEvent(new Event('visibilitychange'));
  }, hidden);
}

export async function openSetup(page: Page, lang: string) {
  await page.getByRole('button', { name: lang === 'zh' ? '开始使用' : 'Get started' }).click();
  await page.locator('.onboarding-assistants').waitFor();
  // The step switch plays a 450ms entrance that translates and blurs the incoming
  // composition. It runs on the document timeline, so a frozen clock does not hold
  // it; geometry read mid-entrance would be geometry of a composition still
  // arriving. CSS animations, unlike timers, settle on their own.
  await page.locator('.onboarding-step').evaluate((node) => Promise.all(
    node.getAnimations({ subtree: false }).map((animation) => animation.finished.catch(() => null)),
  ));
}

/**
 * The two shapes a migration scan really returns. Three credentials the gateway can
 * take over, and two native subscription tokens it must leave where they are — the
 * setup notice counts only the first kind, so a fixture without the second could not
 * show that it does. The detail strings are the server's own masked form, Chinese
 * included, because that is what the capsule has to stay one line tall around.
 */
export const MIGRATION_KEYS = [
  { id: 'claude-key', backend: 'claude', kind: 'api_key', masked_detail: 'sk-ant-…4f2a + 自定义 Base URL', proposed_action: 'import', selected: true, vendor: 'anthropic', display_name: 'Anthropic', masked_credential: 'sk-ant-…4f2a' },
  { id: 'codex-key', backend: 'codex', kind: 'api_key', masked_detail: 'sk-…9d31', proposed_action: 'import', selected: true, vendor: 'openai', display_name: 'OpenAI', masked_credential: 'sk-…9d31' },
  { id: 'opencode-qwen', backend: 'opencode', kind: 'opencode_provider', masked_detail: 'sk-…7c08 · 阿里云百炼', proposed_action: 'import', selected: true, vendor: 'qwen', display_name: 'alibaba-cn', masked_credential: 'sk-…7c08' },
] as const;
export const MIGRATION_NATIVE = [
  { id: 'claude-oauth', backend: 'claude', kind: 'oauth_native', masked_detail: 'Claude 订阅令牌', proposed_action: 'keep_native', selected: false },
  { id: 'codex-oauth', backend: 'codex', kind: 'oauth_native', masked_detail: 'ChatGPT 订阅令牌', proposed_action: 'keep_native', selected: false },
] as const;

/**
 * Turns the Model Gateway capability on and answers its migration scan, so the setup
 * step draws the API-key import offer. Call it AFTER `serveProduct`: Playwright matches
 * routes newest-first, which is what lets this override that fixture's `/api/config`
 * and answer the scan POST the catch-all would otherwise refuse.
 */
export async function serveModelHub(page: Page) {
  const applied: string[][] = [];
  let items = [...MIGRATION_KEYS, ...MIGRATION_NATIVE] as { id: string }[];
  // The scan is a POST, so the product asks for a CSRF token before issuing it. The
  // base fixture refuses every write and everything that enables one, which is what
  // keeps a capture honest; an opt-in write surface has to answer this itself.
  await page.route('**/api/csrf-token', (route) => route.fulfill({ json: { csrf_token: 'fixture-token' } }));
  await page.route('**/api/config', (route) =>
    route.fulfill({ json: { ...CONFIG, capabilities: { model_hub: { enabled: true } } } }),
  );
  await page.route('**/api/models/migration/scan', (route) => route.fulfill({ json: { scan: { items } } }));
  await page.route('**/api/models/migration/apply', (route) => {
    const ids: string[] = JSON.parse(route.request().postData() || '{}').item_ids ?? [];
    applied.push(ids);
    items = items.filter((item) => !ids.includes(item.id));
    return route.fulfill({ json: { applied: ids.length, sources: [] } });
  });
  return applied;
}
