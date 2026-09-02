// Suite E — the two read-only tabs: Usage and Logs.
//
// Scenario IDs are from docs/plans/model-hub-e2e-test-plan.md §3.
//
// Neither tab can be made to show a particular number from a browser: the
// figures come from turns the gateway actually proxied, and this suite does not
// run agent turns. What IS assertable, and is what these specs assert, is that
// each tab reaches a *stated* state rather than a blank panel — an empty window
// says it is empty in words, and a populated one carries the sections it
// promises. Asserting specific figures needs seeded gateway traffic and belongs
// to the pytest lane.
import { hub as copy } from './support/copy';
import { expect, requireModelHub, requireRuntimeRunning, test } from './support/fixtures';

test.describe('E · usage and logs', () => {
  test.beforeEach(async ({ api }) => {
    await requireModelHub(api);
    await requireRuntimeRunning(api);
  });

  test('E1 · the usage tab states its window, and states when the window is empty', async ({ hub, page }) => {
    await hub.goto();
    await hub.openTab('usage');
    await expect(hub.tab('usage')).toHaveAttribute('aria-selected', 'true');

    await expect(page.getByRole('heading', { name: copy('usage.title'), level: 2 })).toBeVisible();

    // The window is the reading's frame, so it is a labelled control, not a
    // caption: a number with no stated window is not a usage report.
    const window = page.getByRole('radiogroup', { name: copy('usage.window.label') });
    await expect(window).toBeVisible();
    await expect(window.getByRole('radio', { checked: true })).toHaveCount(1);

    // Two honest shapes, and the tab has to say which it is. `count()` reads
    // instantly, and the tab renders NEITHER shape while the read is in flight
    // — so branching on it this early would classify a valid empty report as
    // populated. Waiting on one shape or the other settles the read first.
    const empty = page.getByText(copy('usage.empty'), { exact: true });
    const populated = page.getByRole('heading', { name: copy('usage.bySource.title'), level: 3 });
    await expect(empty.or(populated)).toBeVisible();
    if (await empty.count()) {
      await expect(empty).toBeVisible();
      return;
    }
    // Populated owes both breakdowns — a total with no attribution cannot be
    // acted on.
    await expect(populated).toBeVisible();
    await expect(page.getByRole('heading', { name: copy('usage.byDay.title'), level: 3 })).toBeVisible();
  });

  test('E1 · picking another window moves the selection and re-reads the range', async ({ hub, page }) => {
    await hub.goto();
    await hub.openTab('usage');

    const window = page.getByRole('radiogroup', { name: copy('usage.window.label') });
    const options = window.getByRole('radio');
    const total = await options.count();
    test.skip(total < 2, 'This build offers a single usage window, so there is nothing to switch between.');

    const note = page.locator('.model-hub-usage-note');
    // The tab's own loading/unread copy IS the detail text, so a `before`
    // captured mid-read is indistinguishable from a settled empty report — and
    // the branch below would skip the only assertion that the selection moved
    // the reading. Even a successfully loaded empty window renders a range, so
    // waiting for one is waiting for the read to settle, not for data.
    await expect(note).toContainText(/–|—|Nothing metered yet/, { timeout: 20_000 });
    const before = (await note.innerText()).trim();
    // Named, not matched on state: `getByRole('radio').and('[aria-checked=false]')`
    // re-resolves on every assertion, so the moment the click lands it stops
    // pointing at the option that was clicked and starts pointing at the next
    // still-unchecked one — which is, correctly, still false.
    const checked = (await window.getByRole('radio', { checked: true }).innerText()).trim();
    const labels = (await options.allInnerTexts()).map((label) => label.trim());
    const targetLabel = labels.find((label) => label !== checked);
    // A `find` miss here means every option carries the same label, which is a
    // broken radio group, not an environment the instance was born with.
    expect(targetLabel, 'Every usage window option carries the same label.').toBeDefined();
    const target = window.getByRole('radio', { name: targetLabel!, exact: true });
    await target.click();

    // The selection is exclusive — a segmented control that checks a second
    // option without unchecking the first has stopped being a radio group.
    await expect(target).toHaveAttribute('aria-checked', 'true');
    await expect(window.getByRole('radio', { checked: true })).toHaveCount(1);

    // And the reading follows it. The wait above has already ruled out the
    // loading text, so a `before` that still reads as the static explainer is a
    // genuinely empty window: an instance that never metered a turn, where the
    // range has nothing to restate. That is a state, not a pass by default, so
    // it is named here instead of being swept into the assertion.
    if (before !== copy('usage.detail')) {
      await expect
        .poll(async () => (await note.innerText()).trim(), { timeout: 15_000 })
        .not.toBe(before);
    }
  });

  test('E3 · the logs tab shows the switch history, or says there is none', async ({ hub, page }) => {
    await hub.goto();
    await hub.openTab('logs');
    await expect(hub.tab('logs')).toHaveAttribute('aria-selected', 'true');

    // NOTE: the Logs tab has no `settings.models.logs.*` copy of its own — it
    // renders the recent-switches card, whose strings live under
    // `settings.models.recent.*`. Asserting a `logs.*` key here would assert a
    // key that does not exist.
    await expect(page.getByRole('heading', { name: copy('recent.title'), level: 2 })).toBeVisible();

    // The tab must settle. A card left on "Loading…" is the failure this
    // catches, and it is the one an empty-state assertion alone would miss.
    await expect(page.getByText(copy('recent.loadingMore'), { exact: true })).toHaveCount(0, {
      timeout: 20_000,
    });

    const empty = page.getByText(copy('recent.empty'), { exact: true });
    const viewAll = page.getByRole('button', { name: copy('recent.viewAll'), exact: true });
    // A populated card's event rows carry no stable class, so the third honest
    // shape is named by what every row must render — the timestamped cell. The
    // `loadingMore` guard above has already settled the read by this point.
    // `.or()` demands exactly one match and a populated card shows BOTH "View
    // all" and rows, so the populated branch asserts its own element rather
    // than joining the chain.
    const populated = page
      .locator('section')
      .filter({ has: page.getByRole('heading', { name: copy('recent.title'), level: 2 }) })
      .locator('span.font-mono')
      .first();
    if (await empty.count()) {
      await expect(empty).toBeVisible();
      return;
    }
    await expect(populated).toBeVisible();
    // With history present the card offers the rest of it, unless everything
    // already fits — both are stated states, and neither is a blank panel.
    if (await viewAll.count()) {
      await viewAll.click();
      await expect(
        page.getByRole('button', { name: copy('recent.collapse'), exact: true }),
      ).toBeVisible();
    }
  });
});
