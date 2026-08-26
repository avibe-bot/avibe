import { modelsApi, type ModelsApi } from './modelsApi';
import type { AgentSupply, Source } from './types';

export type CollectionReadResult<T> =
  | { kind: 'current'; value: T }
  | { kind: 'stale' };

export type CollectionReadAuthority<T> = {
  read: () => Promise<CollectionReadResult<T>>;
  refresh: () => Promise<CollectionReadResult<T>>;
  readValue: () => Promise<T>;
  invalidate: () => void;
};

class SupersededCollectionRead extends Error {
  constructor() {
    super('Collection read was superseded');
    this.name = 'SupersededCollectionRead';
  }
}

/** The only owner allowed to call a collection endpoint. Every read carries a
 * generation, including reconciliation reads that do not directly render. */
const createCollectionReadAuthority = <T>(
  readCollection: () => Promise<T>,
  refreshCollection?: () => Promise<unknown>,
): CollectionReadAuthority<T> => {
  let generation = 0;
  const readFrom = async (reader: () => Promise<T>): Promise<CollectionReadResult<T>> => {
    const mine = ++generation;
    try {
      const value = await reader();
      return mine === generation ? { kind: 'current', value } : { kind: 'stale' };
    } catch (error) {
      if (mine !== generation) return { kind: 'stale' };
      throw error;
    }
  };
  const read = () => readFrom(readCollection);
  const refresh = refreshCollection
    ? async () => {
      await refreshCollection();
      return read();
    }
    : read;

  return {
    read,
    refresh,
    readValue: async () => {
      const result = await read();
      if (result.kind === 'stale') throw new SupersededCollectionRead();
      return result.value;
    },
    invalidate: () => { generation += 1; },
  };
};

export const createSourceCollectionReadAuthority = (
  api: Pick<ModelsApi, 'listSources'> = modelsApi,
): CollectionReadAuthority<Source[]> => createCollectionReadAuthority(() => api.listSources());

export const createAgentCollectionReadAuthority = (
  api: Pick<ModelsApi, 'listAgents'> & Partial<Pick<ModelsApi, 'refreshAgentPresence'>> = modelsApi,
): CollectionReadAuthority<AgentSupply[]> => {
  const refreshPresence = api.refreshAgentPresence;
  return createCollectionReadAuthority(
    () => api.listAgents(),
    refreshPresence ? () => refreshPresence() : undefined,
  );
};
