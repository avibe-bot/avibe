import { expect, test, type Locator } from '../support/browserTest';

import { DESKTOP, NARROW, ORIGIN, WIDE, config, open, serveProduct } from './support';

/**
 * The packet's geometry claims, measured in a browser rather than inferred from
 * class names. Workbench keeps its adjustable 248px default/sidebar offset;
 * standalone Settings stands in for that sidebar, so its rail is the same 248px
 * and the left column cannot jump when Settings opens. The 944px outer content
 * frame still carries 880px of common content at the desktop reference width.
 */

const SIDEBAR = 'aside.fixed';
const SHELL_SCROLL = 'main#app-shell-scroll';
const SHELL_CONTENT = 'main#app-shell-scroll > div';
const SETTINGS_RAIL = 'nav[aria-label="Settings sections"]';
const SETTINGS_PAGE = 'nav[aria-label="Settings sections"] + section';
const SETTINGS_CONTENT = 'nav[aria-label="Settings sections"] + section > div';
// General now has two preference cards drawn the same way, so name the one the
// geometry claims are about by the selector it owns.
const APPEARANCE_CARD = 'section.bg-background:has([role="radiogroup"][aria-label="Appearance"])';
const PLACEMENT_CARD = 'section.bg-background:has([role="radiogroup"][aria-label="Settings menu"])';
const MOBILE_NAV = 'nav.fixed.bottom-0';
const ULTRA = { width: 1920, height: 1000 };
// A phone too short for the home's group, which therefore scrolls. The composer,
// its last block, must still clear the tab bar.
const NARROW_SHORT = { width: 390, height: 380 };

const MODEL_HUB_AGENT = {
  backend: 'codex',
  cli_present: true,
  mode: 'hub',
  menu_kind: 'fixed',
  selected_model_id: 'fixture-model-中文',
  selected_model_explicit: true,
  sources: { order: [], eligibility: [] },
  routes: {},
  supply_status: 'interrupted',
  model_supply: [{ model_id: 'fixture-model-中文', route_origin: null, chain_length: 0, has_runnable_hop: false }],
  builtin_models: ['fixture-model-中文'],
  named_agents: [],
  menu: null,
};

const MODEL_HUB_RUNTIME = {
  contract_version: 23,
  enabled: true,
  manifest: { name: 'cliproxyapi', resolution: 'resolved', version: 'fixture', source_sha: 'f'.repeat(40), assets: [] },
  status: { installed_version: 'fixture', verified: true, listening: { host: '127.0.0.1', port: 43123 }, health: 'ok' },
};

const MODEL_HUB_SESSION = {
  remote: false,
  authenticated: true,
  authorization_state: 'current',
  instance_kind: 'personal',
  instance_role: 'owner',
  capabilities: {
    is_instance_owner: true, can_read_instance: true, can_chat: true,
    can_manage_projects: true, can_manage_agents: true, can_manage_instance: true,
    can_manage_access_members: true, can_use_agents: true, can_use_skills: true,
    can_use_vault_secrets: true, can_use_show_pages: true, can_use_terminal_files: true,
    can_use_terminal: true, can_use_files: true, can_use_system: true,
  },
};

/**
 * Render the actual Model Hub route with a ready capability, runtime, and
 * Gateway backend. Every response remains test-owned; mutations fall through
 * to serveProduct's strict request guard.
 */
async function serveModelHub(page: import('@playwright/test').Page) {
  const denied = await serveProduct(page);
  const unexpectedModelReads: string[] = [];
  const getOnly = async (route: import('@playwright/test').Route, body: unknown) => {
    if (route.request().method() !== 'GET' && route.request().method() !== 'HEAD') {
      denied.push(`${route.request().method()} ${new URL(route.request().url()).pathname}`);
      return route.abort();
    }
    return route.fulfill({ json: body });
  };
  await page.route('**/api/session', (route) => getOnly(route, MODEL_HUB_SESSION));
  await page.route('**/api/config', (route) => getOnly(route, { ...config('en'), capabilities: { model_hub: { enabled: true } } }));
  await page.route('**/api/models/**', (route) => {
    const path = new URL(route.request().url()).pathname;
    const query = new URL(route.request().url()).search;
    if (path === '/api/models/sources' && !query) return getOnly(route, { sources: [] });
    if (path === '/api/models/runtime/status' && !query) return getOnly(route, MODEL_HUB_RUNTIME);
    if (path === '/api/models/agents' && !query) return getOnly(route, { agents: [MODEL_HUB_AGENT] });
    if (path === '/api/models/agents' && query === '?refresh_cli_presence=1') {
      return getOnly(route, { agents: [MODEL_HUB_AGENT] });
    }
    if (path === '/api/models/agents/codex/chains' && !query) return getOnly(route, { chains: [] });
    // The Tools tab, Sources activation and Source detail share this read.
    if (path === '/api/models/hosted-tools' && !query) return getOnly(route, { ok: true, hosted_tools: { tools: [] } });
    // The page scans for importable credentials on mount; a scan is a POST that
    // changes nothing, so it is answered rather than counted as a write.
    if (path === '/api/models/migration/scan' && !query && route.request().method() === 'POST') {
      return route.fulfill({ json: { ok: true, scan: { items: [] } } });
    }
    unexpectedModelReads.push(`${route.request().method()} ${path}${query}`);
    return route.abort();
  });
  return { denied, unexpectedModelReads };
}

const widthOf = async (page: import('@playwright/test').Page, selector: string) => {
  const box = await page.locator(selector).first().boundingBox();
  expect(box, `${selector} should be laid out`).not.toBeNull();
  return box!.width;
};

const frameGeometry = async (page: import('@playwright/test').Page) => page.locator(SETTINGS_CONTENT).evaluate((node) => {
  const style = getComputedStyle(node);
  const box = node.getBoundingClientRect();
  return {
    x: box.x,
    width: box.width,
    contentWidth: node.clientWidth - Number.parseFloat(style.paddingLeft) - Number.parseFloat(style.paddingRight),
    paddingLeft: Number.parseFloat(style.paddingLeft),
    paddingRight: Number.parseFloat(style.paddingRight),
  };
});

const settleSettingsFrame = async (page: import('@playwright/test').Page) => {
  await page.locator(SETTINGS_CONTENT).evaluate(async (node) => {
    await Promise.all(node.getAnimations().map((animation) => animation.finished));
  });
};

/**
 * What the user's finger would actually land on. `toBeVisible()` cannot answer
 * this: an element covered by a fixed bar is still visible, still has a box, and
 * still fails every tap.
 */
const topmostOver = async (
  page: import('@playwright/test').Page,
  locator: import('@playwright/test').Locator,
) => {
  const box = await locator.boundingBox();
  expect(box, 'control should be laid out').not.toBeNull();
  return page.evaluate(({ x, y }) => {
    const node = document.elementFromPoint(x, y);
    if (!node) return 'nothing';
    const control = node.closest('button, a');
    return control?.getAttribute('aria-label') ?? control?.textContent?.trim() ?? node.tagName;
  }, { x: box!.x + box!.width / 2, y: box!.y + box!.height / 2 });
};

const bottomOf = async (locator: import('@playwright/test').Locator) => {
  const box = await locator.boundingBox();
  expect(box, 'element should be laid out').not.toBeNull();
  return box!.y + box!.height;
};

