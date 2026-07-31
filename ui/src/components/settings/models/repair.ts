// The supply JOURNEYS' decisions — which remedy a stopped source actually has,
// what the server's answer to a repair means, and whether 试跑 has anything real
// to run. Same shape and same reason as `supply.ts` / `sufficiency.ts`: each of
// these is a judgement the components are not free to improvise, this repo's
// vitest has no DOM, so they are plain functions over the contract types.
//
// §4.5 is what makes the first one load-bearing: `needs_action` 「carries a
// detail_key naming the cause, so the row can offer ONE TAP to fix it (re-auth,
// top up, replace key) instead of a dead-end error string」. A row that renders
// the cause and stops is the dead end that sentence forbids.
import type {
  AgentBackend,
  AgentSupply,
  ProbeErrorKey,
  ProbeResult,
  Source,
  SourceDetailKey,
  SourcePolicy,
  SourceRepaired,
  SourceState,
  SupplyChannel,
  SupplyGap,
} from './types';

/**
 * The two statuses a repair can recover from, and the exact set the server calls
 * `recovered` over (`state.status in {needs_action, error}`).
 *
 * Deliberately NOT `needsAttention`, which is wider by one: an 额度用完 cooldown
 * earns the gold sub-line but heals itself on the billing cycle, and offering it
 * a repair button would put a one-tap fix on the one blocker that does not need
 * one. Deliberately NOT `isUnhealthy` either, which is wider by all three
 * cooldowns. Three predicates, three different questions.
 */
export const wasBlocked = (state: SourceState): boolean =>
  state.status === 'needs_action' || state.status === 'error';

// ── Which remedy a source HAS (independent of whether it needs one) ──────
// These three mirror the server's own preconditions, one for one, so the menu
// offers exactly the actions the routes accept. The elective entries read these;
// the blocked row's inline button reads `repairAction` below.

/** `POST …/reauth` accepts subscriptions on BOTH channels — a hub-held grant is
 *  re-obtained by logging in again just as a native CLI login is. */
export const canReauth = (source: Source): boolean => source.kind === 'subscription';

/** `PUT …/credential` is hub-channel api_key only, and needs an existing
 *  credential to replace (`kind != api_key || channel != hub || !credential_ref`
 *  ⇒ `discovery_failed`). */
export const canReplaceKey = (source: Source): boolean =>
  source.kind === 'api_key' && source.supply_channel === 'hub' && Boolean(source.credential_ref);

/** `POST …/refresh` is the contract's ONE recovery endpoint (「v3 does not add a
 *  second 'recover' endpoint」) and rejects native sources, which have nothing to
 *  re-discover. */
export const canRetest = (source: Source): boolean => source.supply_channel === 'hub';

/** What a stopped row offers as its one tap. */
export type RepairKind = 'reauth' | 'replace_key' | 'retest';

/** Causes whose fix is a NEW credential, held by Avibe: the grant or the key
 *  itself stopped working, and only replacing it can help. */
const CREDENTIAL_CAUSES: ReadonlySet<SourceDetailKey> = new Set<SourceDetailKey>([
  'models.source.needs_action.oauth_expired',
  'models.source.needs_action.credential_revoked',
]);

/** 「The last sign-in produced nothing usable」 — the whole of `ErrorDetailKey`,
 *  and on a native source the only blocker another sign-in can actually clear.
 *  Rule 3 in `repairAction` is what reads it. */
const SIGN_IN_INCOMPLETE: SourceDetailKey = 'models.source.error.unclassified';

/**
 * The one-tap remedy for a blocked source, or null when it genuinely has none.
 *
 * Two rules, no per-cause table:
 *
 *  1. The credential itself failed → replace it, by the route that owns that
 *     source's kind of credential (login for a subscription, key for an api_key).
 *  2. Anything else → Avibe cannot name the cause and must not pretend to; what
 *     it can do is stop guessing and re-check, which is exactly what the recovery
 *     test is for. The row's affordance is 「handled it — retry」, not 「top up
 *     here」, because no contract field carries a vendor billing URL and a button
 *     that cannot go there would be the dead end again.
 *  3. …unless the source has no recovery test at all (native) AND its blocker is
 *     the one a fresh sign-in clears — then re-login, because rule 2 would
 *     otherwise leave that row with no tap whatsoever. Narrow on purpose: the
 *     branch comment carries the cost argument for why it is not every cause.
 *
 * A healthy source has no remedy to offer because it has no problem — the caller
 * renders nothing, not a disabled button.
 */
