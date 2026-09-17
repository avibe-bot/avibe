import { expect, test, type Page } from '@playwright/test';
import {
  PHASES, VIEWPORTS, cssEffects, freezeAt, openOnboarding, openSetup, renderedPhase,
  serveProduct, setDocumentHidden, settleEffects, size, storeThemePreference,
} from './support';

/**
 * The 2026-09-17 design is a set of exact numbers, so this suite measures the shipped
 * component in a browser instead of asserting that nothing overflows. Screenshots are
 * written as artifacts from the same runs that make the assertions, so the evidence and
 * the guard can never describe two different builds.
 *
 * Sizes: a Playwright viewport IS the web content box, so nothing is subtracted for
 * chrome. The design's own 1200x800 frames include the native 44px titlebar above the
 * web view, which makes the design's content box 1200x756 — that is the size to overlay
 * a frame against (DESIGN_FRAME below). The dispatch's 1200x800 of effective content is
 * the larger window that same content box needs, and is what the matrix captures.
 *
 * The owner accepted 1104 as the desktop content cap on 2026-09-17, superseding the
 * frames' authored 880. Every number below is that decision applied to the frame's
 * proportion (1104/880 = 1.2545...), not a re-measured design.
 */
const DESIGN_FRAME = { width: 1200, height: 756 };
/** 20px of shell padding plus the 45px language bar: the setup's authored top (IWQi6). */
const SETUP_TOP = 65;

const box = async (page: Page, selector: string, index = 0) => {
  const rect = await page.locator(selector).nth(index).boundingBox();
  if (!rect) throw new Error(`${selector} is not rendered`);
  return rect;
};

/** Every match's real rect in one round trip — what "aligned" has to be measured from. */
const boxes = (page: Page, selector: string) =>
  page.locator(selector).evaluateAll((nodes) => nodes.map((node) => {
    const rect = node.getBoundingClientRect();
    return { x: rect.x, y: rect.y, width: rect.width, height: rect.height };
  }));

const round = (value: number) => Math.round(value * 100) / 100;
const spread = (values: number[]) => Math.max(...values) - Math.min(...values);

