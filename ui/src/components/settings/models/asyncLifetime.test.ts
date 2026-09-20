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
//
//   7. reopen-clears-the-guard. Interleaving 1's other half. The order drawer marks
//      itself busy for the span of its write, which shuts the hand-off to 模型菜单与
//      映射 — whose enrollment notice diffs against the page's copy of that order. But
//      every way out of the drawer stays live while the write runs, and closing
//      unmounts it, so the reopen re-creates the mark reading 「idle」 over a write
//      still outstanding. The user then walks into the menu on a stale baseline and
//      the menu's own save reports the ORDER write's append as its own.
//
//   8. write-lands-read-does-not. Interleaving 7 once more, and the last place it
//      can hide: the mark now spans the re-read, but a re-read is not an outcome.
//      `refreshSourcesAgents` swallows a failure into a toast and the authority
//      drops a superseded run, so the await finishes either way and clears the mark
//      over rows that never moved — the same stale baseline, reached by waiting for
//      exactly the right thing and being told nothing about how it went.
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

import {
  agentsWithEcho,
  classifyModelHubFailure,
  classifyOAuthFailure,
  createFlowAuthority,
  createLatestAsyncAuthority,
  createLatestAsyncAuthorityByKey,
  createLatestEntityAuthorityByKey,
  createPendingWrites,
  failureLanded,
  flowLetGo,
  flowStateTerminal,
  flowStep,
  initialSeedState,
  isDone,
  mapWithConcurrency,
  pollFailureSettles,
  releaseFlow,
  savedMenuKey,
  savedSourcesKey,
  seedStep,
  startNeedsStatusRead,
  terminalArrivalMovedRows,
  type FlowView,
} from './asyncLifetime';
import type { AgentSupply, OAuthFlow, Source } from './types';

const agent = (over: Partial<AgentSupply> = {}): AgentSupply => ({
  backend: 'claude',
  cli_present: true,
  mode: 'hub',
  menu_kind: 'fixed',
  sources: { order: ['src_a', 'src_b'] },
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

  // Interleaving 8, at its source. These are the two ways a re-read finishes with
  // the rows exactly where they were, and the mutation awaiting it cannot tell
  // either one from success — which is what makes 「the re-read finished」 a
  // different fact from 「the row is current」.
  it('reports a superseded run rather than landing it, and hands a failed one back', async () => {
    const landed: string[] = [];
    const authority = createLatestAsyncAuthority<string>((value) => landed.push(value));
    const older = deferred<string>();

    const olderRun = authority.run(() => older.promise);
    await authority.run(() => Promise.resolve('newest'));
    older.resolve('older');

    expect(await olderRun).toBe('stale');
    await expect(authority.run(() => Promise.reject(new Error('read failed')))).rejects.toThrow('read failed');
    expect(landed).toEqual(['newest']);
  });

  it('orders reads per key without making independent backends supersede each other', async () => {
    const olderClaude = deferred<string>();
    const newerClaude = deferred<string>();
    const codex = deferred<string>();
    const landed: string[] = [];
    const authority = createLatestAsyncAuthorityByKey<string, string>((key, value) => landed.push(`${key}:${value}`));

    const olderRun = authority.run('claude', () => olderClaude.promise);
    const codexRun = authority.run('codex', () => codex.promise);
    const newerRun = authority.run('claude', () => newerClaude.promise);
    newerClaude.resolve('new');
    codex.resolve('independent');
    await Promise.all([newerRun, codexRun]);
    olderClaude.resolve('old');

    expect(await olderRun).toBe('stale');
    expect(landed).toEqual(['claude:new', 'codex:independent']);
  });

  it('invalidates pending generations whose keys lose active ownership', async () => {
    const claude = deferred<string>();
    const codex = deferred<string>();
    const landed: string[] = [];
    const authority = createLatestAsyncAuthorityByKey<string, string>((key, value) => landed.push(`${key}:${value}`));

    const claudeRun = authority.run('claude', () => claude.promise);
    const codexRun = authority.run('codex', () => codex.promise);
    authority.invalidateExcept(new Set(['codex']));
    claude.resolve('no longer owned');
    codex.resolve('still active');

    expect(await claudeRun).toBe('stale');
    expect(await codexRun).toBe('landed');
    expect(landed).toEqual(['codex:still active']);
  });

  it('invalidates one pending generation before a write echo takes ownership', async () => {
    const pending = deferred<string>();
    const landed: string[] = [];
    const authority = createLatestAsyncAuthorityByKey<string, string>((key, value) => landed.push(`${key}:${value}`));

    const read = authority.run('claude', () => pending.promise);
    authority.invalidate('claude');
    pending.resolve('pre-commit chain');

    expect(await read).toBe('stale');
    expect(landed).toEqual([]);
  });
});

