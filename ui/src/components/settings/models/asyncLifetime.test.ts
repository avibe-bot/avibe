// Two interleavings that shipped broken, both of the same shape: an async arrival
// changed what the user sees without first checking the state the arrival landed
// in. They are pinned here as sequences, because neither is reachable from a
// single call — only from a specific ORDER of calls.
//
//   1. close-during-PUT, reopen-before-land. The drawer saves, the user closes it
//      (which unmounts it) and reopens it before the PUT lands, so it seeds from
//      props that still carry the pre-save order. When the PUT does land and the
//      refetch delivers the new saved order, the reopened drawer keeps showing the
//      old one — and its next edit diffs against that stale snapshot and writes the
//      old order back, silently undoing the save the user already saw succeed.
//
//   2. late-poll-after-settle. The connect dialog reaches success, and the poll
//      request already in flight comes back `verifying`. The flow the dialog shows
//      is replaced by a non-terminal one, so a connect that succeeded renders as
//      still running — and the same hole in the deadline branch stamps `failed`
//      over a success once the timeout passes.
//
//   3. cancel-after-terminal. A teardown cancels the flow it opened, reading a
//      snapshot that says `awaiting_action` — while the poll in flight is about to
//      report success. `oauth_cancel` materializes a terminal flow instead of
//      cancelling it, so that call IS the write, and the rows the page keeps
//      showing were read before it happened.
//
//   4. cancel-after-handoff. The dialog closes while its start is still in flight
//      and a replacement opens; the server's pending-flow reuse hands the
//      replacement THAT SAME flow. The first journey's cleanup then cancels the
//      login the user is watching in the dialog that replaced it. Its mirror image
//      is the same close-then-reopen for a DIFFERENT row: the reuse is keyed by
//      source id while ownership of the dialog's ref is not, so withholding the
//      cancel there abandons an authorization no successor can ever adopt.
//
//   5. poll-error-during-submit. The 2s status read fails at the moment the paste
//      submit it raced commits. Latching the READER's error settles the view, so
//      the submit's success — the only arrival that carries the terminal tail — is
//      then correctly ignored, and the dialog reports 授权失败 over a credential the
//      server did replace.
//
//   6. start-rejected-after-close. A reauth's start rejects AFTER the dialog closed
//      and after `mark_native_irreversible_start` committed. The close path re-read
//      the rows, but it did so while that request was still in flight, so it can
//      have read them before the write — and the rejection path returned without a
//      second read, leaving healthy rows on a page whose source is 需处理.
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

import {
  createFlowAuthority,
  createLatestAsyncAuthority,
  flowLetGo,
  flowStep,
  initialSeedState,
  isDone,
  pollFailureSettles,
  releaseFlow,
  savedMappingsKey,
  savedMenuKey,
  savedSourcesKey,
  seedStep,
  startNeedsStatusRead,
  terminalArrivalMovedRows,
  type FlowView,
} from './asyncLifetime';
import type { AgentSupply, OAuthFlow } from './types';

const agent = (over: Partial<AgentSupply> = {}): AgentSupply => ({
  backend: 'claude',
  mode: 'hub',
  menu_kind: 'fixed',
  sources: { policy: 'custom', order: ['src_a', 'src_b'] },
  ...over,
});

const flow = (state: OAuthFlow['state']): OAuthFlow => ({
  flow_id: 'oaf_1',
  vendor: 'anthropic',
  channel: 'native_cli',
  state,
  presentation: { auth_url: null, device_code: null, expects: 'none', instructions_key: null },
});

const deferred = <T>() => {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
};

describe('latest async authority', () => {
  it('drops an older refresh that lands after the newer refresh', async () => {
    const older = deferred<string>();
    const newer = deferred<string>();
    const landed: string[] = [];
    const authority = createLatestAsyncAuthority<string>((value) => landed.push(value));

    const olderRun = authority.run(() => older.promise);
    const newerRun = authority.run(() => newer.promise);

    newer.resolve('server order: b,a');
    await newerRun;
    older.resolve('server order: a,b');
    await olderRun;

    expect(landed).toEqual(['server order: b,a']);
  });
});

