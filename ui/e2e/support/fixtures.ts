// Fixtures shared by every spec, plus the preconditions that decide whether a
// spec can run at all.
//
// The suite drives a live instance, so "can this run?" is a question about the
// instance, not about the code. A precondition that is not met produces a SKIP
// whose message says what to start — never a failure that looks like a bug in
// the product.
//
// The line is drawn where the plan draws it (§5a, discipline tightening): only
// an ENVIRONMENTAL fact may skip — the capability off, the mock absent, the
// runtime not running, no backend CLI installed, an instance whose shape means
// the scenario's surface does not exist. Once those have passed, anything that
// then goes wrong is the product going wrong, and it fails. A source the
// instance will not create, a mode switch that does not take, a forced route
// PUT that is refused: each of those used to skip, and each of them was a
// product failure wearing a missing precondition's clothes.
import { type Locator, test as base, expect } from '@playwright/test';

import { HubApi, type Source } from './api';
import { NO_MOCK_UPSTREAM } from './env';
import { ModelHubPage } from './hub';
import { MockUpstream } from './mock';

type HubFixtures = {
  api: HubApi;
  mock: MockUpstream;
  hub: ModelHubPage;
};

// Playwright calls the second fixture argument `use`; it is positional, so the
// name is ours to pick. It is `provide` here because this repo's ESLint runs
// react-hooks over every `.ts` file, and a bare call to `use(...)` reads as
// React 19's `use()` hook being called outside a component. Suppressing that
// would be suppressing a rule that is doing its job on a name we chose.
export const test = base.extend<HubFixtures>({
  api: async ({ request }, provide) => {
    await provide(new HubApi(request));
  },
  mock: async ({ request }, provide) => {
    await provide(new MockUpstream(request));
  },
  hub: async ({ page }, provide) => {
    await provide(new ModelHubPage(page));
  },
});

export { expect };

/** Skips unless the instance actually has the Model Hub capability on. */
export const requireModelHub = async (api: HubApi): Promise<void> => {
  test.skip(
    !(await api.modelHubEnabled()),
    'Model Hub is disabled on this instance. Start it with VIBE_MODEL_HUB_ENABLED=1 (see ui/e2e/README.md).',
  );
};

/** Skips unless a controllable upstream is configured AND answering. */
export const requireMockUpstream = async (mock: MockUpstream): Promise<void> => {
  test.skip(!(await mock.reachable()), NO_MOCK_UPSTREAM);
};

/**
 * The product's own running predicate, not a stricter one of the suite's.
 *
 * `runtimeIsRunning` in `src/components/settings/models/runtimeLifecycle.ts`
 * accepts `degraded` alongside `ok`, and it has to: a gateway serving in its
 * supported degraded state renders the full surface. Insisting on exactly `ok`
 * here reported such an instance as switched off and skipped every source,
 * routing, guard, usage, and logs spec — nearly the whole suite, silently.
 */
export const runtimeIsRunning = (health: string | undefined): boolean =>
  health === 'ok' || health === 'degraded';

/** Skips unless the gateway runtime is installed and running — the precondition
 *  for anything that adds a source or serves a route. */
export const requireRuntimeRunning = async (api: HubApi): Promise<void> => {
  const runtime = await api.runtime();
  test.skip(
    !runtimeIsRunning(runtime?.status?.health),
    'The gateway runtime is not running on this instance. Turn the model gateway on first (see ui/e2e/README.md).',
  );
};

/**
 * Creates a source a spec needs before it can begin, and FAILS if the instance
 * will not make one.
 *
 * Deliberately not a skip. By the time this runs, every environmental
 * precondition has passed — the capability is on, the runtime is up, and the
 * mock has answered its own control plane — so an instance that then refuses a
 * source it was handed a healthy upstream for has done something wrong, and
 * saying so is what the suite is for.
 */
export const requireSource = async (
  api: HubApi,
  displayName: string,
  baseUrl: string,
): Promise<Source> => {
  const source = await api.createApiKeySource(displayName, baseUrl);
  expect(source, `the instance refused to create the precondition source ${displayName}`).not.toBeNull();
  return source!;
};

/**
 * Asserts that `locator` IS on the page and does NOT say `text`.
 *
 * Both halves, always, because the second is meaningless without the first: a
 * negated text assertion is satisfied by an element that says something else AND
 * by an element that is not there at all, and those are opposite verdicts. "The
 * surviving model is not re-announced as new" passes just as green when the
 * refetch dropped the survivor along with the model it was supposed to drop —
 * the row is gone, so it contains nothing, so it does not contain "New".
 *
 * Kept as one call rather than as a convention to write two, so that the
 * presence cannot be the line that gets left out.
 */
export const expectVisibleWithout = async (
  locator: Locator,
  text: string | RegExp,
  options?: { timeout?: number },
): Promise<void> => {
  await expect(locator).toBeVisible(options);
  await expect(locator).not.toContainText(text, options);
};
