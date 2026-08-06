// Where a Model Hub route lands, by capability. Pure so the redirect contract can be
// asserted without mounting the router (see ModelHubCapabilityGate.test.tsx).

export const MODEL_HUB_SETTINGS_PATH = '/admin/settings/models';
export const MODEL_HUB_DISABLED_REDIRECT = '/admin/settings/backends';

export const modelHubRouteTarget = (requestedPath: string, enabled: boolean): string =>
  enabled ? requestedPath : MODEL_HUB_DISABLED_REDIRECT;