test.describe('desktop reference geometry', () => {
  test.use({ viewport: { width: 1200, height: 800 } });

  test('welcome matches the accepted 1104 collaboration and 802.9 access spans', async ({ page }, info) => {
    const denied = await serveProduct(page);
    await openOnboarding(page);
    await freezeAt(page, PHASES['codex-working']);

    const collaboration = await box(page, '.onboarding-collaboration');
    expect(round(collaboration.width)).toBe(1104);
    expect(collaboration.height).toBeCloseTo(341.24, 1);
    // Card size comes from a percentage track and the diagram's own width, so the
    // proportion holds at every width; those ratios are not representable on the
    // engine's 1/64px grid, so the numbers are met to within a sixty-fourth of a pixel.
    for (let index = 0; index < 3; index += 1) {
      const card = await box(page, '.onboarding-collaboration-card', index);
      expect(card.width).toBeCloseTo(301.09, 1);
      expect(card.height).toBeCloseTo(286.04, 1);
    }
    const [first, second, third] = await Promise.all([0, 1, 2].map((index) => box(page, '.onboarding-collaboration-card', index)));
    expect(second.x - (first.x + first.width)).toBeCloseTo(100.36, 1);
    expect(third.x - (second.x + second.width)).toBeCloseTo(100.36, 1);
    // What has to be exact is that the wire endpoints sit on the card edges: both are
    // driven by the same percentage basis, so a port can never drift off its card.
    const port = await box(page, '.onboarding-port', 0);
    expect(port.x + port.width / 2).toBeCloseTo(first.x + first.width, 1);

    const access = await box(page, '.onboarding-access');
    expect(access.width).toBeCloseTo(802.91, 1);
    expect(access.x - collaboration.x).toBeCloseTo(150.55, 1);

    const action = await box(page, '.onboarding-primary-action');
    expect(round(action.height)).toBe(44);
    expect(action.width).toBeGreaterThanOrEqual(144);

    await settleEffects(page);
    await page.screenshot({ path: info.outputPath('welcome-1200x800-dark-en.png') });
    expect(denied).toEqual([]);
  });

  // The design stacks the welcome with one rhythm: a 79 tall message block, then 24
  // between every block (frame bi8Au, whose blocks sit at y=65/168/464/648 of its 756
  // content box). The 1104 amendment makes the collaboration taller, so the blocks below
  // it move down by that much and no other number changes.
  test('welcome keeps the design vertical rhythm between its blocks', async ({ page }) => {
    const denied = await serveProduct(page);
    await openOnboarding(page);
    await freezeAt(page, PHASES['codex-working']);

    const heading = await box(page, '.onboarding-heading');
    // The same type the design specifies: a 34/1.35 title, a 10 gap and a 14/1.57
    // subtitle. The design reports 79 because it rounds each text box up on its own
    // (46 + 10 + 23); the browser stacks the unrounded boxes to 77.9.
    expect(heading.height).toBeCloseTo(77.89, 1);

    const story = await box(page, '.onboarding-story');
    const access = await box(page, '.onboarding-access');
    const action = await box(page, '.onboarding-primary-action');
    expect(story.y - (heading.y + heading.height)).toBeCloseTo(24, 0);
    expect(access.y - (story.y + story.height)).toBeCloseTo(24, 0);
    expect(action.y - (access.y + access.height)).toBeCloseTo(24, 0);
    // The access block's own height is content, not cap: two 42 rows, an 8 gap, the
    // caption and 17 of padding — the design's 160 at any width.
    expect(round(access.height)).toBe(160);
    expect(denied).toEqual([]);
  });

  /**
   * The two steps are anchored differently because the design anchors them differently:
   * JuwcG and KIxnF centre the welcome inside their frame, IWQi6 tops the setup out at
   * its own 64.5 padding. So the welcome's y is a RESULT of the window height and the
   * setup's is a position — and a test that pinned the welcome to 65 was asserting the
   * one window height where those two happen to agree.
   */
  test('the welcome is centred by the window, and the setup is not', async ({ page }) => {
    await serveProduct(page);
    await openOnboarding(page);
    await freezeAt(page, PHASES['codex-working']);

    const centring = async () => {
      const content = await box(page, '.onboarding-shell-content');
      const welcome = await box(page, '.onboarding-welcome');
      return {
        above: welcome.y - content.y,
        below: (content.y + content.height) - (welcome.y + welcome.height),
        headingY: (await box(page, '.onboarding-heading')).y,
      };
    };

    await page.setViewportSize({ width: 1200, height: 1200 });
    const tall = await centring();
    expect(Math.abs(tall.above - tall.below)).toBeLessThanOrEqual(1);
    // Centred means it moved: a tall window puts the heading far below the setup's 65.
    expect(tall.headingY).toBeGreaterThan(SETUP_TOP + 100);

    // At the design's own content box the composition is taller than the window, so
    // there is no slack to share and the same rule lands it on the content edge — where
    // the frame draws it. The shell scrolls from there instead of clipping the top.
    await page.setViewportSize(DESIGN_FRAME);
    const short = await centring();
    expect(short.above).toBeLessThanOrEqual(1);
    expect(round(short.headingY)).toBe(SETUP_TOP);

    await openSetup(page, 'en');
    // The setup's top is the position itself, so it holds at both window heights.
    expect(round((await box(page, '.onboarding-heading')).y)).toBe(SETUP_TOP);
    await page.setViewportSize({ width: 1200, height: 1200 });
    expect(round((await box(page, '.onboarding-heading')).y)).toBe(SETUP_TOP);
  });

  /**
   * A short window used to buy about 30px by taking the step gaps from 24 to 16 and
   * squeezing the access block — spending the design's own spacing at 1200x800, the
   * reference's own frame size, where nothing was wrong. Whitespace IS the composition,
   * so the shell scrolls instead and nothing about the type or the controls changes.
   */
  test('a short window scrolls rather than compressing the composition', async ({ page }) => {
    await serveProduct(page);
    await openOnboarding(page);
    const metrics = () => page.evaluate(() => {
      const read = (selector: string) => {
        const node = document.querySelector(selector);
        const style = node ? getComputedStyle(node) : null;
        return style ? `${style.fontSize}/${style.lineHeight}` : 'missing';
      };
      const rect = (selector: string) => {
        const found = document.querySelector(selector)?.getBoundingClientRect();
        return found ? `${Math.round(found.width)}x${Math.round(found.height)}` : 'missing';
      };
      return {
        type: [read('.onboarding-heading :is(h1, h2)'), read('.onboarding-heading p'), read('.onboarding-access-tile span')],
        action: [rect('.onboarding-primary-action'), read('.onboarding-primary-action')],
        access: rect('.onboarding-access'),
      };
    });
    const tall = await metrics();

    await page.setViewportSize(DESIGN_FRAME);
    await freezeAt(page, PHASES['codex-working']);
    expect(await metrics()).toEqual(tall);

    const heading = await box(page, '.onboarding-heading');
    const story = await box(page, '.onboarding-story');
    const access = await box(page, '.onboarding-access');
    const action = await box(page, '.onboarding-primary-action');
    for (const gap of [story.y - (heading.y + heading.height), access.y - (story.y + story.height), action.y - (access.y + access.height)]) {
      expect(gap).toBeCloseTo(24, 0);
    }
    // Scrolling is the accepted outcome, and it has to actually be reachable.
    expect(await page.evaluate(() => document.documentElement.scrollHeight > window.innerHeight
      || (document.querySelector('.onboarding-shell') as HTMLElement).scrollHeight > window.innerHeight)).toBe(true);
    await page.locator('.onboarding-primary-action').scrollIntoViewIfNeeded();
    await expect(page.locator('.onboarding-primary-action')).toBeInViewport();
  });

  test('assistant setup keeps the shared width, 88px rows and 12px spacing', async ({ page }, info) => {
    const denied = await serveProduct(page);
    await openOnboarding(page);
    const welcomeTitle = await box(page, '.onboarding-heading :is(h1, h2)');
    await openSetup(page, 'en');

    const setupTitle = await box(page, '.onboarding-heading :is(h1, h2)');
    // The title's box is shared between the steps; only its y differs, because only the
    // welcome is centred. Asserting the height keeps the two from drifting apart.
    expect(round(setupTitle.height)).toBe(round(welcomeTitle.height));

    const assistants = await box(page, '.onboarding-assistants');
    expect(round(assistants.width)).toBe(1104);
    await expect(page.locator('.onboarding-assistant')).toHaveCount(3);
    for (let index = 0; index < 3; index += 1) {
      const row = await box(page, '.onboarding-assistant', index);
      // Width follows the shared cap; height stays the design's 88 because the row's
      // content fits it, not because a fixed height forces it.
      expect([round(row.width), round(row.height)]).toEqual([1104, 88]);
      // Inside the row nothing was rescaled: a 44 well 20 from the edge, then identity
      // 16 further in, exactly as frame i3vw9 lays every backend row out.
      const logo = await box(page, '.onboarding-assistant-logo', index);
      expect([round(logo.width), round(logo.height)]).toEqual([44, 44]);
      expect(round(logo.x - row.x)).toBe(20);
      expect(round(logo.y - row.y)).toBe(22);
      const identity = await box(page, '.onboarding-assistant-identity', index);
      expect(round(identity.x - row.x)).toBe(80);
    }
    // 12 between the section bar and the first row, and between rows (design pitch 100).
    const header = await box(page, '.onboarding-assistants-header');
    const [firstRow, secondRow] = await Promise.all([0, 1].map((index) => box(page, '.onboarding-assistant', index)));
    expect(firstRow.y - (header.y + header.height)).toBeCloseTo(12, 0);
    expect(round(secondRow.y - firstRow.y)).toBe(100);
    // The setup heading keeps the welcome's 24 to the block below it.
    const setupHeading = await box(page, '.onboarding-heading');
    expect(header.y - (setupHeading.y + setupHeading.height)).toBeCloseTo(24, 0);

    await settleEffects(page);
    await page.screenshot({ path: info.outputPath('setup-1200x800-dark-en.png') });
    expect(denied).toEqual([]);
  });

  // The live design sets one headline everywhere: 34/1.35/600 at -1.6 tracking (XKQOx,
  // Igagh and hv9DA all agree), which is tighter than the browser default this shipped
  // with. Both steps read it from the same rule, so both are checked.
  test('both steps use the live design headline tracking', async ({ page }) => {
    await serveProduct(page);
    await openOnboarding(page);
    const type = () => page.evaluate(() => {
      const node = document.querySelector('.onboarding-heading :is(h1, h2)');
      const style = node ? getComputedStyle(node) : null;
      return style ? { size: style.fontSize, tracking: style.letterSpacing, weight: style.fontWeight } : null;
    });
    const expected = { size: '34px', tracking: '-1.6px', weight: '600' };
    expect(await type()).toEqual(expected);
    await openSetup(page, 'en');
    expect(await type()).toEqual(expected);
  });

  test('every handoff pulse rides the wire that connects its two cards', async ({ page }, info) => {
    const denied = await serveProduct(page);
    await openOnboarding(page);
    let clock = 0;
    for (const [name, elapsed] of Object.entries(PHASES)) {
      await freezeAt(page, elapsed - clock);
      clock = elapsed;
      const travelling = name.startsWith('handoff') || name === 'return-to-pm';
      await expect(page.getByTestId('handoff-pulse')).toHaveCount(travelling ? 1 : 0);
      await settleEffects(page);
      await page.locator('.onboarding-collaboration').screenshot({ path: info.outputPath(`phase-${name}.png`) });
    }
    expect(denied).toEqual([]);
  });
});

