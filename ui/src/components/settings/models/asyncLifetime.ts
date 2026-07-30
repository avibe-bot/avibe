// Async ownership rules extracted from the Models components so interleavings can
// be exercised directly: which request may land, what speaks for a row when no
// read does, how long a write counts as outstanding, when a drawer re-seeds, and
// whether a connect-flow transition may change an already terminal view.
import type { AgentMenu, AgentSupply, OAuthFlow, OAuthFlowState } from './types';

// ── Latest async result ────────────────────────────────────────────────────
/**
 * Owns request ordering and the only path that may land a result. Callers start
 * reads; they never reconstruct whether a response is still current.
 */
export const createLatestAsyncAuthority = <T>(land: (value: T) => void) => {
  let latestRequest = 0;

  return {
    run: async (read: () => Promise<T>): Promise<'landed' | 'stale'> => {
      const request = ++latestRequest;
      try {
        const value = await read();
        if (request !== latestRequest) return 'stale';
        land(value);
        return 'landed';
      } catch (error) {
        if (request !== latestRequest) return 'stale';
        throw error;
      }
    },
  };
};

// ── What speaks for a row when no read does ────────────────────────────────
/**
 * Takes a write's echoed Agent row into the page's list.
 *
 * A mutation here is followed by a re-read, and for a while the re-read was the
 * only thing that updated the row it moved. That is a read landing in the middle
 * of the two things it must not be confused with: it can FAIL (the page toasts
 * 「视图可能不是最新的」 and carries on), and it can be superseded — the authority
 * above lands only the latest request and reports the rest `stale`. Either way the
 * mutation returns normally over a row that still shows the pre-write state, which
 * for 来源顺序 means the enrollment baseline 模型菜单与映射 diffs against is one
 * write behind, and the menu's own save reports an append the ORDER write made.
 *
 * So the write's own answer is taken directly. Every one of these routes replies
 * with the same `_agent_payload` projection the list read returns
 * (`service.py:1972` returns it straight out of `set_agent_sources`), computed
 * under the mutation lock after the commit — it is not a client-side prediction of
 * what the write did, it is the server's own statement of what the row now is, and
 * it cannot fail separately from the write that produced it.
 *
 * It does not make the re-read redundant: the echo is one Agent, and an order
 * change also moves the source rows, the other Agents' ● 当前 and the event feed.
 * It makes the re-read's LANDING stop being the only way the row can catch up.
 *
 * A backend the page has no row for is left alone rather than appended. A write
 * echo is an update to a row; which rows exist is the list read's to say.
 */
export const agentsWithEcho = (
  agents: readonly AgentSupply[],
  echoed: AgentSupply,
): AgentSupply[] => agents.map((agent) => (agent.backend === echoed.backend ? echoed : agent));

// ── A write that outlives the drawer that issued it ────────────────────────
/**
 * Which keys have a write outstanding, where 「outstanding」 spans the read-back
 * and not merely the request.
 *
 * It lives above the drawers rather than inside one because a drawer is not
 * alive for the whole of its own write. 完成, the close X, Escape and the overlay
 * all stay live while a PUT is in flight, the page unmounts the drawer when it
 * closes, and reopening mints a fresh component whose local flag reads 「idle」
 * over a write that is still outstanding. A flag with that lifetime is not a
 * guard — it can be false while the fact is true — so everything it gates is
 * ungated for exactly the user who closed and reopened, which for the order
 * drawer means a hand-off to 模型菜单与映射 leaving on a baseline the page has not
 * read back. `seedStep` below is the same lifetime fact met from the other side:
 * the drawer's seed has to survive that reopen too, and does it by re-deriving
 * from props instead of by remembering.
 *
 * Counted rather than a flag per key, because 「pending」 is a property of the SET
 * of outstanding writes: two edits can overlap (last-write-wins is fine — each
 * PUT carries the whole list), and the first to finish would otherwise clear the
 * key while the second is still running.
 *
 * Keyed by backend because that is the write's own grain: `PUT
 * /agents/<backend>/sources` moves one backend's order, so a claude write says
 * nothing about codex and has no business gating it.
 */
