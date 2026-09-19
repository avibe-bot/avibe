import { writeFile } from 'node:fs/promises';
import { expect, test, type Locator, type Page, type Route } from '@playwright/test';
import { openOnboarding, openSetup, serveProduct, settleEffects, size } from './support';

async function authFixtures(page: Page) {
  const cancellations: string[] = [];
  await page.route('**/api/csrf-token', (route) => route.fulfill({ json: { csrf_token: 'fixture-token' } }));
  await page.route('**/api/backend/*/auth', (route) => route.fulfill({ json: { ok: true, active_auth_mode: 'none', auth_mode: 'oauth', has_api_key: false, base_url: null } }));
  await page.route('**/api/backend/opencode/providers', (route) => route.fulfill({ json: { ok: true, providers: [
    { id: 'openai', name: 'OpenAI', description: 'ChatGPT subscription or API Key', oauth_available: true, configured: false, local: false, models: [] },
    { id: 'github-copilot', name: 'GitHub Copilot', description: 'GitHub subscription', oauth_available: true, configured: false, local: false, models: [] },
    { id: 'poe', name: 'Poe', description: 'Browser authorization', oauth_available: true, configured: false, local: false, models: [] },
  ] } }));
  await page.route('**/api/backend/*/auth/oauth/start', (route) => route.fulfill({ json: { ok: true, flow_id: 'fixture-flow', state: 'awaiting_code', url: 'https://auth.openai.com/codex/device', device_code: 'ABCD-1234' } }));
  await page.route('**/api/backend/*/auth/oauth/status/*', (route) => route.fulfill({ json: { ok: true, flow_id: 'fixture-flow', state: 'awaiting_code', url: 'https://auth.openai.com/codex/device', device_code: 'ABCD-1234' } }));
  await page.route('**/api/backend/*/auth/oauth/cancel', (route) => { cancellations.push(route.request().postData() || ''); return route.fulfill({ json: { ok: true } }); });
  return cancellations;
}

for (const lang of ['en', 'zh']) for (const theme of ['dark', 'light']) {
  test(`connection family ${lang} ${theme}: native desktop + narrow fields/device/focus`, async ({ page }, info) => {
    const denied = await serveProduct(page);
    const cancellations = await authFixtures(page);
    await page.setViewportSize({ width: 1200, height: 800 });
    await openOnboarding(page, { lang, theme });
    await openSetup(page, lang);
    const subscription = lang === 'zh' ? '添加订阅' : 'Add subscription';
    const key = lang === 'zh' ? '添加 API Key' : 'Add API Key';
    await page.getByLabel('Claude Code', { exact: true }).getByRole('button', { name: subscription }).click();
    const dialog = page.getByRole('dialog');
    await expect(dialog.getByRole('button', { name: lang === 'zh' ? '使用 Claude 登录' : 'Sign in with Claude' })).toBeVisible();
    await settleEffects(page);
    const geometry = await dialog.evaluate((node) => { const rect = node.getBoundingClientRect(); const style = getComputedStyle(node); return { width: rect.width, padding: style.padding, gap: style.gap, radius: style.borderRadius }; });
    expect(geometry).toEqual({ width: 568, padding: '24px', gap: '20px', radius: '16px' });
    await settleEffects(page);
    await dialog.screenshot({ path: info.outputPath(`claude-start-${lang}-${theme}-desktop.png`) });
    await page.keyboard.press('Escape'); await expect(dialog).toHaveCount(0);

    await page.setViewportSize({ width: 390, height: 640 });
    await page.getByLabel('Claude Code', { exact: true }).getByRole('button', { name: key }).click();
    const save = dialog.getByRole('button', { name: lang === 'zh' ? '保存并连接' : 'Save and connect' });
    await expect(save).toBeDisabled();
    await save.scrollIntoViewIfNeeded(); await expect(save).toBeInViewport();
    const overflow = await dialog.evaluate((node) => ({ width: node.getBoundingClientRect().width, extra: node.scrollWidth - node.clientWidth }));
    expect(overflow.width).toBeLessThan(390); expect(overflow.extra).toBeLessThanOrEqual(1);
    for (let index = 0; index < 12; index++) { await page.keyboard.press('Tab'); expect(await dialog.evaluate((node) => node.contains(document.activeElement))).toBe(true); }
    await settleEffects(page);
    await dialog.screenshot({ path: info.outputPath(`claude-key-${lang}-${theme}-narrow.png`) });
    await page.keyboard.press('Escape'); await expect(dialog).toHaveCount(0);

    await page.setViewportSize({ width: 1200, height: 800 });
    await page.getByLabel('Codex', { exact: true }).getByRole('button', { name: subscription }).click();
    await dialog.getByRole('button', { name: lang === 'zh' ? '使用 ChatGPT 登录' : 'Sign in with ChatGPT' }).click();
    await expect(dialog.getByText('ABCD-1234')).toBeVisible();
    await expect(dialog.getByRole('radio').first()).toBeDisabled();
    await settleEffects(page);
    await dialog.screenshot({ path: info.outputPath(`codex-device-${lang}-${theme}-desktop.png`) });
    await page.keyboard.press('Escape'); await expect.poll(() => cancellations.length).toBe(1);

    await page.setViewportSize({ width: 390, height: 640 });
    await page.getByLabel('OpenCode', { exact: true }).getByRole('button', { name: key }).click();
    await dialog.getByRole('button', { name: /OpenAI/ }).click();
    await expect(dialog.getByLabel(lang === 'zh' ? 'Base URL（可选）' : 'Base URL (optional)')).toBeVisible();
    await settleEffects(page);
    await dialog.screenshot({ path: info.outputPath(`opencode-key-${lang}-${theme}-narrow.png`) });
    await page.keyboard.press('Escape');
    expect(denied).toEqual([]);
  });
}

/**
 * The reported defect: switching Subscription/API Key re-measured the dialog and moved
 * its own top edge, because a centred box with content height moves by half of every
 * content change. The fix is a fixed frame whose height belongs to the assistant, so this
 * measures the anchors the contract names — heading, tab bar, dialog bounds, footer —
 * across repeated switches in BOTH directions. Repetition matters: a one-way switch can
 * be stable while the way back is not, and a frame that settles a frame late would pass a
 * single comparison.
 */
