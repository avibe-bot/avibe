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

/** Every projection named by the committed Source report must be read before it lands. */
export const SOURCE_MUTATION_REPORT_PROJECTIONS = {
  sources: 'the Source inventory after the mutation',
  supply: 'the backend supply projection after the mutation',
  runtime: 'the runtime projection that qualifies live chain claims',
  chains: 'every route chain named by the impact evidence',
} as const satisfies Record<keyof SourceMutationProjectionValues, string>;

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

export type SourceMutationLanding =
  | {
      verdict: 'landed';
      reads: SourceMutationLandingReads;
      affectedChains: ModelChainRequest[];
    }
  | {
      verdict: 'degraded';
      reads: SourceMutationLandingReads | null;
      affectedChains: ModelChainRequest[];
    };

export type SourceMutationImpact = { hops: RouteHopRef[]; gaps: SupplyGap[] };

export type SourceMutationReadScope = { affectedChains: ModelChainRequest[] };

export const SOURCE_MUTATION_ACTIONS = ['edit', 'delete'] as const;
export type SourceMutationAction = (typeof SOURCE_MUTATION_ACTIONS)[number];

export type SourceMutationCommit = {
  action: SourceMutationAction;
  impact: SourceMutationImpact | null;
  settle: () => Promise<SourceMutationLanding>;
};

export type PresentSourceMutationCommit = (commit: SourceMutationCommit) => Promise<void>;

/** A landed verdict is proof that every report projection was current and readable. */
export const sourceMutationLanding = (
  reads: SourceMutationLandingReads | null,
  affectedChains: ModelChainRequest[],
  applied: boolean,
): SourceMutationLanding => {
  const reportProjectionFailed = reads
    ? (
        Object.keys(
          SOURCE_MUTATION_REPORT_PROJECTIONS,
        ) as (keyof SourceMutationLandingReads)[]
      ).some((projection) => regionFailed(reads[projection]))
    : true;

  if (!reads || !applied || reportProjectionFailed) {
    return { verdict: 'degraded', reads, affectedChains };
  }
  const chains = foldRegionRead<ModelChainIndex, ModelChainIndex | null>(reads.chains, {
    loading: () => null,
    ready: (data) => data,
    unread: () => null,
    degraded: () => null,
  });
  const chainReadsLanded = chains !== null && affectedChains.every(({ backend, modelId }) => {
    const read = chains[modelChainKey(backend, modelId)];
    return Boolean(read && !regionFailed(read));
  });
  return chainReadsLanded
    ? { verdict: 'landed', reads, affectedChains }
    : { verdict: 'degraded', reads, affectedChains };
};

/** The report evidence is the authority for which exact route projections must land. */
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
