// Suite B — the Add API key dialog, end to end against a controllable upstream.
//
// Scenario IDs are from docs/plans/model-hub-e2e-test-plan.md §3.
//
// Every spec here needs an upstream whose answers it can choose, so every spec
// here skips without one. The mock is reached ONLY over HTTP at
// `VIBE_E2E_MOCK_UPSTREAM_URL`, and its URL enters the product the same way a
// user's would: typed into the dialog's Base URL field.
import type { HubApi, RouteHop } from './support/api';
import { hub as copy } from './support/copy';
import { E2E_SOURCE_PREFIX, mockBaseUrl } from './support/env';
import {
  expectVisibleWithout,
  requireMockUpstream,
  requireModelHub,
  requireRuntimeRunning,
} from './support/fixtures';
// `gateway` rather than `fixtures` for the auto `hubSurface` fixture it carries:
// the Add API key button lives on the gateway surface, and an instance with no
// sources shows the direct home instead. See support/gateway.ts.
import { expect, test } from './support/gateway';
import { fillApiKeyForm } from './support/hub';
import { anthropicInventory } from './support/mock';
import { captureAgentChain, restoreAgentChain } from './support/restore';

/**
 * Runs the routed dry run until the source has settled on a verdict about its
 * KEY, and returns that verdict's detail key.
 *
 * Both halves are load-bearing, and neither works alone:
 *
 *   - drive with the probe, because a dry run is the only thing that reaches the
 *     upstream with a real request; a refetch reads the model list, which an
 *     upstream that rejects completions still serves.
 *   - read the SOURCE, because the first probe that fails also makes the chain
 *     unrunnable — every probe after it is refused `409 probe_no_candidate` and
 *     carries no verdict at all. A poll on the probe's own answer goes null
 *     forever, one round after the state it was waiting for arrived.
 *
 * A healthy instance answers on the first dry run. The loop exists for the one
 * state that is not an answer: a cooldown, which is what an unreachable upstream
 * settles as, and which the state itself dates in `retry_at`. Sleeping to that
 * instant is the difference between one more attempt and a busy wait; the budget
 * is what stops it from waiting forever on an upstream that is genuinely gone.
 */
const settleKeyVerdict = async (
  api: HubApi,
  sourceId: string,
  route: { backend: string; model: string },
  budgetMs = 90_000,
): Promise<string | null> => {
  const deadline = Date.now() + budgetMs;
  let last: string | null = null;
  while (Date.now() < deadline) {
    await api.probeAgent(route.backend, route.model);
    const state = (await api.sources()).find((source) => source.id === sourceId)?.state;
    last = (state?.detail_key as string | null) ?? null;
    // `needs_action` is terminal by construction: nothing but the user acts on
    // it. A cooldown is the opposite — it expires, and then the next dry run is
    // the one that learns what is actually wrong.
    if (state?.status === 'needs_action' || state?.status === 'error') return last;
    const retryAt = typeof state?.retry_at === 'string' ? Date.parse(state.retry_at) : Number.NaN;
    const resumeAt = Number.isNaN(retryAt) ? Date.now() + 1_000 : retryAt + 500;
    const pause = Math.min(Math.max(resumeAt - Date.now(), 500), deadline - Date.now());
    if (pause <= 0) break;
    await new Promise((resolve) => setTimeout(resolve, pause));
  }
  return last;
};

// Each protocol's segment button carries its own label, and the observation is
// supposed to reach the same verdict for all three without being told which.
const PROTOCOLS = [
  { id: 'anthropic', label: copy('addKey.protocol.anthropicMessages') },
  { id: 'openai_responses', label: copy('addKey.protocol.openaiResponses') },
  { id: 'openai_chat', label: copy('addKey.protocol.openaiChatCompletions') },
] as const;

