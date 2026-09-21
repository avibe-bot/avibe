import { expect, test, type Page } from '@playwright/test';
import {
  PHASES, VIEWPORTS, cssEffects, freezeAt, openOnboarding, openSetup, renderedPhase,
  serveModelHub, serveProduct, setDocumentHidden, settleEffects, size, storeThemePreference,
} from './support';

/**
 * The design is a set of exact relations, so this suite measures the shipped component in
 * a browser instead of asserting that nothing overflows. Screenshots are written as
 * artifacts from the same runs that make the assertions, so the evidence and the guard can
 * never describe two different builds.
 *
 * Sizes: a Playwright viewport IS the web content box, so nothing is subtracted for
 * chrome. The design's own 1200x800 frames include the native 44px titlebar above the web
 * view, which makes the design's content box 1200x756 — that is the size to overlay a
 * frame against (DESIGN_FRAME below).
 *
 * What is asserted is the RULE, not a row of copied numbers. The composition is one
 * content column whose width is `clamp(976px, 62.5vw, 1200px)` inside the shell's gutter,
 * and one card height taken from the window; everything else is a ratio of those two. So
 * the helpers below re-derive the column and the card from what the browser reports, and
 * the tests check that every dependent box is the share of them the design draws. A test
 * that hard-coded 976 and 299 would pass on a layout that had stopped deriving them.
 */
const DESIGN_FRAME = { width: 1200, height: 756 };
/**
 * The one moment every still is taken at, carried into the filename so an artifact says
 * which phase it is rather than leaving a reader to infer it. Since `openOnboarding`
 * pauses the clock before the page loads, this is exact and not merely settled: the same
 * phase draws the same cards across themes, languages and sizes, which is what makes a
 * dark/light pair of one size a content A/B rather than two unrelated frames.
 */
const CAPTURE_PHASE = 'codex-working';
const CAPTURE_STATES = ['complete', 'working', 'waiting'];

/** The card's share of the content column: three 299s and two 39.5 gaps in 976, which is
 *  the same fraction as three 367s in 1200. One percentage, both authored rows. */
const CARD_SHARE = 0.306352;
/** The wire box is the card times the reference's own 350/300, so the handoffs stay on
 *  the cards' midline at every tier. */
const WIRE_RATIO = 350 / 300;
/** What the stage reserves beyond the card, spent by the introduction on the return band
 *  and by the connection on the 20 + 44 import capsule. */
const STAGE_EXTRA = 64;

const box = async (page: Page, selector: string, index = 0) => {
  const rect = await page.locator(selector).filter({ visible: true }).nth(index).boundingBox();
  if (!rect) throw new Error(`${selector} is not rendered`);
  return rect;
};

/** Every match's real rect in one round trip — what "aligned" has to be measured from. */
const boxes = (page: Page, selector: string) =>
  page.locator(selector).filter({ visible: true }).evaluateAll((nodes) => nodes.map((node) => {
    const rect = node.getBoundingClientRect();
    return { x: rect.x, y: rect.y, width: rect.width, height: rect.height };
  }));

/**
 * Nothing may sit between a person and the action pair: the centre of each button has to
 * hit the button itself, or its own label. An ancillary caption, capsule or overlay that
 * drifts over the footer intercepts the click long before any box assertion notices, so
 * the anchor fence checks pointer ownership on every screen it walks. An ancestor under
 * the point is not ownership — a covering element's parent is exactly that.
 */
const hitSelf = async (page: Page, selector: string) => {
  const locator = page.locator(selector);
  // On the narrow tiers the pair sits well below the fold, and a point outside the
  // viewport belongs to no element at all; bring it into view first so what comes back
  // is an answer about the layout rather than about the scroll position.
  await locator.scrollIntoViewIfNeeded();
  return locator.evaluate((node) => {
    const rect = node.getBoundingClientRect();
    const hit = document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2);
    return !!hit && (node === hit || node.contains(hit));
  });
};

const round = (value: number) => Math.round(value * 100) / 100;
const spread = (values: number[]) => Math.max(...values) - Math.min(...values);

/**
 * Returns whatever is scrolled to the top, so a measurement is of the LAYOUT rather than of
 * where a click left the view. A phone composition is taller than the screen, and reaching
 * the button that advances the step carries the page down with it; comparing two steps'
 * viewport coordinates across that would compare positions that never coexisted.
 */
const toTop = (page: Page) => page.evaluate(() => {
  document.scrollingElement?.scrollTo(0, 0);
  for (const node of document.querySelectorAll<HTMLElement>('*')) if (node.scrollTop) node.scrollTop = 0;
});

/**
 * The two numbers the whole composition is derived from, read from the page rather than
 * from the requested viewport: `vw` and `dvh` are the window including its scrollbar,
 * which is not always the size a test asked for, and the gutter is a band the stylesheet
 * chooses. Deriving the expectation from these is what makes the checks below a test of
 * the RULE at whatever size the browser actually produced.
 */