describe('savedSourcesKey / savedMappingsKey / savedMenuKey', () => {
  it('moves when the saved state moves', () => {
    expect(savedSourcesKey(agent({ sources: { policy: 'custom', order: ['src_b', 'src_a'] } }))).not.toBe(
      savedSourcesKey(agent()),
    );
    expect(savedSourcesKey(agent({ sources: { policy: 'follow', order: ['src_a', 'src_b'] } }))).not.toBe(
      savedSourcesKey(agent()),
    );
  });

  it('holds still across a refetch that changed nothing', () => {
    // The whole point of comparing content: a background page refresh rebuilds
    // every object, and an inert refresh must stay inert.
    expect(savedSourcesKey(agent())).toBe(savedSourcesKey(agent()));
  });

  it('cannot be spoofed by an id that contains the separator', () => {
    expect(savedSourcesKey(agent({ sources: { policy: 'custom', order: ['src_a src_b'] } }))).not.toBe(
      savedSourcesKey(agent()),
    );
  });

  it('reads the mapping overrides and the menu the drawers seed from', () => {
    const base = agent({ mappings: [{ builtin_id: 'claude-opus-4-6', target_model_id: 'm1', enabled: true }] });
    expect(savedMappingsKey(base)).not.toBe(
      savedMappingsKey(agent({ mappings: [{ builtin_id: 'claude-opus-4-6', target_model_id: 'm1', enabled: false }] })),
    );
    expect(savedMenuKey({ view: 'featured', checked: ['zhipuai/glm-5.2'] })).not.toBe(
      savedMenuKey({ view: 'full', checked: ['zhipuai/glm-5.2'] }),
    );
    expect(savedMenuKey(null)).toBe(savedMenuKey({ view: 'featured', checked: [] }));
  });
});

describe('seedStep', () => {
  it('seeds the drawer once on open', () => {
    const { state, reseed } = seedStep(initialSeedState, savedSourcesKey(agent()));
    expect(reseed).toBe(true);
    expect(state.baseline).toBe(savedSourcesKey(agent()));
  });

  it('ignores a refetch that did not move the saved state', () => {
    // This is the rule the fix must not break: a background refresh must never
    // discard an edit the user is in the middle of making.
    const opened = seedStep(initialSeedState, savedSourcesKey(agent())).state;
    expect(seedStep(opened, savedSourcesKey(agent())).reseed).toBe(false);
  });

  it('re-seats a drawer reopened before its own save landed', () => {
    // Interleaving 1. Saved order is [a, b]; the user drags to [b, a], the PUT
    // goes out, the drawer is closed and reopened while it is still in flight —
    // so it seeds from the pre-save props — and only then does the save land and
    // the refetch deliver [b, a]. Whatever the drawer is holding at that point
    // came from a snapshot the server has since replaced.
    const stale = savedSourcesKey(agent({ sources: { policy: 'custom', order: ['src_a', 'src_b'] } }));
    const landed = savedSourcesKey(agent({ sources: { policy: 'custom', order: ['src_b', 'src_a'] } }));

    const reopened = seedStep(initialSeedState, stale).state;
    const after = seedStep(reopened, landed);

    expect(after.reseed).toBe(true);
    expect(after.state.baseline).toBe(landed);
  });

  it('re-seats only once per move, so a redundant refetch stays inert', () => {
    const landed = savedSourcesKey(agent({ sources: { policy: 'custom', order: ['src_b', 'src_a'] } }));
    const reseated = seedStep(seedStep(initialSeedState, savedSourcesKey(agent())).state, landed).state;
    expect(seedStep(reseated, landed).reseed).toBe(false);
  });
});