export const createPendingWrites = (land: (keys: ReadonlySet<string>) => void) => {
  const outstanding = new Map<string, number>();
  const publish = () => land(new Set(outstanding.keys()));

  return {
    /**
     * Marks `key` pending for the whole of `work`, including whatever `work`
     * awaits after its own request returns. Pairing is not the caller's to get
     * right: there is no way to begin one without ending it.
     */
    track: async (key: string, work: () => Promise<void>): Promise<void> => {
      outstanding.set(key, (outstanding.get(key) ?? 0) + 1);
      publish();
      try {
        await work();
      } finally {
        const left = (outstanding.get(key) ?? 1) - 1;
        if (left > 0) outstanding.set(key, left);
        else outstanding.delete(key);
        publish();
      }
    },
  };
};

/** One key's slot in the registry above, as the component issuing writes sees it. */
export type PendingWrite = {
  /** Whether a write on this key is outstanding — possibly one this component
   *  never issued, because the component that did may already be gone. */
  pending: boolean;
  track: (work: () => Promise<void>) => Promise<void>;
};

// ── The drawers' seed ──────────────────────────────────────────────────────
// A drawer seeds its editable state from the server's saved state, and needs to
// know when that saved state MOVED. Comparing the props object is useless (every
// page refresh builds a new one), so each drawer reduces the fields it seeds from
// to a signature and compares that. The separator is NUL because these parts are
// ids: a separator that cannot occur inside one is what keeps ['a','b'] from
// colliding with ['a b'].
const sig = (parts: readonly (string | number | boolean)[]): string => parts.join('\u0000');

/** 来源顺序 drawer: the per-backend policy + ordered subset it edits. */
export const savedSourcesKey = (agent: AgentSupply): string =>
  sig([agent.sources?.policy ?? 'follow', ...(agent.sources?.order ?? [])]);

/** 映射 drawer: the stored overrides its draft is seeded from. */
export const savedMappingsKey = (agent: AgentSupply): string =>
  sig((agent.mappings ?? []).flatMap((m) => [m.builtin_id, m.target_model_id, m.enabled]));

/** OpenCode 模型菜单 drawer: the stored view + checked identifiers. */
export const savedMenuKey = (menu: AgentMenu | null | undefined): string =>
  sig([menu?.view ?? 'featured', ...(menu?.checked ?? [])]);

/** What a drawer remembers about the saved state it last seeded from. */
export type SeedState = { seeded: boolean; baseline: string | null };

export const initialSeedState: SeedState = { seeded: false, baseline: null };

/**
 * Whether a drawer holding `state` must re-seat itself now that the server's
 * saved state reads `authoritative`.
 *
 * Seeds on open, and again whenever the saved state MOVED under it. Keying on
 * open alone is not enough: the page unmounts a drawer when it closes, so a
 * drawer closed and reopened while its own PUT is in flight seeds from the
 * pre-save props and then never learns that the save landed — and its next edit
 * diffs against that superseded snapshot and writes the old state back over the
 * save the user already saw succeed.
 *
 * A refetch that changed nothing is still inert, which is the whole reason this
 * compares content rather than the props object: that is what protects an edit in
 * flight from a background page refresh. When the saved state genuinely moved,
 * re-seating is the only honest option — a draft built on a base the server has
 * replaced can only be written back as a regression.
 */
export const seedStep = (state: SeedState, authoritative: string): { state: SeedState; reseed: boolean } => {
  if (state.seeded && state.baseline === authoritative) return { state, reseed: false };
  return { state: { seeded: true, baseline: authoritative }, reseed: true };
};

// ── The OAuth dialog's terminal ordering ───────────────────────────────────
/**
 * Every way flow state can reach the connect dialog. A `tick` is the dialog's
 * own deadline check; `reset` starts a new attempt from an unsettled view.
 */
export type FlowEvent =
  | { kind: 'reset' }
  | { kind: 'error'; errorKey: string }
  | { kind: 'response'; flow: OAuthFlow }
  | { kind: 'tick'; overdue: boolean };

/**
 * What the caller must DO about an event. The side effects (toast, refetch,
 * auto-close, stop polling) stay in the component; only the decision lives here.
 *
 *   continue  not finished, keep polling
 *   succeed   first terminal success: fire the connected side effects, once
 *   fail      first terminal failure: show the authority-owned error
 *   timeout   the deadline passed with the flow unfinished
 *   ignore    stale arrival — the dialog already finished
 */
export type FlowAction = 'continue' | 'succeed' | 'fail' | 'timeout' | 'ignore';

/** Everything that decides what the dialog shows, including its terminal latch. */
export type FlowView = { flow: OAuthFlow | null; errorKey: string | null; settled: boolean };

export const initialFlowView: FlowView = { flow: null, errorKey: null, settled: false };

