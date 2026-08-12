export type IntentAuthority = {
  commit: (apply: () => void) => number;
  isCurrent: (generation: number) => boolean;
};

/** Owns UI intent ordering separately from network ordering. A mutation may
 *  finish reconciling after the user has navigated elsewhere; that older
 *  continuation is then observably superseded and must not replay its intent. */
export const createIntentAuthority = (): IntentAuthority => {
  let generation = 0;
  return {
    commit: (apply) => {
      generation += 1;
      apply();
      return generation;
    },
    isCurrent: (candidate) => candidate === generation,
  };
};

export type MutationConvergence<T> = {
  entity?: T;
  applyEntity: (entity: T) => void;
  intent?: { authority: IntentAuthority; apply: () => void };
  reconcile: () => Promise<void>;
};

/** Every mutation converges in the same order: the server-owned entity first,
 *  immediate UI intent before the first await, and then a latest-read
 *  reconciliation. The returned generation verdict lets callers suppress any
 *  optional post-read work without reconstructing intent ownership. */
export async function convergeMutation<T>({
  entity,
  applyEntity,
  intent,
  reconcile,
}: MutationConvergence<T>): Promise<'current' | 'superseded'> {
  if (entity !== undefined) applyEntity(entity);
  const generation = intent?.authority.commit(intent.apply);
  await reconcile();
  return generation === undefined || intent?.authority.isCurrent(generation)
    ? 'current'
    : 'superseded';
}