describe('flowStep', () => {
  const fresh: FlowView = { flow: null, errorKey: null, settled: false };

  it('keeps polling while the flow is running', () => {
    const step = flowStep(fresh, { kind: 'response', flow: flow('awaiting_action') });
    expect(step.action).toBe('continue');
    expect(isDone(step.action)).toBe(false);
    expect(step.view.flow?.state).toBe('awaiting_action');
  });

  it('reports the first success once', () => {
    const step = flowStep(fresh, { kind: 'response', flow: flow('success') });
    expect(step.action).toBe('succeed');
    expect(step.view.errorKey).toBeNull();
    expect(step.view.settled).toBe(true);
    expect(flowStep(step.view, { kind: 'response', flow: flow('success') }).action).toBe('ignore');
  });

  it('discards a poll that comes back non-terminal after success', () => {
    // Interleaving 2. The request in flight when success landed resolves next,
    // carrying the state the server held BEFORE it completed the flow. Showing it
    // turns a finished connect back into a running one.
    const settled = flowStep(fresh, { kind: 'response', flow: flow('success') }).view;
    const late = flowStep(settled, { kind: 'response', flow: flow('verifying') });

    expect(late.action).toBe('ignore');
    expect(late.view.flow?.state).toBe('success');
    expect(late.view.settled).toBe(true);
  });

  it('discards a late failure after success', () => {
    const settled = flowStep(fresh, { kind: 'response', flow: flow('success') }).view;
    const late = flowStep(settled, { kind: 'response', flow: flow('failed') });
    expect(late.action).toBe('ignore');
    expect(late.view.flow?.state).toBe('success');
  });

  it('does not let the deadline overwrite a settled flow', () => {
    // Same hole, other entry point: the dialog stays open after success (nothing
    // adopted the source, so there is no auto-close), the deadline passes, and the
    // tick stamps `failed` over a connect that succeeded.
    const settled = flowStep(fresh, { kind: 'response', flow: flow('success') }).view;
    const tick = flowStep(settled, { kind: 'tick', overdue: true });

    expect(tick.action).toBe('ignore');
    expect(tick.view.flow?.state).toBe('success');
  });

  it('still times out a flow that never finished', () => {
    const running = flowStep(fresh, { kind: 'response', flow: flow('awaiting_action') }).view;
    const tick = flowStep(running, { kind: 'tick', overdue: true });
    expect(tick.action).toBe('timeout');
    expect(tick.view.flow?.state).toBe('failed');
    expect(tick.view.errorKey).toBe('settings.models.oauth.error.timeout');
    expect(isDone(tick.action)).toBe(true);
  });

  it('leaves an unfinished flow alone before the deadline', () => {
    const running = flowStep(fresh, { kind: 'response', flow: flow('awaiting_action') }).view;
    expect(flowStep(running, { kind: 'tick', overdue: false })).toEqual({ view: running, action: 'continue' });
  });

  it('settles on a failure and ignores what follows it', () => {
    const failed = flowStep(fresh, { kind: 'response', flow: flow('cancelled') });
    expect(failed.action).toBe('fail');
    expect(failed.view.errorKey).toBe('settings.models.oauth.error.generic');
    expect(failed.view.settled).toBe(true);
    expect(flowStep(failed.view, { kind: 'response', flow: flow('success') }).action).toBe('ignore');
  });
});