export function repairAction(source: Source): RepairKind | null {
  if (!wasBlocked(source.state)) return null;
  const credentialFailed = Boolean(source.state.detail_key && CREDENTIAL_CAUSES.has(source.state.detail_key));
  if (credentialFailed) {
    if (canReauth(source)) return 'reauth';
    if (canReplaceKey(source)) return 'replace_key';
    return null;
  }
  if (canRetest(source)) return 'retest';
  // Native has no recovery refresh — `refresh_source` refuses `native_cli` outright — so
  // rules 1 and 2 together leave a stopped native row with no tap at all, its
  // remedy surviving only in the overflow menu. Rule 3 covers the one cause where
  // signing in again is the answer rather than a guess, and the cause matters
  // because this fallback SPENDS something: a native re-login invalidates the
  // current sign-in before the vendor page even loads (`reauthCost` below), so
  // offering it for a blocker it cannot fix is the §4.5 dead end plus a bill.
  //
  // `error.unclassified` earns it. It is the only key `ErrorDetailKey` holds, and
  // it reaches a NATIVE source from exactly two places, both inside a native
  // re-login: `completed_source_status` threw, or post-login discovery came back
  // empty. It means 「that sign-in produced nothing usable」, so another one is the
  // convergent recovery — and without this the two sibling writes of one backend
  // helper contradict each other, `_mark_native_reauth_unavailable`'s needs_action
  // branch keeping the button (its key is a credential cause) while its error
  // branch loses it. A declared upstream cause like 余额耗尽 does NOT earn it, and
  // the native/`account_banned` case in `repair.test.ts` is that boundary.
  return source.state.detail_key === SIGN_IN_INCOMPLETE && canReauth(source) ? 'reauth' : null;
}

/**
 * A remedy's label key. A Record over the union, never a template literal:
 * `t(`settings.models.repair.${kind}`)` compiles for every member and then
 * renders the raw key path for the one the bundle spells differently
 * (`replace_key` → `replaceKey`), which is what a blocked api_key row showed. As
 * a Record the compiler refuses a new RepairKind that has no label, and
 * `repair.test.ts` proves each one is translated in both locales.
 */
export const REPAIR_LABEL_KEY: Record<RepairKind, string> = {
  reauth: 'settings.models.repair.reauth',
  replace_key: 'settings.models.repair.replaceKey',
  retest: 'settings.models.repair.retest',
};

/**
 * What STARTING a re-login costs on this source's channel — the confirm's whole
 * subject, and the one thing about it the two channels do not share.
 *
 * `immediate`: a native re-login is irreversible up front.
 * `mark_native_irreversible_start` runs as the login spawns and rewrites every
 * native source of that vendor's backend to 需要处理 with its discovered models
 * stripped; it is rolled back only when the login fails to SPAWN. The price is
 * paid on 「开始」, whatever happens next.
 *
 * And it is not paid back in full: the sentence has to say so, because success
 * restores only the source the dialog is about. `_materialize_completed_oauth`
 * re-discovers onto THAT one; its siblings on the shared CLI keep the empty
 * inventory and the 需处理 the start wrote, each needing its own sign-in
 * (`tests/test_model_hub_api.py::test_native_reauth_invalidates_sibling_sources_for_shared_cli`).
 * 「until you finish」 alone reads as a wait, which is the one thing this is not.
 *
 * `on_failure`: a hub re-login writes nothing at start — the held credential goes
 * on working while the user signs in. The cost arrives only if the flow comes
 * back `failed`, where `_fail_closed_hub_reauth` marks the source 需要处理 because
 * Avibe cannot tell whether the vendor invalidated the old grant. Closing the
 * dialog cancels the flow, and a cancelled flow is not a failed one.
 *
 * Two costs, two sentences. One sentence over both is false on one of them, and
 * 「the old sign-in stops working as soon as you start」 is false on hub in the
 * direction that matters: it warns about a loss that does not happen, and says
 * nothing about the one that does.
 */