const anchorBoxes = (page: Page) => page.evaluate(() => {
  const round = (value: number) => Math.round(value * 10) / 10;
  const read = (selector: string) => {
    const node = document.querySelector(selector);
    if (!node) return null;
    const rect = node.getBoundingClientRect();
    return { x: round(rect.x), y: round(rect.y), width: round(rect.width), height: round(rect.height) };
  };
  return {
    frame: read('.connection-dialog'),
    heading: read('.connection-heading'),
    description: read('.connection-description'),
    // Row three, whichever control an assistant puts there: the method tabs, or — for
    // OpenCode, which has to choose a provider first — the search field and then the
    // chosen-provider capsule. A credential group, when a method has one, lives inside
    // the scrolling middle and is not an anchor.
    controls: read('.connection-dialog .connection-controls, .connection-dialog .backend-connection-form > .connection-radio > [role="radiogroup"]'),
    close: read('.connection-dialog > button:last-of-type'),
    footer: read('.connection-actions'),
  };
});

/** Only the middle may scroll: a scroller inside the frame's other rows — or the frame
 *  itself — would mean it had been asked to hold more than it has room for. */
const scrollers = (page: Page) => page.evaluate(() => [...document.querySelectorAll('.connection-dialog, .connection-dialog *')]
  .filter((node) => node.scrollHeight > node.clientHeight + 1 && ['auto', 'scroll'].includes(getComputedStyle(node).overflowY))
  // The middle carries a second class while it holds the provider list, and it is still
  // the middle — so this asks whether a node IS the body, not what its class attribute
  // happens to spell.
  .filter((node) => !node.classList.contains('connection-body'))
  .map((node) => node.className.toString()));

/** The design's margin. The frame is centred, so each edge owes half of it. */
const CLEARANCE = 16;

/**
 * The room the window actually leaves this frame, read from the browser rather than
 * recomputed from the stylesheet.
 *
 * It is the VISUAL viewport, not `100dvh`: on iOS the layout viewport does not shrink
 * when the soft keyboard opens, so a frame measured against `100dvh` can be "inside the
 * window" and still have its footer under the keyboard. The insets are whatever the
 * device reserves for a notch or a home indicator, which are room the frame does not
 * have either.
 */
const usableViewport = (page: Page) => page.evaluate(() => {
  const probe = document.createElement('div');
  probe.style.cssText = 'position:fixed;visibility:hidden;pointer-events:none;top:0;left:0;'
    + 'width:env(safe-area-inset-left,0px);height:env(safe-area-inset-top,0px);'
    + 'border-right:env(safe-area-inset-right,0px) solid transparent;'
    + 'border-bottom:env(safe-area-inset-bottom,0px) solid transparent';
  document.body.append(probe);
  const style = getComputedStyle(probe);
  const inset = {
    left: parseFloat(style.width) || 0, top: parseFloat(style.height) || 0,
    right: parseFloat(style.borderRightWidth) || 0, bottom: parseFloat(style.borderBottomWidth) || 0,
  };
  probe.remove();
  const view = window.visualViewport;
  const x = view?.offsetLeft ?? 0;
  const y = view?.offsetTop ?? 0;
  return {
    left: x + inset.left, top: y + inset.top,
    right: x + (view?.width ?? window.innerWidth) - inset.right,
    bottom: y + (view?.height ?? window.innerHeight) - inset.bottom,
  };
});

/** The anchors once the dialog's own entrance has finished: Radix animates the frame in
 *  with a transform, so a box read mid-animation is the animation's, not the layout's. */
const settled = async (page: Page) => { await settleEffects(page); return anchorBoxes(page); };

/**
 * The frame's bound, stated as what a person gets rather than as the expression that
 * produces it. Three things, and together they leave the height no freedom: it never
 * grows past what its assistant asked for; it is never pushed against an edge of the
 * room; and when it does have to give height back, it gives back only what the margins
 * need. A frame that shrank for any other reason fails the third, and one that ignored
 * the window fails the second — so neither the assistant's number nor the window's has
 * to be restated here.
 */
async function expectFitsItsRoom(page: Page, assistant: number) {
  await settleEffects(page);
  const room = await usableViewport(page);
  const frame = (await anchorBoxes(page)).frame!;
  const clearance = {
    left: frame.x - room.left, top: frame.y - room.top,
    right: room.right - (frame.x + frame.width), bottom: room.bottom - (frame.y + frame.height),
  };
  expect(frame.height, 'height its assistant asked for').toBeLessThanOrEqual(assistant + 0.5);
  for (const [edge, value] of Object.entries(clearance)) expect(value, `${edge} clearance`).toBeGreaterThanOrEqual(CLEARANCE - 0.5);
  if (frame.height < assistant - 0.5) expect(clearance.top + clearance.bottom, 'height given back').toBeLessThanOrEqual(2 * CLEARANCE + 0.5);
  return frame;
}