/**
 * The states in which the SERVER has reported a flow finished — one list, because
 * two places asking 「is this over?」 with two enumerations is how they drift.
 *
 * `timeout` is deliberately absent: it is the dialog's own verdict about a flow
 * the server still considers live, which is why it arrives as a `tick` and not as
 * a state at all.
 */
export const flowStateTerminal = (state: OAuthFlowState): boolean =>
  state === 'success' || state === 'failed' || state === 'cancelled';

export const flowStep = (view: FlowView, event: FlowEvent): { view: FlowView; action: FlowAction } => {
  if (event.kind === 'reset') return { view: { ...initialFlowView }, action: 'continue' };
  // Nothing gets to change a finished flow — checked before the event is read, so
  // it holds for EVERY entry point (a poll still in flight when success landed, a
  // paste submit racing that poll, and the deadline tick, which used to stamp
  // `failed` over a success on a dialog left open because nothing adopted the
  // source).
  if (view.settled) return { view, action: 'ignore' };
  if (event.kind === 'error') {
    return { view: { flow: view.flow, errorKey: event.errorKey, settled: true }, action: 'fail' };
  }
  if (event.kind === 'tick') {
    if (!event.overdue) return { view, action: 'continue' };
    return {
      view: {
        flow: view.flow ? { ...view.flow, state: 'failed' } : null,
        errorKey: 'settings.models.oauth.error.timeout',
        settled: true,
      },
      action: 'timeout',
    };
  }
  const flow = event.flow;
  if (flow.state === 'success') return { view: { flow, errorKey: null, settled: true }, action: 'succeed' };
  // `settled` means terminal, not successful: a failed flow is just as finished,
  // and a later arrival has just as little business reopening it. Which states
  // those are is `flowStateTerminal`'s to say — success has already returned, so
  // this is「terminal」and not a second, driftable list of the unsuccessful ones.
  if (flowStateTerminal(flow.state)) {
    return {
      view: {
        flow,
        errorKey: flow.error_key ?? 'settings.models.oauth.error.generic',
        settled: true,
      },
      action: 'fail',
    };
  }
  return { view: { flow, errorKey: null, settled: false }, action: 'continue' };
};

/**
 * Whether a START response has to be re-read through the status route before it
 * may settle the dialog — which EVERY terminal start does.
 *
 * `POST …/reauth` REUSES a live pending flow — it only opens a new one once the
 * old one failed or was cancelled — so a start can come back already terminal,
 * and the start envelope carries the flow alone.
 *
 * This used to ask whether that envelope was RICH ENOUGH: a success needs the
 * `{source, recovered, interrupted_pairs}` tail only status carries, while a
 * failure's `error_key` is the whole message, so a failure could latch. But
 * 「enough to DISPLAY」 is not 「enough to SETTLE」. Settling stops the poll — the
 * next tick exits on the terminal latch — and `oauth_start` hands back the
 * adapter's flow without materializing it. The materialization lives one route
 * over: `oauth_status` → `_materialize_completed_oauth`, which is what runs
 * `_fail_closed_hub_reauth` on an unsuccessful hub terminal, strips the discovered
 * models and persists the source as 需处理. Latch a failed start and none of it
 * happens: the dialog explains a failure while the row behind it still draws
 * healthy and still supplies the ● 当前 it no longer can, the binding stays
 * pending, and the missing write only lands when the user eventually closes the
 * dialog and `oauth_cancel` performs it as a side effect of cancelling.
 *
 * So this does not try to know WHICH terminals owe server-side work — that is a
 * fact about the server (`_is_hub_unsuccessful_terminal` is `hub` ×
 * `{failed, cancelled}`, and a create's orphan cleanup keys off the retained
 * material) and a guard over the envelope may not decide it. Every terminal start
 * is read through the one route entitled to report a terminal; the dialog shows
 * whatever that answers, and `terminalArrivalMovedRows` then re-reads the rows.
 * A non-terminal start needs nothing: it is polled, as it always was.
 */
export const startNeedsStatusRead = (flow: OAuthFlow): boolean => flowStateTerminal(flow.state);

export type FlowAuthority = {
  current: () => FlowView;
  transition: (event: FlowEvent) => ReturnType<typeof flowStep>;
  /**
   * WHAT this journey is for: the source id a re-auth is repairing, or `null` for
   * a create, which has no source yet.
   *
   * Carried on the authority because the authority is the journey's identity, and
   * `flowLetGo` has to ask about the identity of a journey that is not the one
   * asking — see there for the only question it answers.
   */
  subject: string | null;
};