test.describe('capped fluid width', () => {
  for (const viewport of VIEWPORTS) {
    test(`fits ${size(viewport)} without horizontal overflow or clipping`, async ({ page }) => {
      await page.setViewportSize(viewport);
      const denied = await serveProduct(page);
      await openOnboarding(page);
      await freezeAt(page, PHASES['codex-working']);

      expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
      const collaboration = await box(page, '.onboarding-collaboration');
      // Capped fluid: never wider than the accepted cap, never wider than the viewport.
      expect(collaboration.width).toBeLessThanOrEqual(1104);
      expect(collaboration.width).toBeLessThanOrEqual(viewport.width);
      const wires = await box(page, '.onboarding-wires');
      // The stretched wire box always covers the cards, so no pulse can detach.
      expect(round(wires.width)).toBe(round(collaboration.width));

      // The primary action stays reachable: short viewports scroll rather than clip.
      const action = page.locator('.onboarding-primary-action');
      await action.scrollIntoViewIfNeeded();
      await expect(action).toBeInViewport();
      const clipped = await page.evaluate(() => {
        const shell = document.querySelector('.onboarding-shell');
        return shell ? getComputedStyle(shell).overflowY === 'hidden' : true;
      });
      expect(clipped).toBe(false);
      expect(denied).toEqual([]);
    });
  }

  /**
   * The access block is drawn 640 wide on the diagram's own 880 box, so its edges land
   * on the vertical legs of the return wire — the ports at x=120 and x=760. Checking
   * that at the cap alone would pass on a block sized from a fixed number that happens
   * to agree there, so it is checked at every width in between: only a width derived
   * from the diagram's ACTUAL width can hold the relation at all of them.
   */
  for (const width of [1440, 1200, 1100, 1024, 900, 768]) {
    test(`the access block spans the return wire legs at ${width}`, async ({ page }) => {
      await page.setViewportSize({ width, height: 900 });
      await serveProduct(page);
      await openOnboarding(page);
      await freezeAt(page, PHASES['codex-working']);

      const access = await box(page, '.onboarding-access');
      // PORTS index 5 is the x=120 leg and index 4 the x=760 one; `preserveAspectRatio`
      // is none, so each maps to that share of the rendered width.
      const left = await box(page, '.onboarding-port', 5);
      const right = await box(page, '.onboarding-port', 4);
      expect(access.x).toBeCloseTo(left.x + left.width / 2, 0);
      expect(access.x + access.width).toBeCloseTo(right.x + right.width / 2, 0);
    });
  }

  /**
   * The one documented exception. Two columns of 640/880 of a phone would be about
   * 200px of tiles inside a 360px screen, so below 600 the block takes the full content
   * width and the tiles stay a readable target — deliberately leaving the wire relation
   * rather than following it into an unusable size.
   */
  test('phones take the full content width instead, on purpose', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await serveProduct(page);
    await openOnboarding(page);
    const access = await box(page, '.onboarding-access');
    const welcome = await box(page, '.onboarding-welcome');
    expect(round(access.width)).toBe(round(welcome.width));
  });

  test('access tiles drop to two columns on phones and keep full labels', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await serveProduct(page);
    await openOnboarding(page);
    const tiles = page.locator('.onboarding-access-tile');
    await expect(tiles).toHaveCount(6);
    const rects = await tiles.evaluateAll((nodes) => nodes.map((node) => node.getBoundingClientRect().top));
    expect(new Set(rects.map((top) => Math.round(top))).size).toBe(3);
    const labels = await tiles.evaluateAll((nodes) => nodes.map((node) => node.scrollWidth <= node.clientWidth + 1));
    expect(labels).toEqual([true, true, true, true, true, true]);
  });
});

