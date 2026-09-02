// Putting the shared instance back, declared once.
//
// Every spec here mutates a live instance, so every spec here owes a
// restoration — and each one used to write its own. That is where a whole class
// of defects came from, because a hand-written restoration independently
// re-decides three things, and each of them is easy to answer with a PROXY that
// is usually right:
//
//   - WHAT to undo. "Whatever appeared since I looked" is a proxy for "the
//     thing my switch imported"; it is correct only while nothing else on the
//     instance moves.
//   - WHAT to put back. "The chain as I found it" is a proxy for "the chain the
//     operator left"; the two differ the moment the suite's own sources have
//     been placed into it, which the server does on create.
//   - WHETHER it is done. "`enabled` is false" is a proxy for "the runtime is
//     stopped"; the runtime has two facts and a failed stop can land one
//     without the other.
//   - WHOSE failure a failure is. `test.fail()` is a proxy for "this assertion
//     is expected to fail"; it marks the whole test, teardown included.
//   - WHEN the debt begins. "The state has changed" is a proxy for "I have asked
//     for it to change"; the request is already gone by then, so a boundary
//     opened on the first is skipped on exactly the path where the second holds.
//
// A proxy can be right about the wrong thing. So the restorations live here,
// stated as POSTCONDITIONS and keyed on identity: what a native source IS, and
// what "running" IS. One declaration each, so a site cannot answer them
// differently, and so the next site added inherits the answer instead of
// picking one.
import type { HubApi, RouteHop, Runtime } from './api';
import { isSuiteSource } from './api';
import { expect, runtimeIsRunning } from './fixtures';

/**
 * Reads the baseline `restoreAgentChain` puts back: the OPERATOR's chain, which
 * is not the same thing as the chain that is there when a spec looks.
 *
 * Adding a source is not an inert act. `_apply_source_placement` walks every
 * backend's menu and appends the new source to each route whose model it
 * matches, so by the time a spec captures its baseline the gateway fixture's
 * own two sources may already be hops in it. Those hops are arrangement, not
 * state: the fixture's sweep deletes the sources that supply them, so they do
 * not outlive the spec either way.
 *
 * Restoring them is worse than pointless, because a spec may delete or rebuild
 * one of those sources mid-body — B7 deletes it to raise the supply guard, B6
 * deletes and recreates it to shake off a sticky cooldown — and `set_agent_chain`
 * rejects a PUT containing ANY unknown source id outright. So one stale
 * suite-owned hop takes every real hop down with it and the route the operator
 * had is gone.
 *
 * Hence the pair: capture and restore live together and are written once,
 * because the defect was capture being re-derived at each site while only the
 * restore was shared.
 */
export const captureAgentChain = async (
  api: HubApi,
  route: { backend: string; model: string },
): Promise<RouteHop[]> => {
  const ours = new Set((await api.sources()).filter(isSuiteSource).map((source) => source.id));
  const chain = (await api.chains(route.backend)).find((entry) => entry.model_id === route.model);
  return (chain?.chain ?? [])
    .filter((hop) => !ours.has(hop.source_id))
    .map((hop) => ({ source_id: hop.source_id, model_id: hop.model_id }));
};

/**
 * Puts one model's route chain back to `original` — including back to EMPTY.
 *
 * Unconditional, where three copies of this used to guard on
 * `original.length`. That guard is the same proxy as the rest of this file: it
 * asks "was there a chain?" when the postcondition is "the chain is what it
 * was", and the one case it gets wrong is the spec that arranged hops onto a
 * route that had none — precisely the arrangement most worth removing. The
 * server treats clearing a route as a first-class mutation (it is what
 * `newly_empty_routes` in `set_agent_chain` exists for), so an empty `original`
 * is a request, not a no-op.
 *
 * A refusal is reported, never swallowed: nothing downstream can reconstruct a
 * displaced chain — the source sweep that follows only deletes rows — so a
 * silent `false` here leaves the instance's routing changed while teardown
 * reads clean.
 */
export const restoreAgentChain = async (
  api: HubApi,
  route: { backend: string; model: string },
  original: RouteHop[],
): Promise<void> => {
  const restored = await api.putAgentChain(route.backend, route.model, original);
  expect(
    restored,
    `Teardown failed to restore the original route chain for ${route.backend}/${route.model} — `
      + "the instance is left with the scenario's arrangement.",
  ).toBe(true);
};