/**
 * Owns the complete view, including its terminal latch, and is the only path
 * that may land a new one. Keeping only `flow` at the landing site would let a
 * caller silently drop `settled` and reopen an already terminal flow.
 */
export const createFlowAuthority = (
  land: (view: FlowView) => void,
  subject: string | null,
): FlowAuthority => {
  let current: FlowView = { ...initialFlowView };

  return {
    current: () => current,
    transition: (event) => {
      const step = flowStep(current, event);
      current = step.view;
      land(current);
      return step;
    },
    subject,
  };
};

/** Whether an action ends the flow, so the caller stops polling. */
export const isDone = (action: FlowAction): boolean => action !== 'continue';

/**
 * Whether a terminal ARRIVAL moved the rows the page behind the dialog draws.
 *
 * Both terminals the server reported are writes, and asking 「did it succeed?」 is
 * what left one of them re-reading nothing. A success materializes the source (or
 * the repair) inside the very call that first reports it. A `failed` hub reauth
 * has already run `_fail_closed_hub_reauth` on its way to answering — the
 * discovered models are stripped and the source is persisted as 需处理 — so the
 * dialog explains a failure while the page behind it still draws that source as
 * healthy, still supplying the ● 当前 it no longer can.
 *
 * `timeout` is not one of them, and that is the line this draws: it is the
 * dialog's OWN verdict, with nothing arrived that could have written. What
 * corrects the rows there is the close path, whose cancel can BE the write
 * (`releaseFlow` below) and which re-reads unconditionally for that reason.
 */
export const terminalArrivalMovedRows = (action: FlowAction): boolean =>
  action === 'succeed' || action === 'fail';

/**
 * Whether a FAILED status poll may speak for the journey.
 *
 * A poll is a reader; the paste submit is the writer of record. `GET …/status`
 * failing while a submit is outstanding says nothing about whether that submit
 * committed — and the submit's own response is the only arrival that carries the
 * terminal tail (`repaired` / `created`) at all. Latching the reader's error
 * settles the view, `flowStep` above then correctly ignores the success that
 * follows, and the dialog says 授权失败 over a credential the server did replace:
 * no toast, no refetch, no repair verdict, on a journey that worked.
 *
 * Dropping it is safe precisely because the other authority is guaranteed to
 * answer: the submit settles the view itself, with its success or through its own
 * catch. So the flow is not left hanging on a poll — and the caller keeps reading
 * meanwhile, which is what preserves the deadline for a submit that never returns.
 *
 * With no submit outstanding a poll IS the only authority — but only when the ROUTE
 * answered. That same materializing call is the reason: a status read that commits
 * the create or the repair and then loses its response (a dropped connection, a
 * proxy's HTML 502, a body that will not parse) throws a failure `call()` minted,
 * not one the server named. Settling on it declares a terminal nobody reported, over
 * an operation that may have committed — the dialog says 授权失败 on a source the
 * server did create, and an ordinary connect retried from there mints a second one.
 * `serverNamed` is the same discriminator `mayHaveWritten` reads for the refetch,
 * asked here about speech instead of writes: a refusal that named itself knows the
 * login went nowhere; a transport failure knows nothing at all.
 *
 * Dropping it is bounded either way — the caller keeps polling, and the deadline at
 * the top of each tick is the dialog's own verdict when nothing ever answers.
 */
export const pollFailureSettles = (submitOutstanding: boolean, serverNamed: boolean): boolean =>
  !submitOutstanding && serverNamed;

/**
 * Whether the failure that just arrived is the one now ON SCREEN.
 *
 * `flowStep` answers `ignore` to everything that reaches a view already settled, so
 * a paste submit rejecting after the poll latched `discovery_failed` leaves the
 * sentence the user is reading belonging to the POLL. The stranded pairs render
 * directly beneath that sentence, as the cost of the failure it names, so they may
 * only come from the arrival that produced it: the late one's own list is empty —
 * the write that stranded those rows happened on the other request — and would
 * erase the gap report rather than restate it.
 *
 * Deliberately not `terminalArrivalMovedRows`, which reads the same `action` to
 * answer a different question. Whether a request may have WRITTEN is independent of
 * whether it may SPEAK, and the refetch beside these pairs is owed either way.
 */
export const failureLanded = (action: FlowAction): boolean => action === 'fail';