export type ReauthCost = 'immediate' | 'on_failure';

export const reauthCost = (source: Source): ReauthCost =>
  source.supply_channel === 'native_cli' ? 'immediate' : 'on_failure';

const REAUTH_BODY_KEY: Record<ReauthCost, string> = {
  immediate: 'settings.models.repair.reauthBody.immediate',
  on_failure: 'settings.models.repair.reauthBody.onFailure',
};

/** The confirm body that is TRUE for this source. */
export const reauthBodyKey = (source: Source): string => REAUTH_BODY_KEY[reauthCost(source)];

// ── What the server's repair answer MEANS ────────────────────────────────

/**
 * The verdict over a repair tail — the single owner of 「did that fix it?」, so
 * the toast, the dialog's closing behaviour and the gap report cannot disagree.
 *
 * All three inputs are the server's own: `interrupted_pairs` is the output of the
 * very guard that would have refused the write, `source.state` is where the write
 * left the source, and `recovered` is its judgement that the source HAD been
 * blocked. Nothing is re-derived here — a client that recomputed 「is anything
 * stranded?」 from `/agents` would be answering a different question (today's
 * supply) than the one it renders.
 *
 * The order is the point:
 *
 *  1. `gaps` outranks everything. A forced replacement that fixed this source
 *     while stranding another Agent's model is not a success story, and the
 *     report is the whole reason the user was asked to confirm.
 *  2. Then the RETURNED STATE, because `recovered` is a statement about the past
 *     — 「it was blocked when this started」 — and cannot claim the source works
 *     now. A native reauth reaches exactly that case: `_materialize_reauth`
 *     commits `needs_action`/`oauth_expired` when the CLI still reports itself
 *     signed out, and answers 200 with `recovered: true` beside it. Reading
 *     `recovered` alone would put 「已恢复可用」 on a row that is still stopped and
 *     dismiss the dialog over it.
 *  3. Only then 「was it broken before?」, which separates a repair from the
 *     elective rotation of a working credential.
 */
export type RepairOutcome =
  | { kind: 'repaired' }
  | { kind: 'refreshed' }
  | { kind: 'unresolved' }
  | { kind: 'gaps'; gaps: SupplyGap[] };

export const repairOutcome = (tail: SourceRepaired): RepairOutcome =>
  tail.interrupted_pairs.length > 0
    ? { kind: 'gaps', gaps: tail.interrupted_pairs }
    : wasBlocked(tail.source.state)
      ? { kind: 'unresolved' }
      : tail.recovered
        ? { kind: 'repaired' }
        : { kind: 'refreshed' };

/**
 * Whether the journey may close itself.
 *
 * Provable from the tail alone: 「nothing was stranded, and the source came back
 * working」 is two fields the server sends. The adoption auto-close next door now
 * stands on the same footing — `covered` needs `skipped_by`, and that field has
 * since landed — so the two differ in which fields they read, not in whether they
 * can reach a verdict at all.
 *
 * An allowlist rather than `!== 'gaps'`: L4's rule is that the 1.4s dismissal is
 * for a plain success and 「every other verdict leaves an instruction on screen
 * that 1.4s is not long enough to read」. Written as an exclusion, the next verdict
 * added would inherit the auto-close silently, which is the wrong default for a
 * verdict that exists because something did not work.
 */
export const repairSettles = (outcome: RepairOutcome): boolean =>
  outcome.kind === 'repaired' || outcome.kind === 'refreshed';

/**
 * A verdict's one line, by the same rule as `REPAIR_LABEL_KEY` above: the key is
 * looked up, never assembled.
 *
 * `gaps` is absent deliberately, and the type says so — a gap report is a heading
 * (`gapsDone`) over a list of pairs, not a sentence, so every caller branches on
 * it before reaching this map and the compiler holds them to it.
 */
export type RepairLine = Exclude<RepairOutcome['kind'], 'gaps'>;

export const REPAIR_LINE_KEY: Record<RepairLine, string> = {
  repaired: 'settings.models.repair.repaired',
  refreshed: 'settings.models.repair.refreshed',
  unresolved: 'settings.models.repair.unresolved',
};

