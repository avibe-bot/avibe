import { useCallback, useRef, useState } from 'react';
import type { ApiContextType } from '../../context/ApiContext';
import type { BuiltinBackend } from '@/lib/agentBackends';
import { compatibleEffort } from '@/lib/effortOptions';
import { useLatestRef } from '@/lib/useLatestRef';
import { draftRowFor, type ChosenCandidate } from '../settings/models/backendCatalog';
import type { CollectionReadAuthority } from '../settings/models/collectionReadAuthority';
import { apiFailure, type ModelsApi } from '../settings/models/modelsApi';
import type { AgentSupply } from '../settings/models/types';
import type { BuiltinModelOffer } from './builtinModelOffer';

/** The server's refusal of a pick whose suppliers moved after the card showed them. */
const CANDIDATES_CHANGED = 'candidate_suppliers_changed';

/**
 * Why a choice did not stand. `suppliersChanged` ends the choice: the rows it was made
 * from are gone and today's are read. `failed` keeps it: the model the person chose is
 * still the one they asked for, and Retry applies it again.
 */
export type BuiltinChoiceFailure = { model: string; kind: 'suppliersChanged' | 'failed' };

type Choice = { pick: ChosenCandidate; writing: boolean; failure: BuiltinChoiceFailure | null };
type ShownOffer = BuiltinModelOffer & { epoch: number };

export type BuiltinModelChoiceDeps = {
  api: Pick<ApiContextType, 'getVibeAgent' | 'updateVibeAgent'>;
  models: Pick<ModelsApi, 'getAgentModelCandidates' | 'putAgentModels'>;
  agentReads?: CollectionReadAuthority<AgentSupply[]>;
  /** The showing of the screen: it moves every time the screen is activated. */
  epoch: () => number;
  /** The card's own Agent, by name, from the current route read. */
  target: (backend: BuiltinBackend) => string | null | undefined;
  /** Redraw the card from the server once a choice has settled either way. */
  onSettled: (backend: BuiltinBackend) => unknown;
};

/**
 * A built-in assistant's one setup decision — which model it runs on — owned end to end:
 * reading the models it could run on, writing the person's choice, and the ways either
 * goes stale or half-done.
 *
 * - **Freshness.** An offer belongs to the showing of the screen that read it. A read
 *   that lands after the person left answers nothing, and a returning screen reads again
 *   rather than offering rows from before.
 * - **Conflict.** A pick refused because its suppliers moved takes its rows with it until
 *   today's read replaces them, so the refused question is not asked again.
 * - **The write.** A choice is up to two writes: the backend's model list, when it lacks
 *   the pick, then the Agent's model, with its effort moved by the Agent editor's rule.
 *   Between them the server fills an Agent that has no model with the list's first row,
 *   so a failure after the first can leave the Agent on a model nobody chose. The choice
 *   is therefore settled by a read of the Agent, not by the writes' answers: until that
 *   read shows the chosen model the choice is held, entry waits on it, and Retry applies
 *   it again. A lost reply to a write that did land settles the same way.
 */