/**
 * Whether a journey may walk away from the flow it opened without cancelling it —
 * i.e. whether SOMEONE ELSE can still be handed that exact flow.
 *
 * Three facts have to agree, and each answers a different question:
 *
 * 1. Ownership — 「is it still mine to cancel?」 `POST …/reauth` REUSES a live
 *    pending flow, so a teardown that cancels after ownership moved does not clean
 *    up after itself: it ends the login the user is watching in the dialog that
 *    replaced it. The answer therefore depends on WHEN a path asks. The effect's
 *    own cleanup asks at the instant ownership transfers and still holds it, while
 *    a start whose dialog closed mid-request asks after that same cleanup already
 *    released it. Identical call, right in one place and wrong in the other.
 * 2. The route — 「could it EVER have become someone else's?」 `oauth_start` mints a
 *    fresh pending source id on every call and never looks for a pending flow, so
 *    a create's flow belongs to the one journey that opened it. Ownership alone
 *    cannot say this, which is why `routeReuses` is asked separately: on a create
 *    there is no successor coming for that flow at all, so it dies with the journey
 *    that opened it.
 * 3. The successor's SUBJECT — 「could this particular one be handed my flow?」
 *    `pending_reauth(source_id)` filters on `binding.source_id`, so the handover is
 *    keyed by SOURCE while ownership of this ref is global to the dialog. Close a
 *    pending re-auth for source A, open one for source B before A's start returns,
 *    and A finds a live owner that can never adopt its flow: withholding the cancel
 *    there leaves A's authorization running until it expires. So the route being
 *    reusable is a claim about a class of successors, not about whoever happens to
 *    hold the ref.
 *
 * A `null` owner is NOT let go, and that is a fact about this dialog rather than a
 * preference. Every successor takes authority SYNCHRONOUSLY in the effect body —
 * `flowAuthorityRef.current = authority` in `OAuthConnectDialog.tsx`, before the
 * start request is awaited — so a successor that exists has already put itself
 * there by the time any late response reads the ref. `null` therefore cannot mean
 * 「a successor is about to」; it means the user closed the dialog and did not
 * reopen it.
 *
 * An earlier revision read it the other way and withheld the cancel to protect a
 * flow some future start might adopt. What that actually left behind was a live
 * authorization the user had just cancelled: the server holds it until it times
 * out, and the next re-auth for that source is handed exactly that flow and can
 * materialize it — a login the user declined, completing later. A start after the
 * cancel just mints a fresh flow, so the real trade is one extra round trip
 * against honouring 取消, and only a successor that EXISTS may suppress it.
 */
export const flowLetGo = (
  journey: FlowAuthority,
  owner: FlowAuthority | null,
  routeReuses: boolean,
): boolean =>
  owner !== null &&
  owner !== journey &&
  routeReuses &&
  owner.subject === journey.subject;

/**
 * Hands back the flow a journey opened, and is the ONLY authorization for a
 * teardown cancel.
 *
 * Whether the cancel is authorized is `flowLetGo`'s call — see there for the three
 * separate facts that have to agree before a journey may walk away from a flow.
 *
 * Rereading is unconditional, and this function is not given the flow so that no
 * caller can argue otherwise from it. `POST /oauth/cancel` is not always a
 * cancel — `oauth_cancel` routes a `success` flow, and a failed hub reauth, into
 * `_materialize_completed_oauth` — so the call can BE the write. The only state a
 * caller could branch on is the last POLLED snapshot, which an in-flight poll or
 * a paste submit can terminalize between that read and the cancel landing. An
 * earlier revision branched on exactly that, from a list named `TERMINAL`;
 * naming a snapshot after a fact did not make it one.
 *
 * A `null` cancel means the journey never got a flow id — there is no call to
 * make, which is not the same as deciding not to make one.
 */
export const releaseFlow = async (
  journey: FlowAuthority,
  owner: FlowAuthority | null,
  ops: {
    cancel: (() => Promise<unknown>) | null;
    reread: () => void;
    /** Whether the ROUTE can hand this same flow to a successor journey. */
    reusable: boolean;
  },
): Promise<void> => {
  if (ops.cancel && !flowLetGo(journey, owner, ops.reusable)) {
    try {
      await ops.cancel();
    } catch {
      // Nothing to show — the dialog this belonged to is already gone. The reread
      // below still has to run: the writes upstream of this call do not depend on
      // it succeeding.
    }
  }
  ops.reread();
};
