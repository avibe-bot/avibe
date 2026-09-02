// Suite A — the capability gate and the gateway runtime switch.
//
// Scenario IDs are from docs/plans/model-hub-e2e-test-plan.md §3.
import {
  MODEL_HUB_DISABLED_REDIRECT,
  MODEL_HUB_SETTINGS_PATH,
} from '../src/components/settings/models/modelHubRoutes';
import type { Agent } from './support/api';
import { surfaceKind } from './support/api';
import { hub as copy } from './support/copy';
import {
  expect,
  requireModelHub,
  requireRuntimeRunning,
  runtimeIsRunning,
  test,
} from './support/fixtures';
import { restoreNativeSources, withRuntimeRestored } from './support/restore';

test.describe('A · capability gate and runtime lifecycle', () => {
  test('A1 · the capability gate, not the browser, decides the route', async ({ page, hub, api }) => {
    // One test covers both halves on purpose. Which half runs is a property of
    // the instance, and the assertion that matters is that the browser obeys
    // whatever the backend declared — splitting it into an "enabled" spec and a
    // "disabled" spec would leave one of them permanently skipped and hide the
    // fact that nobody ever checked the other direction.
    const enabled = await api.modelHubEnabled();
    await hub.goto();

    if (enabled) {
      await expect(page).toHaveURL(new RegExp(`${MODEL_HUB_SETTINGS_PATH}$`));
      await expect(hub.shell).toBeVisible();
      await expect(page.getByRole('heading', { name: copy('shell.title'), level: 1 })).toBeVisible();
      return;
    }
    await expect(page).toHaveURL(new RegExp(`${MODEL_HUB_DISABLED_REDIRECT}$`));
    await expect(hub.shell).toHaveCount(0);
  });

  test('A2 · the switch offers to install when the gateway is not installed', async ({ hub, api }) => {
    await requireModelHub(api);
    const runtime = await api.runtime();
    test.skip(
      runtime?.status?.health !== 'not_installed',
      'The install entry point exists only in the not-installed state — an installed runtime (whatever '
        + 'its health) and an in-progress installation both render different closed states with the '
        + 'switch disabled or busy, so this spec covers neither. '
        + 'Point VIBE_E2E_BASE_URL at a fresh hermetic instance to cover it (see ui/e2e/README.md).',
    );
    // `not_installed` with an `unsupported` manifest is a different closed
    // state the product renders on purpose, with the switch disabled: there is
    // no install to offer on that host, and asserting the install path there
    // would report the unsupported state as a product failure. The product's
    // own gate (`runtimeCanAttemptInstall`) decides which of the two it is.
    test.skip(
      runtime?.manifest.resolution === 'unsupported',
      'The runtime manifest resolves to unsupported on this host, so the product offers no install — '
        + 'that is the closed state it renders, not a defect in the install path.',
    );

    await hub.goto();
    // The closed state has to say the gateway is missing before the switch is
    // touched: a user who cannot install has to learn that from the page.
    await expect(hub.closedState).toContainText(copy('shell.closed.notInstalled.title'));
    await expect(hub.runtimeToggle).toHaveAttribute('aria-label', copy('shell.toggle.turnOn'));

    await hub.runtimeToggle.click();
    await expect(hub.installDialog).toBeVisible();
    await expect(hub.installDialog).toContainText(copy('install.section.effects'));
    // Cancel rather than install: downloading and unpacking a release is the
    // operator's setup step, not something a UI test should do to a machine.
    await hub.installDialog.getByRole('button', { name: copy('install.cancel'), exact: true }).first().click();
    await expect(hub.installDialog).toHaveCount(0);
  });

  test('A2 · turning the gateway off and back on round-trips', async ({ hub, api }) => {
    // The lifecycle alone can lawfully take 150s on a slow host (60s stop +
    // 90s verified restart), and per-expect timeouts do not extend the test
    // timeout — the config's 90s cap used to be able to cancel the spec while
    // its finally was still proving the runtime back up, failing the run AND
    // closing the page over an unconfirmed shared runtime.
    test.setTimeout(240_000);
    await requireModelHub(api);
    const runtime = await api.runtime();
    test.skip(
      !runtimeIsRunning(runtime?.status?.health),
      'The gateway is not running on this instance, so there is no running state to stop.',
    );
    const hubBackends = (await api.agents()).filter((agent) => agent.mode === 'hub');
    test.skip(
      hubBackends.length > 0,
      `Stopping is blocked while ${hubBackends.map((a) => a.backend).join(', ')} is in Gateway mode — that is scenario A3.`,
    );

    await hub.goto();
    await expect(hub.runtimeToggle).toHaveAttribute('aria-label', copy('shell.toggle.turnOff'));
    await expect(hub.runtimeToggle).toHaveAttribute('aria-checked', 'true');

    // The stopping click is INSIDE the boundary, not above it. The debt is owed
    // from the moment the stop is ASKED FOR — the request is already gone when
    // the click's own promise settles, so a page or browser that disconnects
    // mid-flight rejects it with the server still completing the stop, and a
    // boundary opened below this line is skipped on exactly that path.
    // `support/restore.ts` owns both halves: where the boundary starts, and that
    // "up" is a postcondition read from BOTH runtime facts rather than a second
    // guess at which click is owed.
    await withRuntimeRestored(api, async () => {
      await hub.runtimeToggle.click();
      await expect(hub.runtimeToggle).toHaveAttribute('aria-checked', 'false', { timeout: 60_000 });
      // The page must explain the stopped gateway, not just flip a switch.
      await expect(hub.closedState).toBeVisible();
      expect((await api.runtime())?.enabled).toBe(false);

      // The return half of the round trip is the SCENARIO, so it happens here,
      // in the body, through the switch the user would use. It used to live in
      // the teardown, which made the spec's own subject a side effect of its
      // cleanup — and left the assertions below reporting a restart nobody had
      // asserted as a step.
      await hub.runtimeToggle.click();
      await expect(hub.runtimeToggle).toHaveAttribute('aria-checked', 'true', { timeout: 90_000 });
      await expect(hub.closedState).toHaveCount(0);
    });
  });

  test('A3 · the blocked stop names the backends that block it', async ({ hub, api }) => {
    await requireModelHub(api);
    // RUNNING, not merely enabled: the UI computes `stopBlocked` from
    // `runtimeEnabled` plus a Gateway-mode backend without consulting health
    // (`SettingsModelsPage.tsx`), so an enabled-but-`down` instance would pass
    // every assertion below while never exercising the guard on a gateway that
    // could actually be stopped.
    await requireRuntimeRunning(api);

    const restore: string[] = [];
    // Same snapshot discipline as the gateway fixture: the Direct→Gateway
    // switch is not mode-only on a machine whose CLI holds a native login — it
    // imports a `native_cli` subscription source as part of the transition, and
    // that source carries no suite prefix the sweep could find.
    let sourcesBeforeSwitch: Set<string> | null = null;
    const hubBackends = (await api.agents()).filter((agent) => agent.mode === 'hub').map((a) => a.backend);
    try {
      if (hubBackends.length === 0) {
        const candidate = (await api.agents()).find((agent: Agent) => agent.cli_present);
        test.skip(!candidate, 'No agent backend is present on this instance to put into Gateway mode.');
        sourcesBeforeSwitch = new Set((await api.sources()).map((source) => source.id));
        // Recorded BEFORE the PATCH is awaited: the server commits the mode
        // before its response leaves, so a lost or timed-out response rejects
        // the await with the instance already switched — a marker assigned
        // after the await skips the Direct restore on exactly that path.
        // (`sourcesBeforeSwitch` above is snapshotted first for the same
        // reason.) Every exit from here passes through the finally, so the
        // backend cannot stay in Gateway mode on any path out of this spec.
        restore.push(candidate!.backend);
        await api.setAgentMode(candidate!.backend, 'hub');
        // An accepted switch that a later read does not show is a persistence
        // or projection regression, not a precondition: the server's
        // `set_agent_mode` commits the requested mode without requiring an
        // eligible source, so `hub` here is the contract. A skip would turn
        // that regression into a green hole.
        const now = (await api.agents()).filter((agent) => agent.mode === 'hub').map((a) => a.backend);
        expect(
          now,
          `Backend ${candidate!.backend} accepted the Gateway-mode switch but the next /agents read does not show it.`,
        ).toContain(candidate!.backend);
      }

      const names = (await api.agents()).filter((a) => a.mode === 'hub').map((a) => a.backend).join(', ');
      await hub.goto();
      // The product refuses the stop AND says who is holding it. A generic
      // "cannot stop" would satisfy the disabled state and fail the user.
      await expect(hub.runtimeToggle).toBeDisabled();
      await expect(hub.runtimeToggle).toHaveAttribute(
        'aria-label',
        copy('shell.toggle.stopBlocked', { names }),
      );
    } finally {
      // Mode first (the switch imported the source into a Gateway context),
      // then the transition-created source — before any sweep, because it does
      // not carry the prefix. Which source that is belongs to
      // `restoreNativeSources`, shared with the gateway fixture, so the two
      // sites cannot drift into two different answers. Two nested blocks keep
      // the two restorations independent.
      try {
        for (const backend of restore) await api.setAgentMode(backend, 'direct');
      } finally {
        if (sourcesBeforeSwitch) await restoreNativeSources(api, sourcesBeforeSwitch);
        await api.removeSuiteSources();
      }
    }
  });

  test('C6-direct-home · a machine with no sources lands on the direct home', async ({ hub, api }) => {
    await requireModelHub(api);
    // The tab body only renders once the runtime is configurable; below that the
    // page owes a closed state, which is scenario A2's subject, not this one's.
    await requireRuntimeRunning(api);
    // Which surface this instance renders, asked once rather than re-derived
    // here: the direct home has TWO forms, and the one this scenario is about is
    // the one an installed backend puts a card in.
    const surface = surfaceKind(await api.agents(), await api.sources());
    test.skip(
      surface === 'gateway',
      'This instance already has sources or a Gateway backend, so the direct home is not its surface.',
    );
    test.skip(
      surface === 'direct-no-backend',
      'No agent backend CLI is installed, so the direct home renders its install prompt — there is no '
      + 'backend card and no way out of Direct mode to assert. Install a backend CLI (see ui/e2e/README.md).',
    );

    await hub.goto();
    await expect(hub.directHome).toBeVisible();
    await expect(hub.directHome).toContainText(copy('direct.card.current'));
    // The point of this surface is the way out of it, so the way out is what
    // gets asserted — not merely that the card rendered.
    await expect(
      hub.directHome.getByRole('button', { name: copy('direct.action.switchToGateway') }).first(),
    ).toBeVisible();
  });
});
