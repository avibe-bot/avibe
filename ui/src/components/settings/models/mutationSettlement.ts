import type { TranslationKey } from '@/i18n/types';
import type { SourceCreated } from './modelsApi';
import { readFirstPaintRegions } from './firstPaintRegions';
import { modelChainKey, type ModelChainIndex, type ModelChainRequest } from './modelRows';
import { foldRegionRead, readRegion, regionFailed, type RegionRead } from './regionRead';
import type {
  AgentSupply,
  RouteHopRef,
  RuntimeDependency,
  Source,
  SupplyGap,
} from './types';

export type ContinuationTicket = number & { readonly __continuationTicket: unique symbol };

/** The only place an awaited dialog continuation may decide whether to land effects. */
export const createContinuationSettlement = () => {
  let current = 0;
  return {
    begin: (): ContinuationTicket => (++current) as ContinuationTicket,
    invalidate: (): void => { current += 1; },
    settle: (ticket: ContinuationTicket, apply: () => void): 'landed' | 'stale' => {
      if (ticket !== current) return 'stale';
      apply();
      return 'landed';
    },
  };
};

export type ContinuationSettlement = ReturnType<typeof createContinuationSettlement>;

/** Keeps the dialog's mutation delivery private so callers cannot bypass its fence. */
export const createSourceCreatedDelivery = () => {
  let onAdded: (created: SourceCreated) => void = () => {};
  let onClose: () => void = () => {};
  return {
    update: (added: (created: SourceCreated) => void, close: () => void): void => {
      onAdded = added;
      onClose = close;
    },
    settle: (
      authority: ContinuationSettlement,
      ticket: ContinuationTicket,
      created: SourceCreated,
    ): 'landed' | 'stale' => authority.settle(ticket, () => {
      onAdded(created);
      onClose();
    }),
    close: (): void => onClose(),
  };
};

export type SourceInventorySnapshot = { snapshot: number; sources: Source[] };

type SourceMutationProjectionValues = {
  sources: Source[];
  supply: AgentSupply[];
  runtime: RuntimeDependency;
  chains: ModelChainIndex;
};

export type SourceMutationLandingReads = {
  [K in keyof SourceMutationProjectionValues]: RegionRead<SourceMutationProjectionValues[K]>;
};

export type SourceMutationProjectionReaders = {
  sources: () => Promise<Source[]>;
  supply: () => Promise<AgentSupply[]>;
  runtime: () => Promise<RuntimeDependency>;
  chains: (requests: readonly ModelChainRequest[]) => Promise<ModelChainIndex>;
};

export const readSurfaceLanding = async (
  readers: SourceMutationProjectionReaders,
  affectedChains: readonly ModelChainRequest[],
): Promise<SourceMutationLandingReads> => {
  const [surface, chains] = await Promise.all([
    readFirstPaintRegions({
      sources: readers.sources,
      supply: readers.supply,
      runtime: readers.runtime,
    }),
    readRegion(() => readers.chains(affectedChains)),
  ]);
  return { ...surface, chains };
};

/**
 * What a mutation's own read of the model surface published, or `null` when a
 * newer read took the surface over before this one could land.
 *
 * There is deliberately no pass/fail verdict over the whole surface. That
 * verdict existed for the post-commit report, which asked the user to accept a
 * write that had already landed; with the report gone, each region answers for
 * itself through its own `RegionRead` — stale projection, Retry, and whatever
 * the surface that owns it says about a read it could not complete.
 */
export type SourceMutationLanding = SourceMutationLandingReads | null;

/**
 * Whether this read left any projection it installs showing something older
 * than the server — the question the page's refresh line answers.
 *
 * The chain index has to be opened rather than trusted: a route request that
 * failed lands as an unread entry INSIDE a perfectly readable index, so a check
 * that stopped at the index would call a route nobody could re-read a clean
 * refresh.
 *
 * What is deliberately not a failure is a key that is simply absent. A read the
 * newer read took over reports no keys at all rather than unread ones, so «a
 * newer read won» can never arrive here as «the page could not refresh» — the
 * confusion that made a successful save look unconfirmed.
 */