test.describe('workbench home geometry', () => {
  for (const remembered of ['/admin/settings/memory', '/settings/memory']) {
    test(`PWA launch canonicalizes persisted ${remembered} and restores only once`, async ({ page }) => {
      const denied = await serveProduct(page);
      await page.addInitScript((path) => {
        Object.defineProperty(navigator, 'userAgent', { get: () => 'iPhone' });
        Object.defineProperty(navigator, 'standalone', { get: () => true });
        localStorage.setItem('avibe.pwa.last-route.v1', `${path}?old=1#obsolete`);
      }, remembered);
      await open(page, '/');
      await expect(page).toHaveURL(/\/settings\/general$/);
      await expect(page.locator(`${SETTINGS_CONTENT} h1`)).toHaveText('General');
      await expect.poll(() => page.evaluate(() => localStorage.getItem('avibe.pwa.last-route.v1')))
        .toBe('/settings/general');
      // The app pushes a restore entry above the manifest root. Going back is
      // a real in-app action, not another cold launch or another restoration.
      await page.goBack();
      await expect(page).toHaveURL(/\/$/);
      await expect.poll(() => page.evaluate(() => localStorage.getItem('avibe.pwa.last-route.v1'))).toBe('/');
      expect(denied).toEqual([]);
    });
  }
  for (const [remembered, launch, expected] of [
    ['/settings/memory', '/settings/shortcuts', '/settings/shortcuts'],
    ['/settings/memory/unknown', '/', '/'],
    ['//evil.example/settings/memory', '/', '/'],
  ]) {
    test(`PWA launch respects explicit route and safety: ${remembered} at ${launch}`, async ({ page }) => {
      const denied = await serveProduct(page);
      await page.addInitScript((path) => {
        Object.defineProperty(navigator, 'userAgent', { get: () => 'iPhone' });
        Object.defineProperty(navigator, 'standalone', { get: () => true });
        localStorage.setItem('avibe.pwa.last-route.v1', path);
      }, remembered);
      await open(page, launch);
      await expect.poll(() => page.evaluate(() => localStorage.getItem('avibe.pwa.last-route.v1'))).toBe(expected);
      expect(new URL(page.url()).pathname).toBe(expected);
      expect(denied).toEqual([]);
    });
  }
  for (const path of ['/admin/settings/memory', '/settings/memory']) {
    test(`retired bookmark ${path} reaches General without feature requests`, async ({ page }) => {
      const denied = await serveProduct(page);
      await open(page, path);
      await expect(page).toHaveURL(/\/settings\/general$/);
      await expect(page.locator(SETTINGS_RAIL)).toBeVisible();
      await expect(page.locator(`${SETTINGS_CONTENT} h1`)).toHaveText('General');
      expect(denied).toEqual([]);
    });
  }
  test('keeps the sidebar at 248 and lets everything beside it grow', async ({ page }) => {
    const denied = await serveProduct(page);

    await page.setViewportSize(DESKTOP);
    await open(page, '/');
    await expect(page.locator(SIDEBAR)).toBeVisible();

    expect(await widthOf(page, SIDEBAR)).toBe(248);
    const atStaging = await widthOf(page, SHELL_CONTENT);

    await page.setViewportSize(WIDE);
    await page.waitForFunction(() => window.innerWidth === 1600);

    // The sidebar is the fixed part; the column beside it is not. It has to take
    // the full extra 400, because a cap of any size would swallow some of it.
    expect(await widthOf(page, SIDEBAR)).toBe(248);
    const atWide = await widthOf(page, SHELL_CONTENT);
    expect(atWide - atStaging).toBe(WIDE.width - DESKTOP.width);

    // eslint-disable-next-line no-console
    console.log(`home content: ${atStaging} @1200, ${atWide} @1600`);
    expect(denied).toEqual([]);
  });

  test('drops the sidebar on a phone and still fills the width', async ({ page }) => {
    await serveProduct(page);
    await page.setViewportSize(NARROW);
    await open(page, '/');

    await expect(page.locator(SIDEBAR)).toBeHidden();
    expect(await widthOf(page, SHELL_CONTENT)).toBe(NARROW.width);
  });

  /**
   * The phone home centers one group whose last block is the composer
   * (ui/design: home/home/z-layout-spec-phone): nothing is pinned above the tab
   * bar, so a screen too short for the group is reached by scrolling. That makes
   * the defect a primary button you cannot press once you get to it — the end
   * of the page left sitting under the fixed bar — rather than a cropped
   * screenshot. Checked on a normal phone, where it fits without scrolling, and
   * on a short one, where it does not. Both languages, because the controls sit
   * beside translated text whose width moves.
   */
  const PHONE_COMPOSER: { lang: 'en' | 'zh'; placeholder: string; send: string }[] = [
    { lang: 'en', placeholder: 'Message Vibey…', send: 'Send' },
    { lang: 'zh', placeholder: '和歪比说…', send: '发送' },
  ];

  for (const { lang, placeholder, send } of PHONE_COMPOSER) {
    test(`keeps the composer's controls off the tab bar on a phone (${lang})`, async ({ page }, info) => {
      const pageErrors: string[] = [];
      const unknown: string[] = [];
      const reads: string[] = [];
      page.on('pageerror', (error) => pageErrors.push(error.message));
      const denied = await serveProduct(page, lang);
      // Only the inherited, explicitly answered Home reads may fall through.
      // Record failures too; a generic empty API response is not guard proof.
      const inheritedReads = new Set([
        '/api/session', '/api/config', '/api/csrf-token', '/api/projects',
        '/api/workbench/projects-bootstrap', '/api/sessions', '/api/agents',
        '/api/inbox', '/api/notifications/preferences', '/api/version', '/api/events',
        // The sidebar's Bots strip reads the Owner's Bots and each Bot Session's
        // running state (docs/plans/bots.md D5); the fixture's one Bot is the main Bot.
        '/api/bots', '/api/sessions/ses_main',
        // The Owner's home reads the main Bot's conversation, created on first use.
        '/api/sessions/main',
        // The home's Getting started and tip, answered by the shared harness.
        '/api/home/preferences', '/api/remote-access/status', '/api/web-push/status',
      ]);
      await page.route('**/api/**', (route) => {
        const request = route.request();
        const url = new URL(request.url());
        if (request.method() !== 'GET' || url.origin !== ORIGIN) {
          denied.push(`${request.method()} ${request.url()}`);
          return route.abort();
        }
        reads.push(url.pathname);
        if (url.pathname === '/api/asr/status') return route.fulfill({ json: { available: false } });
        if (url.pathname === '/api/dock') return route.fulfill({ json: {
          dock: { order: ['files', 'terminal', 'editor', 'library'], pins: [] },
        } });
        if (url.pathname === '/api/macos/login-session') return route.fulfill({ json: { ok: true, state: 'healthy' } });
        if (inheritedReads.has(url.pathname)) return route.fallback();
        unknown.push(`${request.method()} ${request.url()}`);
        return route.abort();
      });
      try {
        await page.setViewportSize(NARROW);
        await open(page, '/', { lang });

        const composer = page.getByPlaceholder(placeholder);
        const sendButton = page.getByRole('button', { name: send });
        const nav = page.locator(MOBILE_NAV);
        await expect(composer).toBeVisible();
        await expect(nav).toBeVisible();

        // The normal-height phone may fit without scrolling. Clearance and
        // actual hit targets are the invariant, not overflow at this height.
        expect(await page.locator(SHELL_SCROLL).evaluate((node) => node.scrollTop)).toBe(0);
        const navTop = (await nav.boundingBox())!.y;
        expect(await bottomOf(sendButton)).toBeLessThanOrEqual(navTop);
        expect(await topmostOver(page, sendButton)).toBe(send);

        const draft = '给手机上的我写一句话';
        await composer.click();
        await composer.fill(draft);
        await expect(composer).toBeFocused();
        await expect(composer).toHaveValue(draft);
        expect(await page.locator(SHELL_SCROLL).evaluate((node) => node.scrollTop)).toBe(0);
        expect(await topmostOver(page, sendButton)).toBe(send);
        await sendButton.click({ trial: true });

        // Continue the typed draft on the existing short phone. Here overflow
        // must be real, and it must be vertical only: the group wraps, so the
        // page itself never goes sideways.
        await page.setViewportSize(NARROW_SHORT);
        await page.waitForFunction((height) => window.innerHeight === height, NARROW_SHORT.height);
        const overflow = await page.locator(SHELL_SCROLL).evaluate((node) => ({
          top: node.scrollTop,
          hidden: node.scrollHeight - node.clientHeight,
          sideways: node.scrollWidth - node.clientWidth,
        }));
        expect(overflow.top).toBe(0);
        expect(overflow.hidden).toBeGreaterThan(0);
        expect(overflow.sideways).toBe(0);
        await expect(composer).toHaveValue(draft);
        await expect(composer).toBeFocused();

        // Scroll to the end the way a reader would. What the scroll arrives at
        // has to be usable: the shell's own clearance keeps the last row and
        // the primary button off the bar instead of parking them underneath it.
        await page.locator(SHELL_SCROLL).evaluate((node) => { node.scrollTop = node.scrollHeight; });
        await expect(composer).toHaveValue(draft);
        expect(await topmostOver(page, sendButton)).toBe(send);
        expect(await bottomOf(sendButton)).toBeLessThanOrEqual((await nav.boundingBox())!.y);
        await sendButton.click({ trial: true });
      } finally {
        const evidence = info.outputPath('phone-traffic-and-page-errors.json');
        const { writeFile } = await import('node:fs/promises');
        await writeFile(evidence, JSON.stringify({ denied, unknown, pageErrors, reads }, null, 2));
        await info.attach('phone-traffic-and-page-errors', { path: evidence, contentType: 'application/json' });
      }
      expect(denied).toEqual([]);
      expect(unknown).toEqual([]);
      expect(pageErrors).toEqual([]);
    });
  }
});

