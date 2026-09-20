// Suite G — the supply guards, and the copy the failure paths owe the user.
//
// Scenario IDs are from docs/plans/model-hub-e2e-test-plan.md §3.
//
// Baseline discipline (§5a): `fix-first` scenarios land as `test.fixme` with
// the open decision named, never as red tests. Four of them do so below; each
// says what would have to change for it to become an assertion.
import { hasCopy, hub as copy } from './support/copy';
import { E2E_SOURCE_PREFIX, mockBaseUrl } from './support/env';
import {
  expectVisibleWithout,
  requireMockUpstream,
  requireModelHub,
  requireRuntimeRunning,
} from './support/fixtures';
import { expect, test } from './support/gateway';
import { fillApiKeyForm, labelledButton } from './support/hub';
import { captureAgentChain, restoreAgentChain } from './support/restore';

test.describe('G · supply guards and failure copy', () => {
  test.beforeEach(async ({ api, mock }) => {
    await requireModelHub(api);
    await requireRuntimeRunning(api);
    await requireMockUpstream(mock);
  });

  test('B7 · removing the only source of a route is refused, explained, then reported', async ({ hub, api, gateway }) => {
    const source = gateway.sources[0];
    const supplied = source.models[0]?.id;
    expect(
      supplied,
      `The precondition source ${source.display_name} came back with no models, so no route can reach it.`,
    ).toBeDefined();

    // Pruning the last manual hop inherits defaults. Both authorities must
    // depend solely on this owned source for deletion to create a genuine gap.
    const originalDefaults = await api.defaultSourceOrder(gateway.backend);
    const original = await captureAgentChain(api, gateway);
    const failures: unknown[] = [];
    try {
      // Protect both writes even when persistence succeeds but its response is lost.
      await api.setDefaultSourceOrder(gateway.backend, [source.id]);
      expect(await api.defaultSourceOrder(gateway.backend), 'B7 requires only the owned deletion source in defaults')
        .toEqual([source.id]);
      const hops = [{ source_id: source.id, model_id: supplied! }];
      const arranged = await api.putAgentChain(gateway.backend, gateway.model, hops);
      expect(arranged, 'The instance refused the arranged route, so no guard can be raised.').toBe(true);
      const readback = await api.agentChain(gateway.backend, gateway.model);
      expect(readback.manual_override, 'B7 requires the exact saved one-hop manual route').toEqual({ hops });
      expect(readback.chain.map(({ source_id, model_id }) => ({ source_id, model_id })),
        'B7 requires the owned source to be the only effective manual hop').toEqual(hops);

      await hub.goto();
      await hub.openSource(source.id);
      await expect(hub.sourceDetailDialog).toBeVisible();
      await hub.manageMenuTrigger(source.display_name).click();
      await hub.manageItem('delete_source').click();

      // 1 · the plain confirm. The menu item opens it with no evidence in it,
      // because the client has not asked the server anything yet — it only knows
      // that deleting a source is destructive.
      const guard = hub.dialogTitled(copy('guard.title.deleteSource', { source: source.display_name }));
      const confirm = guard.getByRole('button', {
        name: copy('guard.confirm.deleteSource'),
        exact: true,
      });
      await expect(guard).toBeVisible({ timeout: 30_000 });
      await expect(guard).toContainText(copy('guard.subtitle.deleteSource'));
      await expectVisibleWithout(guard, copy('guard.label'));

      // 2 · confirming ATTEMPTS the delete, and the SERVER refuses it. The same
      // dialog comes back carrying the refusal's own plan — which hops go, and
      // which models are left with nothing. That two-step shape is the scenario:
      // a source nothing routes through is deleted on the first confirm, and the
      // evidence below appears only because something is actually at stake.
      await confirm.click();
      await expect(guard).toContainText(copy('guard.label'), { timeout: 30_000 });
      await expect(guard).toContainText(copy('guard.gap.label'));
      await expect(guard).toContainText(copy('guard.hint.interrupt'));
      await expect(guard).toContainText(gateway.model);

      // 3 · confirm again, which re-sends the delete echoing that plan back.
      await confirm.click();

      // 4 · the impact report. Not a toast: the same facts restated in the past
      // tense, so a user who confirmed too fast can still read what they did.
      const report = hub.mutationReport('delete');
      await expect(report).toBeVisible({ timeout: 30_000 });
      await expect(report).toContainText(copy('sourceDetail.remove.impact.title'));
      await expect(report).toContainText(copy('guard.result.label'));
      await expect(report).toContainText(copy('guard.result.gapLabel'));
      await labelledButton(report, copy('sourceDetail.remove.impact.done')).click();
      await expect(report).toHaveCount(0);

      await expect
        .poll(async () => (await api.sources()).some((s) => s.id === source.id), { timeout: 15_000 })
        .toBe(false);
    } catch (error) {
      failures.push(error);
    } finally {
      try {
        await restoreAgentChain(api, gateway, original);
      } catch (error) {
        failures.push(error);
      }
      try {
        const remaining = new Set((await api.sources()).map((item) => item.id));
        expect(originalDefaults.filter((id) => id !== source.id && !remaining.has(id)),
          'B7 cannot restore defaults while unrelated captured sources are missing').toEqual([]);
        // Only this exact owned deletion may change the captured membership.
        const expected = originalDefaults.filter((id) => id !== source.id || remaining.has(id));
        await api.setDefaultSourceOrder(gateway.backend, expected);
        expect(await api.defaultSourceOrder(gateway.backend), 'B7 must restore exact default membership and order')
          .toEqual(expected);
      } catch (error) {
        failures.push(error);
      }
    }
    if (failures.length === 1) throw failures[0];
    if (failures.length) throw new AggregateError(failures, 'B7 failed with independent restoration errors');
  });

  // G1 (fix-first) — "guard echo with a gap missing `agents` → no confirm-loop".
  // The echo is a request body field. A browser cannot send a malformed echo
  // without the product building it, and this lane may not add a product hook to
  // corrupt it, so the probe is not reachable from Playwright at all. It belongs
  // to the pytest lane; recorded here so the scenario is not silently uncovered.
  test.fixme('G1 · a guard echo missing `agents` does not re-open the guard', async () => {
    // Blocked on: open decision B7 / §5 — and on the echo being unreachable from
    // a browser. See docs/plans/model-hub-e2e-test-plan.md §3 G1.
  });

  // B11 (fix-first, baseline expected-fail per open decision D-3) — every server
  // failure must reach the user as human copy, never as a raw
  // `modelHub.errors.*` key. `AddApiKeyDialog.tsx` passes `failure.detail`
  // straight through as an i18n key when it starts with that prefix, and no
  // `modelHub.*` namespace exists in either browser bundle, so the key renders
  // as itself. The form is filled and SUBMITTED, because the defect only shows
  // in the answer to a submission that failed: an unsubmitted dialog shows
  // nothing to leak, and asserting on one would pass with the defect present.
  test.fixme('B11 · a server failure code is rendered as human copy, not as its key', async ({ hub, mock }) => {
    // Unfixme when D-3 lands the missing `modelHub.*` keys. The check itself:
    // drive a failure whose detail carries the prefix, then assert no visible
    // text looks like a bare i18n key.
    await mock.configure({ auth: '5xx', protocol: 'anthropic', models_endpoint: 'ok' });
    await hub.goto();
    await hub.addApiKeyButton.click();
    await expect(hub.addKeyDialog).toBeVisible();
    await fillApiKeyForm(hub.addKeyDialog, {
      name: `${E2E_SOURCE_PREFIX}b11-copy`,
      baseUrl: mockBaseUrl(),
      apiKey: 'e2e-copy-probe',
    });
    await hub.addKeyDialog.getByRole('button', { name: copy('addKey.detect'), exact: true }).click();
    await expect(hub.addKeyDialog).toContainText(/modelHub\.errors\.|settings\.models\./);
    // The defect: the raw key is what reaches the user.
    await expectVisibleWithout(hub.addKeyDialog, /modelHub\.errors\./);
  });

  // D13 (assert) — the member-role dead-ends. Reaching them needs a SECOND auth
  // context: a browser session authenticated as a member rather than as the
  // trusted loopback operator. Every request from Playwright's browser to
  // 127.0.0.1 is trusted by construction, which is exactly why the suite needs
  // no login — and exactly why it cannot demote itself. Covering this needs a
  // member-session bootstrap the harness does not have; reported as a follow-up,
  // not patched into product code.
  test.fixme('D13 · a member sees visible refusals, not silently dead controls', async () => {
    // Blocked on: no second auth context. Loopback is trusted, so this browser
    // is always the operator. See ui/e2e/README.md § "What this suite cannot
    // reach".
  });

  // A copy defect found while writing this suite, not a scenario from §3:
  // `SettingsModelsPage.tsx` renders `settings.models.sourceDetail.gone` when an
  // open source disappears underneath the panel, and that key exists in neither
  // `en.json` nor `zh.json` — so the user is shown the key. Same class as B11
  // and reported with it.
  test.fixme('B11-adjacent · a source that vanishes under the open panel says so in words', async () => {
    // The assertion is on the bundle, not on the browser: the panel only reaches
    // this state when a source is removed out from under an open dialog, and the
    // key being absent is the whole defect.
    expect(hasCopy('settings.models.sourceDetail.gone')).toBe(true);
  });
});