/**
 * Removes the native sources a Direct→Gateway switch imported, identified by
 * WHAT THEY ARE rather than by when they turned up.
 *
 * `supply_channel === 'native_cli'` is the identity: `build_native_migration_source`
 * is the only thing that writes that channel, and every source a user or
 * another session can create meanwhile — the Add API key dialog's — is `hub`.
 * The pre-switch snapshot still narrows it to this switch's own import, so a
 * native source the instance already held is left where it was; what the
 * snapshot no longer does is decide on its own that anything unfamiliar is
 * ours to delete.
 *
 * Every match is attempted before any failure is raised, the same way
 * `removeSuiteSources` does it: a native source supplying a live route refuses
 * until forced and `deleteSource` forces, one already gone is success, and one
 * that genuinely will not go is named rather than silently stranding the ones
 * behind it.
 */
export const restoreNativeSources = async (api: HubApi, before: Set<string>): Promise<void> => {
  const failures: string[] = [];
  for (const source of await api.sources()) {
    if (source.supply_channel !== 'native_cli' || before.has(source.id)) continue;
    try {
      await api.deleteSource(source.id);
    } catch (error) {
      failures.push(`${source.id}: ${error instanceof Error ? error.message : String(error)}`);
    }
  }
  if (failures.length) {
    throw new Error(
      `The mode switch's imported native source(s) are still on the instance:\n  ${failures.join('\n  ')}`,
    );
  }
};

/** The runtime is up when BOTH of its facts say so. Either one alone is a
 *  half-answer: a stop that disabled the engine and then failed to persist
 *  `enabled=false` reads back as enabled-and-`down`. */
const runtimeIsUp = (runtime: Runtime | null): boolean =>
  runtime?.enabled === true && runtimeIsRunning(runtime?.status?.health);

/**
 * Puts the gateway runtime back to RUNNING — the postcondition, not a sequence
 * of clicks.
 *
 * Reached with an explicit start rather than a toggle, for the reason teardown
 * does not click at all: a toggle asks for the OTHER state, so it is right only
 * if the state it read was, and the partial stop above is exactly the case that
 * makes that read unreliable. `POST /api/models/runtime/start` names the target
 * instead of describing a move away from a guess, and an instance already
 * running is unmoved by it.
 *
 * The API is the authority for "up", not the toggle's aria state: that follows
 * a request the browser may never see answered, and a page that has been closed
 * never sees it at all.
 */
const restoreRuntimeRunning = async (api: HubApi): Promise<void> => {
  let startError: string | null = null;
  if (!runtimeIsUp(await api.runtime())) {
    try {
      await api.startRuntime();
    } catch (error) {
      // Held, not raised: an instance that is already starting refuses a second
      // start, and that refusal is not a failure to restore. Only the state the
      // poll below reads is — and if it reports one, this is the evidence for
      // why.
      startError = error instanceof Error ? error.message : String(error);
    }
  }
  await expect
    .poll(async () => runtimeIsUp(await api.runtime()), {
      timeout: 90_000,
      message:
        'The gateway runtime is not back up, so every spec after this one runs without it.'
        + (startError ? ` The start request was refused: ${startError}` : ''),
    })
    .toBe(true);
};

/**
 * Runs `body` with the runtime's restoration already owed — the boundary is
 * opened by the act of deciding to stop the gateway, not by the stop succeeding.
 *
 * The postcondition above is only half of a restoration; the other half is WHERE
 * the boundary starts, and a hand-written `try` gets that wrong in a way that
 * reads correct: it is placed where the state has changed ("the gateway is
 * STOPPED at this point"), which puts the click that stops it OUTSIDE. A click
 * dispatches its request before its promise settles, so a page or browser that
 * disconnects mid-flight rejects it with the server still completing the stop —
 * and execution never enters the block that would put it back. The runtime stays
 * down and every later spec skips on a precondition this spec broke.
 *
 * So the mutation has nowhere to live except inside. There is no ordering left
 * to get right, which is the only version of this that a later site inherits.
 */
export const withRuntimeRestored = async <T>(api: HubApi, body: () => Promise<T>): Promise<T> => {
  try {
    return await body();
  } finally {
    await restoreRuntimeRunning(api);
  }
};