export function useBuiltinModelChoice(deps: BuiltinModelChoiceDeps) {
  const latest = useLatestRef(deps);
  const [offers, setOffers] = useState<Partial<Record<BuiltinBackend, ShownOffer>>>({});
  const [choices, setChoices] = useState<Partial<Record<BuiltinBackend, Choice>>>({});
  const tokens = useRef<Partial<Record<BuiltinBackend, number>>>({});
  const writing = useRef<Partial<Record<BuiltinBackend, boolean>>>({});

  const read = useCallback(async (backend: BuiltinBackend) => {
    const token = (tokens.current[backend] ?? 0) + 1;
    tokens.current[backend] = token;
    const showing = latest.current.epoch();
    const owns = () => tokens.current[backend] === token && latest.current.epoch() === showing;
    // A re-read within the same showing keeps the rows it already shows, so refreshing
    // the routes does not blank the list under the person's pointer.
    setOffers((current) => (current[backend]?.kind === 'ready' && current[backend].epoch === showing
      ? current : { ...current, [backend]: { kind: 'loading', epoch: showing } }));
    try {
      const value = await latest.current.models.getAgentModelCandidates(backend);
      if (owns()) setOffers((current) => ({ ...current, [backend]: { kind: 'ready', read: value, epoch: showing } }));
    } catch {
      if (owns()) setOffers((current) => ({ ...current, [backend]: { kind: 'failed', epoch: showing } }));
    }
  }, [latest]);

  const choose = useCallback(async (backend: BuiltinBackend, pick: ChosenCandidate) => {
    const { api, models, agentReads, target: targetOf } = latest.current;
    const target = targetOf(backend);
    if (!target || !agentReads || writing.current[backend]) return;
    writing.current[backend] = true;
    const model = pick.candidate.id;
    setChoices((current) => ({ ...current, [backend]: { pick, writing: true, failure: null } }));
    let refused = false;
    // The efforts the chosen model takes: what the list holds for it, else what the
    // candidate states — the row the write would add.
    let efforts: readonly string[] = pick.candidate.reasoning_efforts;
    try {
      const supply = (await agentReads.readValue()).find((row) => row.backend === backend);
      if (!supply) throw new Error('supply_unread');
      const baseline = supply.catalog_models ?? [];
      const row = draftRowFor(pick.candidate, [], baseline);
      efforts = row.reasoning_efforts;
      if (!baseline.some((held) => held.id === model)) {
        await models.putAgentModels(backend, {
          baseline, models: [...baseline, row], expected_suppliers: { [model]: pick.expected_suppliers },
        });
      }
      const current = await api.getVibeAgent(target, { cache: false });
      if (!current?.ok) throw new Error('agent_unread');
      const effort = compatibleEffort(current.agent.reasoning_effort, row.reasoning_efforts);
      const updated = await api.updateVibeAgent(target, effort === (current.agent.reasoning_effort ?? null)
        ? { model } : { model, reasoning_effort: effort });
      if (!updated?.ok) throw new Error('agent_update_failed');
    } catch (error) {
      // A refusal committed nothing, which is the one answer a write can be trusted for.
      refused = apiFailure(error)?.code === CANDIDATES_CHANGED;
    }
    // The choice stands when the Agent reads as the whole of it: the chosen model, with
    // an effort that model takes. The server's own fill can name the model without the
    // write that moves the effort ever landing.
    const confirmed = !refused && await api.getVibeAgent(target, { cache: false })
      .then((result) => !!result?.ok && result.agent.model === model
        && compatibleEffort(result.agent.reasoning_effort, efforts) === (result.agent.reasoning_effort ?? null), () => false);
    const failure: BuiltinChoiceFailure | null = refused ? { model, kind: 'suppliersChanged' }
      : confirmed ? null : { model, kind: 'failed' };
    if (refused) {
      setOffers((current) => ({ ...current, [backend]: { kind: 'loading', epoch: latest.current.epoch() } }));
      void read(backend);
    }
    // Still writing until the reads it set off have settled: entry acts on the same
    // shared reads, and a click in between would race them.
    if (confirmed) {
      try { await agentReads.refresh(); } catch { /* The redraw below reports what it cannot read. */ }
    }
    try { await latest.current.onSettled(backend); } finally {
      writing.current[backend] = false;
      setChoices((current) => {
        const next = { ...current };
        if (failure) next[backend] = { pick, writing: false, failure };
        else delete next[backend];
        return next;
      });
    }
  }, [latest, read]);

  const shown = (backend: BuiltinBackend) => {
    const offer = offers[backend];
    return offer && offer.epoch === deps.epoch() ? offer : undefined;
  };
  return {
    read,
    choose,
    /** Apply a choice that did not stand once more. */
    retry: (backend: BuiltinBackend) => {
      const choice = choices[backend];
      if (choice?.failure?.kind === 'failed') void choose(backend, choice.pick);
    },
    /** The candidates this showing of the screen read. */
    offer: (backend: BuiltinBackend): BuiltinModelOffer | undefined => shown(backend),
    /** The model being written, if a choice is in flight. */
    picking: (backend: BuiltinBackend) => (choices[backend]?.writing ? choices[backend].pick.candidate.id : null),
    failure: (backend: BuiltinBackend) => (choices[backend]?.writing ? null : choices[backend]?.failure ?? null),
    /** A choice is in flight or has not stood yet: entry waits on it. */
    holds: (backend: BuiltinBackend) => {
      const choice = choices[backend];
      return !!choice && (choice.writing || choice.failure?.kind === 'failed');
    },
  };
}