for (const viewport of [{ width: 1200, height: 800 }, { width: 390, height: 640 }]) {
  test(`the connection frame holds while its methods change ${viewport.width}x${viewport.height}`, async ({ page }, info) => {
    const denied = await serveProduct(page); await authFixtures(page);
    await page.setViewportSize(viewport);
    await openOnboarding(page); await openSetup(page, 'en');
    await page.getByLabel('Claude Code', { exact: true }).getByRole('button', { name: 'Add subscription' }).click();
    const dialog = page.getByRole('dialog');
    const method = dialog.getByRole('radiogroup', { name: 'Connection method' });
    await expect(dialog.getByRole('button', { name: 'Sign in with Claude' })).toBeVisible();
    await settleEffects(page);

    const baseline = await anchorBoxes(page);
    // The height is the assistant's, so it cannot be a function of what is on screen.
    await expectFitsItsRoom(page, 620);
    for (const anchor of Object.values(baseline)) expect(anchor).not.toBeNull();
    expect(baseline.frame!.y + baseline.frame!.height).toBeLessThanOrEqual(viewport.height);

    // Subscription ⇄ API Key, four times, with the anchors measured after each.
    for (const label of ['API credentials', 'Claude account', 'API credentials', 'Claude account']) {
      await method.getByRole('radio', { name: label, exact: true }).click();
      await expect(method.getByRole('radio', { name: label, exact: true })).toHaveAttribute('aria-checked', 'true');
      await settleEffects(page);
      expect(await anchorBoxes(page)).toEqual(baseline);
      expect(await scrollers(page)).toEqual([]);
    }

    // API Key ⇄ Auth Token, the second switch the contract names. It lives inside the
    // middle, so it is exactly the kind of change that used to move everything above it.
    await method.getByRole('radio', { name: 'API credentials', exact: true }).click();
    const credential = dialog.getByRole('radiogroup', { name: 'Credential type' });
    await expect(credential).toBeVisible();
    for (const label of ['Auth Token', 'API Key', 'Auth Token', 'API Key']) {
      await credential.getByRole('radio', { name: label, exact: true }).click();
      await expect(credential.getByRole('radio', { name: label, exact: true })).toHaveAttribute('aria-checked', 'true');
      await settleEffects(page);
      expect(await anchorBoxes(page)).toEqual(baseline);
    }
    // The label the field asks for follows the choice, which is the only thing that
    // should have changed across all of that.
    await expect(dialog.getByLabel('Auth Token', { exact: true })).toHaveCount(0);
    await expect(dialog.getByLabel('API Key', { exact: true })).toBeVisible();
    await settleEffects(page);
    await dialog.screenshot({ path: info.outputPath(`frame-stability-claude-${viewport.width}x${viewport.height}.png`) });

    // A radio group is ONE tab stop that answers arrow keys, and the selected tab keeps
    // the focus so the next press continues from where the reader is. The loop above left
    // the second tab selected, so that is the one Tab reaches.
    const tabs = method.getByRole('radio');
    expect(await tabs.evaluateAll((nodes) => nodes.map((node) => (node as HTMLElement).tabIndex))).toEqual([-1, 0]);
    await tabs.nth(1).focus();
    await page.keyboard.press('ArrowRight'); // wraps
    await expect(tabs.first()).toBeFocused();
    await expect(tabs.first()).toHaveAttribute('aria-checked', 'true');
    await page.keyboard.press('ArrowRight');
    await expect(tabs.nth(1)).toBeFocused();
    await expect(tabs.nth(1)).toHaveAttribute('aria-checked', 'true');
    await page.keyboard.press('Home');
    await expect(tabs.first()).toBeFocused();
    await page.keyboard.press('End');
    await expect(tabs.nth(1)).toBeFocused();
    await expect(tabs.nth(1)).toHaveAttribute('aria-checked', 'true');
    expect(await anchorBoxes(page)).toEqual(baseline);

    await page.keyboard.press('Escape'); await expect(dialog).toHaveCount(0);

    // The frame is the assistant's own: Codex is the shorter one, and it holds too.
    await page.getByLabel('Codex', { exact: true }).getByRole('button', { name: 'Add API Key' }).click();
    await expect(dialog.getByLabel('Base URL (optional)')).toBeVisible();
    await settleEffects(page);
    const codex = await anchorBoxes(page);
    await expectFitsItsRoom(page, 580);
    for (const label of ['Sign in with ChatGPT', 'OpenAI API Key', 'Sign in with ChatGPT']) {
      await dialog.getByRole('radiogroup', { name: 'Connection method' }).getByRole('radio', { name: label, exact: true }).click();
      await settleEffects(page);
      expect(await anchorBoxes(page)).toEqual(codex);
    }
    await page.keyboard.press('Escape');
    expect(denied).toEqual([]);
  });
}

/** Each assistant's dialog, the height its own frame is sized for, and the two methods
 *  whose switch the contract names. OpenCode has no method row — it chooses a provider
 *  first, and that choice is the larger change: it replaces row three AND the middle. */
const ASSISTANTS = [
  { slug: 'claude', label: 'Claude Code', height: 620, methods: ['API credentials', 'Claude account'] },
  { slug: 'codex', label: 'Codex', height: 580, methods: ['OpenAI API Key', 'Sign in with ChatGPT'] },
  { slug: 'opencode', label: 'OpenCode', height: 640, methods: [] },
] as const;

/** Non-ASCII, because a failure message is user-facing copy and the row it lands in is
 *  one of the things the frame has to stay the same size around. */
const READ_FAILURE = '读取失败 · fixture read failure';

/**
 * The connection reads, with a switch on them. Registered AFTER `authFixtures`, which is
 * what makes them win — Playwright matches routes newest-first.
 *
 * Nothing is stubbed at the component level: loading is a response still in flight, the
 * error state is the product's own read rejecting a body it was given, and retry is the
 * product asking again. The refusal is a 200 carrying `ok: false` rather than a 5xx on
 * purpose — that is the shape this endpoint really answers with, and it keeps the global
 * error toast out of a measurement of the dialog.
 */
async function switchableReads(page: Page) {
  let waiting: (() => void)[] = [];
  let open = true;
  let failing = false;
  const answer = async (route: Route, json: object) => {
    if (!open) await new Promise<void>((resolve) => { waiting.push(resolve); });
    return route.fulfill({ json: failing ? { ok: false, message: READ_FAILURE } : json });
  };
  await page.route('**/api/backend/*/auth', (route) => answer(route, { ok: true, active_auth_mode: 'none', auth_mode: 'oauth', has_api_key: false, base_url: null }));
  await page.route('**/api/backend/opencode/providers', (route) => answer(route, { ok: true, providers: [
    { id: 'openai', name: 'OpenAI', description: 'ChatGPT subscription or API Key', oauth_available: true, configured: false, local: false, models: [] },
    // A prioritised brand under one of its aliases, and one provider that is not
    // prioritised at all — so the picker has both a shortlist and a More to open.
    { id: 'alibaba-cn', name: 'Alibaba (China)', description: '', oauth_available: false, configured: true, local: false, models: [] },
    { id: 'cerebras', name: 'Cerebras', description: '', oauth_available: false, configured: false, local: false, models: [] },
  ] }));
  return {
    hold: () => { open = false; },
    release: () => { open = true; const pending = waiting; waiting = []; for (const resolve of pending) resolve(); },
    fail: (value: boolean) => { failing = value; },
  };
}

/** Reachable is hit-testable at its own centre, not merely present: the way out and the
 *  action are what a short window is most likely to have pushed under an edge. */
async function expectReachable(dialog: Locator) {
  for (const control of [dialog.getByRole('button', { name: 'Close' }), dialog.locator('.connection-actions button').last()]) {
    await expect(control).toBeInViewport();
    expect(await control.evaluate((node) => {
      const rect = node.getBoundingClientRect();
      return node.contains(document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2));
    }), 'hit-testable at its centre').toBe(true);
  }
}