async function frame(page: Page) {
  const window = await page.evaluate(() => {
    const shell = document.querySelector('.onboarding-shell') as HTMLElement;
    const content = document.querySelector('.onboarding-shell-content') as HTMLElement;
    // The column the composition is capped to, read from the step itself: the
    // content box's own rect includes its gutters, which are not the column.
    const step = document.querySelector('.onboarding-welcome, .onboarding-setup') as HTMLElement | null;
    return {
      vw: globalThis.innerWidth,
      vh: globalThis.innerHeight,
      gutter: parseFloat(getComputedStyle(shell).paddingLeft),
      available: step ? step.getBoundingClientRect().width : content.getBoundingClientRect().width,
    };
  });
  const clamp = Math.min(Math.max(976, 0.625 * window.vw), 1200);
  // The card height is the tier the design authored for this window: 232 under an
  // 800px-tall one, 262 under 900, 300 above 1000, 330 at the large 1920x1080 reading.
  // Stacked, the three cards ARE the composition, so the phone band gives them the
  // height the reference draws rather than a share of a window that should scroll.
  const stacked = window.vw < 760;
  const card = stacked ? 258
    : window.vw >= 1600 && window.vh >= 950 ? 330
      : window.vh <= 800 ? 232
        : window.vh <= 1000 ? 262 : 300;
  return {
    ...window,
    column: Math.min(clamp, window.available),
    card,
    stacked,
  };
}

