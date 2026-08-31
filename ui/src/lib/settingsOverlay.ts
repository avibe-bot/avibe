import { createContext, useContext } from 'react';
import type { Location, NavigateFunction } from 'react-router-dom';

import { inAppHistoryIndex } from './navigationHistory';
import { isLegacySettingsEntryPath } from './settingsRoutes';

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

export const isSettingsEntryPath = (pathname: string): boolean =>
  isSettingsRoutePath(pathname) || isLegacySettingsEntryPath(pathname);

export const settingsOverlayOriginFromState = (state: unknown): SettingsOverlayOrigin | null => {
  if (!state || typeof state !== 'object') return null;
  const candidate = (state as SettingsOverlayState)[SETTINGS_BACKGROUND_KEY];
  if (!candidate || typeof candidate !== 'object') return null;

  const stored = candidate as Partial<Location> & { historyIndex?: unknown };
  if (typeof stored.pathname !== 'string' || isSettingsEntryPath(stored.pathname)) return null;
  if (typeof stored.search !== 'string' || typeof stored.hash !== 'string') return null;
  if (typeof stored.key !== 'string') return null;
  const location: Location = {
    pathname: stored.pathname,
    search: stored.search,
    hash: stored.hash,
    state: stored.state,
    key: stored.key,
  };
  const storedHistoryIndex = stored.historyIndex;
  const historyIndex = inAppHistoryIndex({ idx: storedHistoryIndex });
  return { historyIndex, location };
};

const currentHistoryState = (): unknown =>
  typeof window === 'undefined' ? null : window.history.state;

const originFromLocation = (
  location: Location,
  historyState: unknown = currentHistoryState(),
): SettingsOverlayOrigin => ({
  historyIndex: inAppHistoryIndex(historyState),
  location,
});

const overlayStateForOrigin = (origin: SettingsOverlayOrigin): SettingsOverlayState => ({
  [SETTINGS_BACKGROUND_KEY]: {
    pathname: origin.location.pathname,
    search: origin.location.search,
    hash: origin.location.hash,
    state: origin.location.state,
    key: origin.location.key,
    historyIndex: origin.historyIndex,
  },
});

export const settingsOverlayOpenState = (
  location: Location,
  historyState: unknown = currentHistoryState(),
): SettingsOverlayState => overlayStateForOrigin(originFromLocation(location, historyState));

const stateRecord = (state: unknown): Record<string, unknown> =>
  state && typeof state === 'object' && !Array.isArray(state)
    ? state as Record<string, unknown>
    : {};

export const settingsOverlayStateForOrigin = (
  origin: SettingsOverlayOrigin,
  state: unknown,
): SettingsOverlayState & Record<string, unknown> => ({
  ...stateRecord(state),
  ...overlayStateForOrigin(origin),
});

export const settingsOverlayNavigationState = ({
  destinationPathname,
  desktop,
  source,
  targetState,
  historyState = currentHistoryState(),
}: {
  destinationPathname: string;
  desktop: boolean;
  source: Location;
  targetState: unknown;
  historyState?: unknown;
}): unknown => {
  if (!isSettingsEntryPath(destinationPathname)) return targetState;
  if (settingsOverlayOriginFromState(targetState)) return targetState;

  const retainedOrigin = settingsOverlayOriginFromState(source.state);
  const origin = retainedOrigin
    ?? (desktop && !isSettingsEntryPath(source.pathname)
      ? originFromLocation(source, historyState)
      : null);
  if (!origin) return targetState;
  return settingsOverlayStateForOrigin(origin, targetState);
};

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
  if (!isSettingsEntryPath(location.pathname)) return null;
  return settingsOverlayOriginFromState(location.state);
};