describe('flow authority', () => {
  it('ignores a poll flow_not_found that lands after success settled', () => {
    const authority = createFlowAuthority(() => {}, null);

    authority.transition({ kind: 'response', flow: flow('verifying') });
    authority.transition({ kind: 'response', flow: flow('success') });
    const latePoll = authority.transition({
      kind: 'error',
      errorKey: 'settings.models.oauth.error.generic',
    });

    expect(latePoll.action).toBe('ignore');
    expect(authority.current()).toEqual({
      flow: expect.objectContaining({ state: 'success' }),
      errorKey: null,
      settled: true,
    });
  });

  it('ignores a paste rejection that lands after success settled', () => {
    const authority = createFlowAuthority(() => {}, null);

    authority.transition({ kind: 'response', flow: flow('awaiting_action') });
    authority.transition({ kind: 'response', flow: flow('success') });
    const latePaste = authority.transition({
      kind: 'error',
      errorKey: 'settings.models.oauth.error.finalize',
    });

    expect(latePaste.action).toBe('ignore');
    expect(authority.current()).toEqual({
      flow: expect.objectContaining({ state: 'success' }),
      errorKey: null,
      settled: true,
    });
  });

  it('keeps the deadline terminal when a paste success resolves afterward', () => {
    const landed: FlowView[] = [];
    const authority = createFlowAuthority((view) => landed.push(view), null);
    let connected = 0;

    authority.transition({ kind: 'response', flow: flow('success') });
    authority.transition({ kind: 'reset' });
    authority.transition({ kind: 'response', flow: flow('awaiting_action') });
    const timeout = authority.transition({ kind: 'tick', overdue: true });
    const latePaste = authority.transition({ kind: 'response', flow: flow('success') });
    if (latePaste.action === 'succeed') connected += 1;

    expect(timeout.action).toBe('timeout');
    expect(latePaste.action).toBe('ignore');
    expect(connected).toBe(0);
    expect(authority.current()).toEqual({
      flow: expect.objectContaining({ state: 'failed' }),
      errorKey: 'settings.models.oauth.error.timeout',
      settled: true,
    });
    expect(landed.at(-1)).toEqual(authority.current());
  });
});

describe('pollFailureSettles — who speaks for a journey whose submit is outstanding', () => {
  it('lets a failed poll settle the journey when it is the only authority', () => {
    // A status read is also the call that materializes a just-succeeded flow, so on
    // a device-code login its failure can be the one thing that knows the login
    // produced nothing usable.
    expect(pollFailureSettles(false)).toBe(true);
  });

  it('does not let it settle one while the user’s own submit is outstanding', () => {
    // The submit is the writer of record; a read failing beside it says nothing
    // about whether that write committed.
    expect(pollFailureSettles(true)).toBe(false);
  });

  it('shows what latching one costs: the success right behind it is ignored', () => {
    // Interleaving 5 as a sequence. `flowStep` is right to ignore the second
    // arrival — the fix is upstream, at which arrival is allowed to be a verdict.
    const authority = createFlowAuthority(() => {}, null);

    authority.transition({ kind: 'response', flow: flow('awaiting_action') });
    authority.transition({ kind: 'error', errorKey: 'settings.models.oauth.error.generic' });
    const submitSucceeded = authority.transition({ kind: 'response', flow: flow('success') });

    expect(submitSucceeded.action).toBe('ignore');
    expect(authority.current().errorKey).toBe('settings.models.oauth.error.generic');
  });

  it('is what the dialog’s poll actually asks, about the submit’s own flag', () => {
    // A predicate nothing consults is worth nothing, and the value it reads is the
    // other half of the fix: the flow effect is built once per attempt, so it must
    // read the submit's in-flight state through a ref or it reads `false` forever.
    const dialog = readFileSync(join(__dirname, 'OAuthConnectDialog.tsx'), 'utf8');

    expect(dialog).toMatch(/pollFailureSettles\(submittingRef\.current\)/);
    expect(dialog).toMatch(/submittingRef\.current = submitting;/);
  });
});

describe('startNeedsStatusRead — a start response that is already terminal', () => {
  it('sends an already-successful start to the status route', () => {
    // `POST …/reauth` reuses a live pending flow rather than opening a second one,
    // so the start can answer `success` — carrying the flow ALONE. Latching it
    // would settle the dialog on the one arrival that has no
    // `recovered`/`interrupted_pairs` beside it, and `flowStep` would then be
    // right to ignore the status read that does.
    expect(startNeedsStatusRead(flow('success'))).toBe(true);
  });

  it('latches a terminal failure where it lands', () => {
    // The other terminals need no second call: their `error_key` IS the answer,
    // and a status read of a cancelled flow adds nothing to show.
    expect(startNeedsStatusRead(flow('failed'))).toBe(false);
    expect(startNeedsStatusRead(flow('cancelled'))).toBe(false);
  });

  it('leaves a running start to the poll it already schedules', () => {
    expect(startNeedsStatusRead(flow('awaiting_action'))).toBe(false);
    expect(startNeedsStatusRead(flow('verifying'))).toBe(false);
  });
});