test.describe('desktop reference geometry', () => {
  test.use({ viewport: { width: 1200, height: 800 } });

  test('the welcome draws the content column, its three card tracks and their handoff gaps', async ({ page }, info) => {
    const denied = await serveProduct(page);
    await openOnboarding(page);
    await freezeAt(page, PHASES['codex-working']);
    const reference = await frame(page);
    // The reference's own numbers, reached by the rule at its own frame size: a 976
    // column of three 299 cards with 39.5 between them. Stated once, here, so a reader
    // can see that the ratios below are the design and not an arbitrary proportion.
    expect(round(reference.column)).toBe(976);
    expect(round(reference.card)).toBe(232);

    const welcome = await box(page, '.onboarding-welcome');
    expect(welcome.width).toBeCloseTo(reference.column, 1);

    const cards = await boxes(page, '.onboarding-collaboration-card');
    expect(cards).toHaveLength(3);
    for (const card of cards) {
      expect(card.width).toBeCloseTo(reference.column * CARD_SHARE, 1);
      expect(card.height).toBeCloseTo(reference.card, 1);
    }
    const gaps = [1, 2].map((index) => cards[index].x - (cards[index - 1].x + cards[index - 1].width));
    for (const gap of gaps) expect(gap).toBeCloseTo((reference.column - 3 * cards[0].width) / 2, 1);
    // 39.5, to the tenth: the track is a percentage, so the browser's own rounding of it
    // lands a hundredth away and a stricter claim would only be testing that.
    expect(gaps[0]).toBeCloseTo(39.5, 1);

    // The diagram is the card plus the return band the stage floors, and the wire box
    // inside it takes its own height from the card — which is what keeps the handoff
    // wire on the cards' midline at every tier rather than only at the authored one.
    const collaboration = await box(page, '.onboarding-collaboration');
    expect(collaboration.width).toBeCloseTo(reference.column, 1);
    expect(collaboration.height).toBeCloseTo(reference.card + STAGE_EXTRA, 1);
    const wires = await box(page, '.onboarding-wires');
    expect(wires.height).toBeCloseTo(reference.card * WIRE_RATIO, 1);

    // What has to be exact is that the wire endpoints sit on the card edges: the stretched
    // viewBox and the card grid answer to the same width, so a port cannot drift off. To
    // the pixel — a card sized by a percentage has edges on halves, so a tighter claim
    // would be about which way the browser rounded rather than about where the wire is.
    const ports = await boxes(page, '.onboarding-port');
    expect(ports).toHaveLength(4);
    const onPixel = (value: number, target: number) => expect(Math.abs(value - target)).toBeLessThanOrEqual(1);
    onPixel(ports[0].x + ports[0].width / 2, cards[0].x + cards[0].width);
    onPixel(ports[1].x + ports[1].width / 2, cards[1].x);
    onPixel(ports[2].x + ports[2].width / 2, cards[1].x + cards[1].width);
    onPixel(ports[3].x + ports[3].width / 2, cards[2].x);
    // The story's caption is authored per tier like the rest of the card's type —
    // 12 on the standard desktops, 15 at the large reading — so a fixed 10 that
    // reads small against the frame is caught here.
    const capSize = await page.locator('.onboarding-story-status')
      .first().evaluate((node) => parseFloat(getComputedStyle(node).fontSize));
    expect(capSize).toBeCloseTo(reference.card >= 300 ? 15 : 12, 0);

    // The identity header's mark takes its tier's authored size — 28 under a 900px
    // window and below, 32 at 1001+, 35 at the large reading — including the nested
    // span `BackendIcon` sizes inline, or the visible mark stays at one tier while
    // its slot grows.
    const logos = await boxes(page, '.onboarding-card-logo > span');
    const logoSize = reference.card >= 330 ? 35 : reference.card >= 300 ? 32 : 28;
    for (const logo of logos) {
      expect(logo.width).toBeCloseTo(logoSize, 0);
      expect(logo.height).toBeCloseTo(logoSize, 0);
    }

    // In the band that still draws three columns on a narrow window, the identity
    // row has less room than its logo, switch and label want; the name yields
    // before anything spills through the card edge.
    if (reference.column <= 768 + 1) {
      for (let index = 0; index < cards.length; index += 1) {
        const name = (await boxes(page, '.onboarding-card-name'))[index];
        const enable = (await boxes(page, '.onboarding-assistant-enable'))[index];
        expect(name.x).toBeGreaterThanOrEqual(cards[index].x - 0.5);
        expect(enable.x + enable.width).toBeLessThanOrEqual(cards[index].x + cards[index].width + 0.5);
      }
    }

    // And the handoff runs along the cards' shared midline.
    const midline = cards[0].y + reference.card / 2;
    for (const port of ports) onPixel(port.y + port.height / 2, midline);

    await settleEffects(page);
    await page.screenshot({ path: info.outputPath('welcome-1200x800-dark-en.png') });
    expect(denied).toEqual([]);
  });

  /**
   * The reference stacks both steps with one rhythm — heading, 20, stage, 20, action — and
   * reserves the stage at the card plus 64 in both. That reservation is the whole reason
   * the two steps' buttons land on the same coordinates, so it is measured directly rather
   * than inferred from the two steps agreeing.
   */
  test('the welcome spends its column on the reference rhythm', async ({ page }) => {
    const denied = await serveProduct(page);
    await openOnboarding(page);
    await freezeAt(page, PHASES['codex-working']);
    const reference = await frame(page);

    const heading = await box(page, '.onboarding-heading');
    const stage = await box(page, '.onboarding-stage');
    const action = await box(page, '.onboarding-primary-action');
    // The heading block carries its tier's own distance to the stage — 15 under an
    // 800px window — while the stage's distance to the action is 20 in every row.
    const headingGap = await page.evaluate(() =>
      parseFloat(getComputedStyle(document.querySelector('[data-setup-screen-root]:not([hidden]) .onboarding-heading')!).marginBottom));
    expect(round(headingGap)).toBe(15);
    expect(stage.y - (heading.y + heading.height)).toBeCloseTo(headingGap, 0);
    expect(action.y - (stage.y + stage.height)).toBeCloseTo(20, 0);
    expect(stage.height).toBeCloseTo(reference.card + STAGE_EXTRA, 1);

    // The entry block is collapsed throughout setup (owner handoff, design boards and
    // prototype agree), so the rhythm under the action ends at the reserved back row.
    const back = await page.locator('.onboarding-back-action').evaluate((node) => node.getBoundingClientRect().top);
    expect(back).toBeGreaterThan(action.y + action.height);
    expect(denied).toEqual([]);
  });

  /**
   * "Align Get started and Enter Workbench at identical coordinates and dimensions,
   * centered under the Codex column on desktop and as wide as its card."
   *
   * Three claims, each measured: the action is the middle card's column, the cards are the
   * same boxes in both steps, and the action does not move between them. The last one is
   * what a person actually feels — a button that shifts under a pointer when the screen
   * changes — so it is asserted as exact box equality, not as a tolerance.
   */
  test('both steps put the same action under the same middle card', async ({ page }, info) => {
    const denied = await serveProduct(page);
    await openOnboarding(page);
    await freezeAt(page, PHASES['codex-working']);

    const introCards = await boxes(page, '.onboarding-collaboration-card');
    const introIdentities = await boxes(page, '.onboarding-card-identity');
    const introAction = await box(page, '.onboarding-primary-action');
    const introHeader = await box(page, '.onboarding-shell > header');
    // Centred on the middle card, and exactly its width.
    expect(introAction.x).toBeCloseTo(introCards[1].x, 1);
    expect(introAction.width).toBeCloseTo(introCards[1].width, 1);
    expect(round(introAction.height)).toBe(52);
    // The introduction's trailing slot is the assistant's role.
    await expect(page.locator('.onboarding-card-role')).toHaveCount(3);

    await openSetup(page, 'en');
    const setupCards = await boxes(page, '.onboarding-assistant');
    const setupIdentities = await boxes(page, '.onboarding-card-identity');
    const setupAction = await box(page, '.onboarding-primary-action');
    const setupHeader = await box(page, '.onboarding-shell > header');

    // The top bar does not move, and neither does the action.
    expect(setupHeader).toEqual(introHeader);
    for (const key of ['x', 'y', 'width', 'height'] as const) {
      expect(round(setupAction[key])).toBeCloseTo(round(introAction[key]), 1);
    }
    // Same three columns, so the cards read as taking on work rather than being replaced.
    for (let index = 0; index < 3; index += 1) {
      expect(setupCards[index].x).toBeCloseTo(introCards[index].x, 1);
      expect(setupCards[index].width).toBeCloseTo(introCards[index].width, 1);
      expect(setupCards[index].y).toBeCloseTo(introCards[index].y, 1);
      // The connection's card may grow for an error or a wrapped action; it may not shrink.
      expect(setupCards[index].height).toBeGreaterThanOrEqual(introCards[index].height - 1);
      // "The assistant logo and name stay at the top in both states; connection state
      // replaces roles with enable switches" — the identity header, same offset into the
      // same card, in both steps.
      expect(setupIdentities[index].y - setupCards[index].y)
        .toBeCloseTo(introIdentities[index].y - introCards[index].y, 1);
      expect(setupIdentities[index].x).toBeCloseTo(introIdentities[index].x, 1);
    }
    // …and the trailing slot is now the enable switch, in the header rather than below it.
    await expect(page.locator('.onboarding-card-identity .onboarding-assistant-enable')).toHaveCount(3);
    expect(spread(setupIdentities.map((one) => one.y))).toBeLessThanOrEqual(1);

    // The way back is the same object one row down, per the reference.
    const back = await box(page, '.onboarding-back-action');
    expect(back.x).toBeCloseTo(setupAction.x, 1);
    expect(back.width).toBeCloseTo(setupAction.width, 1);

    await settleEffects(page);
    await page.screenshot({ path: info.outputPath('setup-1200x800-dark-en.png') });
    expect(denied).toEqual([]);
  });

  /**
   * A short window used to buy about 30px by taking the step gaps down and squeezing the
   * access block — spending the design's own spacing at the reference's own frame size,
   * where nothing was wrong. Whitespace IS the composition, so the card answers to the
   * window height instead and anything left over scrolls.
   */
  test('a short window shortens the card rather than the composition', async ({ page }) => {
    await serveProduct(page);
    await openOnboarding(page);
    const metrics = () => page.evaluate(() => {
      const read = (selector: string) => {
        const node = document.querySelector(selector);
        const style = node ? getComputedStyle(node) : null;
        return style ? `${style.fontSize}/${style.lineHeight}` : 'missing';
      };
      return [read('.onboarding-heading :is(h1, h2)'), read('.onboarding-heading p'),
        read('.onboarding-access-tile span'), read('.onboarding-primary-action')];
    });
    const tall = await metrics();

    await page.setViewportSize(DESIGN_FRAME);
    await freezeAt(page, PHASES['codex-working']);
    // Type never pays for a short window.
    expect(await metrics()).toEqual(tall);
    const reference = await frame(page);
    const heading = await box(page, '.onboarding-heading');
    const stage = await box(page, '.onboarding-stage');
    const action = await box(page, '.onboarding-primary-action');
    const headingGap = await page.evaluate(() =>
      parseFloat(getComputedStyle(document.querySelector('[data-setup-screen-root]:not([hidden]) .onboarding-heading')!).marginBottom));
    expect(stage.y - (heading.y + heading.height)).toBeCloseTo(headingGap, 0);
    expect(action.y - (stage.y + stage.height)).toBeCloseTo(20, 0);
    // The card, and the whole composition with it, follows the window's tier: the
    // 800px-tall reading draws a 232 card, not a share of whatever is left.
    const card = await box(page, '.onboarding-collaboration-card');
    expect(card.height).toBeCloseTo(reference.card, 1);
    expect(round(card.height)).toBe(232);

    // And at the reference's own frame the composition then fits it exactly: the card
    // paying for the window is what removes the scroll, not a shorter gap or smaller type.
    const overflows = () => page.evaluate(() =>
      (document.querySelector('.onboarding-shell') as HTMLElement).scrollHeight > globalThis.innerHeight);
    expect(await overflows()).toBe(false);

    // Shorter than the frame it was drawn at, the card keeps paying and the leftover
    // scrolls — the spacing and the type are never the budget, at any height.
    await page.setViewportSize({ width: DESIGN_FRAME.width, height: 520 });
    await freezeAt(page, 0);
    expect(await metrics()).toEqual(tall);
    const short = { heading: await box(page, '.onboarding-heading'), stage: await box(page, '.onboarding-stage') };
    const shortAction = await box(page, '.onboarding-primary-action');
    expect(short.stage.y - (short.heading.y + short.heading.height)).toBeCloseTo(headingGap, 0);
    expect(shortAction.y - (short.stage.y + short.stage.height)).toBeCloseTo(20, 0);
    // The tier, not the leftover: a 520px window still draws the 800-row's card and
    // scrolls the rest.
    expect(round((await box(page, '.onboarding-collaboration-card')).height)).toBe(232);
    expect(await overflows()).toBe(true);
  });

  /**
   * The reference's headline rows are authored per tier, not interpolated: 32 at
   * 1366x768 and 1200x800, 34 at 1440x900, 50 at 1920x1080 and 30 on a phone, each with
   * its own line height and its own distance to the stage. Both steps read the same
   * tier, so both are checked at the design's own viewports.
   */
  for (const [viewport, size] of [
    [{ width: 1920, height: 1080 }, 50],
    [{ width: 1440, height: 900 }, 34],
    [{ width: 1366, height: 768 }, 32],
    [{ width: 1200, height: 800 }, 32],
    [{ width: 390, height: 844 }, 30],
  ] as const) {
    test(`the headline is the tier's authored size at ${viewport.width}x${viewport.height}`, async ({ page }) => {
      await page.setViewportSize(viewport);
      await serveProduct(page);
      await openOnboarding(page);
      const type = () => page.evaluate(() => {
        const node = document.querySelector('[data-setup-screen-root]:not([hidden]) .onboarding-heading :is(h1, h2)')!;
        const style = getComputedStyle(node);
        return {
          size: parseFloat(style.fontSize),
          tracking: parseFloat(style.letterSpacing),
          weight: style.fontWeight,
        };
      });
      const intro = await type();
      expect(intro.size).toBeCloseTo(size, 1);
      expect(intro.tracking / intro.size).toBeCloseTo(-0.035, 3);
      expect(intro.weight).toBe('600');
      await openSetup(page, 'en');
      expect(await type()).toEqual(intro);
    });
  }

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
      const reference = await frame(page);

      expect(await page.evaluate(() => document.documentElement.scrollWidth <= globalThis.innerWidth)).toBe(true);
      // Capped fluid: the rule's own width, never wider than the window's content box.
      const welcome = await box(page, '.onboarding-welcome');
      expect(welcome.width).toBeCloseTo(reference.column, 1);
      expect(welcome.width).toBeLessThanOrEqual(reference.available + 0.5);

      const cards = await boxes(page, '.onboarding-collaboration-card');
      if (reference.stacked) {
        // Stacked: one column, and each card the height the reference draws.
        expect(spread(cards.map((card) => card.x))).toBeLessThanOrEqual(1);
        for (const card of cards) expect(card.height).toBeGreaterThanOrEqual(reference.card - 1);
      } else {
        const collaboration = await box(page, '.onboarding-collaboration');
        for (const card of cards) {
          expect(card.width).toBeCloseTo(reference.column * CARD_SHARE, 1);
          expect(card.height).toBeCloseTo(reference.card, 1);
        }
        // The stretched wire box always covers the cards, so no pulse can detach, and
        // its height is the card's own share, which keeps the handoffs on the midline.
        const wires = await box(page, '.onboarding-wires');
        expect(round(wires.width)).toBe(round(collaboration.width));
        expect(wires.height).toBeCloseTo(reference.card * WIRE_RATIO, 1);
        expect(collaboration.height).toBeCloseTo(reference.card + STAGE_EXTRA, 1);
      }

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

});