/**
 * The phone's Apps drawer ends at the tab bar's top edge, and the main Bot's circle in
 * the middle of the bar rises above that edge by `--mobile-nav-center-rise`. With the
 * drawer open, the circle has to be what a finger lands on at its top, not the drawer
 * cutting it off, and the drawer's last row has to sit that rise plus 16px above the
 * bar, clear of the circle. Checked under the shipped rise and under another one: a bar
 * or a drawer that kept its own number instead of reading the shared one fails there.
 */
test.describe('phone tab bar under the Apps drawer', () => {
  test('draws the main Bot above the open drawer and keeps the drawer clear of it', async ({ page }) => {
    const denied = await serveProduct(page);
    await page.route('**/api/asr/status', (route) => route.fulfill({ json: { available: false } }));
    await page.route('**/api/dock', (route) => route.fulfill({ json: {
      dock: { order: ['files', 'terminal', 'editor', 'library'], pins: [] },
    } }));
    await page.setViewportSize(NARROW);
    await open(page, '/');

    const nav = page.locator(MOBILE_NAV);
    const center = nav.getByRole('button', { name: /^Vibey: / });
    await expect(center).toBeVisible();
    await nav.getByRole('button', { name: 'Apps', exact: true }).click();
    const drawer = page.getByRole('dialog', { name: 'Apps' });
    const sheet = drawer.locator('.motion-sheet');
    await expect(sheet).toBeVisible();
    await sheet.evaluate((node) => Promise.all(node.getAnimations().map((animation) => animation.finished)));

    const measure = () => page.evaluate((barSelector) => {
      const bar = document.querySelector(barSelector)!;
      const button = bar.querySelector('button[aria-label^="Vibey: "]')!;
      const circle = button.firstElementChild!.getBoundingClientRect();
      const drawer = document.querySelector('[role="dialog"][aria-label="Apps"] .motion-sheet')!;
      const x = circle.left + circle.width / 2;
      return {
        rise: Number.parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--mobile-nav-center-rise')),
        barTop: bar.getBoundingClientRect().top,
        drawerBottom: drawer.parentElement!.getBoundingClientRect().bottom,
        circleTop: circle.top,
        lastRowBottom: drawer.lastElementChild!.getBoundingClientRect().bottom,
        // What a tap 2px inside the circle's top edge lands on.
        topmost: document.elementFromPoint(x, circle.top + 2)?.closest('button') === button,
      };
    }, MOBILE_NAV);

    for (const rise of [null, '24px']) {
      if (rise) {
        await page.evaluate((value) => document.documentElement.style.setProperty('--mobile-nav-center-rise', value), rise);
      }
      const geometry = await measure();
      if (rise) expect(geometry.rise).toBe(Number.parseFloat(rise));
      expect(Math.abs(geometry.drawerBottom - geometry.barTop), 'the drawer ends at the bar').toBeLessThanOrEqual(0.5);
      expect(Math.abs(geometry.barTop - geometry.circleTop - geometry.rise), 'the circle rises by the shared rise').toBeLessThanOrEqual(0.5);
      expect(geometry.topmost, 'the circle draws above the drawer').toBe(true);
      expect(Math.abs(geometry.circleTop - geometry.lastRowBottom - 16), 'the last row keeps 16px above the circle').toBeLessThanOrEqual(0.5);
    }
    await page.evaluate(() => document.documentElement.style.removeProperty('--mobile-nav-center-rise'));

    // A tap on the part of the circle the drawer used to cover opens the main Bot's
    // conversation, and leaving the page closes the drawer, as a tap anywhere on it did.
    const { circleTop } = await measure();
    const box = (await center.boundingBox())!;
    await page.mouse.click(box.x + box.width / 2, circleTop + 4);
    await expect(page).toHaveURL(/\/chat\/ses_main$/);
    await expect(drawer).toHaveCount(0);
    expect(denied).toEqual([]);
  });

  // The bar is raised only above the drawer, not above what opens over it: a member's
  // center ＋ opens the new-session sheet with the drawer still up, and that sheet has to
  // cover the bar, or the bar would sit on the sheet's own controls.
  test('lets a sheet opened over the drawer cover the bar', async ({ page }) => {
    const denied = await serveProduct(page);
    await page.route('**/api/session', (route) => route.fulfill({ json: {
      ...MODEL_HUB_SESSION,
      remote: true,
      email: 'member@example.com',
      instance_role: 'member',
      capabilities: { ...MODEL_HUB_SESSION.capabilities, is_instance_owner: false, can_manage_instance: false },
    } }));
    await page.setViewportSize(NARROW);
    await open(page, '/inbox');

    const nav = page.locator(MOBILE_NAV);
    await nav.getByRole('button', { name: 'Apps', exact: true }).click();
    await expect(page.getByRole('dialog', { name: 'Apps' }).locator('.motion-sheet')).toBeVisible();
    await nav.getByRole('button', { name: 'New', exact: true }).click();
    const sheet = page.getByRole('dialog', { name: 'New session' });
    await expect(sheet).toBeVisible();
    await sheet.evaluate((node) => Promise.all(node.getAnimations().map((animation) => animation.finished)));
    // Still mounted under the sheet (the sheet hides the page from assistive tech, so by selector).
    await expect(page.locator('[role="dialog"][aria-label="Apps"] .motion-sheet')).toHaveCount(1);

    const barBox = (await nav.boundingBox())!;
    // What is painted on top there. The open sheet turns pointer events off for the page
    // behind it, which would hide the bar from a hit test whatever its layer, so the probe
    // makes the bar hit-testable again for the moment it asks.
    const covered = await page.evaluate(([x, y]) => {
      const bar = document.querySelector('nav.fixed.bottom-0') as HTMLElement;
      bar.style.pointerEvents = 'auto';
      const node = document.elementFromPoint(x, y);
      bar.style.pointerEvents = '';
      return { inBar: Boolean(node?.closest('nav')), inSheet: Boolean(node?.closest('[role="dialog"]:not([aria-label="Apps"])')) };
    }, [barBox.x + 24, barBox.y + barBox.height / 2]);
    expect(covered).toEqual({ inBar: false, inSheet: true });
    expect(denied).toEqual([]);
  });
});

