// Where a Model Hub route lands, by capability. Pure so the redirect contract can be
// asserted without mounting the router (see ModelHubCapabilityGate.test.tsx).

import { SETTINGS_LANDING_PATH } from '@/lib/adminNavigation';

export const MODEL_HUB_SETTINGS_PATH = '/settings/models';
export const MODEL_HUB_DISABLED_REDIRECT = '/settings/backends';

export const modelHubDisabledRedirect = (overSetup: boolean): string =>
  overSetup ? SETTINGS_LANDING_PATH : MODEL_HUB_DISABLED_REDIRECT;

export const modelHubRouteTarget = (
  requestedPath: string,
  enabled: boolean,
  overSetup = false,
): string => enabled ? requestedPath : modelHubDisabledRedirect(overSetup);

// Opens the Model Hub with one Source's detail in front, so a failed turn's
// 「Open Models」 lands on the Source that failed, where its key is replaced.
export const MODEL_HUB_SOURCE_PARAM = 'source';

export const modelHubSourcePath = (sourceId: string): string =>
  `${MODEL_HUB_SETTINGS_PATH}?${MODEL_HUB_SOURCE_PARAM}=${encodeURIComponent(sourceId)}`;