/**
 * The reservation, checked where it is hardest rather than only where it was designed.
 * The action's coordinates are shared by the two steps at every accepted size and in both
 * languages, which is a claim about the HEADING and the STAGE holding their heights when
 * the copy, the step and the locale all change under them. A screen-sized tolerance would
 * make it vacuous, so this is exact box equality.
 */
/**
 * The six entry tiles belong to the reached worksurface, not to the setup journey: the
 * owner handoff, the design boards and the prototype collapse the block for every setup
 * screen. What stays true here is the property — the block remains mounted (its motion
 * lifecycle is a mounted component's), hidden and inert, leaking nothing focusable, and
 * the reserved action anchor does not depend on it at all.
 */
test('setup keeps the entry block mounted, hidden and inert, and the anchor independent of it', async ({ page }) => {
  for (const viewport of [{ width: 1200, height: 800 }, { width: 390, height: 844 }]) {
    await page.setViewportSize(viewport);
    const denied = await serveProduct(page);
    await openOnboarding(page);
    const block = page.locator('.onboarding-access');
    await expect(block).toHaveCount(1);
    await expect(block).toBeHidden();
    expect(await block.evaluate((node) => node.hasAttribute('inert') && getComputedStyle(node).display === 'none')).toBe(true);
    expect(await block.locator('.onboarding-access-tile').count()).toBe(6);
    // Mounted but inert: nothing inside the block can take focus.
    expect(await block.evaluate((node) => {
      (node.querySelector('.onboarding-access-tile') as HTMLElement).focus();
      return node.contains(document.activeElement);
    })).toBe(false);
    // Revealing the block changes what is below the anchor, never the anchor itself.
    // The back row reserves its box through visibility on the first screen, so it is
    // measured rather than filtered for visibility.
    const rects = () => page.evaluate(() => {
      const read = (selector: string) => {
        const rect = document.querySelector(selector)!.getBoundingClientRect();
        return { x: rect.x, y: rect.y, width: rect.width, height: rect.height };
      };
      return { primary: read('.onboarding-primary-action'), back: read('.onboarding-back-action') };
    });
    const before = await rects();
    await block.evaluate((node) => { (node as HTMLElement).hidden = false; });
    const after = await rects();
    for (const key of ['x', 'y', 'width', 'height'] as const) {
      expect(round(after.primary[key])).toBeCloseTo(round(before.primary[key]), 1);
      expect(round(after.back[key])).toBeCloseTo(round(before.back[key]), 1);
    }
    expect(denied).toEqual([]);
  }
});