/**
 * The narrowest phone the matrix names, and a window shorter than a landscape phone —
 * which is also roughly what a soft keyboard leaves one. Both are windows that cannot
 * grant an assistant the height it asked for, so they are where the frame has to decide
 * what to give back, and every state has to survive that decision.
 */
for (const viewport of [{ width: 320, height: 568 }, { width: 390, height: 300 }]) {
  test(`every assistant holds its anchors through loading, failure and retry ${size(viewport)}`, async ({ page }, info) => {
    const denied = await serveProduct(page);
    await authFixtures(page);
    const reads = await switchableReads(page);
    await page.setViewportSize(viewport);
    await openOnboarding(page);
    await openSetup(page, 'en');
    const dialog = page.getByRole('dialog');

    for (const assistant of ASSISTANTS) {
      reads.hold(); reads.fail(true);
      await page.getByLabel(assistant.label, { exact: true }).getByRole('button', { name: 'Add API Key' }).click();

      // Loading, with the read still in flight. All five rows are already there: an
      // anchor that arrived with the data would move everything under it when it did.
      await expect(dialog.getByRole('status')).toBeVisible();
      const anchors = await settled(page);
      for (const [row, box] of Object.entries(anchors)) expect(box, `${assistant.slug} ${row} while loading`).not.toBeNull();
      await expectFitsItsRoom(page, assistant.height);
      await expectReachable(dialog);

      // The failure, then the retry the product offers for it.
      reads.release();
      await expect(dialog.getByRole('alert')).toContainText(READ_FAILURE);
      expect(await settled(page), `${assistant.slug} anchors on failure`).toEqual(anchors);
      await expectFitsItsRoom(page, assistant.height);
      await expectReachable(dialog);
      await settleEffects(page);
      await dialog.screenshot({ path: info.outputPath(`${assistant.slug}-read-failed-${size(viewport)}.png`) });

      reads.fail(false);
      await dialog.getByRole('button', { name: 'Retry' }).click();
      await expect(dialog.getByRole('alert')).toHaveCount(0);
      expect(await settled(page), `${assistant.slug} anchors after retry`).toEqual(anchors);
      expect(await scrollers(page), `${assistant.slug} scrollers`).toEqual([]);
      await expectReachable(dialog);

      // Whatever row three can do on this assistant, twice in each direction.
      for (const label of [...assistant.methods, ...assistant.methods]) {
        await dialog.getByRole('radiogroup', { name: 'Connection method' }).getByRole('radio', { name: label, exact: true }).click();
        await expect(dialog.getByRole('radiogroup', { name: 'Connection method' }).getByRole('radio', { name: label, exact: true })).toHaveAttribute('aria-checked', 'true');
        await settleEffects(page);
        expect(await settled(page), `${assistant.slug} anchors after ${label}`).toEqual(anchors);
        expect(await scrollers(page), `${assistant.slug} scrollers after ${label}`).toEqual([]);
      }
      if (!assistant.methods.length) {
        // Opening More is the biggest thing the middle can be asked to hold, and
        // choosing a provider replaces both row three and the middle at once.
        await dialog.getByRole('button', { name: 'More providers (1)' }).click();
        await expect(dialog.getByRole('button', { name: /Cerebras/ })).toBeVisible();
        expect(await settled(page), 'OpenCode anchors with More open').toEqual(anchors);
        expect(await scrollers(page), 'OpenCode scrollers with More open').toEqual([]);
        await dialog.getByRole('button', { name: /Cerebras/ }).click();
        await expect(dialog.getByLabel('Base URL (optional)')).toBeVisible();
        expect(await settled(page), 'OpenCode anchors after choosing a provider').toEqual(anchors);
        await expectReachable(dialog);
      }
      await settleEffects(page);
      await dialog.screenshot({ path: info.outputPath(`${assistant.slug}-settled-${size(viewport)}.png`) });
      await page.keyboard.press('Escape');
      await expect(dialog).toHaveCount(0);
    }
    expect(denied).toEqual([]);
  });
}

/**
 * The case `100dvh` cannot see. On iOS the layout viewport stays full height while the
 * soft keyboard is up — only the VISUAL viewport shrinks — so a frame sized in `dvh`
 * keeps its footer exactly where the keyboard now is. The keyboard is simulated the only
 * way a headless browser can: the visual viewport really does report a smaller height,
 * and the product's own listener is what has to notice.
 *
 * It needs a touch context, because that is the gate the shared hook uses to decide a
 * soft keyboard can exist at all — and on a desktop it must NOT move, which is the
 * second half of this test.
 */
/** Installed before the bundle, so the resting height the product measures at start-up
 *  is the real one and only what a keyboard does to it afterwards is simulated. */
const simulateSoftKeyboard = (page: Page) => page.addInitScript(() => {
  const view = window.visualViewport;
  if (!view) return;
  const real = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(view), 'height')!.get!;
  let keyboard = 0;
  Object.defineProperty(view, 'height', { configurable: true, get: () => real.call(view) - keyboard });
  Object.defineProperty(window, '__keyboard', { value: (height: number) => { keyboard = height; view.dispatchEvent(new Event('resize')); } });
  // The other half of the same keyboard. iOS also PANS the visual viewport over the
  // layout viewport to keep the focused field in sight: the room does not change
  // size, it changes place — and `scroll`, not `resize`, is what the platform sends.
  // A height alone says how much a person can see and not which part, so anything
  // centred on the height alone is left behind by exactly this much.
  let pan = 0;
  Object.defineProperty(view, 'offsetTop', { configurable: true, get: () => pan });
  Object.defineProperty(window, '__pan', { value: (top: number) => { pan = top; view.dispatchEvent(new Event('scroll')); } });
});
const softKeyboard = (page: Page, height: number) => page.evaluate((value) =>
  (window as unknown as { __keyboard: (height: number) => void }).__keyboard(value), height);
const panTo = (page: Page, top: number) => page.evaluate((value) =>
  (window as unknown as { __pan: (top: number) => void }).__pan(value), top);
const viewportVar = (page: Page, name: string) => page.evaluate((property) =>
  document.documentElement.style.getPropertyValue(property), name);