/**
 * A phone that draws the page edge to edge (the phone app, a Home Screen web app) reports its
 * status bar and home indicator through `env(safe-area-inset-*)`; a Safari tab with its toolbar
 * shown reports none at the bottom. The home indicator takes the place of the tab bar's own bottom
 * padding instead of adding to it, so the labels end where a native tab bar's do, 4px above the
 * inset; without an inset nothing moves. What clears the bar follows it, the main Bot keeps its
 * rise, the brand and chat headers are 44px below the top inset, and the chat composer rests on the
 * inset rather than 16px above it. Measured with the browser reporting the owner's iPhone insets,
 * so a bar, a header, or a composer that went back to adding the inset fails here.
 */
test.describe('phone chrome on the safe area', () => {
  const CHAT = 'ses_geometry';
  const turnState = {
    in_flight: false, foreground: 'idle', native_turn_started: false, pending_input_count: 0,
    background_activities: [], pending_activity_output_count: 0, connection: 'connected',
  };
  const session = {
    id: CHAT, scope_id: null, project_id: null, title: 'Phone chrome', agent_id: 'agt-vibey', agent_name: 'vibey',
    agent_backend: 'vibey', agent_variant: 'vibey', model: 'gpt-6-astra', reasoning_effort: 'high', speed: null,
    status: 'active', agent_status: 'idle', pinned: false, visibility: 'foreground', workdir: '/fixture',
    native_session_id: null, metadata: {}, created_at: '2026-11-08T00:00:00Z', updated_at: '2026-11-08T00:00:00Z',
  };

  for (const inset of [{ top: 0, bottom: 0 }, { top: 62, bottom: 34 }]) {
    test(`keeps the bar, the headers and the composer at native spacing (insets ${inset.top}/${inset.bottom})`, async ({ page }) => {
      const denied = await serveProduct(page);
      await page.route('**/api/asr/status', (route) => route.fulfill({ json: { available: false } }));
      await page.route(`**/api/sessions/${CHAT}/bootstrap`, (route) => route.fulfill({ json: {
        session, capabilities: { can_chat: true }, agents: [], default_agent_name: 'vibey', config: { ui: {} },
        messages: [], queued: [], next_after_id: null, next_before_id: null,
        draft: { text: '', updated_at: null }, turn_state: turnState,
      } }));
      await page.route(`**/api/sessions/${CHAT}`, (route) => route.fulfill({ json: session }));
      await page.route(`**/api/sessions/${CHAT}/turn-state`, (route) => route.fulfill({ json: turnState }));
      await page.route(`**/api/sessions/${CHAT}/activity**`, (route) => route.fulfill({ json: { groups: [] } }));
      const cdp = await page.context().newCDPSession(page);
      await cdp.send('Emulation.setSafeAreaInsetsOverride', { insets: { ...inset, left: 0, right: 0 } });
      await page.setViewportSize(NARROW);
      await open(page, '/');

      const nav = page.locator(MOBILE_NAV);
      await expect(nav.getByRole('button', { name: /^Vibey: / })).toBeVisible();
      const home = await page.evaluate((barSelector) => {
        const height = window.innerHeight;
        const bar = document.querySelector(barSelector)!;
        const center = bar.querySelector('button[aria-label^="Vibey: "]')!;
        const tabs = [...bar.querySelectorAll(':scope > div > :is(a, button)')].filter((tab) => tab !== center);
        return {
          header: document.querySelector('header')!.getBoundingClientRect().height,
          bar: height - bar.getBoundingClientRect().top,
          labels: tabs.map((tab) => height - tab.lastElementChild!.getBoundingClientRect().bottom),
          centerLabel: height - center.lastElementChild!.getBoundingClientRect().bottom,
          rise: bar.getBoundingClientRect().top - center.firstElementChild!.getBoundingClientRect().top,
          sharedRise: Number.parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--mobile-nav-center-rise')),
          clearance: Number.parseFloat(getComputedStyle(document.querySelector('main#app-shell-scroll')!).paddingBottom),
        };
      }, MOBILE_NAV);
      const label = Math.max(16, inset.bottom + 4);
      expect(home.header - inset.top, 'the brand header is 44px below the top inset').toBeCloseTo(44, 1);
      expect(home.labels.length).toBe(4);
      for (const gap of home.labels) expect(gap, 'a tab label ends at native spacing').toBeCloseTo(label, 1);
      expect(home.centerLabel, "the main Bot's name stays level with the labels").toBeCloseTo(label, 1);
      expect(home.rise, 'the main Bot keeps its rise').toBeCloseTo(home.sharedRise, 1);
      expect(home.clearance, 'the scroll area clears the bar it draws').toBeCloseTo(home.bar + 8, 1);
      if (!inset.bottom) expect(home.bar, 'without an inset the bar is as tall as before').toBeCloseTo(80, 1);

      await open(page, `/chat/${CHAT}`);
      const textbox = page.locator('main').getByRole('textbox').last();
      await expect(textbox).toBeVisible();
      const chat = await page.evaluate(() => {
        const compose = [...document.querySelectorAll<HTMLElement>('div[style*="linear-gradient"]')]
          .find((node) => node.querySelector('[role="textbox"], textarea'))!;
        return {
          header: document.querySelector('[data-chat-header]')!.getBoundingClientRect().height,
          composer: window.innerHeight - compose.firstElementChild!.getBoundingClientRect().bottom,
        };
      });
      expect(chat.header - inset.top, 'the chat header is as tall as the brand header').toBeCloseTo(44, 1);
      expect(chat.composer, 'the composer rests on the inset, or 16px up without one').toBeCloseTo(Math.max(16, inset.bottom), 1);
      expect(denied).toEqual([]);
    });
  }
});

/**
 * The phone app draws the page under the status bar, so a surface that starts at the window's top
 * on a phone clears it through `env(safe-area-inset-top)`: the setup header, the image viewer's
 * controls, the configuration-repair notice and Model Hub's default-routing drawer. Desktop, a
 * Safari tab and a Home Screen web app (no `viewport-fit=cover`) report no top inset, and there
 * every one of them stays where it was. Measured with the browser reporting each inset.
 */