test.describe('shared action anchor', () => {
  for (const viewport of [
    { width: 1920, height: 1080 }, { width: 1366, height: 768 }, { width: 1200, height: 800 },
    { width: 768, height: 1024 }, { width: 390, height: 844 }, { width: 320, height: 568 },
  ]) {
    for (const lang of ['en', 'zh'] as const) {
      test(`${size(viewport)} ${lang} keeps one action pair across every registered screen`, async ({ page }) => {
        await page.setViewportSize(viewport);
        const denied = await serveProduct(page);
        await openOnboarding(page, { lang });
        await toTop(page);
        const heading = await box(page, '.onboarding-heading');
        const title = await box(page, '.onboarding-heading h1');
        const subtitle = await box(page, '.onboarding-heading p');
        const stage = await box(page, '.onboarding-stage');
        const action = await box(page, '.onboarding-primary-action');
        await expect(page.getByText('Avibe', { exact: true })).toBeVisible();

        const backBox = await page.locator('.onboarding-back-action').evaluate((node) => {
          const rect = node.getBoundingClientRect(); return { x: rect.x, y: rect.y, width: rect.width, height: rect.height };
        });
        const sequence = (await page.locator('[data-setup-sequence]').getAttribute('data-setup-sequence'))!.split(' ');
        for (const id of sequence.slice(1)) {
          await page.locator('.onboarding-primary-action').click();
          await page.clock.runFor(950);
          await expect(page.locator('[data-setup-sequence]')).toHaveAttribute('data-setup-screen', id);
          await toTop(page);
          const nextHeading = await box(page, '.onboarding-heading');
          const nextTitle = await box(page, '.onboarding-heading h1');
          const nextSubtitle = await box(page, '.onboarding-heading p');
          const nextStage = await box(page, '.onboarding-stage');
          expect(round(nextHeading.height)).toBeCloseTo(round(heading.height), 1);
          expect(nextTitle.y).toBeCloseTo(title.y, 0);
          expect(nextSubtitle.y).toBeCloseTo(subtitle.y, 0);
          expect(round(nextStage.height)).toBeCloseTo(round(stage.height), 1);
          for (const [selector, original] of [['.onboarding-primary-action', action], ['.onboarding-back-action', backBox]] as const) {
            const next = await box(page, selector);
            for (const key of ['x', 'y', 'width', 'height'] as const) expect(round(next[key])).toBeCloseTo(round(original[key]), 1);
          }
          // The pair is not only where it was, it is what a tap actually reaches.
          expect(await hitSelf(page, '.onboarding-primary-action')).toBe(true);
          expect(await hitSelf(page, '.onboarding-back-action')).toBe(true);
          await expect(page.locator('[data-setup-screen-root]:not([hidden]) h1')).toBeFocused();
          await expect(page.locator('[data-setup-screen-root][hidden]:not([inert])')).toHaveCount(0);
        }
        // The last screen contributes a caption, and a caption is ancillary: it is drawn
        // below the pair it explains. That placement is what lets it grow without moving
        // the anchor, and what keeps it off the button it is describing.
        const caption = await box(page, '.onboarding-setup-hint');
        const backRect = await box(page, '.onboarding-back-action');
        expect(caption.y).toBeGreaterThanOrEqual(backRect.y + backRect.height - 1);
        await page.locator('.onboarding-primary-action').scrollIntoViewIfNeeded();
        await expect(page.locator('.onboarding-primary-action')).toBeInViewport();
        // And the last word on reachability is a real click, which Playwright refuses to
        // deliver when something else would receive it. The caption's own controls have
        // to stay live too: it is an aside, not a decoration.
        await page.locator('.onboarding-setup-hint').getByRole('button').first().click();
        await page.locator('.onboarding-back-action').click();
        await expect(page.locator('[data-setup-sequence]')).toHaveAttribute('data-setup-screen', sequence[0]);
        expect(denied).toEqual([]);
      });
    }
  }
});

