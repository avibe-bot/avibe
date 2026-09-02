// The two preconditions the Model Hub's own surfaces impose, as fixtures.
//
// `hubSurface` — the page a spec lands on is not a constant. `modelsSurfaceKind`
// sends `/settings/models` to the DIRECT HOME when every backend is on Direct
// *and* the instance holds no sources, and that home has no source list and no
// Add API key button: its only control is "Switch to gateway". A machine's very
// first source arrives by adopting the gateway, which is C6-direct-home's
// subject. So every spec on this `test` gets one anchor source when the instance
// has none, and the anchor leaves with the test that needed it. Without it a
// suite passes its first spec, deletes its own source in teardown, and every
// later spec times out looking for a button that is no longer on the page.
//
// `gateway` — a backend in Gateway mode with sources of the suite's own to route
// through. Route rows, the priority button, and every supply guard exist only
// for a Gateway-mode backend. Arranging that by clicking would make each of
// those specs a mode-switch test as well, and would report the mode switch's
// failures under their names.
import type { Agent, Source } from './api';
import { E2E_SOURCE_PREFIX, mockBaseUrl, NO_MOCK_UPSTREAM } from './env';
import { test as base } from './fixtures';
import { anthropicInventory } from './mock';

export type Gateway = {
  /** A backend that is in Gateway mode for the duration of the test. */
  backend: string;
  /** One model of that backend — the one its route rows are keyed by. */
  model: string;
  /** Two api_key sources this suite owns outright and may route through. */
  sources: Source[];
};

// `provide` rather than Playwright's usual `use` — see the note in fixtures.ts.
export const test = base.extend<{ hubSurface: void; gateway: Gateway }>({
  hubSurface: [
    async ({ api, mock }, provide, testInfo) => {
      const sources = await api.sources();
      const agents = await api.agents();
      // The product's own condition, read the product's own way round: anything
      // other than "no sources and nothing in Gateway mode" is already the
      // gateway surface, and an anchor would only add a row nobody asked for.
      const directEmpty = sources.length === 0 && agents.every((agent) => agent.mode === 'direct');
      let anchor: Source | null = null;
      if (directEmpty) {
        testInfo.skip(!(await mock.reachable()), NO_MOCK_UPSTREAM);
        // A healthy baseline, because the previous test may have left the mock
        // rejecting everything — and an anchor that cannot be created would
        // report the last test's mock state as this test's missing precondition.
        await mock.configure({ auth: 'ok', protocol: 'anthropic', models_endpoint: 'ok' });
        anchor = await api.createApiKeySource(`${E2E_SOURCE_PREFIX}surface-anchor`, mockBaseUrl());
        testInfo.skip(
          !anchor,
          'The mock upstream refused the anchor source, so the page stays on the direct home.',
        );
      }
      try {
        await provide();
      } finally {
        if (anchor) await api.deleteSource(anchor.id);
      }
    },
    { auto: true },
  ],

  gateway: async ({ api, mock }, provide, testInfo) => {
    await mock.configure({
      auth: 'ok',
      protocol: 'anthropic',
      models_endpoint: 'ok',
      models: anthropicInventory(['e2e-route-1', 'e2e-route-2']),
    });
    const created = [
      await api.createApiKeySource(`${E2E_SOURCE_PREFIX}route-a`, mockBaseUrl()),
      await api.createApiKeySource(`${E2E_SOURCE_PREFIX}route-b`, mockBaseUrl()),
    ];
    // From here on the instance has been changed, so every exit — including the
    // `skip`s below, which throw — has to go through the teardown. A skip that
    // returned early used to leave a backend in Gateway mode and two sources
    // behind for whatever ran next.
    let switched: string | null = null;
    try {
      testInfo.skip(
        created.some((source) => source === null),
        'The mock upstream refused a precondition source, so there is nothing to route through.',
      );

      const agents = await api.agents();
      // A backend already in Gateway mode is used as found: switching a second
      // one would change more of the instance than the test needs.
      const alreadyHub = agents.find((agent: Agent) => agent.mode === 'hub');
      const candidate = alreadyHub ?? agents.find((agent: Agent) => agent.cli_present);
      testInfo.skip(!candidate, 'No agent backend on this instance can be put into Gateway mode.');
      if (!alreadyHub) {
        await api.setAgentMode(candidate!.backend, 'hub');
        switched = candidate!.backend;
      }

      const live = (await api.agents()).find((agent: Agent) => agent.backend === candidate!.backend);
      testInfo.skip(
        live?.mode !== 'hub',
        `Backend ${candidate!.backend} would not switch to Gateway mode on this instance.`,
      );
      // The backend's currently selected model when it is one of the routable
      // ones: a guard that interrupts the model the backend actually runs is the
      // interruption the product is warning about, and B7 reads that warning.
      const supply = live?.model_supply ?? [];
      const model =
        supply.find((entry) => entry.model_id === live?.selected_model_id)?.model_id
        ?? supply[0]?.model_id;
      testInfo.skip(
        !model,
        `Backend ${candidate!.backend} lists no models, so it has no route to open.`,
      );

      await provide({
        backend: candidate!.backend,
        model: model!,
        sources: created as Source[],
      });
    } finally {
      // Put the instance back as it was found: the mode only if this fixture
      // changed it, and always this suite's own sources.
      if (switched) await api.setAgentMode(switched, 'direct');
      await api.removeSuiteSources();
    }
  },
});

export { expect } from './fixtures';
