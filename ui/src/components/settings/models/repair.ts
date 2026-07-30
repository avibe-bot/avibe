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

/**
 * The one-tap remedy for a blocked source, or null when it genuinely has none.
 *
 * Two rules, no per-cause table:
 *
 *  1. The credential itself failed → replace it, by the route that owns that
 *     source's kind of credential (login for a subscription, key for an api_key).
 *  2. Anything else → the cause lives UPSTREAM (balance run out, account
 *     restricted, an unclassified failure). Avibe cannot fix it and must not
 *     pretend to; what it can do is stop guessing and re-check, which is exactly
 *     what the recovery test is for. The row's affordance is 「handled it —
 *     retry」, not 「top up here」, because no contract field carries a vendor
 *     billing URL and a button that cannot go there would be the dead end again.
 *
 * Returns null only where no route applies at all (a native source, whose
 * blockers are cleared by its own CLI). A healthy source has no remedy to offer
 * because it has no problem — the caller renders nothing, not a disabled button.
 */
export function repairAction(source: Source): RepairKind | null {
  if (!wasBlocked(source.state)) return null;
  const credentialFailed = Boolean(source.state.detail_key && CREDENTIAL_CAUSES.has(source.state.detail_key));
  if (credentialFailed) {
    if (canReauth(source)) return 'reauth';
    if (canReplaceKey(source)) return 'replace_key';
    return null;
  }
  return canRetest(source) ? 'retest' : null;
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
  | { kind: 'failed'; sourceName: string; detailKey: SourceDetailKey | null };

export function dryRunOutcome(probe: ProbeResult, sources: Source[]): DryRunOutcome {
  const sourceName = nameOf(sources, probe.source_id);
  return probe.reachable
    ? { kind: 'ok', sourceName, latencyMs: probe.latency_ms }
    : { kind: 'failed', sourceName, detailKey: probe.error };
}