for (const touch of [true, false]) {
  test(`the frame ${touch ? 'gives way to the soft keyboard' : 'ignores a viewport no keyboard shrank'}`, async ({ browser }, info) => {
    const context = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: touch });
    const page = await context.newPage();
    await simulateSoftKeyboard(page);
    const denied = await serveProduct(page);
    await authFixtures(page);
    await openOnboarding(page, { realTime: true });
    await openSetup(page, 'en');
    await page.getByLabel('Claude Code', { exact: true }).getByRole('button', { name: 'Add API Key' }).click();
    const dialog = page.getByRole('dialog');
    const key = dialog.getByLabel('API Key', { exact: true });
    await expect(key).toBeVisible();
    const before = await expectFitsItsRoom(page, 620);
    expect(before.height, 'a window this tall grants the assistant its own height').toBeCloseTo(620, 0);

    // A keyboard opens because a field was focused, which is also what tells the shared
    // helper that this shrink is a keyboard and not a resized window.
    const resting = await page.evaluate(() => window.visualViewport!.height);
    await key.focus();
    await softKeyboard(page, 420);
    if (touch) {
      await expect.poll(() => viewportVar(page, '--app-vvh')).toBe(`${resting - 420}px`);
      const anchored = await expectFitsItsRoom(page, 620);
      // The field being typed into lives in the middle, and a keyboard this large
      // leaves the middle too short to show all of it at once. Reachable therefore
      // means it can be brought into view by the one part that is allowed to scroll,
      // without any of the anchors — or the frame itself — moving to make room.
      await key.scrollIntoViewIfNeeded();
      await expect(key).toBeInViewport();
      expect((await settled(page)).frame).toEqual(anchored);
      await expectReachable(dialog);
      expect(await scrollers(page)).toEqual([]);

      // Then the pan. The room keeps its size and moves down the screen, and the frame
      // is `position: fixed` — laid out against the layout viewport, which did not move
      // at all. So it has to be told, and it has to follow by the whole distance: a
      // centre computed from the height alone stays exactly where it was, which is the
      // half of this that a keyboard test measuring only `height` can never fail on.
      await panTo(page, 120);
      await expect.poll(() => viewportVar(page, '--app-vvt')).toBe('120px');
      const panned = await expectFitsItsRoom(page, 620);
      expect(panned.y - anchored.y, 'follows the room it is centred in').toBeCloseTo(120, 0);
      expect(panned.height, 'a pan moves the room, it does not shrink it').toBeCloseTo(anchored.height, 0);
      await expectReachable(dialog);

      // And back: the field loses focus, the pan returns, and so does the frame.
      await panTo(page, 0);
      await expect.poll(() => viewportVar(page, '--app-vvt')).toBe('0px');
      expect((await settled(page)).frame).toEqual(anchored);
    } else {
      // No soft keyboard exists here, so nothing may move: a shrunken visual viewport on
      // a desktop is a pinch-zoom or a trackpad gesture, not room the frame lost.
      await page.waitForTimeout(100);
      expect(await viewportVar(page, '--app-vvh')).toBe('');
      expect(await viewportVar(page, '--app-vvt')).toBe('');
      expect((await settled(page)).frame!.height).toBeCloseTo(620, 0);
    }
    await settleEffects(page);
    await dialog.screenshot({ path: info.outputPath(`claude-key-${touch ? 'keyboard' : 'desktop'}-390x844.png`) });
    expect(denied).toEqual([]);
    await context.close();
  });
}

/**
 * The room below the floor, which no band can size its way out of.
 *
 * A phone held sideways is 390 tall before a keyboard takes any of it, and the shortest
 * band still owes 232 of that to rows, gaps and padding. The middle is what gives way,
 * and here it is already at nothing — so the frame's own chrome is more than the room,
 * and it is the FRAME that has to give. The bands cannot see this coming either: they
 * are `max-height` on the layout viewport, which a soft keyboard never moves.
 *
 * What must survive is the part a person cannot recover from on their own. A clipped
 * footer is a dialog with no way out; a frame they can scroll is one they can finish.
 */
test('a room smaller than the frame scrolls rather than clipping what it cannot hold', async ({ browser }, info) => {
  const context = await browser.newContext({ viewport: { width: 844, height: 390 }, hasTouch: true });
  const page = await context.newPage();
  await simulateSoftKeyboard(page);
  const denied = await serveProduct(page);
  await authFixtures(page);
  await openOnboarding(page, { realTime: true });
  await openSetup(page, 'en');
  await page.getByLabel('Claude Code', { exact: true }).getByRole('button', { name: 'Add API Key' }).click();
  const dialog = page.getByRole('dialog');
  const key = dialog.getByLabel('API Key', { exact: true });
  await expect(key).toBeVisible();
  // Sideways with no keyboard is still a room the frame fits, and nothing scrolls but
  // the middle. Everything below is what the keyboard alone does to it.
  await expectFitsItsRoom(page, 620);
  await expectReachable(dialog);
  expect(await scrollers(page)).toEqual([]);

  const resting = await page.evaluate(() => window.visualViewport!.height);
  await key.focus();
  await softKeyboard(page, 200);
  await expect.poll(() => viewportVar(page, '--app-vvh')).toBe(`${resting - 200}px`);

  // The frame still holds its room — centred in what is visible, margins on every side,
  // never pushed against an edge. It just cannot hold itself.
  const frame = await expectFitsItsRoom(page, 620);
  const chrome = await dialog.evaluate((node) => node.scrollHeight);
  expect(chrome, 'more frame than there is room').toBeGreaterThan(frame.height);

  // So the frame is the scroller, and the only one: nothing else inside it was asked
  // to hold more than it has room for.
  const overflowing = await scrollers(page);
  expect(overflowing, 'the frame, and nothing else').toHaveLength(1);
  expect(overflowing[0]).toContain('connection-dialog');

  // And the two things a person must be able to get to are reachable — by scrolling,
  // which is the whole point, rather than by luck of where the clip happened to land.
  for (const control of [dialog.getByRole('button', { name: 'Close' }), dialog.locator('.connection-actions button').last()]) {
    await control.scrollIntoViewIfNeeded();
    await expect(control).toBeInViewport();
    expect(await control.evaluate((node) => {
      const rect = node.getBoundingClientRect();
      return node.contains(document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2));
    }), 'hit-testable at its centre').toBe(true);
  }

  await settleEffects(page);
  await dialog.screenshot({ path: info.outputPath('claude-key-keyboard-844x390.png') });
  expect(denied).toEqual([]);
  await context.close();
});

