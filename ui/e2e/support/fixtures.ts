// Fixtures shared by every spec, plus the preconditions that decide whether a
// spec can run at all.
//
// The suite drives a live instance, so "can this run?" is a question about the
// instance, not about the code. A precondition that is not met produces a SKIP
// whose message says what to start — never a failure that looks like a bug in
// the product.
import { test as base, expect } from '@playwright/test';

import { HubApi } from './api';
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

/** Skips unless the gateway runtime is installed and running — the precondition
 *  for anything that adds a source or serves a route. */
export const requireRuntimeRunning = async (api: HubApi): Promise<void> => {
  const runtime = await api.runtime();
  test.skip(
    runtime?.status?.health !== 'ok',
    'The gateway runtime is not running on this instance. Turn the model gateway on first (see ui/e2e/README.md).',
  );
};
