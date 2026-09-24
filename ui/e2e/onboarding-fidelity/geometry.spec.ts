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
/** What the stage reserves beyond the card: the band the introduction draws its return
 *  wire in. */
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
   * draws the story's diagram as its card plus 64. That diagram box is the design's own
   * number and is measured directly; the stage around it is the reservation both steps
   * spend, which is the diagram wherever the connection's card fits its frame and more
   * where that card's sentence wraps to the two lines it is clamped to. So the design is
   * asserted on the box the design draws, and the stage is asserted to hold it.
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
    const diagram = await box(page, '.onboarding-collaboration');
    expect(diagram.height).toBeCloseTo(reference.card + STAGE_EXTRA, 1);
    expect(diagram.y).toBeCloseTo(stage.y, 1);
    expect(stage.height).toBeGreaterThanOrEqual(diagram.height - 0.5);

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
      await page.locator('.onboarding-story').screenshot({ path: info.outputPath(`phase-${name}.png`) });
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

/**
 * The tiers the anchor is held across. One list, because every fence below walks the
 * same journey in a different state and "the same six tiers" has to mean one thing.
 */
const ANCHOR_TIERS = [
  { width: 1920, height: 1080 }, { width: 1366, height: 768 }, { width: 1200, height: 800 },
  { width: 768, height: 1024 }, { width: 390, height: 844 }, { width: 320, height: 568 },
] as const;

/** The pair's real rects, measured whether or not the back row is drawn: it reserves its
 *  box through `visibility` on the first screen, so filtering for visibility would drop
 *  exactly the measurement that proves the reservation. */
const pair = (page: Page) => page.evaluate(() => {
  const read = (selector: string) => {
    const rect = document.querySelector(selector)!.getBoundingClientRect();
    return { x: rect.x, y: rect.y, width: rect.width, height: rect.height };
  };
  return { primary: read('.onboarding-primary-action'), back: read('.onboarding-back-action') };
});

/** The height of the stage the pair currently sits under — every screen keeps one. */
const stageHeight = (page: Page) => page.evaluate(() => {
  const stage = [...document.querySelectorAll('.onboarding-stage')].find((node) => node.getBoundingClientRect().height);
  return stage ? stage.getBoundingClientRect().height : 0;
});

type Pair = Awaited<ReturnType<typeof pair>>;

const expectSamePair = async (page: Page, before: Pair) => {
  await toTop(page);
  const after = await pair(page);
  for (const key of ['x', 'y', 'width', 'height'] as const) {
    expect(round(after.primary[key])).toBeCloseTo(round(before.primary[key]), 1);
    expect(round(after.back[key])).toBeCloseTo(round(before.back[key]), 1);
  }
};

/**
 * One set of coordinates for all three screens.
 *
 * The composition is centred in the window, so anything that joins the column changes
 * where the heading, the cards and the action sit — which is how the assistants step,
 * whose readiness caption is portalled under the pair, came to sit 9px above the two
 * steps before it. The slot now lives in the cell the screens share, resting on its
 * bottom edge, so this asserts both halves: the three screens agree, and filling the
 * slot leaves them agreeing.
 */
