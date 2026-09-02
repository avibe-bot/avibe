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
import { routableModels, surfaceKind } from './api';
import { E2E_SOURCE_PREFIX, mockBaseUrl, NO_MOCK_UPSTREAM } from './env';
import { expect, requireModelHub, requireMockUpstream, requireRuntimeRunning, requireSource, test as base } from './fixtures';
import { anthropicInventory } from './mock';
import { restoreNativeSources } from './restore';

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
      // First, because this is an auto fixture: it runs before the suite's own
      // `beforeEach` guards, so it cannot lean on them. A disabled capability
      // turns every `/api/models/*` read below into the gate's 404 — which
      // `read()` (correctly) throws on — and reports a skip-with-reason as a
      // failure in every B, D, and G spec at once. The runtime and mock guards
      // are here for the same reason: the anchor below CREATES a source, which
      // a stopped runtime refuses, and a spec whose only problem is a
      // documented environmental precondition should skip, not fail.
      await requireModelHub(api);
      await requireRuntimeRunning(api);
      const sources = await api.sources();
      const agents = await api.agents();
      // The product's own condition, asked through the one declaration that
      // owns it: anything other than the gateway surface needs the anchor, and
      // BOTH direct forms do — an instance with no backend installed renders
      // the install prompt, which has no source list either.
      const directEmpty = surfaceKind(agents, sources) !== 'gateway';
      const anchorName = `${E2E_SOURCE_PREFIX}surface-anchor`;
      let anchor: Source | null = null;
      try {
        if (directEmpty) {
          testInfo.skip(!(await mock.reachable()), NO_MOCK_UPSTREAM);
          // A healthy baseline, because the previous test may have left the mock
          // rejecting everything — and an anchor that cannot be created would
          // report the last test's mock state as this test's missing precondition.
          await mock.configure({ auth: 'ok', protocol: 'anthropic', models_endpoint: 'ok' });
          anchor = await requireSource(api, anchorName, mockBaseUrl());
        }
        await provide();
      } finally {
        // By NAME, not only by the id a successful create returned: the server
        // persists the source before its response leaves, so a POST whose
        // response is lost rejects `requireSource` above — with the anchor
        // already on the instance and no id in hand. The prefix sweep catches
        // that path; the id delete covers the ordinary one without re-listing.
        if (anchor) {
          await api.deleteSource(anchor.id);
        } else if (directEmpty) {
          for (const source of await api.sources()) {
            if (source.display_name === anchorName) await api.deleteSource(source.id);
          }
        }
      }
    },
    { auto: true },
  ],

  gateway: async ({ api, mock }, provide, testInfo) => {
    // Same ordering rule as `hubSurface`: this fixture is initialized before
    // the suite's `beforeEach` guards run, so it owns its own preconditions.
    // Without the mock URL `configure` below throws, and without a running
    // runtime every source create is refused — both documented skip states,
    // not failures.
    await requireModelHub(api);
    await requireMockUpstream(mock);
    await requireRuntimeRunning(api);
    await mock.configure({
      auth: 'ok',
      protocol: 'anthropic',
      models_endpoint: 'ok',
      models: anthropicInventory(['e2e-route-1', 'e2e-route-2']),
    });
    // `requireSource`, not a skip: the mock has answered its control plane by
    // this point, so a refused source is the product refusing a healthy
    // upstream (§5a) — even though the fixture's own teardown still has to run.
    let created: Source[] = [];
    // From here on the instance has been changed, so every exit — including
    // the `skip`s below, which throw — has to go through the teardown. A skip
    // that returned early used to leave a backend in Gateway mode and two
    // sources behind for whatever ran next.
    let switched: string | null = null;
    // The Direct→Gateway switch is not mode-only on a machine whose CLI holds
    // a native login: `set_agent_mode` appends a `native_cli` subscription
    // source as part of the transition, and the prefix sweep below would leave
    // it — with its placement — on the instance for every later spec. The
    // snapshot is what separates that import from a native source the instance
    // already had; what it is NOT is the test of whether a source is native.
    let sourcesBeforeSwitch: Set<string> | null = null;
    try {
      created = [
        await requireSource(api, `${E2E_SOURCE_PREFIX}route-a`, mockBaseUrl()),
        await requireSource(api, `${E2E_SOURCE_PREFIX}route-b`, mockBaseUrl()),
      ];

      const agents = await api.agents();
      // What this fixture owes its specs is a backend with a ROUTE to open, and
      // that is two facts, not one. The CLI has to be installed, because the
      // product's own surface filters uninstalled backends out
      // (`installedAgents`) and their route rows never render. And the backend
      // has to expose a model, because an `opencode` sitting in Gateway mode
      // with nothing ticked has an empty `model_supply` — so selecting on
      // installation alone would take that one as found and hand every B, D and
      // G spec a backend with no route, while an installed `claude` in Direct
      // mode next to it had a full builtin menu all along.
      const installed = agents.filter((agent: Agent) => agent.cli_present);
      const usable = installed.filter((agent: Agent) => routableModels(agent).length > 0);
      // Still preferred if it qualifies: switching a second backend would change
      // more of the instance than the test needs.
      const alreadyHub = usable.find((agent: Agent) => agent.mode === 'hub');
      const candidate = alreadyHub ?? usable[0];
      testInfo.skip(!installed.length, 'No agent backend is installed on this instance, so none can be put into Gateway mode.');
      // Its own skip, and its own sentence: "installed but with an empty menu"
      // is a different thing to fix than "nothing installed", and a spec that
      // reported the first as the second would send its reader to reinstall a
      // backend that is already there.
      testInfo.skip(
        !candidate,
        `No installed backend (${installed.map((agent: Agent) => agent.backend).join(', ')}) exposes a model to route. `
        + 'Tick at least one model in a backend\'s model menu, or install a backend whose menu is builtin.',
      );
      if (!alreadyHub) {
        sourcesBeforeSwitch = new Set((await api.sources()).map((source) => source.id));
        // Recorded BEFORE the PATCH is awaited, not after it succeeds: the
        // server commits the mode before the response leaves, so a response
        // lost to a timeout or disconnect rejects that await with the instance
        // already mutated. A marker assigned after the await would skip the
        // Direct restore on exactly that path.
        switched = candidate!.backend;
        await api.setAgentMode(candidate!.backend, 'hub');
      }

      const live = (await api.agents()).find((agent: Agent) => agent.backend === candidate!.backend);
      expect(
        live?.mode,
        `Backend ${candidate!.backend} did not take the Gateway-mode switch the instance accepted.`,
      ).toBe('hub');
      // The backend's currently selected model when it is one of the routable
      // ones: a guard that interrupts the model the backend actually runs is the
      // interruption the product is warning about, and B7 reads that warning.
      const supply = live?.model_supply ?? [];
      const model =
        supply.find((entry) => entry.model_id === live?.selected_model_id)?.model_id
        ?? supply[0]?.model_id;
      // An assertion, not a skip, and it stays one now that the candidate was
      // chosen for having a menu: an empty supply here means the backend's menu
      // said it had models and the Gateway-mode projection of that same menu
      // says it has none. That is the product contradicting itself, not a
      // precondition this instance failed to meet.
      expect(
        model,
        `Backend ${candidate!.backend} offered ${routableModels(candidate!).join(', ')} before the `
        + 'Gateway-mode switch, but lists no models after it, so it has no route to open.',
      ).toBeDefined();

      await provide({
        backend: candidate!.backend,
        model: model!,
        sources: created,
      });
    } finally {
      // Put the instance back as it was found: the mode only if this fixture
      // changed it, and always this suite's own sources. Two nested blocks,
      // because the two restorations are independent promises: a mode PUT that
      // throws must not skip the source sweep, or the two route sources
      // outlive the spec and every later surface check inherits them.
      try {
        if (switched) await api.setAgentMode(switched, 'direct');
      } finally {
        // The transition-imported native source (if the switch made one) goes
        // before the prefix sweep, because it does not carry the prefix and the
        // sweep cannot see it. `restoreNativeSources` owns which source that is
        // — by channel, not by "new and unfamiliar" — so this site and A3
        // cannot answer the question differently.
        // Its own finally: a native deletion that still throws must not skip
        // the prefix sweep, or the suite's own route sources outlive the spec
        // alongside the failure that already stopped it.
        try {
          if (sourcesBeforeSwitch) await restoreNativeSources(api, sourcesBeforeSwitch);
        } finally {
          await api.removeSuiteSources();
        }
      }
    }
  },
});

export { expect } from './fixtures';
