// Tests for the supply journeys' decisions.
//
// One block per question, because each one is a place a component would
// otherwise improvise: which tap a stopped row offers, what the server's repair
// answer means, and whether 试跑 has anything real to run. The last block is the
// grep guard that keeps those questions from growing a second answer next door —
// the same shape, and the same reason, as `sufficiency.test.ts`.
import { readFileSync, readdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

import en from '../../../i18n/en.json';
import zh from '../../../i18n/zh.json';
import {
  disprovedDrawnHead,
  dryRunChainKey,
  dryRunOutcome,
  dryRunPlan,
  dryRunRowView,
  mayHaveWritten,
  probeArrival,
  probeWroteState,
  reauthBodyKey,
  reauthCost,
  repairAction,
  REPAIR_LABEL_KEY,
  REPAIR_LINE_KEY,
  REPAIR_TOAST,
  repairOutcome,
  repairSettles,
} from './repair';
import type { RepairOutcome } from './repair';
import { PROBE_RESULT_CONTRACT_VERSION } from './types';
import type { AgentSupply, ProbeResult, Source, SourceDetailKey, SourcePolicy, SupplyGap } from './types';

/** A healthy hub api_key with a credential to replace — the base every case
 *  below narrows, so each test names only what it is actually about. */
const source = (over: Partial<Source> = {}): Source => ({
  id: 'src_a',
  kind: 'api_key',
  vendor: 'anthropic',
  display_name: 'Key A',
  protocol: 'anthropic',
  supply_channel: 'hub',
  billing: 'metered',
  credential_ref: 'cred_a',
  state: { status: 'active' },
  models: [],
  ...over,
});

const blocked = (detail_key: SourceDetailKey, over: Partial<Source> = {}): Source =>
  source({ state: { status: 'needs_action', detail_key }, ...over });

const subscription = { kind: 'subscription', vendor: 'anthropic', account_label: 'me@example.com' } as const;
const native = { supply_channel: 'native_cli', credential_ref: null } as const;

/** A locale leaf by its full dotted key — the same path the components hand to
 *  `t`, so a key that only exists as a prefix fails the same way it would there. */
const translated = (bundle: unknown, key: string): unknown =>
  key.split('.').reduce<unknown>((node, part) => {
    if (!node || typeof node !== 'object') return undefined;
    return (node as Record<string, unknown>)[part];
  }, bundle);

describe('repairAction — the one tap a stopped row offers (SourceRowMenu)', () => {
  it('offers nothing to a source that is running', () => {
    // Not a disabled button: a healthy row has no problem, and an affordance for
    // a problem it does not have is noise the V6 row has no slot for.
    expect(repairAction(source())).toBeNull();
  });

  it('offers nothing to a cooldown, which heals itself', () => {
    // 额度用完 earns the gold sub-line through `needsAttention`, and that is all it
    // earns. §4.5 makes cooldown the ONLY self-healing blocker; putting a repair
    // button on it would ask the user to fix the one state that fixes itself.
    expect(
      repairAction(
        source({ state: { status: 'cooldown', detail_key: 'models.source.cooldown.quota_exhausted', retry_at: '2030-01-01T00:00:00Z' } }),
      ),
    ).toBeNull();
  });

  it('sends an expired subscription to re-auth on the native channel', () => {
    // AC-2's journey. The state here is exactly what `mark_native_irreversible_
    // start` writes, so this is the row the user comes back to after a login
    // stopped working.
    expect(repairAction(blocked('models.source.needs_action.oauth_expired', { ...subscription, ...native }))).toBe('reauth');
  });

  it('sends an expired subscription to re-auth on the hub channel too', () => {
    // `POST …/reauth` accepts both channels: a hub-held grant is re-obtained by
    // logging in again just as a CLI login is, so the remedy does not fork.
    expect(repairAction(blocked('models.source.needs_action.oauth_expired', subscription))).toBe('reauth');
  });

  it('sends a revoked api_key to key replacement', () => {
    expect(repairAction(blocked('models.source.needs_action.credential_revoked'))).toBe('replace_key');
  });

  it('offers nothing when the credential failed and there is no route to replace it', () => {
    // A native api_key: the credential is the CLI's, not Avibe's. The point of the
    // assertion is that this does NOT fall through to 「retry」 — the cause is
    // named, it is the credential, and a re-test would just re-report it.
    expect(repairAction(blocked('models.source.needs_action.credential_revoked', native))).toBeNull();
  });

  it('offers a re-check when the cause lives upstream', () => {
    // Balance run out, account restricted: Avibe cannot fix either, and no contract
    // field carries a vendor billing URL. What it can do is stop guessing once the
    // user says they handled it.
    expect(repairAction(blocked('models.source.needs_action.balance_exhausted'))).toBe('retest');
    expect(repairAction(blocked('models.source.needs_action.account_banned'))).toBe('retest');
  });

  it('treats an unclassified error as an upstream cause, not a credential one', () => {
    // `error` is a blocker with no diagnosis. Guessing 「replace your key」 from it
    // would ask the user to throw away a working credential.
    expect(repairAction(source({ state: { status: 'error', detail_key: 'models.source.error.unclassified' } }))).toBe(
      'retest',
    );
  });

  it('offers nothing to a native source with an upstream cause', () => {
    // Nothing to re-discover (`POST …/test` rejects native), and NOT the re-login
    // that the unclassified case below gets: a native re-login invalidates the
    // working sign-in up front, so offering it for 账号被封 would charge the user
    // that price for a blocker it cannot clear. This is rule 3's boundary.
    expect(repairAction(blocked('models.source.needs_action.account_banned', { ...subscription, ...native }))).toBeNull();
    expect(repairAction(blocked('models.source.needs_action.balance_exhausted', { ...subscription, ...native }))).toBeNull();
  });

  it('sends a native source back to sign-in when the last one produced nothing usable', () => {
    // The state two backend paths write onto a native source, both from INSIDE a
    // re-login: `completed_source_status` threw, or discovery came back empty. The
    // row must keep a tap — before this it fell through rule 2 to `canRetest`,
    // which native can never satisfy, and the remedy survived only in the overflow
    // menu even though `canReauth` was true the whole time.
    expect(
      repairAction(
        source({
          ...subscription,
          ...native,
          state: { status: 'error', detail_key: 'models.source.error.unclassified' },
        }),
      ),
    ).toBe('reauth');
  });

  it('does not invent a remedy for a native api_key with no sign-in to redo', () => {
    // Same cause, no reauth route (`canReauth` is subscription-only), so rule 3
    // must not hand it the subscription's button.
    expect(
      repairAction(
        source({ ...native, state: { status: 'error', detail_key: 'models.source.error.unclassified' } }),
      ),
    ).toBeNull();
  });
});

const gap = (backend: SupplyGap['backend'], model_id: string): SupplyGap => ({ backend, model_id });

describe('repairOutcome — 「did that fix it?」 (OAuthConnectDialog, ReplaceKeyDialog)', () => {
  it('reports a repair of a source that had been blocked', () => {
    expect(repairOutcome({ source: source(), recovered: true, interrupted_pairs: [] })).toEqual({ kind: 'repaired' });
  });

  it('reports an elective replacement as a refresh, not a repair', () => {
    // `recovered` is the server's own judgement (prior status in {needs_action,
    // error}). A user who rotated a working key is not owed 「已恢复」.
    expect(repairOutcome({ source: source(), recovered: false, interrupted_pairs: [] })).toEqual({ kind: 'refreshed' });
  });

  it('reports the stranded pairs even when the source itself recovered', () => {
    // The ranking that matters: a forced write that fixed this source while
    // stranding another Agent's model is not a success story, and the report is
    // the whole reason the user was asked to confirm.
    expect(
      repairOutcome({ source: source(), recovered: true, interrupted_pairs: [gap('codex', 'gpt-5.6')] }),
    ).toEqual({ kind: 'gaps', gaps: [gap('codex', 'gpt-5.6')] });
  });

  it('refuses to call a still-blocked source repaired', () => {
    // The case that makes this more than a passthrough: a native reauth commits
    // `needs_action`/`oauth_expired` when the CLI still reports itself signed out
    // and answers 200 with `recovered: true` beside it, because `recovered` is a
    // statement about the state BEFORE the attempt. Reading it alone would put
    // 「已恢复可用」 on a row that is still stopped — and dismiss the dialog over it.
    expect(
      repairOutcome({
        source: blocked('models.source.needs_action.oauth_expired'),
        recovered: true,
        interrupted_pairs: [],
      }),
    ).toEqual({ kind: 'unresolved' });
  });

  it('reports an unclassified error state as unresolved too', () => {
    // The other half of `wasBlocked`: a discovery that came back empty leaves the
    // source on `error`, which is no more 「已更新」 than `needs_action` is.
    expect(
      repairOutcome({
        source: source({ state: { status: 'error', detail_key: 'models.source.error.unclassified' } }),
        recovered: false,
        interrupted_pairs: [],
      }),
    ).toEqual({ kind: 'unresolved' });
  });

  it('still ranks the gap report above a source that came back blocked', () => {
    expect(
      repairOutcome({
        source: blocked('models.source.needs_action.oauth_expired'),
        recovered: true,
        interrupted_pairs: [gap('claude', 'claude-opus-4-6')],
      }),
    ).toEqual({ kind: 'gaps', gaps: [gap('claude', 'claude-opus-4-6')] });
  });

  it('lets a clean repair close itself and holds every other verdict open', () => {
    expect(repairSettles({ kind: 'repaired' })).toBe(true);
    expect(repairSettles({ kind: 'refreshed' })).toBe(true);
    expect(repairSettles({ kind: 'unresolved' })).toBe(false);
    expect(repairSettles({ kind: 'gaps', gaps: [gap('codex', 'gpt-5.6')] })).toBe(false);
  });

  /**
   * The verdict a dialog renders has no compile-time link to the bundles, so a
   * fourth kind could ship with no string. `REPAIR_LINE_KEY` is exhaustive over
   * the union by type — a new kind fails the typecheck at its definition — and
   * this proves each key it names is actually translated, in both locales.
   *
   * `gaps` is absent from the map by design (a heading over a list, not a line),
   * which the `Exclude` in its type states and the union check below re-proves.
   */
  it.each(Object.entries(REPAIR_LINE_KEY))('has copy in both locales for the %s verdict', (_kind, key) => {
    for (const bundle of [en, zh]) expect(typeof translated(bundle, key)).toBe('string');
  });

  it('covers every verdict that renders as one line', () => {
    const kinds: RepairOutcome['kind'][] = ['repaired', 'refreshed', 'unresolved', 'gaps'];
    expect(kinds.filter((k) => k !== 'gaps').sort()).toEqual(Object.keys(REPAIR_LINE_KEY).sort());
    // The toast has no list to fall back on, so it answers for the full union —
    // including the one kind the line map leaves to the panel.
    expect(kinds.sort()).toEqual(Object.keys(REPAIR_TOAST).sort());
  });

  it.each(Object.entries(REPAIR_TOAST))('has toast copy in both locales for the %s verdict', (_kind, toast) => {
    for (const bundle of [en, zh]) expect(typeof translated(bundle, toast.key)).toBe('string');
  });

  /**
   * The finding this map exists for: written at the call site as 「warning if
   * unresolved, success otherwise」, a gap report got the green tone — a toast
   * saying 连接成功 over a dialog reporting that Agents now have no source at all.
   *
   * Asserted as pairs rather than as `!== 'success'`, because the mistake to catch
   * is a tone that contradicts its own sentence, and only naming both halves
   * catches it.
   */
  it('never celebrates a verdict whose own line is bad news', () => {
    expect(REPAIR_TOAST.gaps.tone).toBe('warning');
    expect(REPAIR_TOAST.unresolved.tone).toBe('warning');
    expect(REPAIR_TOAST.repaired.tone).toBe('success');
    expect(REPAIR_TOAST.refreshed.tone).toBe('success');
    // And the tone tracks the verdict, not the branch: exactly the verdicts that
    // may auto-dismiss are the ones allowed to be green.
    for (const [kind, toast] of Object.entries(REPAIR_TOAST))
      expect(toast.tone === 'success', kind).toBe(
        repairSettles(kind === 'gaps' ? { kind: 'gaps', gaps: [] } : ({ kind } as RepairOutcome)),
      );
  });

  it('reuses the line copy where a line exists', () => {
    // One sentence per verdict on this page: the toast and the panel must not
    // drift into two wordings of the same finding.
    for (const kind of Object.keys(REPAIR_LINE_KEY) as (keyof typeof REPAIR_LINE_KEY)[])
      expect(REPAIR_TOAST[kind].key).toBe(REPAIR_LINE_KEY[kind]);
    // `gaps` is the exception, and deliberately not `gapsDone`: that string ends
    // in a colon because a list follows it, and a toast has no list.
    expect(REPAIR_TOAST.gaps.key).not.toBe('settings.models.repair.gapsDone');
    for (const bundle of [en, zh]) expect(translated(bundle, REPAIR_TOAST.gaps.key)).not.toMatch(/[:：]$/);
  });
});

describe('the copy each remedy names', () => {
  // The bug this replaces: `t(`settings.models.repair.${kind}`)` compiled for
  // every RepairKind and rendered the literal key path for `replace_key`, whose
  // bundle spelling is `replaceKey` — on the inline button AND its aria-label, so
  // a screen reader read out the key path too.
  it.each(Object.entries(REPAIR_LABEL_KEY))('has a %s label in both locales', (_kind, key) => {
    for (const bundle of [en, zh]) expect(typeof translated(bundle, key)).toBe('string');
  });

  it('warns up front on a native re-login and only about failure on a hub one', () => {
    // `mark_native_irreversible_start` rewrites every native source of that
    // vendor's backend to 需要处理 as the login spawns, so 「开始后旧的登录立即失效」
    // is true there. The hub route writes NOTHING at start — the held credential
    // keeps working — and only `_fail_closed_hub_reauth` marks the source, on a
    // flow that came back `failed`. One sentence over both is false on one.
    expect(reauthCost(source({ ...subscription, ...native }))).toBe('immediate');
    expect(reauthCost(source(subscription))).toBe('on_failure');
  });

  it('has both confirm bodies in both locales', () => {
    for (const s of [source({ ...subscription, ...native }), source(subscription)])
      for (const bundle of [en, zh]) expect(typeof translated(bundle, reauthBodyKey(s))).toBe('string');
  });
});

const agent = (over: Partial<AgentSupply> = {}): AgentSupply => ({
  backend: 'claude',
  mode: 'hub',
  menu_kind: 'fixed',
  sources: { policy: 'follow', order: ['src_a'] },
  current: { model_id: 'claude-opus-4-6', source_id: 'src_a', channel: 'hub' },
  ...over,
});

const probe = (over: Partial<ProbeResult> = {}): ProbeResult => ({
  contract_version: PROBE_RESULT_CONTRACT_VERSION,
  backend: 'claude',
  // v4's conditionals are channel-scoped, so the default has to name a channel:
  // a reachable `hub` result owes an integer latency, and only `native_cli` may
  // answer with none. The cases below that vary the latency vary this with it.
  channel: 'hub',
  reachable: true,
  source_id: 'src_a',
  model_id: 'claude-opus-4-6',
  latency_ms: 412,
  via_mapping: false,
  error: null,
  ...over,
});

describe('dryRunPlan — whether 试跑 has anything to run (SourceOrderDrawer)', () => {
  it('probes an Agent with a resolved chain head', () => {
    expect(dryRunPlan(agent())).toEqual({ kind: 'probe', backend: 'claude' });
    expect(dryRunPlan(agent({ backend: 'codex' }))).toEqual({ kind: 'probe', backend: 'codex' });
  });

  it('runs nothing in direct mode', () => {
    // AC-7: the route refuses with `direct_mode`, because there is no `src_*`
    // identity to report a turn against.
    expect(dryRunPlan(agent({ mode: 'direct', sources: null, current: null }))).toEqual({ kind: 'none' });
  });

  it('runs nothing when there is no head', () => {
    // A null `current` IS waiting/interrupted. The page already says so with the
    // remedy attached; a probe would only re-report it as a failure the user has
    // to re-read.
    expect(dryRunPlan(agent({ current: null, supply_status: 'interrupted' }))).toEqual({ kind: 'none' });
  });
});

describe('dryRunChainKey — which edits make a 试跑 report stop being about anything', () => {
  const base = agent({
    selected_model_id: 'claude-opus-4-6',
    mappings: [{ builtin_id: 'claude-opus-4-6', target_model_id: 'glm-5.2', enabled: true }],
    menu: { view: 'featured', checked: ['zhipuai/glm-5.2'] },
  });
  const key = (over: Partial<AgentSupply> = {}, policy: SourcePolicy = 'follow', order = ['src_a']) =>
    dryRunChainKey({ ...base, ...over }, policy, order);

  it('moves for every surface the user selects the probed turn with', () => {
    // The chain is not only its order: the probe takes no model, so the server
    // resolves one from the same config the mapping drawer next door edits. Edit
    // that and the sentence on screen is about a turn the button would no longer
    // make.
    const same = key();
    expect(key()).toBe(same);
    expect(key({}, 'custom')).not.toBe(same);
    expect(key({}, 'follow', ['src_b', 'src_a'])).not.toBe(same);
    expect(key({ selected_model_id: 'claude-sonnet-5' })).not.toBe(same);
    expect(key({ mappings: [] })).not.toBe(same);
    expect(key({ mappings: [{ builtin_id: 'claude-opus-4-6', target_model_id: 'glm-5.2', enabled: false }] })).not.toBe(
      same,
    );
    expect(key({ menu: { view: 'featured', checked: [] } })).not.toBe(same);
  });

  it('holds still for everything the server derives from those', () => {
    // The trap this key exists inside: a failing 试跑 cools its own head down and
    // the re-read it owes then moves `agent.current`, the rollup, and the source
    // health behind it. Key on any of those and the failing run is the one whose
    // answer erases itself. The head moving is what a failing report is FOR.
    const same = key();
    expect(key({ current: { model_id: 'glm-5.2', source_id: 'src_b', channel: 'hub' } })).toBe(same);
    expect(key({ current: null, supply_status: 'interrupted' })).toBe(same);
    expect(key({ supply_status: 'degraded' })).toBe(same);
  });

  it('reads opencode by its own selection surface, not by the pick', () => {
    // `selected_model_id` is config for a fixed menu, but for opencode with an
    // empty request `resolve_model_hub_turn` walks `menu.checked` and returns the
    // first identifier whose source is runnable — so a cooldown MOVES it. Keying
    // on it there would smuggle head health back in through the model field and
    // re-create the self-erasing report above.
    const oc = { backend: 'opencode' as const, menu_kind: 'open' as const };
    expect(key({ ...oc, selected_model_id: 'zhipuai/glm-5.2' })).toBe(key({ ...oc, selected_model_id: 'openai/gpt-6' }));
    // What the user actually selects with there still counts.
    expect(key({ ...oc, menu: { view: 'full', checked: ['openai/gpt-6'] } })).not.toBe(key(oc));
  });

  it('does not move for the order the two lists were written in', () => {
    // `mappings` and `menu.checked` are sets the user edits by toggling; the API
    // makes no ordering promise about either, and a re-read that returns the same
    // configuration in another order would otherwise clear the report.
    expect(
      key({
        mappings: [
          { builtin_id: 'b', target_model_id: 'y', enabled: true },
          { builtin_id: 'a', target_model_id: 'x', enabled: true },
        ],
        menu: { view: 'featured', checked: ['b', 'a'] },
      }),
    ).toBe(
      key({
        mappings: [
          { builtin_id: 'a', target_model_id: 'x', enabled: true },
          { builtin_id: 'b', target_model_id: 'y', enabled: true },
        ],
        menu: { view: 'featured', checked: ['a', 'b'] },
      }),
    );
  });

  it('keeps the parts apart', () => {
    // A key is only as good as its separators: 「order a, model b」 and 「order a>b,
    // no model」 are different chains and must not collide.
    expect(key({ selected_model_id: 'src_b' }, 'follow', ['src_a'])).not.toBe(
      key({ selected_model_id: '' }, 'follow', ['src_a', 'src_b']),
    );
  });
});

describe('dryRunOutcome — what the probe came back with (SourceOrderDrawer)', () => {
  it('names the source the server actually started from', () => {
    // Not the first row in the order: the point of a dry run is which source the
    // chain resolved to, which can differ from what the list suggests.
    expect(dryRunOutcome(probe({ source_id: 'src_b' }), [source(), source({ id: 'src_b', display_name: 'Key B' })])).toEqual({
      kind: 'ok',
      sourceName: 'Key B',
      latencyMs: 412,
    });
  });

  it('falls back to the id when the source is gone', () => {
    // Same tolerance `chainChips` keeps: an id that no longer resolves is still
    // more informative than an empty name.
    expect(dryRunOutcome(probe({ source_id: 'src_gone' }), [])).toEqual({
      kind: 'ok',
      sourceName: 'src_gone',
      latencyMs: 412,
    });
  });

  it('keeps a null latency null instead of inventing a zero', () => {
    // This is the native_cli answer: the server reports the CLI's readiness and
    // measures nothing, because it never sent a request. 「可用」 without a number
    // is the honest line; a 0 ms would be a measurement nobody took. The channel
    // travels with it — v4 makes null latency the native branch's ALWAYS and the
    // reachable-hub branch's never, so a hub fixture here would assert the copy
    // over a result the contract cannot produce.
    expect(dryRunOutcome(probe({ channel: 'native_cli', latency_ms: null }), [source()])).toEqual({
      kind: 'ok',
      sourceName: 'Key A',
      latencyMs: null,
    });
  });

  it('carries the native unavailability key, which no source-state key spells', () => {
    // v4's other native outcome, and the reason `detailKey` is widened past
    // `state.detail_key`: process unavailability is a fact about this serving
    // process, not about the account, so the ten source-state keys cannot say it.
    // Typing it is not enough — a mirror that is only `cast` is exactly how the
    // last drift got through, so the value is asserted end to end here.
    expect(
      dryRunOutcome(
        probe({ channel: 'native_cli', reachable: false, latency_ms: null, error: 'models.probe.native_cli_unavailable' }),
        [source()],
      ),
    ).toEqual({ kind: 'failed', sourceName: 'Key A', detailKey: 'models.probe.native_cli_unavailable' });
  });

  it('carries the server’s reason through on a failure', () => {
    expect(
      dryRunOutcome(probe({ reachable: false, latency_ms: null, error: 'models.source.needs_action.credential_revoked' }), [
        source(),
      ]),
    ).toEqual({ kind: 'failed', sourceName: 'Key A', detailKey: 'models.source.needs_action.credential_revoked' });
  });

  it('still reports a failure when the server named no reason', () => {
    // `error` is null iff reachable, so this pair should not occur — but the copy
    // must not depend on that, and a null key degrades to the generic line rather
    // than rendering 「undefined」.
    expect(dryRunOutcome(probe({ reachable: false, error: null }), [source()])).toEqual({
      kind: 'failed',
      sourceName: 'Key A',
      detailKey: null,
    });
  });
});

describe('probeWroteState — whether 试跑 left a mark the page has to re-read', () => {
  it('treats a failing probe as a write', () => {
    // `probe_agent` cools the head down or blocks it on the way out, so the row
    // states and ● 当前 behind the sheet are stale the moment this returns. The
    // key here is one of the five `_cooldown` reasons, i.e. the branch that
    // writes `retry_at` onto the source the chain resolved to.
    expect(
      probeWroteState(probe({ reachable: false, latency_ms: null, error: 'models.source.cooldown.server_error' })),
    ).toBe(true);
  });

  it('treats a failing NATIVE probe as a write too, deliberately', () => {
    // The native branch returns before the write block, so today this is a read
    // the rule over-counts. Pinned as a decision rather than left implicit:
    // keying on the channel would oblige every caller to track which branch of
    // `probe_agent` writes, and the failure modes are not symmetric — being
    // wrong this way costs one request on a button the user pressed, the other
    // way it costs a page that silently disagrees with the server.
    expect(
      probeWroteState(
        probe({ channel: 'native_cli', reachable: false, latency_ms: null, error: 'models.probe.native_cli_unavailable' }),
      ),
    ).toBe(true);
  });

  it.each(['hub', 'native_cli'] as const)('reads a reachable %s probe as a pure read', (channel) => {
    // The write block sits entirely under `if not reachable`, and no path clears
    // a cooldown on success — so a green 试跑 must NOT refetch, or every check
    // costs the page two extra requests for nothing.
    expect(probeWroteState(probe({ channel, latency_ms: channel === 'hub' ? 412 : null }))).toBe(false);
  });
});

describe('probeArrival — an answer nobody wants can still be one the page owes', () => {
  const failing = probe({ reachable: false, latency_ms: null, error: 'models.source.cooldown.server_error' });

  it('shows and re-reads when the failure is still the answer to the question asked', () => {
    expect(probeArrival({ kind: 'result', probe: failing }, true)).toEqual({ report: true, reread: true });
  });

  it('re-reads a failure the row will NOT draw', () => {
    // The case: the user reorders while the probe is out. That edit bumped `seq`,
    // so this response is no longer about the chain on screen — but it cooled a
    // source down, and the edit's own PUT refetch went out BEFORE this returned, so
    // it read the source back healthy. Dropping the arrival wholesale is what left
    // the row green under a head the server had already moved.
    expect(probeArrival({ kind: 'result', probe: failing }, false)).toEqual({ report: false, reread: true });
  });

  it('re-reads nothing for a reachable probe, current or stale', () => {
    // Provably no write: the block sits entirely under `if not reachable`, and no
    // path clears a cooldown on success.
    expect(probeArrival({ kind: 'result', probe: probe() }, true)).toEqual({ report: true, reread: false });
    expect(probeArrival({ kind: 'result', probe: probe() }, false)).toEqual({ report: false, reread: false });
  });

  it('trusts a throw the server NAMED to have written nothing', () => {
    // An engine failure propagates out above the write block — one of the outcomes
    // `probeWroteState` checked one by one, and every one of them arrives as a
    // structured failure.
    expect(probeArrival({ kind: 'thrown', serverNamed: true, code: 'engine_down' }, true)).toEqual({
      report: true,
      reread: false,
    });
  });

  it('re-reads a throw the server did not name', () => {
    // Not one of the route's outcomes at all — a lost connection, an unparseable
    // body. The probe may have run and written with the answer lost on the way
    // back, and unknown is not the same as no. Owed even when the row is stale and
    // will draw no error line.
    const unnamed = { kind: 'thrown', serverNamed: false, code: 'bad_response' } as const;
    expect(probeArrival(unnamed, true)).toEqual({ report: true, reread: true });
    expect(probeArrival(unnamed, false)).toEqual({ report: false, reread: true });
  });

  it('re-reads a refusal that wrote nothing and disproved the head anyway', () => {
    // The second, independent reason. `probe_no_candidate` is raised before any
    // write — so 「did it write?」 says no, correctly — and yet the 试跑 button the
    // user pressed was drawn from a chain WITH a runnable head, so the refusal is
    // the server reporting that head gone. Left un-reread, the chip keeps naming a
    // source and the button keeps inviting a run that cannot happen.
    expect(probeArrival({ kind: 'thrown', serverNamed: true, code: 'probe_no_candidate' }, true)).toEqual({
      report: true,
      reread: true,
    });
    // `direct_mode` is the same sentence about the mode the drawer believes it is in.
    expect(probeArrival({ kind: 'thrown', serverNamed: true, code: 'direct_mode' }, false)).toEqual({
      report: false,
      reread: true,
    });
  });

  it('keeps the two reasons independent', () => {
    // Neither subsumes the other: an unnamed failure with no code still re-reads,
    // and a named refusal that is not state-precondition still does not.
    expect(probeArrival({ kind: 'thrown', serverNamed: false, code: null }, true).reread).toBe(true);
    expect(probeArrival({ kind: 'thrown', serverNamed: true, code: 'source_last_supplier' }, true).reread).toBe(
      false,
    );
  });

  it('is how the drawer reads its own probe, at both arrivals', () => {
    // The regression this replaces was a `return` on the staleness guard ABOVE the
    // refetch, so the shape matters as much as the rule: no early exit may sit
    // between a probe arriving and the page being corrected.
    const drawer = readFileSync(join(dirname(fileURLToPath(import.meta.url)), 'SourceOrderDrawer.tsx'), 'utf8');

    expect(drawer).not.toMatch(/if \(seq\.current !== mine\) return;/);
    expect((drawer.match(/probeArrival\(/g) ?? []).length).toBe(2);
    expect(drawer).toMatch(/if \(arrival\.reread\) reread\(\);/);
    // And takes `serverNamed` from the error rather than inferring it from 「is it
    // one of ours?」 — `bad_response` is one of ours and names nothing, so that
    // inference skipped the reread in the one case it was added for.
    expect(drawer).toMatch(/serverNamed: failure\?\.serverNamed \?\? false/);
    expect(drawer).not.toMatch(/serverNamed: failure !== null/);
    // The code rides along so the second reason can be asked at all.
    expect(drawer).toMatch(/code: failure\?\.code \?\? null/);
  });
});

describe('disprovedDrawnHead — a refusal that is news about the chain', () => {
  it('names the two state-precondition refusals', () => {
    expect(disprovedDrawnHead('probe_no_candidate')).toBe(true);
    expect(disprovedDrawnHead('direct_mode')).toBe(true);
  });

  it('leaves every other refusal alone', () => {
    // A supply refusal or an engine outage says nothing about whether the head this
    // page drew is still there.
    expect(disprovedDrawnHead('source_last_supplier')).toBe(false);
    expect(disprovedDrawnHead('engine_down')).toBe(false);
    expect(disprovedDrawnHead(null)).toBe(false);
  });

  it('matches the codes the server actually raises', () => {
    // Both are `ModelHubError` codes on the probe route (`probe_no_candidate` at the
    // no-runnable-candidate branch, `direct_mode` from `_direct_mode_error`), so a
    // rename server-side has a test here to fail against.
    expect(['probe_no_candidate', 'direct_mode'].every((code) => disprovedDrawnHead(code))).toBe(true);
  });
});

describe('mayHaveWritten — the same unknown, wherever it is caught', () => {
  it('calls a named refusal a decided outcome', () => {
    expect(mayHaveWritten({ serverNamed: true })).toBe(false);
  });

  it('calls an unnamed failure unknown', () => {
    // `bad_response` / `http_<n>`: minted by the transport for an answer that never
    // said what happened, which is as consistent with 「committed」 as with 「never ran」.
    expect(mayHaveWritten({ serverNamed: false })).toBe(true);
  });

  it('calls a throw that is not ours unknown too', () => {
    // Reaching it means our own code threw AFTER the await, which puts the write
    // strictly in the past.
    expect(mayHaveWritten(null)).toBe(true);
  });

  it('is what 更换 API Key re-reads on', () => {
    // The write is atomic and re-discovers on commit, so a lost answer leaves the
    // row showing the old key's state while 更换 would provision a second
    // replacement. Reporting the error and re-reading the rows are not alternatives.
    const dialog = readFileSync(join(dirname(fileURLToPath(import.meta.url)), 'ReplaceKeyDialog.tsx'), 'utf8');

    expect(dialog).toMatch(/if \(mayHaveWritten\(failure\)\) onReplaced\(\);\n\s*setPhase\('error'\);/);
    // The named refusal above it still returns before this, and still writes nothing.
    expect(dialog).toMatch(/failure\?\.code === 'source_last_supplier' && !force/);
  });

  it('is one owner, not a predicate re-derived per caller', () => {
    const here = dirname(fileURLToPath(import.meta.url));
    for (const file of ['ReplaceKeyDialog.tsx', 'SourceOrderDrawer.tsx', 'SourceRowMenu.tsx']) {
      expect(readFileSync(join(here, file), 'utf8')).not.toMatch(/!\(?failure\?\.serverNamed/);
    }
  });

  it('is not the question 删除 asks, so 删除 does not ask it', () => {
    // Round 17's finding, and why it is answered without a gate rather than with
    // this one. 更换 API Key asks 「did MY write land?」, because the cost of getting
    // it wrong is provisioning a second replacement — `mayHaveWritten` is that
    // question. A failed DELETE asks 「are the rows I am drawing still right?」, and
    // the two come apart on both sides: a commit whose answer was lost is unknown
    // AND stale, while `source_not_found` is server-NAMED, wrote nothing, and is
    // news precisely because the row is already gone — the same shape
    // `disprovedDrawnHead` above exists for. Gating on 「may it have written」 would
    // leave that phantom row on screen until something else happened to refetch.
    //
    // So it re-reads either way, for the reason `releaseFlow` gives: a redundant
    // re-read is inert, a missing one is the bug. The guarded refusal still leaves
    // through the branch above this one, having provably written nothing.
    const menu = readFileSync(join(dirname(fileURLToPath(import.meta.url)), 'SourceRowMenu.tsx'), 'utf8');

    expect(menu).not.toMatch(/mayHaveWritten/);
    expect(menu).toMatch(/onChanged\(\);\n\s*showToast\(t\('settings\.models\.sourceActions\.deleteFailed'/);
    // Both delete outcomes now reach the page: the refusal escalates to a second
    // confirm, everything else closes and re-reads.
    expect(menu).toMatch(/failure\?\.code === 'source_last_supplier' && !forceMode/);
  });
});

describe('dryRunRowView — a report that outlives the chain it was about', () => {
  const idle = { line: null, saving: false, running: false };

  it('offers a live control while the chain has a head', () => {
    expect(dryRunRowView(dryRunPlan(agent()), idle)).toEqual({ backend: 'claude', enabled: true, report: false });
  });

  it('keeps the answer on screen after 试跑 itself took the head away', () => {
    // The case the row creates: probing the chain's last runnable source and
    // failing cools that source down, so the re-read `probeWroteState` demands
    // comes back with `current: null` — and the plan the row is drawn from turns
    // `none` under the sentence the click just produced. Dropping it there would
    // make the failing run the only one whose answer flashes past.
    const headless = dryRunPlan(agent({ current: null, supply_status: 'interrupted' }));

    expect(dryRunRowView(headless, { ...idle, line: 'Key A 没跑通' })).toEqual({
      backend: null,
      enabled: false,
      report: true,
    });
  });

  it('draws nothing when there is neither a head nor an answer', () => {
    // Direct mode, or a chain that was already stopped when the drawer opened. The
    // page states that one level up with the remedy attached; an empty row here, or
    // a disabled button, would only repeat it.
    expect(dryRunRowView(dryRunPlan(agent({ mode: 'direct', sources: null, current: null })), idle)).toEqual({
      backend: null,
      enabled: false,
      report: false,
    });
  });

  it('holds the probe while the order PUT is still in flight', () => {
    // The save is optimistic: the list and the chain key the report is filed under
    // have already moved to the order the user dropped, while the server still
    // answers for the old one. A probe in that window reports on the superseded
    // chain under the new one's key — and the key cannot clear it, because it IS
    // the new key. The control stays drawn and goes disabled, like every other
    // control in this drawer.
    expect(dryRunRowView(dryRunPlan(agent()), { ...idle, saving: true })).toEqual({
      backend: 'claude',
      enabled: false,
      report: false,
    });
  });

  it('holds it while a probe is already running', () => {
    expect(dryRunRowView(dryRunPlan(agent()), { ...idle, running: true })).toEqual({
      backend: 'claude',
      enabled: false,
      report: false,
    });
  });
});

describe('the class: no component decides a repair for itself', () => {
  const here = dirname(fileURLToPath(import.meta.url));
  const files = readdirSync(here, { recursive: true, encoding: 'utf8' }).filter(
    (f) => /\.tsx?$/.test(f) && !/\.test\.tsx?$/.test(f),
  );
  const read = (f: string) => readFileSync(join(here, f), 'utf8');

  it('sweeps a non-trivial file set', () => {
    // Without this the two rules below pass over an empty list after any move.
    expect(files.length).toBeGreaterThan(15);
    expect(files).toContain('SourceRowMenu.tsx');
  });

  /**
   * 「is this source stopped?」 as a component's own comparison. `wasBlocked` owns
   * it, and it is deliberately narrower than `needsAttention` next door by exactly
   * one cooldown — a component that re-asks with `===` will get that boundary
   * wrong in one direction or the other. Comparisons against 'cooldown' are a
   * different question (the retry ETA) and stay allowed.
   */
  const BLOCKED_PROXY = /\.state\.status\s*[=!]==?\s*'(?:needs_action|error)'/;

  it.each(files.filter((f) => f.endsWith('.tsx')))('%s asks wasBlocked instead of the status', (name) => {
    expect(read(name)).not.toMatch(BLOCKED_PROXY);
  });

  /**
   * 「did anything get stranded?」 as an emptiness test. `repairOutcome` owns it,
   * and it ranks the gap report ABOVE `recovered` — a site that reads the array
   * itself is one `if` away from congratulating the user on a repair that broke
   * another Agent.
   */
  const GAP_PROXY = /interrupted_pairs\)?(?:\s*\?)?\.length/;

  it('leaves the gap verdict to its owner', () => {
    const offenders = files.filter((f) => f !== 'repair.ts' && GAP_PROXY.test(read(f)));
    expect(offenders).toEqual([]);
  });

  /**
   * An ASSEMBLED repair locale key. This is the class round 2 caught: a template
   * literal over a union typechecks for every member, so the one member the bundle
   * spells differently (`replace_key` → `replaceKey`) shipped as a raw key path on
   * screen. `REPAIR_LABEL_KEY` / `REPAIR_LINE_KEY` are the versions the compiler
   * checks and the tests above prove translated; the maps are where a repair key
   * gets composed, and nowhere else.
   *
   * A LITERAL `t('settings.models.repair.gapsDone')` is not this bug and stays
   * allowed: the failure mode is the substitution, not the namespace.
   *
   * `repair.ts` is exempt because its own docs QUOTE the bad form to explain why
   * the maps exist, and it holds no `t` to render one with.
   */
  const ASSEMBLED_KEY = /settings\.models\.repair\.\$\{/;

  it.each(files.filter((f) => f !== 'repair.ts'))('%s looks a repair key up instead of building it', (name) => {
    expect(read(name)).not.toMatch(ASSEMBLED_KEY);
  });

  /**
   * A hand-assembled 试跑 chain key. This is the class round 14 caught: the drawer
   * built `${policy}|${order.join('>')}` inline, which is a complete statement of
   * the chain only while nothing else selects the probed turn — and the model menu
   * next door always did. Inline, the omission is invisible; behind
   * `dryRunChainKey` it is one function's business, and the test above says which
   * surfaces belong to it.
   */
  const INLINE_CHAIN_KEY = /chainKey=\{`/;

  it('leaves the chain key to its owner', () => {
    const offenders = files.filter((f) => INLINE_CHAIN_KEY.test(read(f)));
    expect(offenders).toEqual([]);
    expect(read('SourceOrderDrawer.tsx')).toMatch(/chainKey=\{dryRunChainKey\(agent, policy, order\)\}/);
  });

  /**
   * A translated sentence put into state. `serverText` needs `t`, so whatever it
   * returns is copy in ONE language — stored, it survives the language switch that
   * re-renders everything around it, and the row ends up bilingual with itself. The
   * reason keeps; the sentence is built where it is read.
   */
  const STORED_SENTENCE = /set[A-Z][A-Za-z]*\(\s*(?:serverText|failedLine|t)\(/;

  it.each(files)('%s stores the reason and renders the sentence', (name) => {
    expect(read(name)).not.toMatch(STORED_SENTENCE);
  });

  it('keeps the dry-run failure as a reason, three states and all', () => {
    const src = read('SourceOrderDrawer.tsx');
    // `{ key: null }` is not the same as `null`: 「it failed and nobody named why」
    // has a sentence (the generic line), 「nothing ran yet」 has none.
    expect(src).toMatch(/useState<\{ key: string \| null \} \| null>\(null\)/);
    expect(src).toMatch(/setErrorReason\(\{ key: failure\?\.detail \?\? null \}\)/);
    expect(src).toMatch(/errorReason && serverText\(t, errorReason\.key, 'settings\.models\.dryRun\.error'\)/);
  });
});