test.describe('shared screen anchors', () => {
  for (const viewport of [{ width: 1132, height: 664 }, { width: 1440, height: 900 }, { width: 1920, height: 1080 }]) {
    test(`${viewport.width} desktop captions sit on their own lines`, async ({ page }) => {
      await page.setViewportSize(viewport);
      await serveProduct(page);
      await serveModelHub(page);
      await openOnboarding(page, { lang: 'zh' });
      const center = async (selector: string) => {
        const rect = await box(page, selector);
        return rect.y + rect.height / 2;
      };
      const fontSize = (selector: string) =>
        page.locator(selector).first().evaluate((node) => getComputedStyle(node).fontSize);
      // The welcome caption is a label cut into the return wire, and the wire's run is the
      // line the other screens set their bottom text on: half a summary row above the
      // stage's floor. The loop hangs from the cards' floor, so its legs leave the cards.
      const loop = await box(page, '.onboarding-return');
      const stage = await box(page, '[data-setup-screen-root]:not([hidden]) .onboarding-stage');
      const summaryHalf = await page.locator('.onboarding-shell').evaluate((node) =>
        parseFloat(getComputedStyle(node).getPropertyValue('--ob-summary-h')) / 2);
      expect(loop.y + loop.height).toBeCloseTo(stage.y + stage.height - summaryHalf, 0);
      const cards = await boxes(page, '.onboarding-collaboration-card');
      expect(loop.y).toBeCloseTo(cards[0].y + cards[0].height, 0);
      const welcome = await center('.onboarding-story-caption');
      expect(welcome).toBeCloseTo(loop.y + loop.height, 0);
      const welcomeSize = await fontSize('.onboarding-story-caption');
      await page.getByRole('button', { name: '立即开始' }).click();
      await page.clock.runFor(950);
      await expect(page.locator('.setup-provider-summary')).toContainText('已选');
      const providers = await center('.setup-provider-summary');
      expect(welcomeSize).toBe(await fontSize('.setup-provider-summary'));
      await page.locator('.onboarding-setup-hint button').click();
      await page.clock.runFor(950);
      const assistants = await center('.onboarding-setup-hint');
      expect(assistants).toBeCloseTo(providers, 0);
      // The rescan link is centred on the sentence it follows, in the same type.
      expect(await center('.onboarding-setup-hint-line > button')).toBeCloseTo(await center('.onboarding-setup-hint-line > span'), 0);
      expect(await fontSize('.onboarding-setup-hint-line > span')).toBe(welcomeSize);
      expect(await fontSize('.onboarding-setup-hint-line > button')).toBe(welcomeSize);
    });
  }

  test('the heading and the action keep one y on every screen, with or without an aside', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    const denied = await serveProduct(page);
    await serveModelHub(page);
    await openOnboarding(page, { lang: 'zh' });
    await openSetup(page, 'zh');
    await settleEffects(page);

    const anchors = () => page.evaluate(() => {
      const top = (node: Element) => Math.round(node.getBoundingClientRect().top);
      const aside = document.querySelector('.onboarding-action-aside')!.getBoundingClientRect();
      const content = [...document.querySelectorAll('[data-setup-screen-root]:not([hidden]) .onboarding-stage > *')]
        .map((node) => node.getBoundingClientRect()).filter((rect) => rect.height > 0);
      return {
        headings: [...document.querySelectorAll('[data-setup-screen-root] .onboarding-heading')].map(top),
        action: top(document.querySelector('.onboarding-primary-action')!),
        asideTop: Math.round(aside.top),
        contentBottom: Math.round(Math.max(...content.map((rect) => rect.bottom))),
      };
    });

    const before = await anchors();
    // Every screen's heading starts where the current screen's does, and the action is
    // the same distance below all of them.
    expect(new Set(before.headings).size).toBe(1);

    // The tallest thing the slot carries, on the screen with the least room for it.
    await page.evaluate(() => {
      const paragraph = document.createElement('p');
      paragraph.textContent = 'diagnostic '.repeat(60);
      document.querySelector('.onboarding-action-aside')!.append(paragraph);
    });
    await settleEffects(page);
    const after = await anchors();
    expect(after.headings).toEqual(before.headings);
    expect(after.action).toBe(before.action);
    // It grows upward into empty room and stops before the composition above it.
    expect(after.asideTop).toBeLessThan(before.action);
    expect(after.asideTop).toBeGreaterThanOrEqual(after.contentBottom);
    expect(denied).toEqual([]);
  });
});

