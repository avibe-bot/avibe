import { useState } from 'react';
import type { Location } from 'react-router-dom';

const SETTINGS_BACKGROUND_KEY = 'settingsBackgroundLocation';

type SettingsOverlayState = {
  [SETTINGS_BACKGROUND_KEY]?: unknown;
};

export type SettingsOverlayOrigin = {
  location: Location;
};

export const isSettingsRoutePath = (pathname: string): boolean =>
  pathname === '/settings' || pathname.startsWith('/settings/');

const settingsBackgroundFromState = (state: unknown): Location | null => {
  if (!state || typeof state !== 'object') return null;
  const candidate = (state as SettingsOverlayState)[SETTINGS_BACKGROUND_KEY];
  if (!candidate || typeof candidate !== 'object') return null;

  const location = candidate as Partial<Location>;
  if (typeof location.pathname !== 'string' || isSettingsRoutePath(location.pathname)) return null;
  if (typeof location.search !== 'string' || typeof location.hash !== 'string') return null;
  if (typeof location.key !== 'string') return null;
  return location as Location;
};

export const settingsOverlayOpenState = (location: Location): SettingsOverlayState => ({
  [SETTINGS_BACKGROUND_KEY]: {
    pathname: location.pathname,
    search: location.search,
    hash: location.hash,
    state: location.state,
    key: location.key,
  },
});

export const locationPath = (location: Pick<Location, 'pathname' | 'search' | 'hash'>): string =>
  `${location.pathname}${location.search}${location.hash}`;

export const useSettingsOverlayOrigin = (location: Location): SettingsOverlayOrigin | null => {
  const fromState = settingsBackgroundFromState(location.state);
  const settingsOpen = isSettingsRoutePath(location.pathname);
  const [snapshot, setSnapshot] = useState(() => ({
    routeKey: location.key,
    retained: fromState,
  }));
  let retained = snapshot.retained;

  if (snapshot.routeKey !== location.key) {
    retained = settingsOpen ? fromState ?? snapshot.retained : null;
    setSnapshot({ routeKey: location.key, retained });
  }

  if (!settingsOpen) return null;
  const background = fromState ?? retained;
  return background ? { location: background } : null;
};
