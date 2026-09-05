import { eligibleSources } from "./eligibility";
import type {
  AgentBackend,
  AgentChain,
  AgentSupply,
  RouteHop,
  ManualRouteOverride,
  Source,
} from "./types";

export type RouteCandidate = {
  hop: RouteHop;
  source: Source;
};

export type DraftValidation = {
  invalidIndexes: number[];
  valid: boolean;
};

const identity = (hop: RouteHop): string =>
  `${hop.source_id}\u0000${hop.model_id}`;

/** Candidate inventory for V5. The server-owned eligibility projection is the
 * only admission check; menu mapping is used only to name the upstream model. */
export function routeCandidates(
  agent: AgentSupply,
  sources: Source[],
  draft: RouteHop[],
): RouteCandidate[] {
  const used = new Set(draft.map(identity));
  const sourceOrder = new Map(
    (agent.sources?.order ?? []).map((id, index) => [id, index]),
  );
  return eligibleSources(sources, agent)
    .flatMap((source) =>
      source.models
        .filter((model) => model.retired !== true)
        .map((model) => ({
          source,
          hop: {
            source_id: source.id,
            // The selector may display a mapped menu identity, but PUT accepts the
            // exact upstream model id owned by this Source.
            model_id: model.id,
          },
        })),
    )
    .filter(({ hop }) => !used.has(identity(hop)))
    .sort(
      (left, right) =>
        (sourceOrder.get(left.source.id) ?? Number.MAX_SAFE_INTEGER) -
          (sourceOrder.get(right.source.id) ?? Number.MAX_SAFE_INTEGER) ||
        left.source.display_name.localeCompare(right.source.display_name) ||
        left.hop.model_id.localeCompare(right.hop.model_id),
    );
}

/** V5: unchanged stale identities remain editable; only new/changed pairs need
 * a live eligible inventory row. */
export function validateRouteDraft(
  agent: AgentSupply,
  sources: Source[],
  origin: RouteHop[],
  draft: RouteHop[],
): DraftValidation {
  const unchanged = new Set(origin.map(identity));
  const available = new Map(eligibleSources(sources, agent).map((source) => [source.id, source]));
  const counts = new Map<string, number>();
  draft.forEach((hop) =>
    counts.set(identity(hop), (counts.get(identity(hop)) ?? 0) + 1),
  );
  const allInvalidIndexes = draft.flatMap((hop, index) => {
    const key = identity(hop);
    const source = available.get(hop.source_id);
    const admitted = source && hop.model_id.trim().length > 0
      && hop.model_id === hop.model_id.trim()
      && (source.kind === 'api_key' || source.models.some((model) => model.id === hop.model_id))
      && !source.models.some((model) => model.id === hop.model_id && model.retired === true);
    return counts.get(key)! > 1 || (!unchanged.has(key) && !admitted)
      ? [index]
      : [];
  });
  return {
    invalidIndexes: allInvalidIndexes,
    valid: allInvalidIndexes.length === 0,
  };
}

/** Stable local sort used by the frame control. It never adds or removes hops. */
export function reorderRouteDraft(
  agent: AgentSupply,
  draft: RouteHop[],
): RouteHop[] {
  const sourceOrder = new Map(
    (agent.sources?.order ?? []).map((id, index) => [id, index]),
  );
  return draft
    .map((hop, index) => ({ hop, index }))
    .sort(
      (left, right) =>
        (sourceOrder.has(left.hop.source_id) ? 0 : 1) -
          (sourceOrder.has(right.hop.source_id) ? 0 : 1) ||
        (sourceOrder.get(left.hop.source_id) ?? 0) -
          (sourceOrder.get(right.hop.source_id) ?? 0) ||
        left.index - right.index,
    )
    .map(({ hop }) => hop);
}

export const sameRouteDraft = (left: RouteHop[], right: RouteHop[]): boolean =>
  left.length === right.length &&
  left.every((hop, index) => identity(hop) === identity(right[index]));

export const sameManualOverride = (left: ManualRouteOverride | null, right: ManualRouteOverride | null): boolean =>
  left === null || right === null ? left === right : sameRouteDraft(left.hops, right.hops);

export const routeChainMatchesAttempt = (
  chain: AgentChain,
  attempt: {
    backend: AgentBackend;
    modelId: string;
    submitted: RouteHop[];
    manual_override: ManualRouteOverride | null;
  },
): boolean => {
  const { backend, model_id: menuModel } = chain;
  return (
    backend === attempt.backend &&
    menuModel === attempt.modelId &&
    sameManualOverride(chain.manual_override, attempt.manual_override)
  );
};