/**
 * Narrow widths are where a card stops being a scaled copy of the design and becomes an
 * adaptation, so the thing to check is that the adaptation is ONE arrangement rather
 * than whichever one each label's length happens to produce. Every assertion here reads
 * real bounding boxes: three status rows that start and end together, three identities
 * that do, and no label clipped or truncated to make that true.
 */
test.describe('narrow identity alignment', () => {
  for (const viewport of [{ width: 320, height: 568 }, { width: 390, height: 844 }, { width: 768, height: 1024 }]) {
    for (const lang of ['en', 'zh'] as const) {
      test(`${size(viewport)} ${lang} keeps the three cards on one grid`, async ({ page }, info) => {
        await page.setViewportSize(viewport);
        const denied = await serveProduct(page);
        await openOnboarding(page, { lang });
        await freezeAt(page, PHASES['codex-working']);

        const statuses = await boxes(page, '.onboarding-story-status');
        const identities = await boxes(page, '.onboarding-story-identity');
        expect([statuses.length, identities.length]).toEqual([3, 3]);
        // The header row and the separator above the footer are the two lines the eye
        // reads across the three cards, so both have to be one line, not three.
        expect(spread(statuses.map((one) => one.y))).toBeLessThanOrEqual(1);
        expect(spread(statuses.map((one) => one.height))).toBeLessThanOrEqual(1);
        expect(spread(identities.map((one) => one.y))).toBeLessThanOrEqual(1);
        expect(spread(identities.map((one) => one.height))).toBeLessThanOrEqual(1);

        // Alignment must not have been bought by cutting the labels: every name, role
        // and caption is fully inside its own box, with no ellipsis doing the fitting.
        const clipped = await page.locator(
          '.onboarding-story-identity strong, .onboarding-story-identity > span:last-child, .onboarding-status-text',
        ).evaluateAll((nodes) => nodes
          .filter((node) => {
            const style = getComputedStyle(node);
            return node.scrollWidth > node.clientWidth + 1
              || node.scrollHeight > node.clientHeight + 1
              || style.textOverflow === 'ellipsis';
          })
          .map((node) => node.textContent?.trim() ?? ''));
        expect(clipped).toEqual([]);
        // …and the labels are still the product's own words at a readable size.
        const names = await page.locator('.onboarding-story-identity strong').allInnerTexts();
        expect(names.map((name) => name.replace(/\s+/g, ' ').trim())).toEqual(['Claude Code', 'Codex', 'OpenCode']);
        const smallest = await page.locator('.onboarding-story-identity > span:last-child')
          .evaluateAll((nodes) => Math.min(...nodes.map((node) => parseFloat(getComputedStyle(node).fontSize))));
        expect(smallest).toBeGreaterThanOrEqual(10);

        await settleEffects(page);
        await page.locator('.onboarding-collaboration').screenshot({
          path: info.outputPath(`identity-${size(viewport)}-${lang}.png`),
        });
        expect(denied).toEqual([]);
      });
    }
  }
});