/**
 * A verdict's TOAST — the line and the tone, decided together, for every kind.
 *
 * The toast is not the panel. A gap report renders there as a heading over a list
 * of pairs, which is why the map above has no `gaps` entry; a toast has no list, so
 * it needs a sentence for the one verdict that panel handles structurally.
 *
 * Tone belongs here for the reason the gap case proves: written at the call site as
 * 「warning if unresolved, success otherwise」, the gap report got the one tone that
 * contradicts its own text — a green 「连接成功」 over a dialog reporting that Agents
 * now have no source at all. Tone is a property OF the verdict, not of the branch
 * that happens to render it, and a Record over the full union makes the next
 * verdict added answer for its own tone instead of inheriting green.
 */
export const REPAIR_TOAST: Record<RepairOutcome['kind'], { key: string; tone: 'success' | 'warning' }> = {
  repaired: { key: REPAIR_LINE_KEY.repaired, tone: 'success' },
  refreshed: { key: REPAIR_LINE_KEY.refreshed, tone: 'success' },
  // The call succeeded and the source is still stopped: this page's gold 需处理
  // tone, not a red failure.
  unresolved: { key: REPAIR_LINE_KEY.unresolved, tone: 'warning' },
  gaps: { key: 'settings.models.repair.gaps', tone: 'warning' },
};

// ── 试跑 (dry run) ───────────────────────────────────────────────────────

export type DryRunPlan = { kind: 'probe'; backend: AgentBackend } | { kind: 'none' };

/** A dry run exists only while a managed Agent has a runnable supply rollup. */
export function dryRunPlan(agent: AgentSupply): DryRunPlan {
  if (agent.mode !== 'hub') return { kind: 'none' };
  if (agent.supply_status !== 'ok' && agent.supply_status !== 'degraded') return { kind: 'none' };
  return { kind: 'probe', backend: agent.backend };
}

/** A source's name for copy, falling back to its id — the same tolerance
 *  the source rows keep for an id that no longer resolves. */
const nameOf = (sources: Source[], id: string): string =>
  sources.find((s) => s.id === id)?.display_name || id;

