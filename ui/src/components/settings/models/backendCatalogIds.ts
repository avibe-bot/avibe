import type { BackendModel } from './types';

// Shared by the first-screen model picker and the deferred catalog editor. Keep
// this read independent of editing so the picker does not load the editor's code.
export function routeableCatalogModelIds(catalog: readonly BackendModel[]): string[] {
  const seen = new Set<string>();
  return catalog
    .filter((model) => model.routeable && model.id && !seen.has(model.id) && seen.add(model.id))
    .map((model) => model.id);
}
