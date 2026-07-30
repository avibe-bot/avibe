// Tests for the one owner of 「will the next turn actually run?」.
//
// Written test-first, one block per consuming site, because the class this module
// closes was five sites each answering that question with a `.length` — and the
// review had named only two of them. The last block is the grep guard that makes a
// sixth site impossible to add quietly.
import { readFileSync, readdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

import {
  SUPPLY_WARNINGS,
  adoptionVerdict,
  connectOutcome,
  isSupplyWarning,
  orderSufficiency,
} from './sufficiency';
import type { AdoptedBy, AgentSupply, Source, SourceEligibility, SourceState, SourceStatus } from './types';

const source = (id: string, status: SourceStatus): Source => ({
  id,
  kind: 'api_key',
  vendor: 'anthropic',
  display_name: id,
  protocol: 'anthropic',
  supply_channel: 'hub',
  billing: 'metered',
  state: { status, ...(status === 'cooldown' ? { retry_at: '2030-01-01T00:00:00Z' } : {}) } as SourceState,
  models: [],
});

/** Sources the server says this Agent's machine cannot launch — healthy, eligible,
 *  and still unable to take the turn. The rows carry `eligible: true` on purpose:
 *  the two questions are independent, and this one has to bite on its own. */
const unlaunchable = (ids: readonly string[]): SourceEligibility[] =>
  ids.map((id) => ({ source_id: id, eligible: true, process_availability_reason: 'native_cli_unavailable' }));

/** The Agent-grain facts `orderSufficiency` hangs off. Its `order` is deliberately
 *  the WRONG list — the ids under test come in as the first argument. */
const facts = (...cannotLaunch: string[]): Pick<AgentSupply, 'sources'> => ({
  sources: { policy: 'follow', order: ['not-the-list-under-test'], eligibility: unlaunchable(cannotLaunch) },
});

/** Direct mode, and every payload that omits the optional field: nothing claimed. */
const NO_FACTS: Pick<AgentSupply, 'sources'> = { sources: null };

const hub = (
  order: string[],
  supply_status: AgentSupply['supply_status'] = null,
  cannotLaunch: string[] = [],
): AgentSupply => ({
  backend: 'claude',
  mode: 'hub',
  menu_kind: 'fixed',
  sources: { policy: 'follow', order, eligibility: unlaunchable(cannotLaunch) },
  supply_status,
});

const adopted = (backend: AdoptedBy['backend'], position: number): AdoptedBy => ({
  backend,
  policy: 'follow',
  position,
});

describe('adoptionVerdict — the creation dialogs (AdoptionNote, AddApiKeyDialog, OAuthConnectDialog)', () => {
  it('reports an empty adopter list as adopted_none, which IS provable', () => {
    // The server sent the array and it is empty: nobody took the source. That is a
    // closed fact, not an absence of information.
    expect(adoptionVerdict([])).toEqual({ kind: 'adopted_none' });
  });

  it('refuses to call a non-empty adopter list covered while skipped_by is missing', () => {
    // The honesty rule. `_adopted_by` filters `policy == "follow"`, so a `custom`
    // backend that skipped the source is ABSENT — indistinguishable in this payload
    // from a backend that was never eligible. Non-empty proves someone took it; it
    // proves nothing about who did not.
    expect(adoptionVerdict([adopted('claude', 1)])).toEqual({ kind: 'indeterminate' });
  });

  it('has no verdict at all when the creation result never arrived', () => {
    expect(adoptionVerdict(null)).toEqual({ kind: 'indeterminate' });
  });

  // The two branches the server half unlocks. Reachable today only through the
  // parameter, so the wiring is proved before the field exists rather than after.
  it('is covered once the server states that nothing eligible was skipped', () => {
    expect(adoptionVerdict([adopted('claude', 1)], [])).toEqual({ kind: 'covered' });
  });

  it('names the skipped backends when the server states them', () => {
    const verdict = adoptionVerdict(
      [adopted('claude', 1)],
      [{ backend: 'codex', reason: 'custom_order' }],
    );
    expect(verdict).toEqual({ kind: 'partly_skipped', backends: ['codex'] });
  });

  it('names the orders when nobody adopted it and the server said who left it out', () => {
    // Not a corner case: this is the normal shape of an install where every eligible
    // backend keeps a hand-picked order, and it is the case `skipped_by` was added
    // for. The remedy matches `adopted_none`, and the difference is the whole value —
    // 「go add it somewhere」 vs. 「go add it in these two」.
    expect(adoptionVerdict([], [{ backend: 'codex', reason: 'custom_order' }])).toEqual({
      kind: 'skipped_all',
      backends: ['codex'],
    });
  });

  it('stays at adopted_none when an empty adopter list is all the server sent', () => {
    // Nothing to name, from either direction: no complement at all, and a complement
    // that is itself empty (nothing eligible was left out either).
    expect(adoptionVerdict([], null)).toEqual({ kind: 'adopted_none' });
    expect(adoptionVerdict([], [])).toEqual({ kind: 'adopted_none' });
  });
});

describe('orderSufficiency — the drawer (SourceOrderDrawer) and the connect toasts', () => {
  it('separates 「nothing enabled」 from 「nothing works」, because the remedies differ', () => {
    expect(orderSufficiency([], [source('a', 'active')], NO_FACTS)).toEqual({ kind: 'adopted_none' });
    expect(orderSufficiency(['a'], [source('a', 'needs_action')], NO_FACTS)).toEqual({ kind: 'nothing_runnable' });
  });

  it('is covered when any enabled source can serve, not when the list is non-empty', () => {
    expect(orderSufficiency(['a', 'b'], [source('a', 'error'), source('b', 'standby')], NO_FACTS)).toEqual({
      kind: 'covered',
    });
  });

  it('counts a cooling source as unable to serve right now', () => {
    // `cooldown` heals itself, but the turn taken during it still fails, and this
    // verdict answers 「the NEXT turn」.
    expect(orderSufficiency(['a'], [source('a', 'cooldown')], NO_FACTS)).toEqual({ kind: 'nothing_runnable' });
  });

  it('will not claim nothing runs when an enabled id is missing from the inventory', () => {
    // The two reads can disagree; an id we cannot resolve is unknown, not broken.
    expect(orderSufficiency(['a', 'ghost'], [source('a', 'error')], NO_FACTS)).toEqual({ kind: 'indeterminate' });
  });

  it('is indeterminate where the source inventory is not loaded', () => {
    expect(orderSufficiency(['a'], null, NO_FACTS)).toEqual({ kind: 'indeterminate' });
    expect(orderSufficiency(null, [source('a', 'active')], NO_FACTS)).toEqual({ kind: 'indeterminate' });
  });

  // The v4 caveat this closes: a `native_cli` source whose CLI is not usable on this
  // machine reports itself perfectly healthy, because the credential IS fine.
  it('counts a healthy source this machine cannot launch as unable to serve', () => {
    expect(orderSufficiency(['a'], [source('a', 'active')], facts('a'))).toEqual({ kind: 'nothing_runnable' });
  });

  it('needs BOTH halves before it says covered', () => {
    // One reachable-and-launchable source is enough; neither half alone is.
    expect(orderSufficiency(['a', 'b'], [source('a', 'active'), source('b', 'active')], facts('a'))).toEqual({
      kind: 'covered',
    });
    expect(orderSufficiency(['a', 'b'], [source('a', 'active'), source('b', 'cooldown')], facts('a'))).toEqual({
      kind: 'nothing_runnable',
    });
  });

  it('reads silence as runnable rather than inventing an outage', () => {
    // Direct mode, a server that omits the optional field, and a source with no row
    // all mean 「nothing claimed」 — the weaker claim, never the false alarm.
    expect(orderSufficiency(['a'], [source('a', 'active')], NO_FACTS)).toEqual({ kind: 'covered' });
    expect(orderSufficiency(['a'], [source('a', 'active')], facts())).toEqual({ kind: 'covered' });
    expect(orderSufficiency(['a'], [source('a', 'active')], facts('b'))).toEqual({ kind: 'covered' });
  });

  it('grades the ids it was handed, never the order saved on the Agent', () => {
    // The drawer asks about the list the user is CURRENTLY editing. `facts()` carries
    // a decoy order for exactly this: the Agent comes along for the server facts that
    // hang off it, and for nothing else.
    expect(orderSufficiency(['a'], [source('a', 'active')], facts())).toEqual({ kind: 'covered' });
  });
});

/** The same Agent-grain facts with the route gate's two inputs made explicit: the
 *  order the server's answer was ABOUT, and per-source membership in the selected
 *  model's chain. An id left out of `membership` has no eligibility row at all —
 *  the payload that never mentions it. */
const routed = (
  savedOrder: readonly string[],
  membership: Record<string, boolean | null>,
  cannotLaunch: readonly string[] = [],
): Pick<AgentSupply, 'sources'> => ({
  sources: {
    policy: 'custom',
    order: [...savedOrder],
    eligibility: Object.entries(membership).map(([id, inChain]) => ({
      source_id: id,
      eligible: true,
      in_current_model_chain: inChain,
      process_availability_reason: cannotLaunch.includes(id) ? 'native_cli_unavailable' : null,
    })),
  },
});

describe('orderSufficiency — of the sources the selected model can actually reach', () => {
  it('stops a source that cannot serve THIS turn from answering for it', () => {
    // The finding. Enabled: a healthy metered key that does not stock the selected
    // model, and the one source that does — a native CLI this machine cannot launch.
    // A rollup over 「can any of these serve something」 says 「fine」 about a turn that
    // has already been decided to fail.
    expect(
      orderSufficiency(
        ['a', 'b'],
        [source('a', 'active'), source('b', 'active')],
        routed(['a', 'b'], { a: false, b: true }, ['b']),
      ),
    ).toEqual({ kind: 'nothing_runnable' });
  });

  it('still lets an on-route source answer for it', () => {
    // The other side of the same gate: dropping the off-route ones may not turn a
    // working order into a warning.
    expect(
      orderSufficiency(
        ['a', 'b'],
        [source('a', 'active'), source('b', 'active')],
        routed(['a', 'b'], { a: false, b: true }),
      ),
    ).toEqual({ kind: 'covered' });
  });

  it('warns when the route is empty because nothing enabled stocks the model', () => {
    // Not 「everything is down」 — everything here is healthy and launchable, and the
    // next turn still has no supplier. One verdict, because the warning offers both
    // remedies and the user has to do one of them either way.
    expect(
      orderSufficiency(
        ['a', 'b'],
        [source('a', 'active'), source('b', 'active')],
        routed(['a', 'b'], { a: false, b: false }),
      ),
    ).toEqual({ kind: 'nothing_runnable' });
  });

  it('does not apply the answer to an id the answer was not about', () => {
    // The false alarm this gate has to not become. `in_current_model_chain` is
    // computed by walking `config.effective_source_order(backend)` — the very list
    // the payload reports as `sources.order` — so a source the user has just enabled
    // in the drawer and not yet saved reads `false` because it was outside that walk,
    // for a reason about the ORDER and not about the model. Gated on that, the drawer
    // would say 「下一个回合会失败，再启用一个」 at the exact moment the user enabled
    // the one that fixes it.
    expect(
      orderSufficiency(
        ['a', 'b'],
        [source('a', 'active'), source('b', 'active')],
        routed(['b'], { a: false, b: true }, ['b']),
      ),
    ).toEqual({ kind: 'covered' });
  });

  it('reads a null or absent membership as the silence it is', () => {
    // `null` is what every row carries while no model is selected: there is no route,
    // so there is nothing to be off. An absent row is a payload that never spoke.
    expect(
      orderSufficiency(
        ['a', 'b'],
        [source('a', 'active'), source('b', 'active')],
        routed(['a', 'b'], { a: null, b: true }, ['b']),
      ),
    ).toEqual({ kind: 'covered' });
    expect(
      orderSufficiency(
        ['a', 'b'],
        [source('a', 'active'), source('b', 'active')],
        routed(['a', 'b'], { b: true }, ['b']),
      ),
    ).toEqual({ kind: 'covered' });
  });

  it('keeps 「unknown id」 ahead of the route gate', () => {
    // An id the inventory cannot resolve is still unknown, not broken — even when
    // everything the gate DID resolve is off the route.
    expect(
      orderSufficiency(['a', 'ghost'], [source('a', 'active')], routed(['a', 'ghost'], { a: false })),
    ).toEqual({ kind: 'indeterminate' });
  });
});

describe('connectOutcome — the two mode-switch toasts', () => {
  it('reports a switch that did not take', () => {
    expect(connectOutcome({ ...hub([]), mode: 'direct' }, [])).toBe('failed');
  });

  it('keeps the server word whenever the server graded the supply', () => {
    for (const status of ['degraded', 'waiting', 'interrupted'] as const) {
      expect(connectOutcome(hub(['a'], status), [source('a', 'active')])).toBe(status);
    }
    expect(connectOutcome(hub(['a'], 'ok'), [source('a', 'active')])).toBe('connected');
  });

  it('warns about an empty order before consulting the grade', () => {
    expect(connectOutcome(hub([], 'ok'), [])).toBe('noSources');
  });

  it('no longer reads a null grade as success', () => {
    // The finding. `supply_status: null` means the server had no model to resolve
    // (`_requested_model` returned nothing) — it is silence about the order, and the
    // old code answered it with the order's length.
    expect(connectOutcome(hub(['a']), [source('a', 'needs_action')])).toBe('nothingRunnable');
    expect(connectOutcome(hub(['a']), [source('a', 'active')])).toBe('connected');
  });

  it('warns about an order it cannot launch, even though every source reads healthy', () => {
    // The switch just took, the server has no model to resolve yet, and the only
    // enabled source is a native CLI this machine cannot run. Health alone would
    // call this connected.
    expect(connectOutcome(hub(['a'], null, ['a']), [source('a', 'active')])).toBe('nothingRunnable');
  });

  it('still defers to the grade the server did give', () => {
    // `supply_status` is the server's own answer about the resolved model, computed
    // where this fact came from. Our inventory read does not get to overrule it.
    expect(connectOutcome(hub(['a'], 'ok', ['a']), [source('a', 'active')])).toBe('connected');
  });

  it('is never reached by the route gate, because the two states cannot coexist', () => {
    // The verdict's route gate only bites where a model is selected, and this caller
    // only consults the verdict where `supply_status` is null — which is the server
    // saying it had no model to resolve, the same condition that makes
    // `in_current_model_chain` null for every row. So the narrowing has nothing to
    // say here, and a payload that somehow carried both would still be answered by
    // the membership rather than by the grade's absence.
    const noSelection = { ...hub(['a', 'b']), sources: routed(['a', 'b'], { a: null, b: null }).sources };
    expect(connectOutcome(noSelection, [source('a', 'active'), source('b', 'active')])).toBe('connected');
  });

  it('says 「I did not check」 rather than 「fine」 where the inventory is absent', () => {
    // BackendSupplyModeCard holds no source list. Its toast states the mode change,
    // which is a fact it owns; it must not upgrade that into a supply claim.
    expect(connectOutcome(hub(['a']), null)).toBe('indeterminate');
    expect(isSupplyWarning('indeterminate')).toBe(false);
  });

  it('routes exactly the warning outcomes through the warning copy family', () => {
    expect([...SUPPLY_WARNINGS].sort()).toEqual(
      ['degraded', 'interrupted', 'noSources', 'nothingRunnable', 'waiting'].sort(),
    );
    expect(isSupplyWarning('connected')).toBe(false);
    expect(isSupplyWarning('failed')).toBe(false);
  });
});

describe('the class: no site answers sufficiency with an emptiness test', () => {
  const here = dirname(fileURLToPath(import.meta.url));
  const files = readdirSync(here, { recursive: true, encoding: 'utf8' })
    .filter((f) => /\.tsx?$/.test(f) && !/\.test\.tsx?$/.test(f) && f !== 'sufficiency.ts');

  /**
   * The identifiers that carry the question, used as a BOOLEAN. A count is fine
   * (`{ count: enabledIds.length }` renders a group header and claims nothing), so
   * the operator is what the rule keys on — which is why it needs no allowance list.
   */
  const PROXY = /\b(?:adopted_by|adoptedBy|order|enabledIds)\.length\s*(?:[=!<>]=|[<>]|\?|\)|&&|\|\|)/;

  it('sweeps a non-trivial file set', () => {
    // Without this the rule below passes over an empty list after any move.
    expect(files.length).toBeGreaterThan(15);
    expect(files).toContain('AdoptionNote.tsx');
  });

  it.each([
    'AdoptionNote.tsx',
    'AddApiKeyDialog.tsx',
    'OAuthConnectDialog.tsx',
    'SourceOrderDrawer.tsx',
    'supply.ts',
  ])('%s asks the owner instead of keeping its own proxy', (name) => {
    // supply.ts is on the list because `connectOutcome` MOVED out of it: the module
    // that used to own the wrong answer must not grow a replacement.
    expect(readFileSync(join(here, name), 'utf8')).not.toMatch(PROXY);
  });

  it('holds for every other module on the page too', () => {
    const offenders = files.filter((f) => PROXY.test(readFileSync(join(here, f), 'utf8')));
    expect(offenders).toEqual([]);
  });
});