/**
 * The identity of the chain a 试跑 report is ABOUT, so that a report outlives every
 * refresh except the ones that make it answer for something else.
 *
 * The rule this obeys, and the reason it is not just 「everything on `agent`」: key
 * on the surface the USER selects with, never on the pick the server RESOLVES from
 * it. The probe deliberately sends no model, so the server resolves whichever
 * selection is live at request time — which makes the selection part of the chain
 * the report describes, not context around it. But resolution outputs move for a
 * second reason: a failing probe cools its own head down, and keying on anything
 * that moves for THAT reason erases the sentence the click just produced (the
 * mistake this key was already once rebuilt to escape — see the reset effect in
 * `SourceOrderDrawer.tsx`).
 *
 * So the members are the config-level ones, verified against the server rather than
 * assumed:
 *
 * - `policy` / `order` as the USER currently has them, not as `agent.sources` holds
 *   them: a drag has already moved them before any PUT lands.
 * - `mappings` — pruned only by `_available_model_ids`, which is
 *   `source_eligible_for_backend` over the inventory and reads no `state.status`
 *   and no cooldown. An inventory change is a real chain change; a cooldown cannot
 *   fake one.
 * - `menu.checked` — same pruning via `_available_opencode_identifiers`, and for
 *   opencode this IS the selection surface. It goes in IN ORDER, unsorted, because
 *   on opencode the order is the selection: with no explicit request the resolver
 *   walks `checked` and takes the first identifier whose source is runnable
 *   (`resolver.py:171-182`), so `[A, B]` and `[B, A]` are different turns — a
 *   different model and possibly a different source — with the same membership.
 *   Nothing on this page writes `checked`, so a re-order arrives the way the
 *   inventory does, from another client; canonicalizing it here would hide exactly
 *   the change the member exists to catch. On the fixed-menu backends the order
 *   carries nothing, and the sensitivity costs at most a still-true report cleared
 *   by an edit that did not matter, which the user answers by re-running 一次试跑
 *   — the same asymmetry the inventory paragraph below settles the same way.
 *   `savedMenuKey` already reads this list in order for the drawer's own baseline.
 * - `selected_model_id`, but only while `selected_model_explicit` says the user is
 *   the one who asked for it. One rule for every backend, because the flag states
 *   the rule the backends were a proxy for: it is a CONFIG fact, derived from the
 *   stored request before anything resolves (`service.py:2109` —
 *   `mode == "hub" and bool(requested_model)`), while `selected_model_id` is the
 *   resolver's echo of that same request. On the fixed-menu backends the two agree
 *   and the guard changes nothing: the hub path echoes `requested_model` back
 *   verbatim in every return (only `target_model` takes the mapping), so a
 *   non-null id there always had a request behind it, and an unset request stays
 *   empty through to a null id. On opencode it separates the two cases that used to
 *   be one — an explicit request comes back merely normalized and IS now keyed on,
 *   while an empty one sends the resolver walking `checked` for the first
 *   identifier whose source is runnable (`resolver.py:171-182`), a pick that
 *   `unavailable_source_ids` and cooldown gating decide and that `checked` above
 *   stands in for.
 *
 *   Reading the flag is what makes the member health-free, and it is why this
 *   waited for the server instead of approximating. Both client-side stand-ins
 *   keyed on something the failing probe itself moves: on the VALUE, a
 *   single-source setup erases its own report, because the probe cools the only
 *   source the head model had and the pick moves; on `supply_status` (sound —
 *   the loop only ever returns a candidate WITH a source, so
 *   `waiting`/`interrupted` proves the request was explicit) the same erasure,
 *   through the guard rather than the value. `selected_model_explicit` moves only
 *   when the user edits the request, so the report survives its own failure.
 *
 * - the sources' model INVENTORIES, and this one is not the user's edit at all.
 *   Everything above is a surface the user changes from this page, and the
 *   paragraph on `mappings` claims 「an inventory change is a real chain change」
 *   while relying on the pruning of two lists to notice one. It does not always
 *   notice: an agent with no mappings and an empty `checked` has nothing
 *   inventory-derived in the key, and under the `follow` policy `order` is a
 *   recommendation rather than the membership — eligibility runs over the
 *   inventory (`source_eligible_for_backend`), so a discovery, a key replacement
 *   or a re-auth elsewhere can move a source in or out of the chain with every
 *   surface on this page untouched. Another client is enough; so is the same user
 *   in a second tab.
 *
 *   Keying on it is safe for the reason the `selected_model_id` paragraph above is
 *   about, read the other way: an inventory is not health. The two writers a
 *   failing 试跑 reaches — `_cooldown` and `_set_source_blocker` — assign
 *   `source.state` and nothing else, so the probe cannot move this member and the
 *   self-erasing report that killed both remedies for the OpenCode gap cannot
 *   happen here. It is `models[].id` only: no `state`, no `usage`, no
 *   `retry_at`, no `provenance` (which changes without changing eligibility).
 *
 *   Scope is every source, not the ones `order` names, precisely because of the
 *   `follow` case above — under that policy a source can join the chain without
 *   appearing in `order` first. The cost of being that broad is an unrelated
 *   source's discovery clearing a report that was still true, and the user re-runs
 *   一次试跑; the cost of being narrow is a sentence that answers for a supplier
 *   chain that no longer exists. Only one of those two is a wrong answer.
 *
 * Excluded on purpose: resolved-head state and everything else about health. A
 * head moving is what a failing report is for.
 */
export const dryRunChainKey = (
  agent: AgentSupply,
  policy: SourcePolicy,
  order: string[],
  sources: Source[],
): string =>
  [
    policy,
    order.join('>'),
    agent.selected_model_explicit ? (agent.selected_model_id ?? '') : '',
    (agent.mappings ?? [])
      .map((m) => `${m.builtin_id}>${m.target_model_id}:${m.enabled ? 'on' : 'off'}`)
      .sort()
      .join(','),
    (agent.menu?.checked ?? []).join(','),
    sources
      .map((s) => `${s.id}:${s.models.map((m) => m.id).sort().join('+')}`)
      .sort()
      .join(';'),
  ].join('|');

