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
  SourceRepaired,
  SourceState,
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

/** `POST …/test` is the contract's ONE recovery endpoint (「v3 does not add a
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
  // Native has no recovery test — `test_source` refuses `native_cli` outright — so
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
 * Provable from the tail alone, unlike the adoption auto-close next door: that
 * one stays dormant because `covered` needs a `skipped_by` the contract does not
 * carry yet, whereas 「nothing was stranded, and the source came back working」 is
 * two fields the server sends.
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

/**
 * Whether a dry run has anything real to run for this Agent.
 *
 * `none` covers direct mode (no `src_*` identity to report — the route refuses
 * with `direct_mode`, AC-7) and a null `current`, which is precisely
 * waiting/interrupted: there is no head, the page already says so with the
 * remedy attached, and a probe would only re-report it as a failure.
 *
 * No `model` in the request on purpose. `current.model_id` is the RESOLVED id
 * (post-mapping), while the route treats its `model` argument as the REQUESTED
 * one and maps it again — passing the head back would resolve twice and probe a
 * different model than the drawer displays. Omitted, the server uses the very
 * `_requested_model(agent)` that produced `current`, so the probe and the row
 * agree by construction rather than by a client-side guess.
 *
 * Note there is no `native` branch: 「don't fake a native test」 is honoured by
 * the SERVER, which answers a native_cli head with its CLI readiness and
 * `latency_ms: null` instead of sending a request. A client-side branch would
 * have to invent that readiness — the copy keys on the null latency instead.
 */
export type DryRunPlan = { kind: 'probe'; backend: AgentBackend } | { kind: 'none' };

/** A source's name for copy, falling back to its id — the same tolerance
 *  `chainChips` keeps for an id that no longer resolves. */
const nameOf = (sources: Source[], id: string): string =>
  sources.find((s) => s.id === id)?.display_name || id;

export function dryRunPlan(agent: AgentSupply): DryRunPlan {
  if (agent.mode !== 'hub') return { kind: 'none' };
  if (!agent.current) return { kind: 'none' };
  return { kind: 'probe', backend: agent.backend };
}

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
  | { kind: 'ok'; sourceName: string; latencyMs: number | null }
  /** v4 widened the key set past `state.detail_key`: the native branch reports
   *  process unavailability, the case the paragraph above is about, and no
   *  source-state key can express it. */
  | { kind: 'failed'; sourceName: string; detailKey: ProbeErrorKey | null };

export function dryRunOutcome(probe: ProbeResult, sources: Source[]): DryRunOutcome {
  const sourceName = nameOf(sources, probe.source_id);
  return probe.reachable
    ? { kind: 'ok', sourceName, latencyMs: probe.latency_ms }
    : { kind: 'failed', sourceName, detailKey: probe.error };
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
 */
export type ProbeAnswer =
  | { kind: 'result'; probe: ProbeResult }
  /** `serverNamed` is the error's OWN account of where its code came from
   *  (`ApiCallError.serverNamed`), which is what makes it one of the checked
   *  outcomes. Not 「is it one of ours?」: the transport mints `bad_response` and
   *  `http_<n>` itself for a response that never said what happened, and those
   *  are the exact answers this exists for — a probe that ran, wrote, and lost
   *  its reply on the way back throws one of ours with nothing named in it. */
  | { kind: 'thrown'; serverNamed: boolean };

export type ProbeArrival = {
  /** Whether this answer is still the answer to what the row is asking. */
  report: boolean;
  /** Whether the source rows and ● 当前 behind this sheet are now stale. */
  reread: boolean;
};

export const probeArrival = (answer: ProbeAnswer, stillCurrent: boolean): ProbeArrival => ({
  report: stillCurrent,
  reread: answer.kind === 'result' ? probeWroteState(answer.probe) : !answer.serverNamed,
});

/**
 * What the 试跑 row IS right now — which is not the same question as what the
 * chain is, because the row outlives the chain it reported on.
 *
 * Two rules, and neither is visible from `dryRunPlan` alone:
 *
 *  1. A REPORT survives losing its head. 试跑 is what takes the head away:
 *     probing the chain's last runnable source and failing cools that source
 *     down (`probeWroteState`), so the re-read that failure demands comes back
 *     with `current: null` and the plan turns `none`. Dropping the row there
 *     erases the sentence the click produced — making the failing run, the one
 *     the user most needs to read, the only one whose answer flashes past. So
 *     the CONTROL goes (there is nothing runnable to reach, and the page states
 *     that one level up with the remedy attached) while the LINE stays.
 *  2. The control waits for the drawer's save. That PUT is optimistic: the list
 *     — and the chain key the report is filed under — have already moved to the
 *     order the user dropped while the server still answers for the old one. A
 *     probe launched in that window reports on the superseded chain under the
 *     new one's key, and the key cannot clear it because it IS already the new
 *     key. Every other control in this drawer is disabled for that window; this
 *     one is not special.
 */
export type DryRunRowView = {
  /** The backend to probe, or `null` when there is nothing runnable to reach. */
  backend: AgentBackend | null;
  /** Whether the control may be pressed now. */
  enabled: boolean;
  /** Whether the last answer is still worth drawing. */
  report: boolean;
};

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