describe('terminalArrivalMovedRows — which terminal is also a write', () => {
  it('counts a success, which materializes what it reports', () => {
    expect(terminalArrivalMovedRows('succeed')).toBe(true);
  });

  it('counts a FAILURE, which a hub reauth has already been persisted by', () => {
    // The finding this pins: `_fail_closed_hub_reauth` runs inside the status read
    // that answers `failed` — the discovered models are stripped and the source is
    // saved as 需处理 before the response is written. A dialog that refreshes only
    // on success explains the failure over a page still drawing that source as
    // healthy, and still crediting it with the ● 当前 it can no longer supply.
    expect(terminalArrivalMovedRows('fail')).toBe(true);
  });

  it('does not count the dialog’s own timeout, where nothing arrived', () => {
    // The line between the two: `succeed` and `fail` are the server's verdicts,
    // `timeout` is this dialog's. Nothing arrived that could have written, and the
    // close path — whose cancel can BE the write — is what re-reads there.
    expect(terminalArrivalMovedRows('timeout')).toBe(false);
  });

  it('does not count an arrival the latch already ignored, or a pending one', () => {
    // `ignore` means the terminal it would report was already handled by whichever
    // arrival got there first, and that one re-read.
    expect(terminalArrivalMovedRows('ignore')).toBe(false);
    expect(terminalArrivalMovedRows('continue')).toBe(false);
  });

  it('leaves the dialog one place that says the rows behind it moved', () => {
    // Structural, and the point of the extraction: the two success branches used
    // to refetch inline, which is why the failure between them refetched nowhere.
    // One predicate decides for all three, and one owner carries it out — so the
    // parent's callback is reached from exactly one line.
    const dialog = readFileSync(join(__dirname, 'OAuthConnectDialog.tsx'), 'utf8');

    expect(dialog).toMatch(/if \(terminalArrivalMovedRows\(step\.action\)\) rowsBehindAreStale\(\);/);
    expect((dialog.match(/onConnectedRef\.current\(\)/g) ?? []).length).toBe(1);
    // And that owner no longer asks which journey it is before re-reading: a
    // create's `oauth_cancel` commits the source it was told to throw away.
    expect(dialog).not.toMatch(/if \(!isReauth\) return;/);
  });
});

