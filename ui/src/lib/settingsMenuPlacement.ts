import { useSyncExternalStore } from 'react';

import { useIsDesktop } from './useIsDesktop';

/**
 * Where the Settings menu sits on a desktop viewport.
 *
 * `standalone` (the shipped default) gives Settings the whole window: its rail
 * stands in for the app sidebar, which is hidden for as long as Settings is
 * open. `inline` keeps the app sidebar live and opens Settings beside it, so
 * the project tree stays reachable while a setting is being changed.
 *
 * This is a per-person, per-device view preference — the same tier as the theme
 * — so it lives in localStorage rather than in instance config, where one
 * member's choice would re-lay out every other member's window. Storage
 * conventions mirror agentsViewMemory: versioned `avibe.*` key, injectable
 * storage for tests, best-effort try/catch so blocked storage degrades to the
 * default instead of throwing.
 */
export type SettingsMenuPlacement = 'standalone' | 'inline';

export const SETTINGS_MENU_PLACEMENTS: readonly SettingsMenuPlacement[] = ['standalone', 'inline'];
export const DEFAULT_SETTINGS_MENU_PLACEMENT: SettingsMenuPlacement = 'standalone';

export const SETTINGS_MENU_PLACEMENT_STORAGE_KEY = 'avibe.settings.menu-placement.v1';
export const SETTINGS_MENU_PLACEMENT_CHANGED_EVENT = 'avibe:settings-menu-placement-changed';

type PlacementStorage = Pick<Storage, 'getItem' | 'setItem'>;

function browserStorage(storage?: PlacementStorage): PlacementStorage | undefined {
  return storage ?? (typeof window !== 'undefined' ? window.localStorage : undefined);
}

export function readSettingsMenuPlacement(storage?: PlacementStorage): SettingsMenuPlacement {
  try {
    const raw = browserStorage(storage)?.getItem(SETTINGS_MENU_PLACEMENT_STORAGE_KEY);
    // An unknown value (older build, hand-edited storage) opens the default
    // rather than selecting neither choice.
    return (SETTINGS_MENU_PLACEMENTS as readonly string[]).includes(raw ?? '')
      ? (raw as SettingsMenuPlacement)
      : DEFAULT_SETTINGS_MENU_PLACEMENT;
  } catch {
    return DEFAULT_SETTINGS_MENU_PLACEMENT;
  }
}

export function writeSettingsMenuPlacement(
  placement: SettingsMenuPlacement,
  storage?: PlacementStorage,
): void {
  try {
    browserStorage(storage)?.setItem(SETTINGS_MENU_PLACEMENT_STORAGE_KEY, placement);
  } catch {
    // View memory is best-effort in private browsing and restricted storage contexts.
  }
  // The shell, the overlay frame and the Settings rail read this from three
  // different React trees — one of them a portal outside the shell's subtree —
  // so the write has to announce itself rather than ride a shared provider.
  // `storage` events only reach *other* tabs, which is the half this covers.
  if (typeof window !== 'undefined') {
    window.dispatchEvent(new Event(SETTINGS_MENU_PLACEMENT_CHANGED_EVENT));
  }
}

function subscribeToSettingsMenuPlacement(listener: () => void): () => void {
  if (typeof window === 'undefined') return () => undefined;
  const onStorage = (event: StorageEvent) => {
    if (event.key === SETTINGS_MENU_PLACEMENT_STORAGE_KEY) listener();
  };
  window.addEventListener('storage', onStorage);
  window.addEventListener(SETTINGS_MENU_PLACEMENT_CHANGED_EVENT, listener);
  return () => {
    window.removeEventListener('storage', onStorage);
    window.removeEventListener(SETTINGS_MENU_PLACEMENT_CHANGED_EVENT, listener);
  };
}

export function useSettingsMenuPlacement(): SettingsMenuPlacement {
  // The snapshot is a plain string, so identity comparison is value comparison
  // and no caching layer is needed between reads.
  return useSyncExternalStore(
    subscribeToSettingsMenuPlacement,
    () => readSettingsMenuPlacement(),
    () => DEFAULT_SETTINGS_MENU_PLACEMENT,
  );
}

/**
 * Whether the Settings menu stands IN FOR the app sidebar rather than opening
 * beside it — the one question the shell, the overlay frame and the Settings
 * rail each have to answer, so it is answered once here.
 *
 * The preference only describes a desktop window. Below md there is no room for
 * two rails, so Settings covers the shell at every viewport that narrow and the
 * preference is simply not in force.
 */
export function useStandaloneSettingsMenu(): boolean {
  const placement = useSettingsMenuPlacement();
  const isDesktop = useIsDesktop();
  return placement === 'standalone' || !isDesktop;
}