/**
 * The same form, rendered where there is no dialog.
 *
 * `.connection-dialog` has exactly one consumer — the setup dialog — and what Settings
 * and onboarding actually share is `.backend-connection-form`. The dialog's five-row
 * grid is what hoists that form's three regions into place, and every rule that does so
 * is scoped under `.connection-dialog`; this is the browser saying so, because a jsdom
 * render resolves no stylesheet and therefore cannot tell a hoisted region from an
 * ordinary one. Settings has to keep the plain column it always had.
 */
test('the Settings form keeps its own column and never inherits the dialog grid', async ({ page }, info) => {
  const denied = await serveProduct(page);
  await page.route('**/api/codex/models', (route) => route.fulfill({ json: { ok: true, models: [] } }));
  await page.route('**/api/backend/codex/auth', (route) => route.fulfill({ json: { ok: true, active_auth_mode: 'api_key', auth_mode: 'api_key', has_api_key: true, api_key_masked: 'sk-••••fixture', base_url: 'https://fixture.invalid/v1', file_store_active: true } }));
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/e2e/onboarding-fidelity/fixture.html?surface=disabled-settings&lang=en&theme=dark');
  const form = page.locator('.backend-connection-form');
  await expect(form).toBeVisible();

  const layout = await form.evaluate((node) => {
    const style = getComputedStyle(node);
    return {
      dialogs: document.querySelectorAll('.connection-dialog').length,
      // The dialog's own rows belong to the dialog: Settings has its own page heading.
      dialogRows: document.querySelectorAll('.connection-heading, .connection-description').length,
      display: style.display,
      flow: style.flexDirection,
      overflowX: node.scrollWidth - node.clientWidth,
      regions: [...node.children].map((child) => {
        const own = getComputedStyle(child);
        const box = child.getBoundingClientRect();
        return { name: child.className.toString().split(' ')[0], row: own.gridRowStart, overflowY: own.overflowY, top: Math.round(box.top), height: Math.round(box.height) };
      }),
    };
  });
  expect(layout.dialogs).toBe(0);
  expect(layout.dialogRows).toBe(0);
  // `display: contents` is how the dialog hoists these three into its grid. Here the
  // form is a box of its own, its regions are placed by flow and not by row, and the
  // middle is not a scroller — it has the whole page to grow into.
  expect(layout.display).toBe('flex');
  expect(layout.flow).toBe('column');
  expect(layout.regions.map((region) => region.name)).toEqual(['connection-radio', 'connection-body', 'connection-actions']);
  expect(layout.regions.map((region) => region.row)).toEqual(['auto', 'auto', 'auto']);
  expect(layout.regions.map((region) => region.overflowY)).toEqual(['visible', 'visible', 'visible']);
  for (const region of layout.regions) expect(region.height, region.name).toBeGreaterThan(0);
  expect(layout.regions.map((region) => region.top)).toEqual([...layout.regions.map((region) => region.top)].sort((left, right) => left - right));
  expect(layout.overflowX).toBeLessThanOrEqual(1);

  const save = form.getByRole('button', { name: 'Save', exact: true });
  await save.scrollIntoViewIfNeeded();
  await expect(save).toBeInViewport();
  await settleEffects(page);
  await page.screenshot({ path: info.outputPath('settings-form-column-390x844.png') });
  expect(denied).toEqual([]);
});

for (const lang of ['en', 'zh']) {
  test(`corrected compact labels and Light action ${lang}`, async ({ page }, info) => {
    const denied = await serveProduct(page); await authFixtures(page);
    await page.setViewportSize({ width: 390, height: 640 });
    await openOnboarding(page, { lang, theme: 'light' }); await openSetup(page, lang);
    const addKey = lang === 'zh' ? '添加 API Key' : 'Add API Key';
    await page.getByLabel('Claude Code', { exact: true }).getByRole('button', { name: addKey }).click();
    const dialog = page.getByRole('dialog');
    const credential = dialog.getByRole('radiogroup', { name: lang === 'zh' ? '凭据类型' : 'Credential type' });
    await expect(credential.getByRole('radio', { name: 'API Key', exact: true })).toBeVisible();
    await expect(credential.getByRole('radio', { name: 'Auth Token', exact: true })).toBeVisible();
    const bounds = await credential.evaluate((node) => [...node.querySelectorAll('button')].map((button) => {
      const range = document.createRange(); range.selectNodeContents(button);
      const text = range.getBoundingClientRect(); const box = button.getBoundingClientRect();
      return text.left >= box.left && text.right <= box.right && text.top >= box.top && text.bottom <= box.bottom;
    }));
    expect(bounds).toEqual([true, true]);
    await page.keyboard.press('Tab');
    await credential.getByRole('radio', { name: 'Auth Token' }).focus();
    const focus = await credential.getByRole('radio', { name: 'Auth Token' }).evaluate((node) => {
      const style = getComputedStyle(node); return { width: style.outlineWidth, style: style.outlineStyle, color: style.outlineColor };
    });
    expect(focus.width).toBe('2px'); expect(focus.style).toBe('solid');
    const save = dialog.getByRole('button', { name: lang === 'zh' ? '保存并连接' : 'Save and connect' });
    const disabled = await save.evaluate((node) => { const style = getComputedStyle(node); return { opacity: style.opacity, fill: style.backgroundColor, text: style.color }; });
    expect(disabled.opacity).toBe('0.4'); expect(disabled.text).toBe('rgb(255, 255, 255)');
    await settleEffects(page); await dialog.screenshot({ path: info.outputPath(`corrected-claude-key-${lang}-light-disabled.png`) });
    await dialog.getByLabel('API Key', { exact: true }).fill('nonsecret-fixture-value');
    await expect(save).toBeEnabled();
    await settleEffects(page);
    const enabled = await save.evaluate((node) => { const style = getComputedStyle(node); return { opacity: style.opacity, fill: style.backgroundColor, text: style.color }; });
    expect(enabled).toEqual({ ...disabled, opacity: '1' });
    await settleEffects(page); await dialog.screenshot({ path: info.outputPath(`corrected-claude-key-${lang}-light-enabled.png`) });
    await page.keyboard.press('Escape');
    await page.getByLabel('OpenCode', { exact: true }).getByRole('button', { name: addKey }).click();
    await dialog.getByRole('button', { name: /OpenAI/ }).click();
    await expect(dialog.getByLabel('API Key', { exact: true })).toBeVisible();
    await expect(dialog.getByText(/ANTHROPIC_/)).toHaveCount(0);
    await settleEffects(page); await dialog.screenshot({ path: info.outputPath(`corrected-opencode-key-${lang}-light-narrow.png`) });
    await page.keyboard.press('Escape'); expect(denied).toEqual([]);
  });
}