describe('releaseFlow — what a teardown does with the flow it opened', () => {
  /** A journey, plus a recorder for the two things a teardown can do. */
  const teardown = () => {
    const log: string[] = [];
    return {
      log,
      // Re-authing source A: the journey whose flow a successor can be handed.
      journey: createFlowAuthority(() => {}, 'src_a'),
      // `reusable` defaults to the reauth journey, which is the one the ownership
      // rule was written for; the create cases below say so explicitly.
      ops: (cancel: (() => Promise<unknown>) | null, reusable = true) => ({
        cancel: cancel && (() => (log.push('cancel'), cancel())),
        reread: () => log.push('reread'),
        reusable,
      }),
    };
  };

  it('rereads only after the cancel it made has settled', async () => {
    // Interleaving 3. The snapshot this teardown could have read said
    // `awaiting_action`, and the poll in flight is about to report success — so
    // this cancel reaches `_materialize_completed_oauth` and IS the write. A
    // reread beside it, rather than after it, reads the rows before it happened.
    const { log, journey, ops } = teardown();
    const settle = deferred<void>();

    const released = releaseFlow(journey, journey, ops(() => settle.promise));

    expect(log).toEqual(['cancel']);
    settle.resolve();
    await released;
    expect(log).toEqual(['cancel', 'reread']);
  });

  it('rereads even when its own cancel failed', async () => {
    // The writes this journey already made upstream do not depend on the cleanup
    // succeeding, so a failed cancel is a reason to reread rather than not to.
    const { log, journey, ops } = teardown();
    await releaseFlow(journey, journey, ops(() => Promise.reject(new Error('engine_down'))));
    expect(log).toEqual(['cancel', 'reread']);
  });

  it('lets go once a newer journey owns the source flow', async () => {
    // Interleaving 4. `POST …/reauth` reuses a live pending flow, so the
    // replacement dialog FOR THE SAME ROW is polling THIS flow id. Cancelling it
    // here ends a login the user is watching — and the reread is still owed,
    // because the start this journey made had already committed the irreversible
    // half.
    const { log, journey, ops } = teardown();
    const successor = createFlowAuthority(() => {}, 'src_a');

    await releaseFlow(journey, successor, ops(() => Promise.resolve()));

    expect(log).toEqual(['reread']);
  });

  it('CANCELS when the successor is re-authing a different row', async () => {
    // `pending_reauth(source_id)` filters on `binding.source_id`, so the handover
    // is keyed by SOURCE while ownership of the dialog's ref is global. Close a
    // pending re-auth for A, open one for B before A's start returns, and A finds
    // a live owner that can never be handed its flow: letting go there leaves A's
    // authorization running until it expires.
    const { log, journey, ops } = teardown();
    const otherRow = createFlowAuthority(() => {}, 'src_b');

    await releaseFlow(journey, otherRow, ops(() => Promise.resolve()));

    expect(log).toEqual(['cancel', 'reread']);
  });

  it('CANCELS when the successor is a create, which adopts nothing', async () => {
    // The same rule with the successor on the other route: a create has no source
    // yet, so it is not the journey `pending_reauth` would hand this flow to.
    const { log, journey, ops } = teardown();
    const create = createFlowAuthority(() => {}, null);

    await releaseFlow(journey, create, ops(() => Promise.resolve()));

    expect(log).toEqual(['cancel', 'reread']);
  });

  it('lets go when nobody owns the flow yet', async () => {
    // The same handoff one beat earlier: the replacement's start request is in
    // flight and the server has not handed it this flow yet. 「Nobody owns it」 and
    // 「a successor is about to」 are indistinguishable from here, so this path does
    // not get to guess — and guessing wrong kills a live login.
    const { log, journey, ops } = teardown();
    await releaseFlow(journey, null, ops(() => Promise.resolve()));
    expect(log).toEqual(['reread']);
  });

  it('cancels an abandoned CREATE flow even with its ownership already gone', async () => {
    // The same interleaving as the two above, on the journey where the reasoning
    // behind them does not hold: `oauth_start` mints a fresh pending source id
    // every call and never adopts a pending flow, so no successor can be handed
    // this one. 「Nobody owns it」 is then nobody, and letting go leaves the
    // authorization and its registry binding live until the server expires them.
    const { log, journey, ops } = teardown();
    await releaseFlow(journey, null, ops(() => Promise.resolve(), false));
    expect(log).toEqual(['cancel', 'reread']);
  });

  it('still asks ownership first for a reusable flow', async () => {
    // The pair of the above, stated so neither can be simplified into the other:
    // ownership answers 「is it still mine to cancel?」 and `reusable` answers
    // 「could it ever have become someone else's?」. Only a `false` second answer
    // makes the first one moot.
    const { log, journey, ops } = teardown();
    const successor = createFlowAuthority(() => {}, 'src_a');
    await releaseFlow(journey, successor, ops(() => Promise.resolve(), true));
    expect(log).toEqual(['reread']);
  });

  it('keeps the three questions independent', async () => {
    // Stated on `flowLetGo` directly so none of them can be folded into another:
    // ownership (「still mine?」), the route (「could it EVER move?」) and the
    // successor's subject (「could THIS one be handed it?」).
    const mine = createFlowAuthority(() => {}, 'src_a');
    const sameRow = createFlowAuthority(() => {}, 'src_a');
    const otherRow = createFlowAuthority(() => {}, 'src_b');

    expect(flowLetGo(mine, mine, true)).toBe(false); // still mine
    expect(flowLetGo(mine, sameRow, false)).toBe(false); // route never hands it over
    expect(flowLetGo(mine, otherRow, true)).toBe(false); // successor cannot adopt it
    expect(flowLetGo(mine, sameRow, true)).toBe(true); // all three agree
    // No owner: no subject to compare, and the conservative direction is not
    // killing a flow the user's next start for this row would be handed.
    expect(flowLetGo(mine, null, true)).toBe(true);
    expect(flowLetGo(mine, null, false)).toBe(false);
  });

  it('takes the journey each teardown is releasing from the dialog', () => {
    // Structural, because the rule above is only as good as its wiring: both
    // release sites state which journey they are in rather than letting
    // `releaseFlow` infer it from an ownership that cannot tell the two apart.
    const dialog = readFileSync(join(__dirname, 'OAuthConnectDialog.tsx'), 'utf8');
    const releases = dialog.match(/releaseFlow\(/g) ?? [];
    const declared = dialog.match(/reusable: isReauth/g) ?? [];

    expect(releases.length).toBe(2);
    expect(declared.length).toBe(releases.length);
    // And the authority carries the row it is for, which is what makes the
    // successor's subject answerable at all.
    expect(dialog).toMatch(/createFlowAuthority\(setView, reauthId\)/);
  });

  it('still rereads when there was no flow to release', async () => {
    // No flow id means no call to make, which is not the same as deciding a
    // reread is unnecessary. A redundant reread is inert; a missing one is the bug.
    const { log, journey, ops } = teardown();
    await releaseFlow(journey, journey, ops(null));
    expect(log).toEqual(['reread']);
  });

  it('leaves the cleanup cancel to the one owner that also rereads', () => {
    // Structural, because this class has now been filed four times: both teardown
    // paths hand their cancel to `releaseFlow`, so neither can decide for itself
    // whether to reread — and no snapshot-shaped list is left behind to decide
    // from. A `TERMINAL` array is what the retracted argument was made of.
    const dialog = readFileSync(join(__dirname, 'OAuthConnectDialog.tsx'), 'utf8');
    const calls = dialog.match(/modelsApi\.cancelOAuth\(/g) ?? [];
    const handedOver = dialog.match(/=> modelsApi\.cancelOAuth\(/g) ?? [];

    expect(calls.length).toBeGreaterThan(0);
    expect(handedOver.length).toBe(calls.length);
    expect(dialog).not.toMatch(/TERMINAL/);
  });

  it('hands the rows back on every exit a closed journey can still take', () => {
    // Interleaving 6, structurally, because the reachable proof needs a DOM this
    // repo's vitest does not have. THREE requests of one reauth journey can resolve
    // after the dialog is gone — a start that rejects, a status read that
    // materialized a just-succeeded flow, a poll that failed — and the close path's
    // own re-read was issued while all three were still in flight, so it can predate
    // their write. Each therefore re-reads on its way out, through one named owner.
    //
    // Exactly one guard may still return bare: the one at the TOP of `poll`, where
    // nothing has been requested yet on that tick and so nothing can have written.
    const dialog = readFileSync(join(__dirname, 'OAuthConnectDialog.tsx'), 'utf8');
    const bare = dialog.match(/if \(cancelled\) return;/g) ?? [];
    const handedBack = dialog.match(/if \(cancelled\) \{\s*resolvedAfterClose\(\);\s*return;\s*\}/g) ?? [];

    expect(bare.length).toBe(1);
    expect(handedBack.length).toBe(3);
    // And the fourth exit — a start that SUCCEEDED after the close — reaches the
    // same owner through the release it also has to make.
    expect(dialog).toMatch(/reread: resolvedAfterClose,/);
  });
});
