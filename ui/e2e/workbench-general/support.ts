import type { Page, Route } from '@playwright/test';

export const ORIGIN = 'http://127.0.0.1:5213';

/**
 * 1200 is the width the design was staged at; the others are what prove the
 * claim the packet actually makes — the 248 left column (the sidebar, or the
 * standalone Settings rail standing in for it) is the only fixed measurement,
 * and everything to its right is fluid. A capture at the staging width alone
 * cannot tell a fluid column from a 856-wide one.
 */
export const DESKTOP = { width: 1200, height: 900 };
export const WIDE = { width: 1600, height: 900 };
export const NARROW = { width: 390, height: 844 };

export type Lang = 'en' | 'zh';

const SESSION = {
  remote: false,
  authenticated: true,
  authorization_state: 'current',
  instance_kind: 'personal',
  instance_role: 'owner',
};

/** The instance as this harness's server holds it — exported so a spec that
 *  answers a WRITE to `/api/config` can answer with the same instance it reads,
 *  rather than describing a second one. */
export const config = (language: Lang) => ({
  mode: 'local',
  setup_state: { needs_setup: false },
  platforms: { enabled: [] },
  agents: {
    codex: { enabled: true, cli_path: 'codex' },
    claude: { enabled: true, cli_path: 'claude' },
    opencode: { enabled: true, cli_path: 'opencode' },
  },
  agent: { default_cwd: '/fixture/work' },
  language,
});

// A first-task home with one workspace, which is the state the source board
// draws: a workspace chip with a name in it and no sessions yet. The name and
// the path are deliberately non-ASCII — truncation, measurement and the chip's
// own width are exactly where a Latin-only fixture would flatter the render.
const PROJECTS = {
  projects: [{
    id: 'proj-1',
    scope_id: 'scope-1',
    display_name: '中文项目',
    folder_path: '/Users/max/工作区/中文项目',
    created_at: '2026-01-01T00:00:00Z',
    last_active_at: null,
    archived: false,
    capabilities: { can_chat: true, has_folder: true },
  }],
  sessions: {},
};

const AGENTS = {
  ok: true,
  agents: [
    { name: 'codex', backend: 'codex', enabled: true },
    { name: 'claude', backend: 'claude', enabled: true },
  ],
  default_agent_name: 'codex',
};

// Web fonts are a read from a public CDN and are what the product actually
// draws with; blocking them would make every capture compare the design against
// fallback metrics. Nothing else off this origin is allowed.
const FONT_HOSTS = ['https://fonts.googleapis.com/', 'https://fonts.gstatic.com/'];

/**
 * Answers the product's reads locally and refuses everything else. The guard is
 * registered LAST so Playwright consults it FIRST: anything that is not a
 * same-origin GET — every write verb, and anything addressed off this origin
 * other than the font CDN — is aborted and recorded, so a capture can prove it
 * never touched a running service or any real user state. Same-origin reads
 * fall through to the answers below, and no `/api` request is ever left to
 * reach the (dead) backend port.
 */
export async function serveProduct(page: Page, lang: Lang = 'en') {
  const denied: string[] = [];

  await page.route('**/api/**', (route) => route.fulfill({ json: {} }));

  await page.route('**/api/session', (route) => route.fulfill({ json: SESSION }));
  // The instance language is server-side state, so a capture that only seeded
  // the browser detector would still render whatever this says.
  await page.route('**/api/config', (route) => route.fulfill({ json: config(lang) }));
  await page.route('**/api/csrf-token', (route) => route.fulfill({ json: { csrf_token: 'fixture' } }));
  await page.route('**/api/projects**', (route) => route.fulfill({ json: PROJECTS }));
  await page.route('**/api/workbench/projects-bootstrap**', (route) => route.fulfill({ json: PROJECTS }));
  await page.route('**/api/sessions**', (route) => route.fulfill({ json: { sessions: [] } }));
  await page.route('**/api/agents**', (route) => route.fulfill({ json: AGENTS }));
  await page.route('**/api/inbox**', (route) => route.fulfill({ json: { sessions: [], unread: {} } }));
  // The service badge polls the bare `/status`, not an `/api` path.
  await page.route('**/status', (route) => route.fulfill({ json: { state: 'running' } }));
  await page.route('**/api/version**', (route) => route.fulfill({
    json: { current: '2.4.0', latest: '2.4.0', has_update: false, build: { kind: 'package' } },
  }));
  // The live stream: answered as an open, silent stream. Fulfilling it as JSON
  // would make EventSource reconnect in a loop and repaint mid-capture.
  await page.route('**/api/events**', (route) => route.fulfill({
    status: 200,
    headers: { 'content-type': 'text/event-stream', 'cache-control': 'no-cache' },
    body: ': idle\n\n',
  }));

  await page.route('**/*', async (route: Route) => {
    const request = route.request();
    const method = request.method();
    const url = request.url();
    const isRead = method === 'GET' || method === 'HEAD';

    if (url.startsWith(ORIGIN) && isRead) {
      // Dev-only detail: Vite proxies `/settings` (and a few other prefixes) to
      // the backend, so a deep link would fetch from the dead port instead of
      // the app. Serving index.html for a document request is what the shipped
      // server does for any unknown path, so this restores the product's own
      // behaviour rather than inventing one for the harness.
      if (request.resourceType() === 'document' && url !== `${ORIGIN}/`) {
        const index = await route.fetch({ url: `${ORIGIN}/` });
        return route.fulfill({ response: index, headers: { 'content-type': 'text/html' } });
      }
      return route.fallback();
    }
    if (isRead && FONT_HOSTS.some((host) => url.startsWith(host))) return route.fallback();

    denied.push(`${method} ${url}`);
    return route.abort();
  });

  return denied;
}

/**
 * Opens a real product route. Theme rides the same `?theme=` the app's own
 * ThemeProvider reads, and language is seeded into the key the app's detector
 * reads — neither is restated here, so a capture shows the shipped behaviour.
 */
export async function open(
  page: Page,
  path: string,
  options: { lang?: Lang; theme?: 'light' | 'dark' | 'system' } = {},
) {
  const lang = options.lang ?? 'en';
  await page.addInitScript((value) => {
    window.localStorage.setItem('i18nextLng', value);
  }, lang);
  const query = options.theme ? `?theme=${options.theme}` : '';
  await page.goto(`${path}${query}`);
}

export const box = (page: Page, selector: string) => page.locator(selector).first().boundingBox();