test.describe('shared action anchor', () => {
  for (const viewport of ANCHOR_TIERS) {
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
        // The desktop hint sits in the free band above the action; phone keeps its
        // own compact placement below the pair. Neither position moves the action.
        const caption = await box(page, '.onboarding-setup-hint');
        const backRect = await box(page, '.onboarding-back-action');
        if (viewport.width >= 760) {
          expect(caption.y + caption.height).toBeLessThanOrEqual(action.y - 8);
        } else {
          expect(caption.y).toBeGreaterThanOrEqual(backRect.y + backRect.height - 1);
        }
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
 * The anchor fence above walks a journey where nothing goes wrong, and everything the
 * shell's post-action slot exists for appears only when something does: a detection that
 * failed, a permission write that was refused, a completion that found a saved platform
 * it cannot use. All three carry a server's own sentence, so all three GROW — and each of
 * them used to be drawn inside the screen, above the footer, which is the one place in
 * this layout where content can drag the anchor. So the same tiers are walked again with
 * those states actually produced rather than mocked into place.
 */
const LONG_DETAIL = `probe exited 1: ${'the cli-detect subprocess reported an unreadable execution environment; '.repeat(3)}see the service log for the full trace`;
const LONG_PERMISSION = `opencode.json could not be written: ${'the configuration directory is owned by another user and the write was refused; '.repeat(2)}resolve the ownership and try again`;

/** The toast container, which is `fixed` and therefore a different surface with its own
 *  placement. It is dismissed before any pointer question is asked, so the answer is
 *  about the setup layout rather than about a transient global overlay. */
const clearToasts = async (page: Page) => {
  await page.clock.runFor(3100);
  await expect(page.locator('div.fixed.right-4.z-50 > div')).toHaveCount(0);
};

/** The path probe, with a switch on it. A 500 carrying the server's sentence is the shape
 *  this endpoint really fails with, and the same sentence for all three binaries is what
 *  the toast layer coalesces — one overlay to spend, not three. */
async function switchableDetect(page: Page) {
  let failing = true;
  await page.route('**/api/cli/detect**', (route) => {
    if (failing) return route.fulfill({ status: 500, json: { error: LONG_DETAIL } });
    const binary = new URL(route.request().url()).searchParams.get('binary') ?? '';
    return route.fulfill({ json: { found: true, path: `/fixture/bin/${binary.split('/').pop()}` } });
  });
  return { recover: () => { failing = false; } };
}

/** OpenCode's permission write, held open so the callout's loading, refusal and success
 *  are three observable moments rather than one settled render. */
async function deferredPermission(page: Page) {
  const waiting: (() => void)[] = [];
  let failing = true;
  await page.route('**/api/opencode/permission-status', (route) =>
    route.fulfill({ json: { ok: true, permission_allowed: false, config_path: '/fixture/opencode.json' } }));
  await page.route('**/api/opencode/setup-permission', async (route) => {
    await new Promise<void>((resolve) => { waiting.push(resolve); });
    return route.fulfill({ json: failing
      ? { ok: false, message: LONG_PERMISSION, config_path: '/fixture/opencode.json' }
      : { ok: true, message: 'Allowed', config_path: '/fixture/opencode.json' } });
  });
  return {
    settle: async () => { await expect.poll(() => waiting.length).toBeGreaterThan(0); waiting.shift()!(); },
    succeed: () => { failing = false; },
  };
}

test.describe('post-action aside', () => {
  for (const viewport of ANCHOR_TIERS) {
    for (const lang of ['en', 'zh'] as const) {
      test(`${size(viewport)} ${lang} explains a failed detection below the pair and retries from it`, async ({ page }) => {
        await page.setViewportSize(viewport);
        const denied = await serveProduct(page);
        const detect = await switchableDetect(page);
        await openOnboarding(page, { lang });
        await toTop(page);
        const before = await pair(page);

        await page.locator('.onboarding-primary-action').click();
        const alert = page.locator('.onboarding-action-aside [role="alert"]');
        await expect(alert).toContainText(lang === 'zh' ? '未能检测助手' : 'Could not check your assistants');
        // In the shell's slot after the footer, not in the screen above it.
        await expect(page.locator('[data-setup-screen-root="intro"] [role="alert"]')).toHaveCount(0);
        // The same sentence also reaches the global toast layer. Spend it now so every
        // question below is about the slot rather than about a transient overlay.
        await clearToasts(page);
        await expectSamePair(page, before);

        // Expanding the diagnostic is the growth the old placement could not absorb.
        const aside = page.locator('.onboarding-action-aside');
        const closed = (await box(page, '.onboarding-action-aside')).height;
        await aside.getByText(lang === 'zh' ? '查看详情' : 'View details').click();
        await expect(aside.locator('details')).toContainText(LONG_DETAIL);
        expect((await box(page, '.onboarding-action-aside')).height).toBeGreaterThan(closed);
        await expectSamePair(page, before);
        // Grown, and still not over the button it explains.
        expect((await box(page, '.onboarding-action-aside')).y)
          .toBeGreaterThanOrEqual(before.primary.y + before.primary.height - 1);
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= globalThis.innerWidth)).toBe(true);

        expect(await hitSelf(page, '.onboarding-primary-action')).toBe(true);

        // The last word on reachability is a real click, which Playwright refuses to
        // deliver when anything else would receive it. The primary now says Retry.
        detect.recover();
        await expect(page.locator('.onboarding-primary-action')).toContainText(lang === 'zh' ? '重试' : 'Retry');
        await page.locator('.onboarding-primary-action').click();
        await page.clock.runFor(950);
        const sequence = (await page.locator('[data-setup-sequence]').getAttribute('data-setup-sequence'))!.split(' ');
        await expect(page.locator('[data-setup-sequence]')).toHaveAttribute('data-setup-screen', sequence[1]);
        await expect(page.locator('.onboarding-action-aside [role="alert"]')).toHaveCount(0);
        expect(denied).toEqual([]);
      });

      test(`${size(viewport)} ${lang} holds a refused permission write under the pair`, async ({ page }) => {
        await page.setViewportSize(viewport);
        const denied = await serveProduct(page);
        await serveModelHub(page);
        const permission = await deferredPermission(page);
        await openOnboarding(page, { lang });
        // The fence starts one screen earlier than the state it is about. The reservation
        // claims the pair is in the same place on the introduction as on a connection step
        // carrying the offer and the refused permission write at once, so the introduction's
        // pair is what every box below is compared against — not the first setup render.
        await toTop(page);
        const before = await pair(page);
        await openSetup(page, lang);

        // The permission callout is in the slot after the pair: an aside under the anchor.
        const callout = page.locator('.onboarding-action-aside').getByText(
          lang === 'zh' ? '不设置时 OpenCode' : 'Without this, OpenCode');
        await expect(callout).toBeVisible();
        await expectSamePair(page, before);

        // A write that has not answered yet: the callout says so and nothing moves.
        const setup = page.locator('.onboarding-action-aside').getByRole('button', {
          name: lang === 'zh' ? '在 opencode.json 写入 allow' : 'Allow tool calls in opencode.json' });
        await setup.scrollIntoViewIfNeeded();
        await setup.click();
        await expect(setup).toBeDisabled();
        await expectSamePair(page, before);

        await permission.settle();
        await expect(page.locator('.onboarding-action-aside').getByText(LONG_PERMISSION)).toBeVisible();
        await expectSamePair(page, before);
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= globalThis.innerWidth)).toBe(true);
        await clearToasts(page);
        expect(await hitSelf(page, '.onboarding-primary-action')).toBe(true);
        expect(await hitSelf(page, '.onboarding-back-action')).toBe(true);

        // Granted, the callout is not a cleared message but an absent one.
        permission.succeed();
        await setup.click();
        await permission.settle();
        await expect(page.locator('.onboarding-action-aside').getByText(LONG_PERMISSION)).toHaveCount(0);
        await expect(callout).toHaveCount(0);
        await expectSamePair(page, before);
        await clearToasts(page);

        // The stage is floored at the tallest step's cards, so a card that wraps past
        // `--ob-card-h` cannot move the pair either.
        const stageBefore = await stageHeight(page);
        await expectSamePair(page, before);
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= globalThis.innerWidth)).toBe(true);
        expect(await hitSelf(page, '.onboarding-primary-action')).toBe(true);
        expect(await hitSelf(page, '.onboarding-back-action')).toBe(true);

        // A portal escapes `inert`, so leaving the screen has to empty the slot rather
        // than leave reachable content behind a hidden screen.
        await page.locator('.onboarding-back-action').click();
        await page.clock.runFor(950);
        await expect(page.locator('[data-setup-sequence]')).toHaveAttribute('data-setup-screen', 'intro');
        expect(await page.locator('.onboarding-action-aside').evaluate((node) => ({
          children: node.childElementCount,
          focusable: node.querySelectorAll('a[href], button, input, select, textarea, [tabindex]').length,
        }))).toEqual({ children: 0, focusable: 0 });
        // Back is the other half of the cross-screen claim: the introduction the pair
        // returns to is the one it started on, after the step it came from had spent a
        // permission write.
        await expectSamePair(page, before);

        // And re-entering is not a third position. The clock here is frozen and only a
        // test moves it, so the handoff is nudged until the screen has arrived rather
        // than once, which races the click's own commit.
        await page.getByRole('button', { name: lang === 'zh' ? '立即开始' : 'Get started' }).click();
        await expect.poll(async () => {
          await page.clock.runFor(950);
          return page.locator('[data-setup-sequence]').getAttribute('data-setup-screen');
        }).toBe('assistants');
        await page.locator('.onboarding-assistants').waitFor();
        await expectSamePair(page, before);
        expect(await stageHeight(page)).toBeCloseTo(stageBefore, 1);
        expect(await hitSelf(page, '.onboarding-primary-action')).toBe(true);
        expect(await hitSelf(page, '.onboarding-back-action')).toBe(true);
        expect(denied).toEqual([]);
      });
    }
  }
});