test('corrected locked Codex Light has one opacity layer and visible selected method', async ({ page }, info) => {
  const denied = await serveProduct(page); await authFixtures(page);
  await page.setViewportSize({ width: 1200, height: 800 });
  await openOnboarding(page, { theme: 'light' }); await openSetup(page, 'en');
  await page.getByLabel('Codex', { exact: true }).getByRole('button', { name: 'Add subscription' }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByRole('button', { name: 'Sign in with ChatGPT' }).click();
  await expect(dialog.getByText('ABCD-1234')).toBeVisible();
  const radio = dialog.getByRole('radio', { name: 'Sign in with ChatGPT' });
  await expect(radio).toBeDisabled(); await expect(radio).toHaveAttribute('aria-checked', 'true');
  const styles = await radio.evaluate((node) => {
    const style = getComputedStyle(node); const parent = getComputedStyle(node.parentElement!);
    return { opacity: style.opacity, parentOpacity: parent.opacity, color: style.color, fill: style.backgroundColor, border: style.borderColor };
  });
  expect(styles.opacity).toBe('1'); expect(styles.parentOpacity).toBe('0.6');
  expect(styles.fill).not.toBe('rgba(0, 0, 0, 0)'); expect(styles.border).not.toBe('rgba(0, 0, 0, 0)');
  await settleEffects(page); await dialog.screenshot({ path: info.outputPath('corrected-codex-locked-light.png') });
  await page.keyboard.press('Escape'); expect(denied).toEqual([]);
});

for (const width of [1200, 390]) {
  test(`OpenCode inline model recovery ${width}`, async ({ page }, info) => {
    const denied = await serveProduct(page); await authFixtures(page);
    let model = 'openai/gpt-5.6-sol'; let writes = 0;
    const agent = () => ({ id: 'fixture-agent', name: 'opencode', display_name: 'OpenCode', backend: 'opencode', enabled: true, model, reasoning_effort: null, source: 'builtin', archived: false });
    await page.route('**/api/backend/*/connection', (route) => { const backend = new URL(route.request().url()).pathname.split('/').at(-2); return route.fulfill({ json: { ok: true, backend, ready: backend === 'opencode', entry_eligible: backend === 'opencode', installed: true, enabled: true, auth: 'api_key', application: 'applied' } }); });
    await page.route('**/api/agents', (route) => route.fulfill({ json: { ok: true, default_agent_name: 'opencode', agents: [agent()] } }));
    await page.route('**/api/agents/opencode', (route) => { if (route.request().method() === 'PATCH') { model = route.request().postDataJSON().model; writes++; } return route.fulfill({ json: { ok: true, agent: agent() } }); });
    await page.route('**/api/backend/opencode/providers', (route) => route.fulfill({ json: { ok: true, default_provider: 'openai', providers: [{ id: 'anthropic', name: 'Anthropic', description: '', local: false, oauth_available: false, active_auth_type: 'api', configured: true, models: ['chosen-model'] }] } }));
    await page.route('**/api/models/agents/opencode/models', (route) => route.fulfill({ json: { ok: true, agent: { backend: 'opencode', mode: 'direct' } } }));
    await page.route('**/api/opencode/options', (route) => route.fulfill({ json: { ok: true, data: { models: { providers: [{ id: 'anthropic', models: { 'chosen-model': {} } }] } } } }));
    await page.setViewportSize({ width, height: 800 });
    await openOnboarding(page); await openSetup(page, 'en');
    await page.getByRole('button', { name: 'Enter workspace' }).click();
    const recovery = page.getByRole('region', { name: 'Choose a model for this connection' });
    await expect(recovery).toBeVisible(); expect(writes).toBe(0);
    const picker = recovery.getByRole('combobox'); await expect(picker).toBeEnabled(); await picker.click();
    await page.getByRole('option', { name: 'anthropic/chosen-model' }).click();
    const apply = recovery.getByRole('button', { name: 'Apply model and enter' });
    await apply.scrollIntoViewIfNeeded(); await expect(apply).toBeInViewport();
    expect(await recovery.evaluate((node) => node.scrollWidth - node.clientWidth)).toBeLessThanOrEqual(1);
    await settleEffects(page); await recovery.screenshot({ path: info.outputPath(`model-recovery-${width}.png`) });
    await recovery.getByRole('button', { name: 'Cancel' }).click(); expect(writes).toBe(0);
    await expect(recovery).toHaveCount(0); expect(denied).toEqual([]);
  });
}

test('saved Slack recovery narrow uses the existing form only after explicit repair', async ({ page }, info) => {
  const denied = await serveProduct(page); await authFixtures(page);
  let manifests = 0;
  await page.route('**/api/config', (route) => route.fulfill({ json: { platforms: { enabled: ['slack'] }, platform_catalog: [{ id: 'slack', config_key: 'slack', credential_fields: ['bot_token'] }], slack: { has_app_token: true, bot_token: '' }, agents: { claude: { enabled: true }, codex: { enabled: true }, opencode: { enabled: true } } } }));
  await page.route('**/api/backend/claude/connection', (route) => route.fulfill({ json: { ok: true, backend: 'claude', ready: true, entry_eligible: true, enabled: true, installed: true, application: 'applied', auth: 'api_key' } }));
  await page.route('**/api/slack/manifest', (route) => { manifests++; return route.fulfill({ json: { ok: true, manifest: '{}' } }); });
  await page.setViewportSize({ width: 390, height: 640 });
  await openOnboarding(page); await openSetup(page, 'en');
  await page.getByRole('button', { name: 'Enter workspace' }).click();
  const recovery = page.getByRole('region', { name: 'Repair saved messaging configuration' });
  await expect(recovery).toBeVisible(); expect(manifests).toBe(0);
  await recovery.getByRole('button', { name: 'Repair saved messaging configuration' }).click();
  await expect.poll(() => manifests).toBe(1);
  await recovery.getByRole('button', { name: /Get Bot Token/ }).click();
  await recovery.getByPlaceholder('xoxb-... (paste here)').fill('xoxb-fixture-only');
  const apply = recovery.getByRole('button', { name: /Apply/ });
  await expect(apply).toBeEnabled(); await apply.scrollIntoViewIfNeeded(); await expect(apply).toBeInViewport();
  expect(await recovery.evaluate((node) => node.scrollWidth - node.clientWidth)).toBeLessThanOrEqual(1);
  await settleEffects(page); await recovery.screenshot({ path: info.outputPath('saved-slack-recovery-narrow.png') });
  await recovery.getByRole('button', { name: 'Cancel' }).click();
  expect(denied).toEqual([]);
});

for (const lang of ['en', 'zh']) {
  test(`disabled Settings credentials stay saved without connection ${lang}`, async ({ page }, info) => {
    const denied = await serveProduct(page);
    let saves = 0;
    let enabled = false;
    await page.route('**/api/codex/models', (route) => route.fulfill({ json: { ok: true, models: [] } }));
    await page.route('**/api/config', (route) => {
      if (route.request().method() === 'POST') {
        expect(route.request().postDataJSON()).toEqual({ agents: { codex: { enabled: true } } });
        enabled = true;
      }
      return route.fulfill({ json: { agents: { codex: { enabled, cli_path: '/fixture/bin/codex' } }, platforms: { enabled: [] }, agent_backend_runtime: { hot_reconciled: true } } });
    });
    await page.route('**/api/csrf-token', (route) => route.fulfill({ json: { csrf_token: 'fixture-token' } }));
    await page.route('**/api/backend/codex/auth', (route) => {
      if (route.request().method() === 'POST') {
        saves++;
        return route.fulfill({ json: { ok: true, restart: { ok: true } } });
      }
      return route.fulfill({ json: { ok: true, active_auth_mode: 'api_key', auth_mode: 'api_key', has_api_key: true, api_key_masked: 'sk-••••test', base_url: 'https://fixture.invalid/v1', file_store_active: true } });
    });
    await page.route('**/api/backend/codex/connection', (route) => route.fulfill({ json: { ok: true, backend: 'codex', installed: true, enabled, auth: 'api_key', application: 'applied', ready: enabled, entry_eligible: enabled } }));
    await page.setViewportSize({ width: 390, height: 640 });
    await page.goto(`/e2e/onboarding-fidelity/fixture.html?surface=disabled-settings&lang=${lang}&theme=light`);
    const saved = lang === 'zh' ? '凭据已保存。此助手当前未启用。' : 'Credentials saved. This assistant is currently disabled.';
    await expect(page.getByText(saved)).toBeVisible();
    await page.locator('.backend-connection-form').getByRole('button', { name: lang === 'zh' ? '保存' : 'Save', exact: true }).click();
    await expect.poll(() => saves).toBe(1);
    await expect(page.getByRole('alert')).toHaveCount(0);
    await expect(page.getByText(lang === 'zh' ? '已连接' : 'Connected', { exact: true })).toHaveCount(0);
    await page.reload(); await expect(page.getByText(saved)).toBeVisible();
    const bounds = await page.getByText(saved).evaluate((node) => {
      const range = document.createRange(); range.selectNodeContents(node);
      const text = range.getBoundingClientRect(); const rect = node.getBoundingClientRect();
      return text.left >= rect.left && text.right <= rect.right && text.bottom <= rect.bottom;
    });
    expect(bounds).toBe(true);
    expect(await page.getByText(saved).evaluate((node) => node.classList.contains('text-muted'))).toBe(true);
    await expect(page.getByRole('switch')).toHaveAttribute('aria-checked', 'false');
    const save = page.locator('.backend-connection-form').getByRole('button', { name: lang === 'zh' ? '保存' : 'Save', exact: true });
    const inspectActions = () => save.evaluate((button) => {
      const ancestors = [];
      for (let node: Element | null = button; node; node = node.parentElement) {
        const style = getComputedStyle(node);
        ancestors.push({ tag: node.tagName, class: node.className, rect: node.getBoundingClientRect().toJSON(), scrollTop: node.scrollTop, clientHeight: node.clientHeight, scrollHeight: node.scrollHeight, overflowY: style.overflowY, visibility: style.visibility, display: style.display, opacity: style.opacity });
      }
      const rect = button.getBoundingClientRect();
      return { ancestors, hit: button.contains(document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2)) };
    });
    const before = await inspectActions();
    await info.attach('pre-click-action-geometry', { body: JSON.stringify(before, null, 2), contentType: 'application/json' });
    await save.click(); // real actionability and hit-target checks after reload
    await expect.poll(() => saves).toBe(2);
    await expect(page.getByText(saved)).toBeVisible();
    await expect(save).toBeEnabled();
    await page.getByLabel(lang === 'zh' ? 'Base URL（可选）' : 'Base URL (optional)').focus();
    await page.keyboard.press('Tab'); // existing Remove key
    await page.keyboard.press('Tab'); // Save
    await expect(save).toBeFocused();
    await page.keyboard.press('Enter');
    await expect.poll(() => saves).toBe(3);
    await expect(page.getByText(saved)).toBeVisible();
    await save.scrollIntoViewIfNeeded();
    await expect(save).toBeInViewport();
    await expect(page.getByText(saved)).toBeInViewport();
    const after = await inspectActions();
    expect(after.hit).toBe(true);
    const geometryPath = info.outputPath(`post-reload-action-geometry-${lang}.json`);
    await writeFile(geometryPath, JSON.stringify({ before, after }, null, 2));
    await info.attach('post-reload-action-geometry', { path: geometryPath, contentType: 'application/json' });
    await page.screenshot({ path: info.outputPath(`post-reload-disabled-settings-${lang}-light-narrow.png`) });
    const baseUrl = page.getByLabel(lang === 'zh' ? 'Base URL（可选）' : 'Base URL (optional)');
    await baseUrl.fill('https://unsaved.invalid');
    await page.getByRole('switch').click();
    await expect(page.getByText(saved)).toHaveCount(0);
    await expect(page.getByText(lang === 'zh' ? '已连接' : 'Connected', { exact: true })).toBeVisible();
    await expect(baseUrl).toHaveValue('https://unsaved.invalid');
    await expect(page.getByRole('alert')).toHaveCount(0);
    expect(saves).toBe(3);
    expect(denied).toEqual([]);
  });
}
