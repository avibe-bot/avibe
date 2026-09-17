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

export const VIEWPORTS = [
  { width: 1200, height: 800 },
  { width: 1440, height: 900 },
  { width: 1024, height: 768 },
  { width: 768, height: 1024 },
  { width: 390, height: 844 },
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
    if (request.url().startsWith(ORIGIN) && (method === 'GET' || method === 'HEAD')) return route.fallback();
    denied.push(`${method} ${request.url()}`);
    return route.abort();
  });
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
export async function openOnboarding(
  page: Page,
  options: { lang?: string; theme?: string | null; realTime?: boolean } = {},
) {
  if (!options.realTime) await page.clock.install();
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
  return page.evaluate(() => document.getAnimations()
    .filter((animation) => {
      if (!('animationName' in animation)) return false;
      // The onboarding's own motion, which is the whole of what its lifecycle owns.
      const target = (animation.effect as KeyframeEffect | null)?.target ?? null;
      return !!target?.closest('.onboarding-shell');
    })
    .map((animation) => ({
      name: String((animation as unknown as { animationName: string }).animationName),
      state: animation.playState,
      at: Math.round(Number(animation.currentTime ?? 0)),
    }))
    .sort((left, right) => `${left.name}${left.at}`.localeCompare(`${right.name}${right.at}`)));
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
}