/**
 * The probe's own verdict, named for copy. `reachable` is the discriminator the
 * contract pins (`error` is null iff reachable), so nothing else is consulted.
 *
 * A null `latencyMs` is not a missing measurement, it is the answer for a head
 * the Hub does not carry the request for: the caller says 「可用」 without a
 * number rather than printing a zero it did not measure.
 *
 * One copy constraint the key names badly: `models.probe.native_cli_unavailable`
 * is NOT 「you are not signed in」. `_default_native_cli_ready` returns false when
 * an ANTHROPIC_/OPENAI_ key or base-url overrides the CLI's own sign-in, when a
 * codex login is genuinely absent, AND for every backend that has no native
 * channel at all. The first case is the common one, and there the sign-in is
 * perfectly valid — so the string states that the sign-in cannot be used and
 * instructs nothing, the same rule that stopped `oauth_expired` from claiming a
 * wall-clock expiry it cannot prove.
 */
export type DryRunOutcome =
  | { kind: 'ok'; sourceName: string; latencyMs: number | null; channel: SupplyChannel }
  /** v4 widened the key set past `state.detail_key`: the native branch reports
   *  process unavailability, the case the paragraph above is about, and no
   *  source-state key can express it. */
  | { kind: 'failed'; sourceName: string; detailKey: ProbeErrorKey | null; channel: SupplyChannel };

export function dryRunOutcome(probe: ProbeResult, sources: Source[]): DryRunOutcome {
  const sourceName = nameOf(sources, probe.source_id);
  return probe.reachable
    ? { kind: 'ok', sourceName, latencyMs: probe.latency_ms, channel: probe.channel }
    : { kind: 'failed', sourceName, detailKey: probe.error, channel: probe.channel };
}

/**
 * Did that probe leave a mark on the server? 试跑 is a real turn, so a failing
 * one is not a read: `probe_agent` routes the failure through `_cooldown` or
 * `_set_source_blocker`, which changes the source's health and can move the
 * Agent's head and supply rollup with it. A caller that only stores the verdict
 * shows the row states the failure just invalidated.
 *
 * Deliberately 「any unreachable probe」 rather than 「an unreachable hub probe」.
 * The native branch answers from `native_source_ready` and returns before the
 * write block, so today it writes nothing — but keying on the channel would
 * oblige every reader to track which branch writes, and the cost of being wrong
 * is a stale page, while the cost of the extra read is one request on a button
 * the user pressed. A reachable probe is the case that provably writes nothing:
 * the write block sits entirely under `if not reachable`, and no path clears a
 * cooldown on success.
 *
 * A thrown probe the server NAMED needs no re-read for the same reason, checked
 * outcome by outcome: `probe_no_candidate` is raised before any write, and an
 * engine failure propagates out of `_engine_call` above the write block. A throw
 * the server did not name is not one of those outcomes — see `probeArrival`.
 */
export const probeWroteState = (probe: ProbeResult): boolean => !probe.reachable;

/**
 * Whether a caught failure leaves the server's state UNKNOWN to us — the one
 * question two dialogs and this drawer all had to answer, so it lives once.
 *
 * `serverNamed` is the error's own account of where its code came from: the
 * transport mints `bad_response` for a body that would not parse and `http_<n>`
 * for one that parsed saying nothing, and a request that reached the route and
 * lost its answer coming back is exactly as consistent with 「it committed」 as
 * with 「it never ran」. A failure with no name is not one of the route's
 * enumerated outcomes, so nothing about it can be checked outcome by outcome.
 *
 * `null` — a throw that is not an `ApiCallError` at all — counts as unknown for
 * the same reason and one more: reaching it means our own code threw AFTER the
 * await, which puts the server's write strictly in the past.
 */
export const mayHaveWritten = (failure: { serverNamed: boolean } | null): boolean =>
  !(failure?.serverNamed ?? false);