test.describe('phone surfaces under the status bar', () => {
  const CHAT = 'ses_top_inset';
  const turnState = {
    in_flight: false, foreground: 'idle', native_turn_started: false, pending_input_count: 0,
    background_activities: [], pending_activity_output_count: 0, connection: 'connected',
  };
  const session = {
    id: CHAT, scope_id: null, project_id: null, title: 'Top inset', agent_id: 'agt-vibey', agent_name: 'vibey',
    agent_backend: 'vibey', agent_variant: 'vibey', model: null, reasoning_effort: null, speed: null,
    status: 'active', agent_status: 'idle', pinned: false, visibility: 'foreground', workdir: '/fixture',
    native_session_id: null, metadata: {}, created_at: '2026-11-08T00:00:00Z', updated_at: '2026-11-08T00:00:00Z',
  };
  const imageMessage = {
    id: 'msg-image', scope_id: null, session_id: CHAT, platform: 'avibe', author: 'user', type: 'user',
    source: 'user', author_id: null, author_name: null, native_message_id: null, parent_native_message_id: null,
    projection: null, text: 'image', metadata: {},
    content: { attachments: [{ url: '/api/media/med_top', name: 'top.svg', mime: 'image/svg+xml', width: 64, height: 48 }] },
    created_at: '2026-11-08T00:00:00Z', updated_at: '2026-11-08T00:00:00Z', delivered_at: '2026-11-08T00:00:01Z', read_at: null,
  };
  const image = '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="48"><rect width="64" height="48" fill="#0891b2"/></svg>';

  for (const top of [0, 62]) {
    const phone = async (page: import('@playwright/test').Page) => {
      const cdp = await page.context().newCDPSession(page);
      await cdp.send('Emulation.setSafeAreaInsetsOverride', { insets: { top, bottom: 0, left: 0, right: 0 } });
      await page.setViewportSize(NARROW);
    };

    test(`keeps the setup header's brand below the status bar (top inset ${top})`, async ({ page }) => {
      const denied = await serveProduct(page);
      await phone(page);
      await open(page, '/setup');
      const header = page.locator('.onboarding-shell > header');
      await expect(header.locator('.onboarding-brand')).toBeVisible();
      const geometry = await header.evaluate((node) => {
        const box = node.getBoundingClientRect();
        const brand = node.querySelector('.onboarding-brand')!.getBoundingClientRect();
        return { top: box.top, height: box.height, brandCenter: brand.top + brand.height / 2 };
      });
      expect(geometry.top, 'the header still starts at the top').toBeCloseTo(0, 1);
      expect(geometry.height - top, 'the header keeps its 82px below the inset').toBeCloseTo(82, 1);
      expect(geometry.brandCenter - top, 'the brand stays centred in those 82px').toBeCloseTo(41, 1);
      expect(denied).toEqual([]);
    });

    test(`keeps the configuration-repair notice below the status bar (top inset ${top})`, async ({ page }) => {
      const denied = await serveProduct(page);
      await page.route('**/api/config', (route) => route.fulfill({
        json: { ...config('en'), config_recovery: { required: true, warnings: [] } },
      }));
      await phone(page);
      await open(page, '/');
      const notice = page.getByRole('alert').filter({ hasText: 'Configuration repair required' });
      await expect(notice).toBeVisible();
      const noticeTop = await notice.evaluate((node) => node.getBoundingClientRect().top);
      expect(noticeTop - top, 'the notice keeps its 8px below the inset').toBeCloseTo(8, 1);
      expect(denied).toEqual([]);
    });

    test(`keeps the image viewer's controls below the status bar (top inset ${top})`, async ({ page }) => {
      const denied = await serveProduct(page);
      await page.route('**/api/asr/status', (route) => route.fulfill({ json: { available: false } }));
      await page.route(`**/api/sessions/${CHAT}/bootstrap`, (route) => route.fulfill({ json: {
        session, capabilities: { can_chat: true }, agents: [], default_agent_name: 'vibey', config: { ui: {} },
        messages: [imageMessage], queued: [], next_after_id: null, next_before_id: null,
        draft: { text: '', updated_at: null }, turn_state: turnState,
      } }));
      await page.route(`**/api/sessions/${CHAT}`, (route) => route.fulfill({ json: session }));
      await page.route(`**/api/sessions/${CHAT}/turn-state`, (route) => route.fulfill({ json: turnState }));
      await page.route(`**/api/sessions/${CHAT}/activity**`, (route) => route.fulfill({ json: { groups: [] } }));
      await page.route('**/api/media/med_top', (route) => route.fulfill({ status: 200, contentType: 'image/svg+xml', body: image }));
      await phone(page);
      await open(page, `/chat/${CHAT}`);
      const inlineImage = page.locator('span img[src="/api/media/med_top"]');
      await expect(inlineImage).toHaveJSProperty('naturalWidth', 64);
      await inlineImage.click({ position: { x: 8, y: 8 } });
      const viewer = page.locator('[role="dialog"]:has(img[src^="/api/media/"])');
      const close = viewer.getByRole('button', { name: 'Close' });
      await expect(close).toBeVisible();
      const controlsTop = await close.evaluate((node) => node.parentElement!.getBoundingClientRect().top);
      expect(controlsTop - top, 'the controls keep their 16px below the inset').toBeCloseTo(16, 1);
      expect(denied).toEqual([]);
    });

    test(`keeps the default-routing drawer's head below the status bar (top inset ${top})`, async ({ page }) => {
      const { denied, unexpectedModelReads } = await serveModelHub(page);
      // The drawer reads the agent's current order when it opens.
      await page.route('**/api/models/agents/codex/sources', (route) => {
        expect(route.request().method()).toBe('GET');
        return route.fulfill({ json: { agent: MODEL_HUB_AGENT } });
      });
      await phone(page);
      await open(page, '/settings/models');
      const card = page.locator('[data-agent-backend="codex"]');
      await expect(card).toBeVisible();
      await card.getByRole('button', { name: 'Default routing' }).click();
      const drawer = page.locator('.model-hub-order-drawer');
      await expect(drawer).toBeVisible();
      const geometry = await drawer.evaluate((node) => ({
        top: node.getBoundingClientRect().top,
        head: node.querySelector('.model-hub-order-head')!.getBoundingClientRect().top,
      }));
      expect(geometry.top, 'the drawer still runs from the top').toBeCloseTo(0, 1);
      expect(geometry.head, 'its head starts below the inset').toBeCloseTo(top, 1);
      expect(denied).toEqual([]);
      expect(unexpectedModelReads).toEqual([]);
    });
  }
});

test.describe('settings overlay geometry', () => {
  test('opens as a standalone zero-offset surface and gives the home back its draft on close', async ({ page }) => {
    const denied = await serveProduct(page);

    await page.setViewportSize(DESKTOP);
    await open(page, '/');

    // A draft is what the overlay exists to preserve, so the assertion starts by
    // writing one. Non-ASCII on purpose.
    const draft = '给中文项目写一份说明';
    const composer = page.getByPlaceholder('Message Vibey…');
    await composer.fill(draft);
    await expect(composer).toHaveValue(draft);

    await page.locator('aside [data-settings-toggle="true"]').click();
    await expect(page).toHaveURL(/\/settings\/general$/);

    const overlay = page.locator('[data-settings-overlay="true"]');
    await expect(overlay).toBeVisible();

    // Standalone Settings owns the viewport at every sidebar width. The origin
    // remains mounted behind the surface for draft/session/selection retention,
    // but no Workbench offset is allowed to leak into the foreground.
    const surface = await overlay.boundingBox();
    expect(surface!.x).toBe(0);
    expect(surface!.width).toBe(DESKTOP.width);
    await expect(page.locator(SIDEBAR)).toBeHidden();

    await page.getByRole('button', { name: 'Close Settings' }).click();
    await expect(page).toHaveURL(`${ORIGIN}/`);
    await expect(overlay).toHaveCount(0);
    await expect(page.getByPlaceholder('Message Vibey…')).toHaveValue(draft);

    expect(denied).toEqual([]);
  });
});