/**
 * The approved design has no playback toolbar, so the story owns its own lifecycle:
 * it starts with the screen, loops, and stops only for the two things the browser tells
 * it about — a reduced-motion preference and a tab that is not being presented.
 */
test.describe('motion lifecycle', () => {
  test.use({ viewport: { width: 1200, height: 800 } });

  const PLAYBACK = /pause|play|resume|replay|restart|暂停|播放|重播|重新播放/i;

  test('ships no playback control on either step', async ({ page }) => {
    await serveProduct(page);
    await openOnboarding(page);
    await freezeAt(page, PHASES['tests-running']);

    // Nothing inside the diagram is operable at all — not a button, not a control that
    // merely looks like text, and not a reserved slot waiting for one.
    await expect(page.locator('.onboarding-story button, .onboarding-story [role="button"], .onboarding-story input')).toHaveCount(0);
    const named = async () => (await page.locator('button, [role="button"], input, a').evaluateAll((nodes) => nodes
      .map((node) => `${node.getAttribute('aria-label') ?? ''} ${node.textContent ?? ''} ${node.getAttribute('title') ?? ''}`)))
      .filter((label) => PLAYBACK.test(label));
    expect(await named()).toEqual([]);
    await openSetup(page, 'en');
    expect(await named()).toEqual([]);
  });

  /**
   * `page.clock` controls the timers the cards are read from; it does not control the
   * document timeline the shimmer and the test ticks run on. So a suspension that only
   * stopped the React clock would leave those playing on unseen and reappearing
   * mid-sweep — which is why this asserts the RENDERED animation state, not the
   * attribute that requests it.
   */
  test('a hidden tab freezes what is drawn, and resuming does not skip', async ({ page }) => {
    await serveProduct(page);
    await openOnboarding(page);
    await freezeAt(page, PHASES['codex-working']);
    const before = await renderedPhase(page);
    expect(await cssEffects(page)).not.toEqual([]);

    await setDocumentHidden(page, true);
    await expect(page.locator('.onboarding-collaboration')).toHaveAttribute('data-motion', 'paused');
    const stopped = await cssEffects(page);
    expect(stopped.every((effect) => effect.state === 'paused')).toBe(true);

    // Time passes while the tab is in the background, and nothing moves.
    await page.clock.runFor(3000);
    await page.waitForTimeout(250);
    expect(await cssEffects(page)).toEqual(stopped);
    expect(await renderedPhase(page)).toEqual(before);

    await setDocumentHidden(page, false);
    await expect(page.locator('.onboarding-collaboration')).toHaveAttribute('data-motion', 'running');
    // Resuming continues the same sweep: the phase is where it was left, not where
    // wall-clock time had carried it, and the CSS effects pick up their own timelines.
    expect(await renderedPhase(page)).toEqual(before);
    const resumed = await cssEffects(page);
    expect(resumed.some((effect) => effect.state === 'running')).toBe(true);

    // And it is genuinely running again, rather than merely unpaused.
    await freezeAt(page, PHASES['pm-summary'] - PHASES['codex-working']);
    expect(await renderedPhase(page)).not.toEqual(before);
  });

  test('a reduced-motion preference draws the settled story and no pulse', async ({ page }, info) => {
    await serveProduct(page);
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await openOnboarding(page);
    await expect(page.getByTestId('handoff-pulse')).toHaveCount(0);
    expect(await cssEffects(page)).toEqual([]);
    await page.screenshot({ path: info.outputPath('reduced-motion.png') });
  });
});

