// Where a Model Hub route lands, by capability. Pure so the redirect contract can be
// asserted without mounting the router (see ModelHubCapabilityGate.test.tsx).

export const MODEL_HUB_SETTINGS_PATH = '/settings/models';
export const MODEL_HUB_DISABLED_REDIRECT = '/settings/backends';

export const modelHubRouteTarget = (requestedPath: string, enabled: boolean): string =>
  enabled ? requestedPath : MODEL_HUB_DISABLED_REDIRECT;

// Opens the Model Hub with one backend's model catalog dialog in front, so a
// model picker can send the user straight to where its list is edited.
export const MODEL_HUB_MANAGE_PARAM = 'manage';

export const modelHubCatalogPath = (backend: string): string =>
  `${MODEL_HUB_SETTINGS_PATH}?${MODEL_HUB_MANAGE_PARAM}=${encodeURIComponent(backend)}`;