export const sourceMutationReadFailed = (landing: SourceMutationLandingReads): boolean => {
  const chains = foldRegionRead<ModelChainIndex, ModelChainIndex>(landing.chains, {
    loading: () => ({}),
    ready: (index) => index,
    unread: () => ({}),
    degraded: (staleIndex) => staleIndex,
  });
  return Object.values(landing).some((read) => regionFailed(read))
    || Object.values(chains).some((read) => regionFailed(read));
};

export type SourceMutationImpact = { hops: RouteHopRef[]; gaps: SupplyGap[] };

export type SourceMutationReadScope = { affectedChains: ModelChainRequest[] };

export const SOURCE_MUTATION_ACTIONS = ['edit', 'delete'] as const;
export type SourceMutationAction = (typeof SOURCE_MUTATION_ACTIONS)[number];

/**
 * What the write actually left behind, which is not always what was asked for:
 * an edit whose Source turns out to be absent lands `gone`, not `updated`.
 *
 * This is deliberately NOT the action. The action is the flow the user is in and
 * belongs to the panel's stage machine; the outcome is what the surface may tell
 * them, and announcing 「已更新」 over a panel that says the provider is no longer
 * there is exactly the mismatch a separate word prevents.
 */
export const SOURCE_MUTATION_OUTCOMES = ['updated', 'removed', 'gone'] as const;
export type SourceMutationOutcome = (typeof SOURCE_MUTATION_OUTCOMES)[number];

/**
 * The line AND the tone for each outcome, decided together — same rule as
 * `REPAIR_TOAST`: tone is a property of the outcome, not of the branch that
 * happens to render it, and a Record over the full union makes the next outcome
 * added answer for its own tone instead of inheriting green.
 *
 * `gone` reuses the copy the detail surface already shows for the same fact, so
 * the toast and the panel under it say one thing rather than two.
 */
export const SOURCE_MUTATION_TOAST: Record<
  SourceMutationOutcome,
  { key: TranslationKey; tone: 'success' | 'warning' }
> = {
  updated: { key: 'settings.models.sourceDetail.edit.settlement.title', tone: 'success' },
  removed: { key: 'settings.models.sourceDetail.remove.settlement.title', tone: 'success' },
  gone: { key: 'settings.models.sourceDetail.gone', tone: 'warning' },
};

export type SourceMutationCommit = {
  outcome: SourceMutationOutcome;
  impact: SourceMutationImpact | null;
  settle: () => Promise<SourceMutationLanding>;
};

export type PresentSourceMutationCommit = (commit: SourceMutationCommit) => Promise<void>;

/** The impact evidence is the authority for which exact route projections to read. */
export const sourceMutationReadScope = (
  impact: SourceMutationImpact | null,
): SourceMutationReadScope => {
  const requests = new Map<string, ModelChainRequest>();
  for (const hop of impact?.hops ?? []) {
    const request = { backend: hop.backend, modelId: hop.menu_model };
    requests.set(modelChainKey(request.backend, request.modelId), request);
  }
  for (const gap of impact?.gaps ?? []) {
    const request = { backend: gap.backend, modelId: gap.model_id };
    requests.set(modelChainKey(request.backend, request.modelId), request);
  }
  return { affectedChains: [...requests.values()] };
};

export type SourceMutationSettlement = {
  source: (source: Source, scope?: SourceMutationReadScope) => Promise<SourceMutationLanding>;
  gone: (
    sourceId: string,
    inventory?: SourceInventorySnapshot,
    scope?: SourceMutationReadScope,
  ) => Promise<SourceMutationLanding>;
  unread: (scope?: SourceMutationReadScope) => Promise<SourceMutationLanding>;
  release: () => void;
  readInventory: () => Promise<SourceInventorySnapshot>;
};

export type TrackSourceMutation = <T>(
  work: (source: Source, settlement: SourceMutationSettlement) => Promise<T>,
) => Promise<T>;