describe('latest Source entity authority', () => {
  it('lands the later per-Source generation and rejects an older echo', () => {
    const landed: Source[][] = [];
    const authority = createLatestEntityAuthorityByKey((source: Source) => source.id, (sources) => landed.push(sources));
    const initialSnapshot = authority.beginSnapshot();
    const initial = { id: 'src_a', display_name: 'initial' } as Source;
    authority.settleSnapshot(initialSnapshot, [initial]);
    const older = authority.begin('src_a');
    const newer = authority.begin('src_a');
    const newerSource = { ...initial, display_name: 'newer' };

    expect(authority.settle(newer, newerSource)).toBe('landed');
    expect(authority.settle(older, { ...initial, display_name: 'older' })).toBe('stale');
    expect(authority.current('src_a')).toEqual(newerSource);
    expect(landed.at(-1)).toEqual([newerSource]);
  });

  it('does not let a snapshot reserved before a mutation overwrite its echo', () => {
    const landed: Source[][] = [];
    const authority = createLatestEntityAuthorityByKey((source: Source) => source.id, (sources) => landed.push(sources));
    const initial = { id: 'src_a', display_name: 'initial' } as Source;
    const initialSnapshot = authority.beginSnapshot();
    authority.settleSnapshot(initialSnapshot, [initial]);
    const olderSnapshot = authority.beginSnapshot();
    const mutation = authority.begin('src_a');
    const echoed = { ...initial, display_name: 'echoed' };

    expect(authority.settle(mutation, echoed)).toBe('landed');
    authority.settleSnapshot(olderSnapshot, [initial]);
    expect(landed.at(-1)).toEqual([echoed]);
  });

  it('does not let a scoped gone reconciliation invalidate a sibling write already in flight', () => {
    const landed: Source[][] = [];
    const authority = createLatestEntityAuthorityByKey((source: Source) => source.id, (sources) => landed.push(sources));
    const sourceA = { id: 'src_a', display_name: 'A' } as Source;
    const sourceB = { id: 'src_b', display_name: 'B' } as Source;
    const initial = authority.beginSnapshot();
    authority.settleSnapshot(initial, [sourceA, sourceB]);
    const removingA = authority.begin(sourceA.id);
    const mutatingB = authority.begin(sourceB.id);
    const reconciliation = authority.beginSnapshot();

    authority.settleSnapshotEntries(reconciliation, [sourceB]);
    expect(authority.settleRemoval(removingA)).toBe('landed');
    const echoedB = { ...sourceB, display_name: 'B after mutation' };
    expect(authority.settle(mutatingB, echoedB)).toBe('landed');
    expect(landed.at(-1)).toEqual([echoedB]);
  });
});

describe('bounded async map', () => {
  it('caps concurrent work and preserves input order', async () => {
    const gates = Array.from({ length: 12 }, () => deferred<number>());
    let active = 0;
    let maxActive = 0;

    const run = mapWithConcurrency(gates, 3, async (gate, index) => {
      active += 1;
      maxActive = Math.max(maxActive, active);
      const value = await gate.promise;
      active -= 1;
      return `${index}:${value}`;
    });

    expect(active).toBe(3);
    gates.forEach((gate, index) => gate.resolve(index * 2));
    await expect(run).resolves.toEqual(gates.map((_, index) => `${index}:${index * 2}`));
    expect(maxActive).toBe(3);
  });
});