test.describe('page background family', () => {
  test('draws the home flat and leaves the console wash on its siblings', async ({ page }) => {
    await serveProduct(page);
    await page.setViewportSize(DESKTOP);

    // Board 04 stages the home on flat $--background.
    await open(page, '/');
    await expect(page.locator(SIDEBAR)).toBeVisible();
    expect(await page.locator(SHELL_SCROLL).evaluate((n) => getComputedStyle(n).backgroundImage)).toBe('none');

    // …and the rest of the console family keeps the aurora it already had, which
    // is the half of this decision a capture of the home alone cannot show.
    await open(page, '/agents');
    expect(await page.locator(SHELL_SCROLL).evaluate((n) => getComputedStyle(n).backgroundImage))
      .toContain('radial-gradient');
  });
});

test.describe('general settings geometry', () => {
  test('keeps the standalone rail on the sidebar width and the common content at 880px', async ({ page }) => {
    const denied = await serveProduct(page);

    await page.setViewportSize(DESKTOP);
    await open(page, '/settings/general');
    await expect(page.locator(SETTINGS_RAIL)).toBeVisible();

    expect(await widthOf(page, SETTINGS_RAIL)).toBe(248);
    const cardAtStaging = await widthOf(page, APPEARANCE_CARD);

    // The desktop Settings frame is 944px wide with 32px horizontal padding,
    // leaving an 880px common content column.
    await page.setViewportSize(ULTRA);
    await page.waitForFunction(() => window.innerWidth === 1920);

    expect(await widthOf(page, SETTINGS_RAIL)).toBe(248);
    const cardAtUltra = await widthOf(page, APPEARANCE_CARD);
    expect(cardAtStaging).toBe(880);
    expect(cardAtUltra).toBe(880);
    expect(denied).toEqual([]);
  });

  test('gives the landing its own heading block and leaves the sections alone', async ({ page }) => {
    await serveProduct(page);
    await page.setViewportSize(DESKTOP);

    const headingOf = async (path: string) => {
      await open(page, path);
      const heading = page.locator(`${SETTINGS_PAGE} h1`).first();
      await expect(heading).toBeVisible();
      return heading.evaluate((node) => {
        const style = getComputedStyle(node);
        return { size: style.fontSize, weight: style.fontWeight };
      });
    };

    // Ozmrf draws the landing at 27/600; every section keeps the 28/700 it
    // already shipped with, which is why the variant is opt-in.
    expect(await headingOf('/settings/general')).toEqual({ size: '27px', weight: '600' });
    expect(await headingOf('/settings/shortcuts')).toEqual({ size: '28px', weight: '700' });
  });

  test('uses the same standalone frame for a direct URL at a real desktop width', async ({ page }) => {
    await serveProduct(page);
    await page.setViewportSize({ width: 1448, height: 900 });
    await open(page, '/settings/general');
    await expect(page.locator(SETTINGS_RAIL)).toBeVisible();

    // dqfES — the source's standalone Settings content frame.
    expect(await widthOf(page, SETTINGS_CONTENT)).toBe(944);
    await expect(page.locator(SIDEBAR)).toBeHidden();
  });

  // A rail label is the only thing that says where a row goes, and English has
  // two neighbours that both ended in "Platform…" when the rail was narrower.
  // The rail width is the sidebar's, so the labels wrap rather than widen it.
  test('shows every rail label in full without widening the rail', async ({ page }) => {
    await serveProduct(page);
    await page.setViewportSize(DESKTOP);
    await open(page, '/settings/platforms');
    await expect(page.locator(SETTINGS_RAIL)).toBeVisible();

    expect(await widthOf(page, SETTINGS_RAIL)).toBe(248);

    // Measured, not eyeballed: a wrapped label's scrollWidth equals its
    // clientWidth; a clipped one exceeds it.
    const clipped = await page
      .locator(`${SETTINGS_RAIL} a[href^="/settings"] span, ${SETTINGS_RAIL} button[aria-expanded] span`)
      .evaluateAll((nodes) => nodes
        .filter((node) => node.getClientRects().length > 0 && node.scrollWidth > node.clientWidth)
        .map((node) => node.textContent?.trim() ?? ''));
    expect(clipped).toEqual([]);

    const rail = page.locator(SETTINGS_RAIL);
    await expect(rail.getByRole('button', { name: 'Messaging Platforms' })).toBeVisible();
    await expect(rail.getByRole('link', { name: 'Platform Connections' })).toBeVisible();

    // Two lines at this size still fit the row height the rail already had, so
    // the rows that never wrapped do not move.
    const rows = await rail.locator('a[href^="/settings"]').evaluateAll((nodes) =>
      nodes.map((node) => Math.round(node.getBoundingClientRect().height)));
    expect(new Set(rows).size).toBe(1);
  });

  test('keeps the common 880px content width on every ordinary settings page', async ({ page }) => {
    await serveProduct(page);
    await page.setViewportSize(ULTRA);
    await open(page, '/settings/shortcuts');
    await expect(page.locator(SETTINGS_RAIL)).toBeVisible();

    expect(await widthOf(page, SETTINGS_CONTENT)).toBe(944);
  });

  for (const viewport of [
    { width: 1366, height: 768 },
    { width: 1920, height: 1000 },
    { width: 390, height: 844 },
  ]) {
    test(`keeps Model Hub fluid and ordinary pages constrained at ${viewport.width}px`, async ({ page }) => {
      const { denied, unexpectedModelReads } = await serveModelHub(page);
      const pageErrors: string[] = [];
      page.on('pageerror', (error) => pageErrors.push(error.message));
      await page.setViewportSize(viewport);

      const modelHub = async (path: string) => {
        await open(page, path);
        const shell = page.locator('.model-hub-shell');
        await expect(shell).toBeVisible();
        await settleSettingsFrame(page);
        await expect(page.locator('[data-agent-backend="codex"]')).toBeVisible();
        const pane = page.locator(SETTINGS_PAGE);
        const frame = page.locator(SETTINGS_CONTENT);
        const [paneBox, frameBox] = await Promise.all([pane.boundingBox(), frame.boundingBox()]);
        expect(paneBox).not.toBeNull();
        expect(frameBox).not.toBeNull();
        expect(frameBox!.width).toBe(paneBox!.width);
        expect(frameBox!.x).toBe(paneBox!.x);
        expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(viewport.width);
      };

      await modelHub('/settings/models');
      await modelHub('/settings/models/');

      // A retained origin reaches the same real Model Hub route through the
      // supported desktop Settings toggle or the supported mobile Apps drawer,
      // proving the fluid exception survives overlay entry at every width.
      await open(page, '/');
      if (viewport.width >= 768) {
        await page.locator('aside [data-settings-toggle="true"]').click();
      } else {
        await page.getByRole('button', { name: 'Apps', exact: true }).click();
        await page.getByRole('dialog', { name: 'Apps' }).getByRole('link', { name: 'Settings', exact: true }).click();
        await expect(page.getByRole('navigation', { name: 'Settings sections' })).toBeVisible();
      }
      await expect(page.locator('[data-settings-overlay="true"]')).toBeVisible();
      await page.getByRole('navigation', { name: 'Settings sections' })
        .getByRole('link', { name: 'Models', exact: true }).click();
      await expect(page).toHaveURL(/\/settings\/models$/);
      await expect(page.locator('.model-hub-shell')).toBeVisible();
      await settleSettingsFrame(page);
      const retainedPane = await page.locator(SETTINGS_PAGE).boundingBox();
      const retainedFrame = await page.locator(SETTINGS_CONTENT).boundingBox();
      expect(retainedFrame?.width).toBe(retainedPane?.width);
      expect(retainedFrame?.x).toBe(retainedPane?.x);

      // General and Shortcuts remain ordinary pages with the shared 944px
      // outer frame and 880px content column whenever the viewport provides
      // enough room. Measure the actual bounds because auto margins resolve to
      // pixels in computed style.
      for (const path of ['/settings/general', '/settings/shortcuts']) {
        await open(page, path);
        await settleSettingsFrame(page);
        const frame = await frameGeometry(page);
        expect(frame.width).toBe(viewport.width >= 944 + 248 ? 944 : viewport.width);
        expect(frame.paddingLeft).toBe(viewport.width >= 944 + 248 ? 32 : 16);
        expect(frame.paddingRight).toBe(frame.paddingLeft);
        expect(frame.contentWidth).toBe(viewport.width >= 944 + 248 ? 880 : viewport.width - 32);
        const pane = await page.locator(SETTINGS_PAGE).boundingBox();
        expect(pane).not.toBeNull();
        if (viewport.width >= 944 + 248) {
          expect(Math.abs(frame.x - pane!.x - (pane!.width - frame.width) / 2)).toBeLessThanOrEqual(0.5);
          await expect(page.locator(SETTINGS_RAIL)).toBeVisible();
        } else {
          expect(frame.x).toBeGreaterThanOrEqual(pane!.x);
          expect(frame.x + frame.width).toBeLessThanOrEqual(pane!.x + pane!.width);
        }
        if (path.endsWith('general')) {
          await expect(page.getByRole('radiogroup', { name: 'Appearance' })).toBeVisible();
        } else {
          await expect(page.getByRole('button', { name: /Change Chat voice input shortcut/ })).toBeVisible();
        }
        expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(viewport.width);
      }
      expect(denied).toEqual([]);
      expect(unexpectedModelReads).toEqual([]);
      expect(pageErrors).toEqual([]);
    });
  }

  // Class-based component tests cannot see a scoped Hub stylesheet overriding
  // a shared token. Compare the actual marks and computed ink/tile in both themes.
  for (const theme of ['light', 'dark'] as const) {
    for (const viewport of [DESKTOP, NARROW]) {
      test(`matches backend identity across Settings and Model Hub in ${theme} at ${viewport.width}px`, async ({ page }) => {
        const { denied, unexpectedModelReads } = await serveModelHub(page);
        const backends = ['vibey', 'claude', 'codex', 'opencode'];
        const labels = ['Vibey', 'Claude Code', 'Codex', 'OpenCode'];
        let direct = false;
        await page.route('**/api/models/agents/*/chains', (route) => {
          expect(route.request().method()).toBe('GET');
          return route.fulfill({ json: { chains: [] } });
        });
        // This mount-time scan is answered entirely by the test, never the host.
        await page.route('**/api/models/migration/scan', (route) => {
          expect(route.request().method()).toBe('POST');
          return route.fulfill({ json: { items: [] } });
        });
        await page.route(/\/api\/models\/agents(?:\?.*)?$/, (route) => {
          expect(route.request().method()).toBe('GET');
          return route.fulfill({ json: { agents: [...backends].reverse()
            .filter((backend) => !direct || backend !== 'vibey')
            .map((backend) => ({
              backend, cli_present: backend !== 'vibey', mode: direct ? 'direct' : 'hub',
              menu_kind: 'fixed', catalog_models: [], named_agents: [],
            })) } });
        });
        const identity = (icon: Locator) => icon.evaluate((svg) => ({
          mark: svg.innerHTML,
          ink: getComputedStyle(svg).color,
          tile: getComputedStyle(svg.parentElement!).backgroundColor,
        }));
        await page.setViewportSize(viewport);
        await open(page, '/settings/backends', { theme });
        const settingsIdentities = [];
        for (const label of labels) {
          const row = page.locator('.rounded-xl').filter({ has: page.getByText(label, { exact: true }) });
          await expect(row).toBeVisible();
          settingsIdentities.push(await identity(row.locator('svg').first()));
        }
        await page.screenshot({ path: `e2e/.artifacts/workbench-general/backends-${theme}-${viewport.width}.png`, fullPage: true });

        await open(page, '/settings/models', { theme });
        const cards = page.locator('[data-agent-backend]');
        await expect(cards.locator('h2')).toHaveText(labels);
        for (let index = 0; index < backends.length; index += 1) {
          const icon = cards.nth(index).locator('.model-hub-agent-head-summary svg').first();
          await expect.poll(() => identity(icon)).toEqual(settingsIdentities[index]);
        }
        await page.screenshot({ path: `e2e/.artifacts/workbench-general/hub-${theme}-${viewport.width}.png`, fullPage: true });

        direct = true;
        await open(page, '/settings/models', { theme });
        const nativeRows = page.locator('.model-hub-direct-row--backend');
        await expect(nativeRows.locator('.model-hub-direct-backend-name')).toHaveText(labels.slice(1));
        for (let index = 0; index < 3; index += 1) {
          await expect.poll(() => identity(nativeRows.nth(index).locator('svg').first()))
            .toEqual(settingsIdentities[index + 1]);
        }
        expect(denied).toEqual([]);
        expect(unexpectedModelReads).toEqual([]);
      });
    }
  }

  test('draws the preference card, the selector and the selected choice to spec', async ({ page }) => {
    await serveProduct(page);
    await page.setViewportSize(DESKTOP);
    await open(page, '/settings/general', { theme: 'dark' });

    const card = page.locator(APPEARANCE_CARD);
    await expect(card).toBeVisible();

    const cardStyle = await card.evaluate((node) => {
      const outer = getComputedStyle(node);
      const inner = getComputedStyle(node.firstElementChild as Element);
      return { radius: outer.borderRadius, pad: inner.padding, gap: inner.rowGap, bg: outer.backgroundColor };
    });
    // le5QU / Q8zxF1: padding 22, radius 12 — not the 16 the older `panel`
    // variant renders, which is why `preference` is its own variant.
    expect(cardStyle.radius).toBe('12px');
    expect(cardStyle.pad).toBe('22px');
    expect(cardStyle.gap).toBe('20px');

    // Every other settings page draws its cards as an outline on the page, and
    // General is not a different kind of page. The source fills this card with
    // surface-2; matching its neighbours is worth more than matching that fill,
    // so the card carries the page background and keeps its border.
    const background = await page.evaluate(() => {
      const probe = document.createElement('div');
      probe.style.backgroundColor = 'var(--background)';
      document.body.appendChild(probe);
      const value = getComputedStyle(probe).backgroundColor;
      probe.remove();
      return value;
    });
    expect(cardStyle.bg).toBe(background);
    // …and "the same as its neighbours" is the claim, so read a neighbour.
    await open(page, '/settings/shortcuts', { theme: 'dark' });
    const neighbour = page.locator(`${SETTINGS_PAGE} section.border`).first();
    await expect(neighbour).toBeVisible();
    expect(await neighbour.evaluate((node) => getComputedStyle(node).backgroundColor)).toBe(background);
    await open(page, '/settings/general', { theme: 'dark' });

    const select = page.getByLabel('Language', { exact: true });
    const selectBox = await select.boundingBox();
    expect(selectBox!.width).toBe(190);
    expect(selectBox!.height).toBe(40);
    expect(await select.evaluate((node) => getComputedStyle(node).borderRadius)).toBe('9px');

    const dark = page.getByRole('radio', { name: 'Dark' });
    await expect(dark).toHaveAttribute('aria-checked', 'true');
    const selectedBorder = await dark.evaluate((node) => {
      const style = getComputedStyle(node);
      return { width: style.borderTopWidth, color: style.borderTopColor };
    });
    expect(selectedBorder.width).toBe('2px');

    const mint = await page.evaluate(() => {
      const probe = document.createElement('div');
      probe.style.color = 'var(--mint)';
      document.body.appendChild(probe);
      const value = getComputedStyle(probe).color;
      probe.remove();
      return value;
    });
    expect(selectedBorder.color).toBe(mint);

    // Picking a card must not nudge the row: the extra border pixel is absorbed
    // by padding, so every card in the group stays the same size.
    const appearance = page.getByRole('radiogroup', { name: 'Appearance' });
    const boxes = await appearance.getByRole('radio').evaluateAll((nodes) =>
      nodes.map((node) => node.getBoundingClientRect().width));
    expect(new Set(boxes.map((w) => Math.round(w))).size).toBe(1);

    // Q8zxF1: a 14 gutter between the three choices, and an 88-tall preview at
    // radius 6 inside each one.
    expect(await appearance.evaluate((n) => getComputedStyle(n).columnGap)).toBe('14px');
    const preview = await dark.locator('[aria-hidden="true"]').first().evaluate((node) => ({
      height: node.getBoundingClientRect().height,
      radius: getComputedStyle(node).borderRadius,
    }));
    expect(preview.height).toBe(88);
    expect(preview.radius).toBe('6px');

    // The menu-placement card is the same kind of control drawn the same way,
    // which is the point of the shared choice-card component.
    const placement = page.getByRole('radiogroup', { name: 'Settings menu' });
    expect(await widthOf(page, PLACEMENT_CARD)).toBe(await widthOf(page, APPEARANCE_CARD));
    const placementPreview = await placement.getByRole('radio').first()
      .locator('[aria-hidden="true"]').first()
      .evaluate((node) => node.getBoundingClientRect().height);
    expect(placementPreview).toBe(preview.height);
  });

  // Inline puts Settings beside a sidebar that stays live, so both columns are
  // on screen at once. That is a layout claim a class name cannot settle.
  test('opens Settings beside a live sidebar once inline is picked', async ({ page }) => {
    const denied = await serveProduct(page);
    await page.setViewportSize(ULTRA);
    await open(page, '/');
    await page.locator(`${SIDEBAR} [data-settings-toggle="true"]`).click();

    const overlay = page.locator('[data-settings-overlay="true"]');
    await expect(overlay).toHaveAttribute('data-settings-menu-placement', 'standalone');
    await expect(page.locator(SIDEBAR)).toBeHidden();
    expect(await overlay.boundingBox()).toMatchObject({ x: 0, width: ULTRA.width });
    const standaloneRail = await widthOf(page, SETTINGS_RAIL);

    await page.getByRole('radio', { name: 'Inline' }).click();
    await expect(overlay).toHaveAttribute('data-settings-menu-placement', 'inline');

    // The app sidebar is back, and Settings starts exactly where it ends.
    const sidebar = await page.locator(SIDEBAR).boundingBox();
    expect(sidebar).toMatchObject({ x: 0, width: standaloneRail });
    expect(await overlay.boundingBox()).toMatchObject({ x: standaloneRail, width: ULTRA.width - standaloneRail });
    // A secondary nav beside the real one does not deserve a second full column.
    expect(await widthOf(page, SETTINGS_RAIL)).toBe(196);

    // Live, not a picture of a sidebar. `toBeEnabled` only reads the element;
    // whether anything is stacked over it is a hit test, and a hit test is the
    // one thing a class name, a bounding box and jsdom all cannot settle. So
    // ask the browser what is actually under the pointer there — a transparent
    // full-viewport layer would answer here and nowhere else.
    const toggle = page.locator(`${SIDEBAR} [data-settings-toggle="true"]`);
    await expect(toggle).toBeEnabled();
    const toggleBox = (await toggle.boundingBox())!;
    expect(await page.evaluate(([x, y]) => {
      const hit = document.elementFromPoint(x, y);
      return {
        sidebar: Boolean(hit?.closest('[data-settings-toggle="true"]')),
        covered: Boolean(hit?.closest('[data-settings-overlay="true"]')),
      };
    }, [toggleBox.x + toggleBox.width / 2, toggleBox.y + toggleBox.height / 2])).toEqual({
      sidebar: true,
      covered: false,
    });

    // The preference outlives the surface that set it.
    await toggle.click();
    await expect(overlay).toHaveCount(0);
    await open(page, '/settings/general');
    await expect(page.locator(SETTINGS_RAIL)).toBeVisible();
    expect(await widthOf(page, SETTINGS_RAIL)).toBe(196);

    await page.getByRole('radio', { name: 'Standalone' }).click();
    await expect(page.locator(SIDEBAR)).toBeHidden();
    expect(await widthOf(page, SETTINGS_RAIL)).toBe(standaloneRail);
    expect(denied).toEqual([]);
  });

  // Keeping that column live means keeping its controls. Apps is one of them,
  // and the one a boundary can quietly take away: it portals its button and Dock
  // to `document.body`, so nothing in the sidebar's markup shows it missing.
  test('keeps the Apps launcher usable beside inline Settings', async ({ page }) => {
    const denied = await serveProduct(page);
    await page.setViewportSize(ULTRA);
    await open(page, '/');
    const apps = page.getByRole('button', { name: 'Apps', exact: true });
    await expect(apps).toBeVisible();

    const overlay = page.locator('[data-settings-overlay="true"]');
    await page.locator(`${SIDEBAR} [data-settings-toggle="true"]`).click();
    // Standalone stands in for the sidebar, so the sidebar's controls go with it.
    await expect(overlay).toHaveAttribute('data-settings-menu-placement', 'standalone');
    await expect(apps).toHaveCount(0);

    await page.getByRole('radio', { name: 'Inline' }).click();
    await expect(overlay).toHaveAttribute('data-settings-menu-placement', 'inline');
    await expect(apps).toBeVisible();

    // Reachable, not merely rendered: it floats at z-40 over a surface at z-30,
    // and which of the two the pointer lands on is a hit test.
    const box = (await apps.boundingBox())!;
    expect(await page.evaluate(([x, y]) => Boolean(
      document.elementFromPoint(x, y)?.closest('button[aria-haspopup="menu"]'),
    ), [box.x + box.width / 2, box.y + box.height / 2])).toBe(true);

    // And it still opens what it is for. The window layer stays hidden for as
    // long as Settings is open, so bringing a window forward leaves Settings
    // first — otherwise the window would arrive behind an opaque surface.
    await apps.click();
    const dock = page.getByRole('menu', { name: 'Apps', exact: true });
    await expect(dock).toBeVisible();
    await dock.getByRole('button', { name: 'Files', exact: true }).click();
    await expect(overlay).toHaveCount(0);
    await expect(page.locator('[data-window-id]').first()).toBeVisible();

    // And the window keeps the focus it just took. The window chords resolve
    // their target from DOM focus, so Settings restoring focus to the toggle it
    // was opened from — its ordinary way out — would leave this window on
    // screen and deaf, with ⌘W falling through to the browser's close-tab.
    // Polled: the restore Settings would attempt is deferred a frame.
    await expect
      .poll(() => page.evaluate(() => Boolean(document.activeElement?.closest('[data-window-id]'))))
      .toBe(true);
    expect(denied).toEqual([]);
  });
});