/**
 * The completion recovery is the largest thing the slot ever carries — a whole form,
 * opened by the action it sits under, on the tiers with the least room for it. While it
 * owns the journey the pair is held, which is a state the pair has to survive without
 * moving, and cancelling has to give both the journey and the slot back.
 */
test.describe('completion recovery in the slot', () => {
  for (const viewport of [{ width: 390, height: 640 }, { width: 320, height: 568 }]) {
    for (const lang of ['en', 'zh'] as const) {
      test(`${size(viewport)} ${lang} opens the repair under the held pair and gives it back on cancel`, async ({ page }, info) => {
        await page.setViewportSize(viewport);
        const denied = await serveProduct(page);
        let manifests = 0;
        // A saved platform whose required credential is gone: the shape that makes
        // completion stop and hand the slot a form instead of entering the workspace.
        await page.route('**/api/config', (route) => route.fulfill({ json: {
          version: 'v2', setup_completed: false, runtime: {},
          capabilities: { model_hub: { enabled: true } }, model_hub: { enabled: true },
          platforms: { primary: 'slack', enabled: ['slack'] },
          platform_catalog: [{ id: 'slack', config_key: 'slack', credential_fields: ['bot_token'] }],
          slack: { has_app_token: true, bot_token: '' },
          agents: { claude: { enabled: true }, codex: { enabled: true }, opencode: { enabled: true } },
        } }));
        await page.route('**/api/backend/claude/connection', (route) => route.fulfill({ json: {
          ok: true, backend: 'claude', ready: true, entry_eligible: true,
          enabled: true, installed: true, application: 'applied', auth: 'api_key' } }));
        await page.route('**/api/slack/manifest', (route) => { manifests++; return route.fulfill({ json: { ok: true, manifest: '{}' } }); });
        await openOnboarding(page, { lang });
        await openSetup(page, lang);
        await toTop(page);
        const before = await pair(page);

        await page.locator('.onboarding-primary-action').click();
        const recovery = page.getByRole('region', { name: lang === 'zh' ? '修复已保存的消息配置' : 'Repair saved messaging configuration' });
        await expect(recovery).toBeVisible();
        expect(await recovery.evaluate((node) => !!node.closest('[data-setup-action-aside]'))).toBe(true);
        await expectSamePair(page, before);
        // Held, not busy: the journey is refused while the form owns it, and refusing is
        // not the same as pretending something is running.
        await expect(page.locator('.onboarding-primary-action')).toBeDisabled();
        await expect(page.locator('.onboarding-back-action')).toBeDisabled();
        // The spinner carries Tailwind's `motion-safe:` variant, so the token in the DOM
        // is the whole `motion-safe:animate-spin`; a bare `.animate-spin` finds nothing
        // here whatever the footer renders.
        expect(await page.locator('.onboarding-primary-action [class~="motion-safe:animate-spin"]').count()).toBe(0);
        expect(manifests).toBe(0);

        // Expanding it into the real form is the growth these two tiers have least room
        // for, and its own controls still have to be reachable rather than merely present.
        await recovery.getByRole('button', { name: lang === 'zh' ? '修复已保存的消息配置' : 'Repair saved messaging configuration' }).click();
        await expect.poll(() => manifests).toBe(1);
        // Exact: the Slack step headers inside the form spell "Slack 应用" / "Slack app".
        const apply = recovery.getByRole('button', { name: lang === 'zh' ? '应用' : 'Apply', exact: true });
        await apply.scrollIntoViewIfNeeded();
        await expect(apply).toBeInViewport();
        expect(await recovery.evaluate((node) => node.scrollWidth - node.clientWidth)).toBeLessThanOrEqual(1);
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= globalThis.innerWidth)).toBe(true);
        await expectSamePair(page, before);
        await settleEffects(page);
        await recovery.screenshot({ path: info.outputPath(`recovery-slot-${size(viewport)}-${lang}.png`) });

        // Cancel is a real click, and it gives back both the slot and the journey.
        const cancel = recovery.getByRole('button', { name: lang === 'zh' ? '取消' : 'Cancel' });
        await cancel.scrollIntoViewIfNeeded();
        await cancel.click();
        await expect(recovery).toHaveCount(0);
        await expectSamePair(page, before);
        await expect(page.locator('.onboarding-primary-action')).toBeEnabled();
        await expect(page.locator('.onboarding-back-action')).toBeEnabled();
        expect(await hitSelf(page, '.onboarding-primary-action')).toBe(true);
        expect(await hitSelf(page, '.onboarding-back-action')).toBe(true);
        expect(denied).toEqual([]);
      });
    }
  }
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
        await page.locator('.onboarding-story').screenshot({
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
