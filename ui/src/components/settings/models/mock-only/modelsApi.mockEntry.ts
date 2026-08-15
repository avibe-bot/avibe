import type { ModelsApi } from '../modelsApi';

/** Test-only entry: production modules never import this lazy corpus boundary. */
export const loadMockModelsApi = (): Promise<ModelsApi> =>
  import('./modelsApi.mock').then(({ createMockApi }) => createMockApi());
