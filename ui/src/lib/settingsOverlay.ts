import { createContext, useContext, useState } from 'react';
import type { Location, NavigateFunction } from 'react-router-dom';

import { inAppHistoryIndex } from './navigationHistory';

const SETTINGS_BACKGROUND_KEY = 'settingsBackgroundLocation';

type SettingsOverlayState = {
  [SETTINGS_BACKGROUND_KEY]?: unknown;
};

export type SettingsOverlayOrigin = {
  historyIndex: number | null;
  location: Location;
};

export const SettingsOverlayOriginContext = createContext<SettingsOverlayOrigin | null>(null);

export const useSettingsOverlayContext = (): SettingsOverlayOrigin | null =>
  useContext(SettingsOverlayOriginContext);

export const isSettingsRoutePath = (pathname: string): boolean =>
  pathname === '/settings' || pathname.startsWith('/settings/');

const settingsBackgroundFromState = (state: unknown): SettingsOverlayOrigin | null => {
  if (!state || typeof state !== 'object') return null;
  const candidate = (state as SettingsOverlayState)[SETTINGS_BACKGROUND_KEY];
  if (!candidate || typeof candidate !== 'object') return null;

  const location = candidate as Partial<Location>;
  if (typeof location.pathname !== 'string' || isSettingsRoutePath(location.pathname)) return null;
  if (typeof location.search !== 'string' || typeof location.hash !== 'string') return null;
  if (typeof location.key !== 'string') return null;
  const storedHistoryIndex = (candidate as { historyIndex?: unknown }).historyIndex;
  const historyIndex = inAppHistoryIndex({ idx: storedHistoryIndex });
  return { historyIndex, location: location as Location };
};

const currentHistoryState = (): unknown =>
  typeof window === 'undefined' ? null : window.history.state;

const originFromLocation = (location: Location): SettingsOverlayOrigin => ({
  historyIndex: inAppHistoryIndex(currentHistoryState()),
  location,
});

export const settingsOverlayOpenState = (
  location: Location,
  historyState: unknown = currentHistoryState(),
): SettingsOverlayState => ({
  [SETTINGS_BACKGROUND_KEY]: {
    pathname: location.pathname,
    search: location.search,
    hash: location.hash,
    state: location.state,
    key: location.key,
    historyIndex: inAppHistoryIndex(historyState),
  },
});

export const locationPath = (location: Pick<Location, 'pathname' | 'search' | 'hash'>): string =>
  `${location.pathname}${location.search}${location.hash}`;

export const settingsOverlayHistoryDelta = (
  origin: SettingsOverlayOrigin,
  historyState: unknown = currentHistoryState(),
): number | null => {
  const currentIndex = inAppHistoryIndex(historyState);
  if (
    origin.historyIndex === null ||
    currentIndex === null ||
    currentIndex <= origin.historyIndex
  ) return null;
  return origin.historyIndex - currentIndex;
};

export const closeSettingsOverlay = (
  navigate: NavigateFunction,
  origin: SettingsOverlayOrigin,
  historyState: unknown = currentHistoryState(),
): void => {
  const delta = settingsOverlayHistoryDelta(origin, historyState);
  if (delta !== null) {
    navigate(delta);
    return;
  }
  navigate(locationPath(origin.location), {
    replace: true,
    state: origin.location.state,
  });
};

export const useSettingsOverlayOrigin = (location: Location): SettingsOverlayOrigin | null => {
  const fromState = settingsBackgroundFromState(location.state);
  const settingsOpen = isSettingsRoutePath(location.pathname);
  const [snapshot, setSnapshot] = useState(() => ({
    lastNonSettings: settingsOpen ? null : originFromLocation(location),
    routeKey: location.key,
    retained: fromState,
  }));
  let lastNonSettings = snapshot.lastNonSettings;
  let retained = snapshot.retained;

  if (snapshot.routeKey !== location.key) {
    if (settingsOpen) {
      retained = fromState ?? snapshot.retained ?? snapshot.lastNonSettings;
    } else {
      lastNonSettings = originFromLocation(location);
      retained = null;
    }
    setSnapshot({ lastNonSettings, routeKey: location.key, retained });
  }

  if (!settingsOpen) return null;
  return fromState ?? retained;
};