/**
 * The import offer. Everything about WHICH keys it counts is a component-level rule and is
 * tested there; what only a browser can show is that the capsule is an aside rather than a
 * banner, that refusing it does not move the button underneath, and that its help text can
 * actually be reached by a pointer, a keyboard and a finger.
 */
test.describe('import capsule', () => {
  test('is a centred capsule whose dismissal leaves the action where it was', async ({ page }, info) => {
    await page.setViewportSize({ width: 1200, height: 800 });
    const denied = await serveProduct(page);
    await serveModelHub(page);
    await openOnboarding(page, { lang: 'zh' });
    await openSetup(page, 'zh');

    const notice = page.locator('.onboarding-import-notice');
    // Three importable keys among five scanned rows: the two native subscription tokens
    // are the settings migration's business, never this entry's.
    await expect(notice).toHaveText(/发现 3 个可导入模型网关的 API Key/);

    const capsule = await box(page, '.onboarding-import-notice');
    const cards = await box(page, '.onboarding-assistants');
    const action = await box(page, '.onboarding-primary-action');
    expect(round(capsule.height)).toBe(44);
    // Content width, centred under the cards — not a full-width banner.
    expect(capsule.width).toBeLessThan(cards.width);
    expect(capsule.x + capsule.width / 2).toBeCloseTo(cards.x + cards.width / 2, 0);
    expect(capsule.y - (cards.y + cards.height)).toBeCloseTo(20, 0);
    expect(action.y - (capsule.y + capsule.height)).toBeCloseTo(20, 0);
    expect(await page.locator('.onboarding-import-notice').evaluate((node) => getComputedStyle(node).boxShadow)).toBe('none');

    await settleEffects(page);
    await page.screenshot({ path: info.outputPath('import-capsule-1200x800-dark-zh.png') });

    // The layout space is reserved, so refusing the offer moves nothing.
    await page.getByRole('button', { name: '关闭导入提示' }).click();
    await expect(notice).toHaveCount(0);
    const after = await box(page, '.onboarding-primary-action');
    expect(round(after.y)).toBe(round(action.y));
    expect(round(after.x)).toBe(round(action.x));
    expect(denied).toEqual([]);
  });

  test('phones put the offer and its refusal on one line and the actions on the next', async ({ page }, info) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await serveProduct(page);
    await serveModelHub(page);
    await openOnboarding(page, { lang: 'zh' });
    await openSetup(page, 'zh');

    const text = await box(page, '.onboarding-import-notice-text');
    const dismiss = await box(page, '.onboarding-import-notice-dismiss');
    const actions = await box(page, '.onboarding-import-notice-actions');
    expect(dismiss.y).toBeCloseTo(text.y + text.height / 2 - dismiss.height / 2, 0);
    expect(actions.y).toBeGreaterThanOrEqual(text.y + text.height);
    // The full Chinese sentence at the verified width: wrapped if it must be, never cut.
    const intact = await page.locator('.onboarding-import-notice-text')
      .evaluate((node) => node.scrollWidth <= node.clientWidth + 1 && getComputedStyle(node).textOverflow !== 'ellipsis');
    expect(intact).toBe(true);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= globalThis.innerWidth)).toBe(true);
    await settleEffects(page);
    await page.screenshot({ path: info.outputPath('import-capsule-390x844-dark-zh.png') });
  });

  // A real touch context, because the whole point of the hint's pointer handling is that
  // a finger and a mouse arrive differently: Chromium only reports `pointerType: 'touch'`
  // when the context actually has a touchscreen, so a synthetic event could not show that
  // a tap produces one clean toggle instead of hover-open-then-click-closed.
  test.describe(() => {
    test.use({ hasTouch: true });

    test('the help text opens on hover, on focus and on tap, and closes on Escape or outside', async ({ page }) => {
      await page.setViewportSize({ width: 1200, height: 800 });
      await serveProduct(page);
      await serveModelHub(page);
      await openOnboarding(page, { lang: 'zh' });
      await openSetup(page, 'zh');

      const help = page.getByRole('button', { name: '什么是模型网关？' });
      const body = page.getByText(/模型网关管理你选择的认证信息/);

      // Pointer: the panel is portalled, so it must survive the trip from trigger to panel.
      await help.hover();
      await expect(body).toBeVisible();
      await body.hover();
      await expect(body).toBeVisible();
      await page.mouse.move(10, 10);
      // Leaving defers the close by 120ms, which is exactly what let the pointer make
      // the trip above. The fixture's clock is frozen, so that delay has to be spent.
      await expect(body).toBeVisible();
      await page.clock.runFor(200);
      await expect(body).toBeHidden();

      // Keyboard: focus reveals it and Escape takes it back, without trapping the tab order.
      await help.focus();
      await expect(body).toBeVisible();
      await page.keyboard.press('Escape');
      await expect(body).toBeHidden();
      await expect(help).toBeFocused();

      // Touch: one clean toggle, and a tap elsewhere dismisses it.
      const target = (await help.boundingBox())!;
      await page.touchscreen.tap(target.x + target.width / 2, target.y + target.height / 2);
      await expect(body).toBeVisible();
      // The dismissable layer arms its outside listener in a zero-delay timeout, which a
      // frozen clock never reaches. Spending it is the fixture's business, not a wait.
      await page.clock.runFor(50);
      await page.touchscreen.tap(8, 400);
      await expect(body).toBeHidden();
    });
  });
});

