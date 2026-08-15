import type { SourceCreated } from './modelsApi';
import type { Source } from './types';

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

export type SourceMutationLanding = 'landed' | 'degraded';

export type SourceMutationSettlement = {
  source: (source: Source) => Promise<SourceMutationLanding>;
  gone: (sourceId: string, inventory?: SourceInventorySnapshot) => Promise<SourceMutationLanding>;
  unread: () => Promise<SourceMutationLanding>;
  release: () => void;
  readInventory: () => Promise<SourceInventorySnapshot>;
};

export type TrackSourceMutation = <T>(
  work: (source: Source, settlement: SourceMutationSettlement) => Promise<T>,
) => Promise<T>;