/**
 * Refusals whose ARRIVAL disproves what the page was already drawing, whether or
 * not they wrote anything.
 *
 * `probe_no_candidate` is the case: it is raised before any write (the chain
 * payload is computed, no runnable item is found, nothing is saved), so
 * `mayHaveWritten` is correctly false for it — and yet the 试跑 control the user
 * pressed was rendered from a loaded chain WITH a head, so the refusal is the
 * server saying that head is gone. Another turn, or another client, cooled or
 * blocked it in between. `direct_mode` is the same sentence about the mode: the
 * drawer only offers a chain for an agent it believes is in hub mode.
 *
 * Leaving those two un-reread is the one stale state the user cannot clear by
 * looking: the chip still names a source and the button still invites a run that
 * cannot happen. A write is one reason to re-read; being contradicted is another,
 * and they are independent.
 */
const STATE_PRECONDITION_REFUSALS = new Set(['probe_no_candidate', 'direct_mode']);

export const disprovedDrawnHead = (code: string | null): boolean =>
  code !== null && STATE_PRECONDITION_REFUSALS.has(code);

/**
 * What a probe's arrival still gets to do. Two questions that look like one and
 * are not: whether this answer may be SHOWN, and whether the page behind the
 * sheet has to be RE-READ.
 *
 * They come apart the moment the user edits the chain while a probe is in flight.
 * The edit bumps the sequence, so the response stops being the answer to anything
 * on screen — and dropping it there dropped the acknowledgment that it WROTE.
 * Nothing else covers that: the edit's own PUT refetch is issued while this probe
 * is still out, so it can read the source back exactly as it was before the
 * cooldown landed, leaving a healthy row and a ● 当前 the server has already
 * moved. Staleness is a fact about the QUESTION; the write is a fact about the
 * server, and no guard over the first may decide the second.
 *
 * A THROW is the same split with a different answer to the second half.
 * `probeWroteState`'s outcome-by-outcome check covers the failures the route
 * NAMES, and every one of those arrives as a structured `ApiCallError`. A throw
 * with no server name is not one of the route's outcomes at all — a lost
 * connection, an unparseable body — so the probe may have run and written with
 * the response never reaching us. Unknown is not the same as no, and this takes
 * `probeWroteState`'s own trade: one extra read costs a request, while being
 * wrong the other way costs a page that silently disagrees with the server.
 *
 * And a re-read is owed for a SECOND reason that has nothing to do with writing:
 * a refusal can arrive that contradicts the state the page is drawing from
 * (`disprovedDrawnHead`). The two are independent — `probe_no_candidate` writes
 * nothing and still disproves the head — so they are asked separately and only
 * their answers are combined.
 */
export type ProbeAnswer =
  | { kind: 'result'; probe: ProbeResult }
  /** `serverNamed` is the error's OWN account of where its code came from
   *  (`ApiCallError.serverNamed`), which is what makes it one of the checked
   *  outcomes. Not 「is it one of ours?」: the transport mints `bad_response` and
   *  `http_<n>` itself for a response that never said what happened, and those
   *  are the exact answers this exists for — a probe that ran, wrote, and lost
   *  its reply on the way back throws one of ours with nothing named in it.
   *  `code` is that name when there is one, for the refusals whose arrival is
   *  itself news about the chain. */
  | { kind: 'thrown'; serverNamed: boolean; code: string | null };

export type ProbeArrival = {
  /** Whether this answer is still the answer to what the row is asking. */
  report: boolean;
  /** Whether the source rows and serving-chain status are now stale. */
  reread: boolean;
};

export const probeArrival = (answer: ProbeAnswer, stillCurrent: boolean): ProbeArrival => ({
  report: stillCurrent,
  reread:
    answer.kind === 'result'
      ? probeWroteState(answer.probe)
      : mayHaveWritten(answer) || disprovedDrawnHead(answer.code),
});

export type DryRunRowView = {
  backend: AgentBackend | null;
  enabled: boolean;
  report: boolean;
};

/** Keep a completed report visible even when its probe changes supply health. */
export const dryRunRowView = (
  plan: DryRunPlan,
  row: { line: string | null; saving: boolean; running: boolean },
): DryRunRowView => {
  const backend = plan.kind === 'probe' ? plan.backend : null;
  return {
    backend,
    enabled: backend !== null && !row.saving && !row.running,
    report: row.line !== null,
  };
};