test.describe('B · add an API-key source', () => {
  test.beforeEach(async ({ api, mock }) => {
    await requireModelHub(api);
    await requireRuntimeRunning(api);
    await requireMockUpstream(mock);
  });

  // Every spec adds sources; none of them may outlive the spec that made them.
  // Teardown runs on failure too, which is the case that matters: a red test
  // that also poisons the next five is two problems reported as six.
  test.afterEach(async ({ api }) => {
    await api.removeSuiteSources();
  });

  for (const protocol of PROTOCOLS) {
    test(`B1 · Add identifies a ${protocol.id} upstream without being told`, async ({ hub, mock, api }) => {
      await mock.configure({ auth: 'ok', protocol: protocol.id, models_endpoint: 'ok' });
      const name = `${E2E_SOURCE_PREFIX}auto-${protocol.id}`;

      await hub.goto();
      await hub.addApiKeyButton.click();
      await expect(hub.addKeyDialog).toBeVisible();
      // No protocol is chosen: "Auto detect" is the default, and the response
      // shape is the only evidence the product is allowed to use.
      await fillApiKeyForm(hub.addKeyDialog, { name, baseUrl: mockBaseUrl(), apiKey: 'e2e-add' });
      await hub.addKeyDialog.getByRole('button', { name: copy('addKey.detect'), exact: true }).click();
      await hub.addKeyDialog.getByRole('button', { name: copy('addKey.confirm'), exact: true }).click({ timeout: 30_000 });

      // Success is defined by where the user lands, not by a toast: the dialog
      // closes and the new source's detail panel opens.
      await expect(hub.addKeyDialog).toHaveCount(0, { timeout: 30_000 });
      await expect(hub.sourceDetailDialog).toBeVisible();
      await expect(hub.sourceDetailDialog).toContainText(name);

      const created = (await api.sources()).find((source) => source.display_name === name);
      expect(created?.protocol).toBe(protocol.id);
    });
  }

  test('B1 · a rejected credential is named as a credential problem', async ({ hub, mock }) => {
    await mock.configure({ auth: '401', protocol: 'anthropic', models_endpoint: 'ok' });

    await hub.goto();
    await hub.addApiKeyButton.click();
    // The suite prefix is on this "doomed" create too: if the regression under
    // test commits the source before reporting the failure, teardown still has
    // to be able to sweep it. A failure path that can leave state behind is a
    // failure path that poisons whichever spec runs next.
    await fillApiKeyForm(hub.addKeyDialog, {
      name: `${E2E_SOURCE_PREFIX}auth-rejected`,
      baseUrl: mockBaseUrl(),
      apiKey: 'e2e-bad-key',
    });
    await hub.addKeyDialog.getByRole('button', { name: copy('addKey.detect'), exact: true }).click();

    // The three lines are one message: what went wrong, where to look, and that
    // Add cannot proceed until it is fixed. A test that asserted only the first
    // would pass on a dialog that has stopped explaining itself.
    await expect(hub.addKeyDialog).toContainText(copy('addKey.fail.auth'), { timeout: 30_000 });
    await expect(hub.addKeyDialog).toContainText(copy('addKey.fail.auth.detail'));
    await expect(hub.addKeyDialog).toContainText(copy('addKey.fail.subtitle'));
    await expect(hub.addKeyDialog).toBeVisible();
  });

  test('B2 · naming the wrong interface is refused, and says why', async ({ hub, mock }) => {
    // The upstream speaks OpenAI Chat Completions; the user insists on Anthropic
    // Messages. The probe path then 404s, so nothing proves the interface.
    await mock.configure({ auth: 'ok', protocol: 'openai_chat', models_endpoint: 'ok' });

    await hub.goto();
    await hub.addApiKeyButton.click();
    await fillApiKeyForm(hub.addKeyDialog, {
      // Prefixed for the same reason as B1's rejected credential: a mismatch
      // that commits anyway must still be sweepable.
      name: `${E2E_SOURCE_PREFIX}mismatch`,
      baseUrl: mockBaseUrl(),
      apiKey: 'e2e-mismatch',
      protocol: copy('addKey.protocol.anthropicMessages'),
    });
    await hub.addKeyDialog.getByRole('button', { name: copy('addKey.detect'), exact: true }).click();

    // "Connected and authenticated, but we cannot tell which interface" — the
    // distinction from a credential failure is the whole point of the copy.
    await expect(hub.addKeyDialog).toContainText(copy('addKey.undetermined.title'), { timeout: 30_000 });
    await expect(hub.addKeyDialog).toContainText(copy('addKey.undetermined.detail'));
    await expectVisibleWithout(hub.addKeyDialog, copy('addKey.fail.auth'));
    // Retry stays available because a different choice is a different probe.
    await expect(
      hub.addKeyDialog.getByRole('button', { name: copy('addKey.retry'), exact: true }),
    ).toBeEnabled();
  });

  test('B3 · Detect reports the count, and only ids survive the save', async ({ hub, mock, api }) => {
    // The mock returns rich rows on purpose: `display_name`, `context_length`
    // and `pricing` all come back and all are dropped. §3 marks B3
    // `assert-current` — this documents the drop, it does not bless it.
    const inventory = anthropicInventory(['e2e-alpha', 'e2e-beta', 'e2e-gamma']);
    await mock.configure({
      auth: 'ok',
      protocol: 'anthropic',
      models_endpoint: 'ok',
      models: inventory,
    });
    const name = `${E2E_SOURCE_PREFIX}inventory`;

    await hub.goto();
    await hub.addApiKeyButton.click();
    await fillApiKeyForm(hub.addKeyDialog, { name, baseUrl: mockBaseUrl(), apiKey: 'e2e-pull' });

    // Detect is the mandatory pre-flight: it reports, it does not save.
    await hub.addKeyDialog.getByRole('button', { name: copy('addKey.detect'), exact: true }).click();
    await expect(hub.addKeyDialog).toContainText(
      copy('addKey.pull.result', { count: inventory.length }),
      { timeout: 30_000 },
    );
    expect(await api.sources()).not.toContainEqual(expect.objectContaining({ display_name: name }));

    await hub.addKeyDialog.getByRole('button', { name: copy('addKey.confirm'), exact: true }).click({ timeout: 30_000 });
    await expect(hub.sourceDetailDialog).toBeVisible({ timeout: 30_000 });

    const created = (await api.sources()).find((source) => source.display_name === name);
    expect(created?.models.map((model) => model.id).sort()).toEqual(
      inventory.map((model) => model.id).sort(),
    );
    for (const model of created?.models ?? []) {
      // assert-current: everything the upstream said about a model except its id
      // is discarded on the way to storage.
      expect(Object.keys(model).sort()).toEqual(
        ['discovered_at', 'display_name', 'id', 'origin', 'reasoning_efforts', 'retired'],
      );
      expect(model.display_name).toBeNull();
      expect(model.reasoning_efforts).toEqual([]);
    }
  });

  test('B4 · a proven interface with no model list can still be added, on the record', async ({ hub, mock, api }) => {
    // The interface answers; only discovery is broken. That is a different
    // situation from "we do not know what this is", and the product treats it
    // as one: it offers to add the source anyway. `models_endpoint: http_500`
    // is exactly this shape: the interface probes 200, `/v1/models` 500s.
    await mock.configure({ auth: 'ok', protocol: 'anthropic', models_endpoint: 'http_500' });
    const name = `${E2E_SOURCE_PREFIX}no-inventory`;

    await hub.goto();
    await hub.addApiKeyButton.click();
    await fillApiKeyForm(hub.addKeyDialog, { name, baseUrl: mockBaseUrl(), apiKey: 'e2e-empty' });
    await hub.addKeyDialog.getByRole('button', { name: copy('addKey.detect'), exact: true }).click();

    await expect(hub.addKeyDialog).toContainText(copy('addKey.inventory.title'), { timeout: 30_000 });
    const addAnyway = hub.addKeyDialog.getByRole('button', {
      name: copy('addKey.addAnyway'),
      exact: true,
    });
    await expect(addAnyway).toBeVisible();
    await addAnyway.click();

    await expect(hub.sourceDetailDialog).toBeVisible({ timeout: 30_000 });
    const created = (await api.sources()).find((source) => source.display_name === name);
    // The source commits, and it commits carrying the failure — a source that
    // came in this way must not look identical to one that discovered cleanly.
    expect(created?.protocol).toBe('anthropic');
    expect(created?.models).toEqual([]);
    expect(created?.state.status).toBe('error');
  });

  test('B6 · replacing the key reuses the same dialog, and reports what it did', async ({ hub, mock, api, gateway }) => {
    // The only spec in the suite that may have to sit out a 30-second cooldown
    // before its precondition even exists. See `settleKeyVerdict`.
    test.setTimeout(300_000);
    // A source offers to have its key replaced only once the key has actually
    // been rejected: `repairAction` reads `needs_action.credential_revoked`, and
    // that button is the dialog's ONLY entry point in replace mode. Nothing
    // about a healthy source opens it, so the rejection has to be arranged —
    // and arranged the way a real one happens, since a refetch will not do it.
    // The upstream still serves its model list to an unauthorised caller; only a
    // request that runs down the chain learns the key is dead.
    let source = gateway.sources[0];
    const supplied = source.models[0]?.id;
    const arrange = async (): Promise<void> => {
      expect(
        supplied,
        `The precondition source ${source.display_name} came back with no models, so no route can reach it.`,
      ).toBeDefined();
      const arranged = await api.putAgentChain(gateway.backend, gateway.model, [
        { source_id: source.id, model_id: supplied! },
      ]);
      expect(arranged, 'The instance refused the arranged route, so the key cannot be rejected.').toBe(true);
    };

    // The operator's chain only. The retry below deletes and recreates the
    // precondition source, so a baseline carrying its hop would name a source id
    // that no longer exists — and that refusal is not partial.
    const original: RouteHop[] = await captureAgentChain(api, gateway);
    try {
      // The restoration boundary is entered BEFORE the arranged PUT (inside
      // `arrange` below), not after it returns: a PUT whose response is lost
      // or times out rejects that await with the chain already replaced
      // server-side, and a finally outside it would leave the user's route
      // swapped for the arrangement — then the fixture's source sweep empties
      // it.
      await arrange();

      // `cooldown.server_error` is the one answer that is not an answer: the
      // gateway engine fails the dry run itself, without the upstream ever
      // receiving it, and every retry re-arms a fresh thirty-second cooldown on
      // top of the last — roughly one run in three (#1818). Recreating the source
      // sometimes clears it; waiting, restarting the engine, and cycling the whole
      // gateway do not. So a sticky cooldown is retried by rebuilding the
      // arrangement, bounded to three attempts.
      //
      // A defect that survives all three attempts is #1818's exact signature
      // (5xx + the upstream received nothing + the cooldown re-arming), and the
      // run then retires ITSELF via `test.fixme` naming #1818, so an
      // intermittent defect never burns the suite red. Any OTHER verdict still
      // fails hard below; a wrong classification is what the scenario exists to
      // catch.
      //
      // `test.fixme`, not `test.fail`. `test.fail` sets the expected status of
      // the whole TEST — its finally, the suite's afterEach, and the gateway
      // fixture's teardown included — so a restoration that then failed would
      // satisfy "expected to fail" and the run would report green over a
      // displaced route chain. Measured, not assumed: a mid-body `test.fail`
      // with a throwing finally reports PASSED, while `test.fixme` in the same
      // shape reports the teardown failure. Fixme also states the outcome
      // honestly — one skipped spec naming the issue, rather than a green tick
      // over a scenario that was never reached.
      // The #1818 signature is TWO facts together, and both are checked before
      // the marker fires: the source in `cooldown.server_error`, AND the mock
      // having received no DRY RUN while it got there. A cooldown with the dry
      // run on the log is a different defect — the upstream answered 401 and
      // something classified it badly — and it must fail hard, not ride #1818's
      // marker to green. Two discrimination rules keep that line honest:
      //
      //   - Only a POST down a protocol path counts (`/v1/messages`,
      //     `/v1/responses`, `/v1/chat/completions`): the 401 the marker
      //     exists to route travels only on the dry run, while a `GET
      //     /v1/models` is model-list traffic an engine reads at startup — and
      //     #1818's engine restarts mid-settle, so startup GETs land on a
      //     post-reset log without any completion ever being attempted.
      //   - The marker reads the FINAL attempt's log, not an OR across
      //     attempts: an earlier attempt's dial that still ended in a sticky
      //     cooldown says nothing about the attempt that produced the verdict
      //     being marked, and OR-ing them let #1818 ride in green under an
      //     earlier attempt's history.
      const upstreamSawDryRun = async (): Promise<boolean> => {
        const requests = await mock.requests();
        return requests.some(
          (request) => request.method === 'POST'
            && /^\/v1\/(messages|responses|chat\/completions)$/.test(request.path.split('?')[0]),
        );
      };
      await mock.resetRequests();
      await mock.configure({ auth: '401' });
      let verdict = await settleKeyVerdict(api, source.id, gateway);
      // Per-attempt, not cumulative: the marker at the end must be able to say
      // "the attempt that produced THIS verdict never reached the upstream."
      let upstreamReceived = await upstreamSawDryRun();
      for (let attempt = 0; attempt < 3 && verdict === 'models.source.cooldown.server_error'; attempt += 1) {
        // The recreation has to happen against a HEALTHY upstream — the create
        // probes the source before committing it, and the mock is still set to
        // 401 for the settle. The rejection is re-armed after the new source
        // exists, by the same dry run as before.
        await mock.configure({ auth: 'ok' });
        await api.deleteSource(source.id);
        const rebuilt = await api.createApiKeySource(source.display_name, mockBaseUrl());
        if (!rebuilt) {
          // The instance that cannot rebuild this arrangement is a product
          // failure wearing the cooldown's clothes — named, not skipped, and not
          // #1818 either.
          throw new Error('The instance refused to recreate the precondition source.');
        }
        expect(
          rebuilt.models.some((model) => model.id === supplied),
          'The recreated source lost the model the route depends on.',
        ).toBe(true);
        source = rebuilt;
        await arrange();
        await mock.configure({ auth: '401' });
        // One cooldown window (plus slack) is enough for a retry attempt: the
        // defect this loop exists for re-arms the cooldown on EVERY probe, so a
        // second window after the first re-armed would only burn another 30 s on
        // the way to the same verdict. The FIRST settle keeps the full budget,
        // where a genuine one-off transient still gets its chance to clear.
        await mock.resetRequests();
        verdict = await settleKeyVerdict(api, source.id, gateway, 35_000);
        upstreamReceived = await upstreamSawDryRun();
      }
      test.fixme(
        verdict === 'models.source.cooldown.server_error' && !upstreamReceived,
        'Engine 5xx on the routed dry run (#1818): the upstream never received the request and the '
          + 'cooldown re-armed on every retry, so the replace-key flow was never reachable this run.',
      );
      expect(verdict).toBe('models.source.needs_action.credential_revoked');
      // The replacement has to be a key that works, or the dialog would be
      // reporting the same failure over again under a different name.
      await mock.configure({ auth: 'ok' });

      await hub.goto();
      await hub.openSource(source.id);
      await expect(hub.sourceDetailDialog).toBeVisible();

      // What a stopped row owes the user is one tap to the fix, not an error
      // string. The attribute is the product's own statement of where that tap
      // goes, and the label is what the user reads on it.
      const repair = hub.sourceDetailDialog.locator('[data-repair-destination="replace_key_dialog"]');
      await expect(repair).toHaveText(copy('repair.replaceKey'));
      await repair.click();

      const dialog = hub.addKeyDialog;
      await expect(dialog).toBeVisible();
      await expect(dialog).toContainText(copy('repair.replaceTitle', { name: source.display_name }));

      await dialog.getByLabel(copy('repair.replaceLabel'), { exact: true }).fill('e2e-second-key');
      await dialog.getByRole('button', { name: copy('repair.replaceSubmit'), exact: true }).click();

      // The upstream still lists the same models, so the new key costs the route
      // nothing — a plain repair, and the dialog says which of the two it was.
      await expect(dialog).toContainText(copy('repair.repaired'), { timeout: 30_000 });
      // And the row it was raised from is no longer stopped. A dialog that
      // reports a repair over a source still marked blocked has reported a
      // repair that did not happen.
      await expect
        .poll(
          async () => (await api.sources()).find((s) => s.id === source.id)?.state.detail_key,
          { timeout: 15_000 },
        )
        .toBeNull();
    } finally {
      // Whatever happened above, the instance's chain for this model goes back
      // to what it was — the arrangement was the scenario's, not the user's.
      await restoreAgentChain(api, gateway, original);
    }
  });
});
