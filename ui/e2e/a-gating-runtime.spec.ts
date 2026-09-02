// Suite A — the capability gate and the gateway runtime switch.
//
// Scenario IDs are from docs/plans/model-hub-e2e-test-plan.md §3.
import {
  MODEL_HUB_DISABLED_REDIRECT,
  MODEL_HUB_SETTINGS_PATH,
} from '../src/components/settings/models/modelHubRoutes';
import type { Agent } from './support/api';
import { hub as copy } from './support/copy';
import {
  expect,
  requireModelHub,
  requireRuntimeRunning,
  runtimeIsRunning,
  test,
} from './support/fixtures';
import { E2E_SOURCE_PREFIX } from './support/env';

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
      Boolean(runtime?.status?.installed_version),
      'The gateway is already installed on this instance, so the install entry point is not reachable. '
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

    await hub.runtimeToggle.click();
    // Wrapped in try/finally — and not just this click — because the gateway is
    // STOPPED at this point: if any assertion from here on fails, the instance
    // is left without its runtime for every spec after this one. The restart is
    // fully performed AND verified inside the finally — a click alone only
    // dispatches the request; the page can close while startup is still in
    // flight — so the original failure is re-raised over a running instance,
    // and the run's red is one problem, not one problem plus a dark instance
    // for every later spec.
    try {
      await expect(hub.runtimeToggle).toHaveAttribute('aria-checked', 'false', { timeout: 60_000 });
      // The page must explain the stopped gateway, not just flip a switch.
      await expect(hub.closedState).toBeVisible();
      expect((await api.runtime())?.enabled).toBe(false);
    } finally {
      // Conditional, not blind: the first click above may not have taken — the
      // request refused, or the stop stalled — and the runtime may still be
      // running. A cleanup click in THAT state performs a real stop and leaves
      // the shared instance dark for every later spec, which is worse than the
      // failure already being reported. Only a runtime the API agrees is OFF
      // gets turned back on.
      if ((await api.runtime())?.enabled === false) {
        await hub.runtimeToggle.click().catch(() => {});
      }
      // Verified restart, still inside the finally: the API is the authority —
      // the toggle's own aria state follows a request this browser may never
      // see answered.
      await expect
        .poll(async () => (await api.runtime())?.status?.health, { timeout: 90_000 })
        .toEqual(expect.stringMatching(/^(ok|degraded)$/));
    }
    await expect(hub.runtimeToggle).toHaveAttribute('aria-checked', 'true', { timeout: 90_000 });
    await expect(hub.closedState).toHaveCount(0);
  });

  test('A3 · the blocked stop names the backends that block it', async ({ hub, api }) => {
    await requireModelHub(api);
    const runtime = await api.runtime();
    test.skip(runtime?.enabled !== true, 'The gateway is off, so stopping cannot be blocked.');

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
        await api.setAgentMode(candidate!.backend, 'hub');
        // The cleanup boundary is ENTERED the moment the mutation succeeds:
        // every exit from here — including a throwing verification read or the
        // skip below — passes through the finally, so the backend cannot stay
        // in Gateway mode any path out of this spec can take.
        restore.push(candidate!.backend);
        const now = (await api.agents()).filter((agent) => agent.mode === 'hub').map((a) => a.backend);
        test.skip(
          now.length === 0,
          `Backend ${candidate!.backend} could not be switched to Gateway mode — it has no eligible source yet. `
            + 'Add a source first (see ui/e2e/README.md).',
        );
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
      // not carry the prefix. Best-effort per source: one already gone is
      // success. Two nested blocks keep the two restorations independent.
      try {
        for (const backend of restore) await api.setAgentMode(backend, 'direct');
      } finally {
        if (sourcesBeforeSwitch) {
          for (const source of await api.sources()) {
            if (!sourcesBeforeSwitch.has(source.id)
                && !source.display_name.startsWith(E2E_SOURCE_PREFIX)) {
              await api.deleteSource(source.id);
            }
          }
        }
        await api.removeSuiteSources();
      }
    }
  });

  test('C6-direct-home · a machine with no sources lands on the direct home', async ({ hub, api }) => {
    await requireModelHub(api);
    // The tab body only renders once the runtime is configurable; below that the
    // page owes a closed state, which is scenario A2's subject, not this one's.
    await requireRuntimeRunning(api);
    const agents = await api.agents();
    const sources = await api.sources();
    // The condition is the product's own (`modelsSurfaceKind`): every backend
    // direct AND no sources. Anything else is the gateway surface, and asserting
    // the direct home there would only prove the instance was dirty.
    test.skip(
      sources.length > 0 || agents.some((agent) => agent.mode !== 'direct'),
      'This instance already has sources or a Gateway backend, so the direct home is not its surface.',
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