/**
 * Narrow widths are where a card stops being a scaled copy of the design and becomes an
 * adaptation, so the thing to check is that the adaptation is ONE arrangement rather than
 * whichever one each label's length happens to produce. Every assertion here reads real
 * bounding boxes: three identity headers that start and end together, three status rows
 * that do, and no label clipped or truncated to make that true.
 */
test.describe('narrow identity alignment', () => {
  for (const viewport of [
    { width: 320, height: 568 },
    { width: 375, height: 667 },
    { width: 390, height: 844 },
    { width: 767, height: 1024 },
    { width: 768, height: 1024 },
  ]) {
    for (const lang of ['en', 'zh'] as const) {
      test(`${size(viewport)} ${lang} keeps the three cards on one grid`, async ({ page }, info) => {
        await page.setViewportSize(viewport);
        const denied = await serveProduct(page);
        await openOnboarding(page, { lang });
        await page.evaluate(() => document.fonts.ready);
        let previous = 0;
        for (const [phase, elapsed] of Object.entries(PHASES)) {
          await test.step(phase, async () => {
            // freezeAt advances the clock; these phase offsets are absolute.
            await freezeAt(page, elapsed - previous);
            previous = elapsed;
            const identities = await boxes(page, '.onboarding-card-identity');
            const statuses = await boxes(page, '.onboarding-story-status');
            const skeletons = await boxes(page, '.onboarding-skeleton');
            const cards = await boxes(page, '.onboarding-collaboration-card');
            expect([identities.length, statuses.length, skeletons.length]).toEqual([3, 3, 3]);
            // The header and status boundaries belong to the arrangement, not to whichever
            // caption happens to be short enough at this point in the animation. Below 760
            // the three cards stack, so the boundary they share is the offset INSIDE the
            // card: the same claim, and on one row the same measurement.
            for (const row of [identities, statuses, skeletons]) {
              expect(spread(row.map((one, index) => one.y - cards[index].y))).toBeLessThanOrEqual(1);
              expect(spread(row.map((one) => one.height))).toBeLessThanOrEqual(1);
            }
            // Side by side, that offset is also one screen row; stacked, it cannot be.
            expect(spread(cards.map((card) => card.y)) <= 1).toBe(viewport.width >= 760);
            for (let index = 0; index < cards.length; index++) {
              for (const row of [identities, statuses, skeletons]) {
                expect(row[index].x).toBeGreaterThanOrEqual(cards[index].x);
                expect(row[index].x + row[index].width).toBeLessThanOrEqual(cards[index].x + cards[index].width);
              }
              expect(skeletons[index].height).toBeGreaterThan(0);
              // Identity first, then the status line, then the work — in both steps.
              expect(identities[index].y).toBeGreaterThanOrEqual(cards[index].y - 1);
              expect(identities[index].y + identities[index].height).toBeLessThanOrEqual(statuses[index].y + 1);
              expect(statuses[index].y + statuses[index].height).toBeLessThanOrEqual(skeletons[index].y + 1);
              expect(skeletons[index].y + skeletons[index].height).toBeLessThanOrEqual(cards[index].y + cards[index].height + 1);
            }

            // Equal rows must not be bought with clipping, truncation or overflow into an
            // adjacent row/card. Check the actual visible label boxes.
            const clipped = await page.locator(
              '.onboarding-card-name, .onboarding-card-role, .onboarding-status-text',
            ).filter({ visible: true }).evaluateAll((nodes) => nodes
              .filter((node) => {
                const style = getComputedStyle(node);
                const label = node.getBoundingClientRect();
                const row = node.closest('.onboarding-card-identity, .onboarding-story-status')!.getBoundingClientRect();
                return node.scrollWidth > node.clientWidth + 1
                  || node.scrollHeight > node.clientHeight + 1
                  || style.textOverflow === 'ellipsis'
                  || label.left < row.left - 1 || label.right > row.right + 1
                  || label.top < row.top - 1 || label.bottom > row.bottom + 1;
              })
              .map((node) => node.textContent?.trim() ?? ''));
            expect(clipped).toEqual([]);
          });
        }
        // …and the labels are still the product's own words at a readable size.
        const names = await page.locator('.onboarding-card-name').filter({ visible: true }).allInnerTexts();
        expect(names.map((name) => name.replace(/\s+/g, ' ').trim())).toEqual(['Claude Code', 'Codex', 'OpenCode']);
        const smallest = await page.locator('.onboarding-card-role')
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
  for (const [theme, geometry] of [['dark', '0px 2px 12px 0px'], ['light', '0px 2px 16px -4px']] as const) {
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
        // Settled stills: the clock is held at one named phase and every CSS effect is
        // held past its end, so two runs — or two themes — produce the same drawing and
        // it can be compared against a static frame. The phase is in the filename because
        // a still that does not say which moment it is cannot be compared with anything.
        test(`${size(viewport)} ${theme} ${lang}`, async ({ page }, info) => {
          await page.setViewportSize(viewport);
          await serveProduct(page);
          await serveModelHub(page);
          await openOnboarding(page, { lang, theme });
          await freezeAt(page, PHASES[CAPTURE_PHASE]);
          expect((await renderedPhase(page)).states).toEqual(CAPTURE_STATES);
          await settleEffects(page);
          await page.screenshot({ path: info.outputPath(`welcome-${size(viewport)}-${theme}-${lang}-${CAPTURE_PHASE}.png`), fullPage: true });
          await openSetup(page, lang);
          await settleEffects(page);
          await page.screenshot({ path: info.outputPath(`setup-${size(viewport)}-${theme}-${lang}.png`), fullPage: true });
        });
      }
    }
  }

  // The design's own content box, for overlaying an exported frame at 1:1. A viewport crop
  // is the whole composition here — both steps end inside 756 with nothing cut off — so
  // these stills are directly comparable to the exported frames and not a partial view.
  test('design frame content box', async ({ page }, info) => {
    await page.setViewportSize(DESIGN_FRAME);
    await serveProduct(page);
    await openOnboarding(page);
    await freezeAt(page, PHASES[CAPTURE_PHASE]);
    expect((await renderedPhase(page)).states).toEqual(CAPTURE_STATES);
    await settleEffects(page);
    await page.screenshot({ path: info.outputPath(`welcome-design-frame-1200x756-${CAPTURE_PHASE}.png`) });
    await openSetup(page, 'en');
    await expect(page.locator('.onboarding-primary-action')).toBeInViewport({ ratio: 1 });
    await settleEffects(page);
    await page.screenshot({ path: info.outputPath('setup-design-frame-1200x756.png') });
  });
});
