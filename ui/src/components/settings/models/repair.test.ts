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

import { dryRunOutcome, dryRunPlan, repairAction, repairOutcome, repairSettles } from './repair';
import { CONTRACT_VERSION } from './types';
import type { AgentSupply, ProbeResult, Source, SourceDetailKey, SupplyGap } from './types';

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
    // Nothing to re-discover: `POST …/test` rejects native sources, whose blockers
    // are cleared by their own CLI.
    expect(repairAction(blocked('models.source.needs_action.account_banned', { ...subscription, ...native }))).toBeNull();
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

  it('lets a clean repair close itself and holds a gap report open', () => {
    expect(repairSettles({ kind: 'repaired' })).toBe(true);
    expect(repairSettles({ kind: 'refreshed' })).toBe(true);
    expect(repairSettles({ kind: 'gaps', gaps: [gap('codex', 'gpt-5.6')] })).toBe(false);
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
  contract_version: CONTRACT_VERSION,
  backend: 'claude',
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
    // is the honest line; a 0 ms would be a measurement nobody took.
    expect(dryRunOutcome(probe({ latency_ms: null }), [source()])).toEqual({
      kind: 'ok',
      sourceName: 'Key A',
      latencyMs: null,
    });
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
});