describe('agentsWithEcho — what speaks for a row when no read does', () => {
  const claude = agent({ backend: 'claude', sources: { order: ['src_a'] } });
  const codex = agent({ backend: 'codex', sources: { order: ['src_b'] } });

  it('takes the write’s echo into the row it is about', () => {
    const echoed = agent({ backend: 'claude', sources: { order: ['src_a', 'src_c'] } });

    expect(agentsWithEcho([claude, codex], echoed)).toEqual([echoed, codex]);
  });

  it('leaves every other Agent to the read that owns it', () => {
    // An order write is per backend; it says nothing about the others, so it may
    // not answer for them either.
    const echoed = agent({ backend: 'codex', sources: { order: ['src_b', 'src_c'] } });

    expect(agentsWithEcho([claude, codex], echoed)[0]).toBe(claude);
  });

  it('does not invent a row the page has not read', () => {
    // Which Agents exist is the list read's to say. A write echo is an update.
    const echoed = agent({ backend: 'opencode' });

    expect(agentsWithEcho([claude], echoed)).toEqual([claude]);
  });

  it('does not mutate the list it was handed', () => {
    const before = [claude, codex];
    agentsWithEcho(before, agent({ backend: 'claude', sources: { order: [] } }));

    expect(before).toEqual([claude, codex]);
  });

  // Interleaving 8. TypeScript already forces every drawer to hand its echo over —
  // `onSaved` takes one — so what is left to guard is the page: that it TAKES the
  // echo rather than dropping it beside a re-read, and that no Agent write reports
  // itself any other way.
  it('is how every Agent write on the page reports itself', () => {
    const page = readFileSync(join(__dirname, 'SettingsModelsPage.tsx'), 'utf8');

    expect(page).toMatch(/setSupplyRead\(\(previous\) => readyRegion\(agentsWithEcho\(foldRegionRead\(previous,[\s\S]*?echoed\)\)\)/);
    expect(page).toMatch(/const agentSaved[\s\S]*?convergeMutation\(\{/);
    // The mode PATCH echoes the same row the drawers' writes do.
    expect(page).toMatch(/await agentSaved\(echoed\)/);

    expect(page).toMatch(/onSaved=\{agentSaved\}/);
    expect(page).toMatch(/const echoed = await modelsApi\.setAgentMode[\s\S]*?await agentSaved\(echoed\)/);
  });
});

describe('Source entity landing through the shared authority', () => {
  it('routes Source-detail rereads and full reads through the same per-Source settlement', () => {
    const page = readFileSync(join(__dirname, 'SettingsModelsPage.tsx'), 'utf8');
    const detail = readFileSync(join(__dirname, 'SourceDetailPanel.tsx'), 'utf8');
    const sourceRetry = page.slice(page.indexOf('const retrySources'), page.indexOf('const retrySupply'));
    const supplyRetry = page.slice(page.indexOf('const retrySupply'), page.indexOf('const retryEvents'));

    expect(detail).toMatch(/trackMutation\(async \(latest, settlement\)[\s\S]*?modelsApi\.refreshSource\(latest\.id, confirmation\)/);
    expect(detail).toMatch(/await settlement\.source\(answer\.source\)/);
    expect(page).toMatch(/sourceWriteRegistry\.track\(sourceId[\s\S]*?sourceEntityAuthority\.current\(sourceId\)[\s\S]*?sourceEntityAuthority\.begin\(sourceId\)[\s\S]*?const settlement: SourceMutationSettlement/);
    expect(page).toMatch(/source: async \(echoed, scope\)[\s\S]*?sourceEntityAuthority\.settle\(generation/);
    expect(page).not.toMatch(/const sourceMutation\s*=|activeSourceGenerations/);
    expect(page).toMatch(/await refreshAuthority\.run/);
    expect(sourceRetry).toMatch(/await refresh\(\)/);
    expect(supplyRetry).toMatch(/await refreshAgentPresence\(\)/);
    expect(sourceRetry).not.toMatch(/modelsApi\.listSources/);
    expect(supplyRetry).not.toMatch(/modelsApi\.listAgents/);
  });

  it('keeps the mutating refresh failure outside stale-read suppression', () => {
    const detail = readFileSync(join(__dirname, 'SourceDetailPanel.tsx'), 'utf8');
    const refresh = detail.slice(detail.indexOf('const refetch ='), detail.indexOf('const remove ='));

    expect(refresh).toMatch(/modelsApi\.refreshSource/);
    expect(refresh).toMatch(/catch \(error\)[\s\S]*?setRefetchFailed\(true\)/);
    expect(detail).toMatch(/refetchFailed[\s\S]*?sourceDetail\.fail\.refetch/);
  });
});

describe('createPendingWrites — a write that outlives the drawer that issued it', () => {
  /** The published set, as the page would hold it in state. */
  const registry = () => {
    let keys: ReadonlySet<string> = new Set();
    const writes = createPendingWrites((next) => {
      keys = next;
    });
    return { writes, pending: (key: string) => keys.has(key) };
  };

  /** Lets the queue's own chaining run, without settling any of the work. */
  const flush = async () => {
    for (let i = 0; i < 4; i += 1) await Promise.resolve();
  };

  it('holds the mark for the whole of the work, not just its request', async () => {
    const put = deferred<void>();
    const reread = deferred<void>();
    const { writes, pending } = registry();

    const run = writes.track('claude', async () => {
      await put.promise;
      await reread.promise;
    });

    expect(pending('claude')).toBe(true);
    put.resolve();
    await Promise.resolve();
    // The PUT has returned and the page has NOT read it back yet — the gap the
    // hand-off may not leave through.
    expect(pending('claude')).toBe(true);
    reread.resolve();
    await run;
    expect(pending('claude')).toBe(false);
  });

  it('survives the drawer that issued the write', async () => {
    // Interleaving 7. Nothing here is the drawer: the write is issued, the drawer
    // that issued it is gone (closed → unmounted), a fresh one opens and asks
    // whether a write is outstanding. It gets the truth, because the fact was
    // never the drawer's to hold.
    const put = deferred<void>();
    const { writes, pending } = registry();

    const run = writes.track('claude', () => put.promise);
    const reopened = pending('claude');

    put.resolve();
    await run;

    expect(reopened).toBe(true);
    expect(pending('claude')).toBe(false);
  });

  it('stays pending until the LAST of two overlapping writes finishes', async () => {
    // Two drags in quick succession. 「Pending」 is a property of the SET of writes on
    // the key, so the first to return must not report the second one finished.
    const first = deferred<void>();
    const second = deferred<void>();
    const { writes, pending } = registry();

    const runFirst = writes.track('claude', () => first.promise);
    const runSecond = writes.track('claude', () => second.promise);

    first.resolve();
    await runFirst;
    expect(pending('claude')).toBe(true);

    second.resolve();
    await runSecond;
    expect(pending('claude')).toBe(false);
  });

  it('runs two edits on one key in the order the user made them', async () => {
    // 「The later edit wins」 is a claim about COMMIT order. Sent together, two PUTs
    // settle in whichever order they reach the server's mutation lock — and each
    // carries the WHOLE list, so the loser is discarded rather than merged into the
    // winner. The order left on disk could be the user's second-to-last edit while
    // the drawer shows their last.
    const first = deferred<void>();
    const second = deferred<void>();
    const started: string[] = [];
    const { writes } = registry();

    const runFirst = writes.track('claude', () => {
      started.push('first');
      return first.promise;
    });
    const runSecond = writes.track('claude', () => {
      started.push('second');
      return second.promise;
    });

    await flush();
    expect(started).toEqual(['first']);

    first.resolve();
    await runFirst;
    expect(started).toEqual(['first', 'second']);

    second.resolve();
    await runSecond;
  });

  it('does not strand the edit waiting behind a rejected write', async () => {
    // The edit behind it is the user's newer intent and still has to reach the
    // server — and the write that failed rolled its own optimistic display back, so
    // there is nothing for its successor to inherit.
    const second = deferred<void>();
    let ran = false;
    const { writes, pending } = registry();

    const runFirst = writes.track('claude', () => Promise.reject(new Error('put rejected')));
    const runSecond = writes.track('claude', () => {
      ran = true;
      return second.promise;
    });

    await expect(runFirst).rejects.toThrow('put rejected');
    await flush();
    expect(ran).toBe(true);
    expect(pending('claude')).toBe(true);

    second.resolve();
    await runSecond;
    expect(pending('claude')).toBe(false);
  });

  it('does not let one backend’s write gate another', async () => {
    // `PUT /agents/<backend>/sources` moves one backend's order, so this is the
    // write's own grain: a claude order edit says nothing about codex's menu — and
    // so has no business making codex's own edit wait for it either.
    const claude = deferred<void>();
    const codex = deferred<void>();
    const started: string[] = [];
    const { writes, pending } = registry();

    const runClaude = writes.track('claude', () => {
      started.push('claude');
      return claude.promise;
    });
    const runCodex = writes.track('codex', () => {
      started.push('codex');
      return codex.promise;
    });

    expect(pending('claude')).toBe(true);
    expect(pending('opencode')).toBe(false);
    await flush();
    expect(started).toEqual(['claude', 'codex']);

    claude.resolve();
    codex.resolve();
    await Promise.all([runClaude, runCodex]);
    expect(pending('claude')).toBe(false);
    expect(pending('codex')).toBe(false);
  });

  it('clears the mark when the work throws', async () => {
    const { writes, pending } = registry();

    await expect(
      writes.track('claude', async () => {
        throw new Error('put rejected');
      }),
    ).rejects.toThrow('put rejected');

    // A rejected write is a finished one. Leaving it marked would disable the
    // drawer's controls for the rest of the session.
    expect(pending('claude')).toBe(false);
  });

  it('is where the order drawer’s busy flag actually lives', () => {
    // The guard the round-1 fix put in the drawer was real and still incomplete:
    // it was component state, and 完成 / the X / Escape / the overlay all stay live
    // while the write runs. So the assertion is not 「the drawer disables things」
    // but 「the drawer does not OWN the fact it disables them on」.
    const drawer = readFileSync(join(__dirname, 'SourceOrderDrawer.tsx'), 'utf8');
    const page = readFileSync(join(__dirname, 'SettingsModelsPage.tsx'), 'utf8');

    expect(drawer).toMatch(/const saving = orderWrite\.pending;/);
    expect(drawer).not.toMatch(/setSaving/);
    // And the mark spans the whole of `persist`, read-back included, because
    // `track` is what opens and closes it.
    expect(drawer).toMatch(/orderWrite\.track\(async \(\) => \{/);
    expect(drawer).toMatch(/await Promise\.resolve\(onSaved\(echoed\)\)\.catch\(\(\) => \{\}\);/);

    expect(page).toMatch(/createPendingWrites\(setAgentWrites\)/);
    expect(page).toMatch(/pending: agentWrites\.has\(orderAgent\.backend\)/);
    expect(page).toMatch(/track: \(work\) => agentWriteRegistry\.track\(orderAgent\.backend, work\)/);
    // The page owns the write after the drawer unmounts, and only a Hub backend
    // can remain the owner of an open order drawer.
    expect(page).toMatch(/agents\.find\(\(agent\) => agent\.backend === orderBackend && agent\.mode === 'hub'\)/);
    expect(page).toMatch(/pending: agentWrites\.has\(orderAgent\.backend\)/);
  });
});

describe('savedSourcesKey / savedMenuKey', () => {
  it('moves when the saved state moves', () => {
    expect(savedSourcesKey(agent({ sources: { order: ['src_b', 'src_a'] } }))).not.toBe(
      savedSourcesKey(agent()),
    );
    expect(savedSourcesKey(agent({ sources: { order: ['src_a', 'src_b', 'src_c'] } }))).not.toBe(savedSourcesKey(agent()));
  });

  it('holds still across a refetch that changed nothing', () => {
    // The whole point of comparing content: a background page refresh rebuilds
    // every object, and an inert refresh must stay inert.
    expect(savedSourcesKey(agent())).toBe(savedSourcesKey(agent()));
  });

  it('cannot be spoofed by an id that contains the separator', () => {
    expect(savedSourcesKey(agent({ sources: { order: ['src_a src_b'] } }))).not.toBe(
      savedSourcesKey(agent()),
    );
  });

  it('reads the menu the drawer seeds from', () => {
    expect(savedMenuKey({ view: 'featured', checked: ['glm-5.2'] })).not.toBe(
      savedMenuKey({ view: 'full', checked: ['glm-5.2'] }),
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
    const stale = savedSourcesKey(agent({ sources: { order: ['src_a', 'src_b'] } }));
    const landed = savedSourcesKey(agent({ sources: { order: ['src_b', 'src_a'] } }));

    const reopened = seedStep(initialSeedState, stale).state;
    const after = seedStep(reopened, landed);

    expect(after.reseed).toBe(true);
    expect(after.state.baseline).toBe(landed);
  });

  it('re-seats only once per move, so a redundant refetch stays inert', () => {
    const landed = savedSourcesKey(agent({ sources: { order: ['src_b', 'src_a'] } }));
    const reseated = seedStep(seedStep(initialSeedState, savedSourcesKey(agent())).state, landed).state;
    expect(seedStep(reseated, landed).reseed).toBe(false);
  });
});

describe('flowStep', () => {
  const fresh: FlowView = {
    flow: null,
    errorKey: null,
    failureClass: null,
    settled: false,
  };

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
    expect(tick.view.failureClass).toBe('inconclusive');
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
    expect(failed.view.failureClass).toBe('retryable-provider');
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
      failureClass: 'retryable-provider',
    });

    expect(latePoll.action).toBe('ignore');
    expect(authority.current()).toEqual({
      flow: expect.objectContaining({ state: 'success' }),
      errorKey: null,
      failureClass: null,
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
      failureClass: 'retryable-provider',
    });

    expect(latePaste.action).toBe('ignore');
    expect(authority.current()).toEqual({
      flow: expect.objectContaining({ state: 'success' }),
      errorKey: null,
      failureClass: null,
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
      failureClass: 'inconclusive',
      settled: true,
    });
    expect(landed.at(-1)).toEqual(authority.current());
  });
});

describe('Model Hub failure classification', () => {
  const named = (code: string) => ({ serverNamed: true, code });

  it('keeps transport failures and engine outages inconclusive', () => {
    expect(classifyModelHubFailure(null)).toBe('inconclusive');
    expect(classifyModelHubFailure({ serverNamed: false, code: 'bad_response' })).toBe('inconclusive');
    expect(classifyModelHubFailure(named('engine_down'))).toBe('inconclusive');
    expect(classifyModelHubFailure(named('modelHub.errors.engine_down'))).toBe('inconclusive');
  });

  it.each(['source_not_found', 'flow_settled', 'already_connected'])(
    'treats %s as an authoritative terminal',
    (code) => {
      expect(classifyModelHubFailure(named(code))).toBe('authoritative-terminal');
    },
  );

  it('defaults other server-named failures to retryable provider failures', () => {
    expect(classifyModelHubFailure(named('discovery_failed'))).toBe('retryable-provider');
    expect(classifyOAuthFailure(named('discovery_failed'))).toBe('retryable-provider');
  });

  it('lets a failed poll settle the journey when it is the only authority', () => {
    // A status read is also the call that materializes a just-succeeded flow, so on
    // a device-code login its failure can be the one thing that knows the login
    // produced nothing usable — when the route is what said so.
    expect(pollFailureSettles(false, 'retryable-provider')).toBe(true);
    expect(pollFailureSettles(false, 'authoritative-terminal')).toBe(true);
  });

  it('does not let it settle one while the user’s own submit is outstanding', () => {
    // The submit is the writer of record; a read failing beside it says nothing
    // about whether that write committed.
    expect(pollFailureSettles(true, 'retryable-provider')).toBe(false);
  });

  it('does not let a failure the route never named settle one either', () => {
    // The other half of 「only authority」: being the only one who could speak is not
    // the same as having spoken. The call that materializes a just-succeeded create
    // is this very read, so a dropped connection or an unparseable body means the
    // source may exist while the dialog declares a terminal nobody reported — and
    // an ordinary connect retried from that sentence mints a second source. Same
    // `serverNamed` bit `mayHaveWritten` reads for the refetch, asked about speech.
    expect(pollFailureSettles(false, 'inconclusive')).toBe(false);
    // …and the submit's precedence does not depend on it.
    expect(pollFailureSettles(true, 'inconclusive')).toBe(false);
  });

  it('shows what latching one costs: the success right behind it is ignored', () => {
    // Interleaving 5 as a sequence. `flowStep` is right to ignore the second
    // arrival — the fix is upstream, at which arrival is allowed to be a verdict.
    const authority = createFlowAuthority(() => {}, null);

    authority.transition({ kind: 'response', flow: flow('awaiting_action') });
    authority.transition({
      kind: 'error',
      errorKey: 'settings.models.oauth.error.generic',
      failureClass: 'retryable-provider',
    });
    const submitSucceeded = authority.transition({ kind: 'response', flow: flow('success') });

    expect(submitSucceeded.action).toBe('ignore');
    expect(authority.current().errorKey).toBe('settings.models.oauth.error.generic');
  });

  it('is what the dialog’s poll actually asks, about the submit’s own flag', () => {
    // A predicate nothing consults is worth nothing, and the value it reads is the
    // other half of the fix: the flow effect is built once per attempt, so it must
    // read the submit's in-flight state through a ref or it reads `false` forever.
    const dialog = readFileSync(join(__dirname, 'OAuthConnectDialog.tsx'), 'utf8');

    expect(dialog).toMatch(/pollFailureSettles\(submittingRef\.current, failureClass\)/);
    expect(dialog).toMatch(/submittingRef\.current = submitting;/);
    // The second argument only exists if the failure is read BEFORE the question is
    // asked — reading it after the settle branch is how this reverts silently.
    expect(dialog.indexOf('const failure = apiFailure(err);')).toBeLessThan(
      dialog.indexOf('const failureClass = classifyOAuthFailure(failure);'),
    );
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

  it('sends an already-FAILED start there too', () => {
    // The finding: this used to latch, because a failure's `error_key` is the whole
    // message — enough to DISPLAY, which is not enough to SETTLE. Settling stops
    // the poll at its next tick, and `oauth_start` never materializes: it is
    // `oauth_status` → `_materialize_completed_oauth` that runs
    // `_fail_closed_hub_reauth`, strips the discovered models and saves the source
    // as 需处理. Latched, the dialog explained a failure over a row still drawn
    // healthy and a binding still pending, until the user's eventual close made
    // `oauth_cancel` do the write as a side effect.
    expect(startNeedsStatusRead(flow('failed'))).toBe(true);
    expect(startNeedsStatusRead(flow('cancelled'))).toBe(true);
  });

  it('asks 「is it terminal」 and nothing about which terminal', () => {
    // WHICH terminals owe server-side work is a fact about the server
    // (`_is_hub_unsuccessful_terminal` is hub × {failed, cancelled}) and a guard
    // over the envelope may not decide it. So this is exactly `flowStateTerminal`
    // — one list, so the two cannot drift into disagreeing.
    for (const state of ['success', 'failed', 'cancelled'] as const)
      expect(startNeedsStatusRead(flow(state))).toBe(flowStateTerminal(state));
    for (const state of ['starting', 'awaiting_action', 'verifying'] as const)
      expect(startNeedsStatusRead(flow(state))).toBe(flowStateTerminal(state));
  });

  it('leaves a running start to the poll it already schedules', () => {
    expect(startNeedsStatusRead(flow('awaiting_action'))).toBe(false);
    expect(startNeedsStatusRead(flow('verifying'))).toBe(false);
  });
});

describe('flowStateTerminal — one list of the states the server calls finished', () => {
  it('counts every state a flow can be reported finished in', () => {
    expect(flowStateTerminal('success')).toBe(true);
    expect(flowStateTerminal('failed')).toBe(true);
    expect(flowStateTerminal('cancelled')).toBe(true);
  });

  it('does not count a flow still running', () => {
    expect(flowStateTerminal('starting')).toBe(false);
    expect(flowStateTerminal('awaiting_action')).toBe(false);
    expect(flowStateTerminal('verifying')).toBe(false);
  });

  it('is the list flowStep latches on, not a second copy of it', () => {
    // Two enumerations of 「finished」 in one file is how they drift; the dialog's
    // 「read this through status」 and the reducer's 「nothing may reopen this」 have
    // to be the same set or one of them is wrong about a state the other handles.
    const source = readFileSync(join(__dirname, 'asyncLifetime.ts'), 'utf8');
    expect(source).toMatch(/if \(flowStateTerminal\(flow\.state\)\) \{/);
    expect(source).toMatch(/startNeedsStatusRead = \(flow: OAuthFlow\): boolean =>\s*flowStateTerminal\(flow\.state\)/);
    // `timeout` is the dialog's OWN verdict about a flow the server still holds
    // live, which is why it arrives as a tick and is not in this list.
    expect(flowStateTerminal('starting')).toBe(false);
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

    // The second argument is round 16's: silence became the default, so the arrival
    // that IS the account of record for the view it just settled says so out loud.
    expect(dialog).toMatch(
      /if \(terminalArrivalMovedRows\(step\.action\)\) rowsBehindAreStale\(undefined, true\);/,
    );
    expect((dialog.match(/onConnectedRef\.current\(\)/g) ?? []).length).toBe(1);
    // And that owner no longer asks which journey it is before re-reading: a
    // create's `oauth_cancel` commits the source it was told to throw away.
    expect(dialog).not.toMatch(/if \(!isReauth\) return;/);
  });
});

describe('failureLanded — whose account of the rows is the one on screen', () => {
  it('accepts the failure the authority actually latched', () => {
    expect(failureLanded('fail')).toBe(true);
  });

  it('refuses an arrival that reached an already-settled view', () => {
    // The finding this pins. A poll can settle terminally with `discovery_failed`
    // AND a non-empty `interrupted_pairs`; a paste submit rejecting afterwards is
    // still `isCurrent()` — settling changes neither the authority nor the flow id —
    // so it used to hand its own (empty) list to the owner and erase the gap report
    // while `flowStep` correctly kept the poll's sentence above it.
    expect(failureLanded('ignore')).toBe(false);
  });

  it('refuses the states no error arrival can produce', () => {
    // `flowStep` answers an `error` event with `fail` or `ignore` and nothing else,
    // so these are unreachable from the two call sites — pinned so a later branch
    // that starts producing one of them has to come back to this rule.
    expect(failureLanded('timeout')).toBe(false);
    expect(failureLanded('succeed')).toBe(false);
    expect(failureLanded('continue')).toBe(false);
  });

  it('gates the pairs at every catch that carries them, and nothing else', () => {
    // Structural: the two `catch` sites are the only places a failure's pairs enter
    // the owner, and each has to hand over the authority's own answer. The reorder
    // is the substance of the fix — the transition has to have happened before the
    // pairs are offered, or there is no answer to hand over.
    const dialog = readFileSync(join(__dirname, 'OAuthConnectDialog.tsx'), 'utf8');
    const carrying = [...dialog.matchAll(/rowsBehindAreStale\(failure[^)]*\)/g)].map((m) => m[0]);

    // The pattern must exist somewhere, or the sweep proves nothing.
    expect(carrying.length).toBeGreaterThan(0);
    for (const call of carrying) expect(call).toMatch(/failureLanded\(step\.action\)/);
    // And the refetch is still owed on the ignored path: the gate is the second
    // argument, never a reason to skip the call.
    expect(dialog).not.toMatch(/if \(failureLanded\([^)]*\)\)\s*rowsBehindAreStale/);
  });

  it('makes silence the default, and lets exactly one arrival opt out of it', () => {
    // Round 16's finding, and why it is fixed at the DEFAULT rather than at the two
    // call sites that had it wrong. This component outlives the attempt: both hosts
    // leave it mounted and toggle `open`, so `stranded` survives a close, and a
    // request abandoned by attempt A can land after attempt B has put its own gap
    // report on screen — one click away, since closing a re-login and starting
    // another on a different source does exactly that. The two answers are not
    // equally wrong. Forgetting to speak costs nothing, because the refetch runs
    // either way; forgetting to stay silent writes an empty list over the one part
    // of B's report that no later read of `/agents` reproduces.
    const dialog = readFileSync(join(__dirname, 'OAuthConnectDialog.tsx'), 'utf8');

    expect(dialog).toMatch(/pairsSpeak = false/);

    // One opt-in, and it belongs to the arrival that just settled the view these
    // pairs render under. Any other site passing a bare `true` is this bug again.
    const speaking = [...dialog.matchAll(/rowsBehindAreStale\([^)]*true[^)]*\)/g)].map((m) => m[0]);
    expect(speaking).toEqual(['rowsBehindAreStale(undefined, true)']);
    expect(dialog).toMatch(
      /terminalArrivalMovedRows\(step\.action\)\) rowsBehindAreStale\(undefined, true\)/,
    );

    // The two paths reached after the attempt is over take the default rather than
    // restating it — including the release re-read, which awaits the cancel first
    // and is therefore the latest-landing arrival in the file.
    expect(dialog).toMatch(
      /const resolvedAfterAttempt = React\.useCallback\(\(\) => rowsBehindAreStale\(\), \[rowsBehindAreStale\]\);/,
    );
    expect(dialog).toMatch(/reread: \(\) => rowsBehindAreStale\(\),/);
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

  it('CANCELS when nobody owns the flow, because an existing successor would be there', async () => {
    // The handoff one beat earlier — a replacement whose own start is still in
    // flight. That beat is not observable from here, and does not need to be: a
    // successor takes the ref SYNCHRONOUSLY as its effect body runs, before its
    // start request is awaited, so any successor that EXISTS has already put
    // itself there by the time this late response reads the ref. `null` is
    // therefore the user closing the dialog and not reopening it, and letting go
    // leaves the authorization they just cancelled live until the server expires
    // it — where the next re-auth for the row is handed exactly that flow and can
    // materialize a login they declined. Only a successor that exists may suppress
    // the cancel.
    const { log, journey, ops } = teardown();
    await releaseFlow(journey, null, ops(() => Promise.resolve()));
    expect(log).toEqual(['cancel', 'reread']);
  });

  it('cancels an abandoned CREATE flow for its own second reason', async () => {
    // Same ownership, other route, and the answer may not depend on the one above:
    // `oauth_start` mints a fresh pending source id every call and never adopts a
    // pending flow, so no successor could be handed this one whatever the ref
    // holds. Read the route wrong and the abandoned authorization plus its
    // registry binding stay live until the server expires them.
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
    // No owner: nothing to hand it to. A successor is in the ref before it awaits
    // anything, so an absent one is absent rather than pending.
    expect(flowLetGo(mine, null, true)).toBe(false);
    expect(flowLetGo(mine, null, false)).toBe(false);
  });

  it('has the successor in the ref before it awaits anything', () => {
    // Structural, because the rule above is a claim about the dialog rather than a
    // preference: `flowLetGo` may read an absent owner as 「no successor exists」
    // only while every successor publishes itself before its first suspension
    // point. Move that write below the start request and a real successor becomes
    // invisible for a round trip, which is the interleaving the cancel above would
    // then break.
    const dialog = readFileSync(join(__dirname, 'OAuthConnectDialog.tsx'), 'utf8');
    const claims = dialog.indexOf('const authority = createFlowAuthority(');
    const takes = dialog.indexOf('flowAuthorityRef.current = authority;', claims);

    expect(claims).toBeGreaterThan(-1);
    expect(takes).toBeGreaterThan(claims);
    expect(dialog.slice(0, takes)).not.toMatch(/\bawait\b/);
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
    const handedBack = dialog.match(/if \(cancelled\) \{\s*resolvedAfterAttempt\(\);\s*return;\s*\}/g) ?? [];

    expect(bare.length).toBe(1);
    expect(handedBack.length).toBe(3);
    // And the fourth exit — a start that SUCCEEDED after the close — reaches the
    // same owner through the release it also has to make.
    expect(dialog).toMatch(/reread: resolvedAfterAttempt,/);
  });

  it('counts the paste submit among those exits, on both of its outcomes', () => {
    // Round 17's, and the reason the owner is no longer scoped to the effect: the
    // submit could not reach it. Its two exits ask a different question — `cancelled`
    // is 「the effect tore down」, `isCurrent()` is 「this attempt is still the one on
    // screen」, which a mid-open restart also answers no — but they are the same fact
    // about the rows, and both of them write before they can be dropped. The success
    // went through `_materialize_completed_oauth` to be a success at all; the
    // rejection can be `discovery_failed` raised one line after
    // `_materialize_reauth` saved the source as 需处理 with its models stripped.
    // Returning bare there left the close path's re-read — issued while the submit
    // was in flight — as the last word on rows the submit changed afterwards.
    const dialog = readFileSync(join(__dirname, 'OAuthConnectDialog.tsx'), 'utf8');
    const superseded =
      dialog.match(/if \(!isCurrent\(\)\) \{\s*resolvedAfterAttempt\(\);\s*return;\s*\}/g) ?? [];

    expect(superseded.length).toBe(2);
    expect(dialog).not.toMatch(/if \(!isCurrent\(\)\) return;/);
    // Component scope, not effect scope, or the two above cannot see it.
    expect(dialog).toMatch(/^ {2}const resolvedAfterAttempt = /m);
  });

  it('cancels the flow the request opened, not the one the view landed', () => {
    // Round 17's other one. A start can come back ALREADY terminal, and that flow is
    // deliberately never landed in the view — `startNeedsStatusRead` routes it
    // through `poll` so that `settle` is what terminalizes it. Cleanup took its
    // cancel id out of the view, so for exactly that start it found nothing and
    // passed `cancel: null` — which `releaseFlow` is entitled to read as 「this
    // journey never got a flow id」 when what had happened is that nobody told the
    // view. Close during the status read and the flow stayed un-materialized;
    // un-materialized is `not completed`, the only thing `pending_reauth` filters
    // on, so `POST …/reauth` hands the next attempt on that source the login the
    // close was abandoning — and reading its status is what commits it.
    const dialog = readFileSync(join(__dirname, 'OAuthConnectDialog.tsx'), 'utf8');

    expect(dialog).toMatch(/let openedFlowId: string \| null = null;/);
    expect(dialog).toMatch(/openedFlowId = started\.flow_id;/);
    expect(dialog).toMatch(/cancel: opened \? \(\) => modelsApi\.cancelOAuth\(opened\) : null,/);
    // The view is not asked, because it was never the thing that knew.
    expect(dialog).not.toMatch(/authority\.current\(\)\.flow;/);
  });
});