/**
 * The onboarding is the first screen a new install shows, so it is also the first place
 * the theme has to behave: follow the system when nothing was chosen, and keep an
 * explicit choice when the system disagrees with it.
 */
test.describe('theme', () => {
  test.use({ viewport: { width: 1200, height: 800 } });

  const themeState = (page: Page) => page.evaluate(() => ({
    attribute: document.documentElement.getAttribute('data-theme'),
    background: getComputedStyle(document.documentElement).getPropertyValue('--background').trim(),
  }));

  test('follows the system when no preference has been made', async ({ page }) => {
    await serveProduct(page);
    await page.emulateMedia({ colorScheme: 'dark' });
    await openOnboarding(page, { theme: null });
    const dark = await themeState(page);
    // A fresh install starts in system mode, which sets no attribute at all: the
    // stylesheet's own media query answers, and that is what lets the OS switch through
    // without a reload.
    expect(dark.attribute).toBeNull();

    await page.emulateMedia({ colorScheme: 'light' });
    const light = await themeState(page);
    expect(light.attribute).toBeNull();
    expect(light.background).not.toBe(dark.background);
  });

  test('keeps a stored preference when the system disagrees', async ({ page }) => {
    await serveProduct(page);
    await storeThemePreference(page, 'light');
    await page.emulateMedia({ colorScheme: 'dark' });
    await openOnboarding(page, { theme: null });
    const chosen = await themeState(page);
    expect(chosen.attribute).toBe('light');

    // The OS moving underneath a made choice must not take it away, in either direction.
    await page.emulateMedia({ colorScheme: 'light' });
    expect(await themeState(page)).toEqual(chosen);
    await page.emulateMedia({ colorScheme: 'dark' });
    expect(await themeState(page)).toEqual(chosen);
    await page.reload();
    await page.locator('.onboarding-collaboration').waitFor();
    expect((await themeState(page)).attribute).toBe('light');
  });

  /**
   * The active card's halo is the one effect the reference specifies per theme: a
   * spread-less 28px mint in dark, and light's own tighter 16/-4. It is asserted on the
   * rendered `box-shadow` because the token behind it is substituted into its utilities
   * at build time — reading the custom property would pass on a theme override that
   * compiles to nothing.
   */
  for (const [theme, geometry] of [['dark', '0px 0px 28px 0px'], ['light', '0px 0px 16px -4px']] as const) {
    test(`${theme} draws its own active-card halo, and idle cards none`, async ({ page }) => {
      await serveProduct(page);
      await openOnboarding(page, { theme });
      await freezeAt(page, PHASES['codex-working']);
      // The card fades its halo in over 650ms, so reading mid-transition measures the
      // interpolation rather than the design's value. Settling first asks for the value
      // the card actually arrives at.
      await settleEffects(page);

      const shadows = await page.locator('.onboarding-collaboration-card').evaluateAll((nodes) => nodes
        .map((node) => ({ active: node.getAttribute('data-active'), shadow: getComputedStyle(node).boxShadow })));
      const active = shadows.filter((card) => card.active === 'true');
      expect(active).toHaveLength(1);
      expect(active[0].shadow).toContain(geometry);
      // The glow is what says which card is working, so it cannot also be on the others.
      for (const card of shadows.filter((one) => one.active !== 'true')) expect(card.shadow).toBe('none');
    });
  }
});

test.describe('capture', () => {
  for (const viewport of VIEWPORTS) {
    for (const theme of ['dark', 'light'] as const) {
      for (const lang of ['en', 'zh'] as const) {
        // Settled stills: every CSS effect is held past its end, so two runs of the same
        // phase produce the same image and it can be compared against a static frame.
        test(`${size(viewport)} ${theme} ${lang}`, async ({ page }, info) => {
          await page.setViewportSize(viewport);
          await serveProduct(page);
          await openOnboarding(page, { lang, theme });
          await freezeAt(page, PHASES['codex-working']);
          await settleEffects(page);
          await page.screenshot({ path: info.outputPath(`welcome-${size(viewport)}-${theme}-${lang}.png`), fullPage: true });
          await openSetup(page, lang);
          await settleEffects(page);
          await page.screenshot({ path: info.outputPath(`setup-${size(viewport)}-${theme}-${lang}.png`), fullPage: true });
        });
      }
    }
  }

  // The design's own content box, for overlaying an exported frame at 1:1.
  test('design frame content box', async ({ page }, info) => {
    await page.setViewportSize(DESIGN_FRAME);
    await serveProduct(page);
    await openOnboarding(page);
    await freezeAt(page, PHASES['codex-working']);
    await settleEffects(page);
    await page.screenshot({ path: info.outputPath('welcome-design-frame-1200x756.png') });
    await openSetup(page, 'en');
    await settleEffects(page);
    await page.screenshot({ path: info.outputPath('setup-design-frame-1200x756.png') });
  });
});
