declare global {
  interface Window {
    /** Defined by the Avibe desktop shell before any page script runs, top-level document only. */
    readonly __AVIBE_DESKTOP_SHELL__?: true;
    readonly __AVIBE_DESKTOP_VERSION__?: string;
    /** The macOS shell grants Tauri window dragging to this page. */
    readonly __AVIBE_DESKTOP_DRAG__?: true;
    /** The shell supports the desktop pet IPC. Conversation selection is main-only. */
    readonly __AVIBE_DESKTOP_PET__?: true;
    /** The shell supports Desktop Mini settings IPC on the Workbench page. */
    readonly __AVIBE_DESKTOP_PET_SETTINGS__?: true;
    /** The macOS shell answers the Computer Use guide's actions. */
    readonly __AVIBE_DESKTOP_COMPUTER_USE__?: true;
    /** The current Workbench handled the shell's update-popover request. */
    __AVIBE_DESKTOP_UPDATES_HANDLED__?: boolean;
  }
}

/**
 * True when this document is the Avibe desktop shell's own window rather than a
 * browser tab or an installed PWA. This is the host, not the viewport size: see
 * `useIsDesktop` for the layout breakpoint.
 *
 * The shell draws the window's title bar and owns the native Settings… menu, so
 * the page leaves out the top bars a browser tab needs for the same job.
 */
export function isDesktopShell(): boolean {
  return typeof window !== 'undefined' && window.__AVIBE_DESKTOP_SHELL__ === true;
}

/**
 * The native Settings… item (⌘,) dispatches this cancelable event on `window`.
 * A listener that opens Settings in place cancels it; left uncancelled, the
 * shell loads Settings as a page instead.
 */
export const DESKTOP_OPEN_SETTINGS_EVENT = 'avibe:desktop-open-settings';

/** Only a direct press on this layout element drags; descendants keep their input. */
export function desktopDragRegion(): '' | undefined {
  // Dragging is granted by the native shell marker. It must not depend on the
  // Runtime UI version: a desktop shell can load an older loopback Workbench
  // whose HTML predates the optional metadata tag.
  return isDesktopShell() && window.__AVIBE_DESKTOP_DRAG__ === true ? '' : undefined;
}

/**
 * Retained capability probe for the native shell's frontend contract.
 * Main-only mini no longer exposes a Workbench "Show in pet" action.
 */
export function canShowInDesktopPet(): boolean {
  return isDesktopShell() && window.__AVIBE_DESKTOP_PET__ === true;
}

/** Probe the settings contract itself so an older shell cannot render a broken control. */
export function canConfigureDesktopPet(): boolean {
  return isDesktopShell() && window.__AVIBE_DESKTOP_PET_SETTINGS__ === true;
}

export const DESKTOP_OPEN_SHORTCUT_SETTINGS_EVENT = 'avibe:desktop-open-shortcut-settings';
export const DESKTOP_PET_SETTINGS_EVENT = 'avibe:desktop-pet-settings';

export type DesktopPetVoiceMode = 'hold' | 'toggle';
export type DesktopPetShortcutTarget = 'voice' | 'toggle';

export type DesktopPetShortcutError = {
  target: DesktopPetShortcutTarget;
  shortcut: string;
};

export type DesktopPetSettings = {
  enabled: boolean;
  voice_shortcut: string;
  toggle_shortcut: string;
  voice_mode: DesktopPetVoiceMode;
  error: DesktopPetShortcutError | null;
};

type DesktopPetSettingsListener = (state: DesktopPetSettings) => void;

const desktopPetSettingsInvoke = (): ShellInvoke | null => {
  if (!canConfigureDesktopPet()) return null;
  const internals = (window as unknown as { __TAURI_INTERNALS__?: { invoke?: ShellInvoke } }).__TAURI_INTERNALS__;
  return internals?.invoke ? internals.invoke.bind(internals) : null;
};

export type DesktopPetSettingsActions = {
  getState: () => Promise<DesktopPetSettings>;
  setEnabled: (enabled: boolean) => Promise<DesktopPetSettings>;
  setShortcut: (target: DesktopPetShortcutTarget, shortcut: string) => Promise<DesktopPetSettings>;
  setVoiceMode: (mode: DesktopPetVoiceMode) => Promise<DesktopPetSettings>;
};

export function desktopPetSettingsActions(): DesktopPetSettingsActions | null {
  const invoke = desktopPetSettingsInvoke();
  if (!invoke) return null;
  return {
    getState: async () => (await invoke('pet_settings')) as DesktopPetSettings,
    setEnabled: async (enabled) => (await invoke('pet_set_enabled', { enabled })) as DesktopPetSettings,
    setShortcut: async (target, shortcut) => (
      await invoke('pet_set_shortcut', { target, shortcut })
    ) as DesktopPetSettings,
    setVoiceMode: async (mode) => (await invoke('pet_set_voice_mode', { mode })) as DesktopPetSettings,
  };
}

export function listenDesktopPetSettings(handler: DesktopPetSettingsListener): () => void {
  const listener = (event: Event) => {
    const state = (event as CustomEvent<DesktopPetSettings>).detail;
    if (state && typeof state.enabled === 'boolean') handler(state);
  };
  window.addEventListener(DESKTOP_PET_SETTINGS_EVENT, listener);
  return () => window.removeEventListener(DESKTOP_PET_SETTINGS_EVENT, listener);
}

type ShellInvoke = (command: string, args?: Record<string, unknown>) => Promise<unknown>;
type ShellTransformCallback = (callback: (event: unknown) => void, once?: boolean) => number;

const computerUseInvoke = (): ShellInvoke | null => {
  if (!isDesktopShell() || window.__AVIBE_DESKTOP_COMPUTER_USE__ !== true) return null;
  const internals = (window as unknown as { __TAURI_INTERNALS__?: { invoke?: ShellInvoke } }).__TAURI_INTERNALS__;
  return internals?.invoke ? internals.invoke.bind(internals) : null;
};

export type DesktopUpdatePhase =
  | 'idle'
  | 'checking'
  | 'current'
  | 'available'
  | 'downloading'
  | 'installing'
  | 'installed'
  | 'disabled'
  | 'error';

export type DesktopUpdateState = {
  phase: DesktopUpdatePhase;
  latest: string | null;
  channel: 'stable' | 'test';
  skippedVersion: string | null;
  interactive: boolean;
};

const desktopUpdateInvoke = (): ShellInvoke | null => {
  if (!isDesktopShell()) return null;
  const internals = (window as unknown as { __TAURI_INTERNALS__?: { invoke?: ShellInvoke } }).__TAURI_INTERNALS__;
  return internals?.invoke ? internals.invoke.bind(internals) : null;
};

export const DESKTOP_OPEN_UPDATES_EVENT = 'avibe:desktop-open-updates';
export const DESKTOP_UPDATE_STATE_EVENT = 'avibe:desktop-update-state';

type DesktopUpdateStateEvent = {
  payload: unknown;
};

type DesktopUpdateStateListener = (state: DesktopUpdateState) => void;

/** The desktop updater controls available to the Workbench's version popover. */
export type DesktopUpdateShellActions = {
  getState: () => Promise<DesktopUpdateState>;
  check: () => Promise<void>;
  install: () => Promise<void>;
  skip: () => Promise<void>;
};

export function desktopUpdateShellActions(): DesktopUpdateShellActions | null {
  const invoke = desktopUpdateInvoke();
  if (!invoke) return null;
  return {
    getState: async () => (await invoke('desktop_update_state')) as DesktopUpdateState,
    check: async () => { await invoke('desktop_update_check'); },
    install: async () => { await invoke('desktop_update_install'); },
    skip: async () => { await invoke('desktop_update_skip'); },
  };
}

/**
 * Listen to native updater state changes without making the Web UI depend on
 * the desktop package. The desktop shell grants the core event permission to
 * the Workbench capability; a browser or an older shell simply has no
 * listener bridge and keeps using snapshot reads.
 */
export async function listenDesktopUpdateState(
  handler: DesktopUpdateStateListener,
): Promise<(() => Promise<void>) | null> {
  const invoke = desktopUpdateInvoke();
  const internals = (window as unknown as {
    __TAURI_INTERNALS__?: {
      transformCallback?: ShellTransformCallback;
    };
  }).__TAURI_INTERNALS__;
  if (!invoke || !internals?.transformCallback) return null;

  const eventId = await invoke('plugin:event|listen', {
    event: DESKTOP_UPDATE_STATE_EVENT,
    target: { kind: 'Any' },
    handler: internals.transformCallback((event: unknown) => {
      const payload = (event as DesktopUpdateStateEvent).payload as DesktopUpdateState;
      handler(payload);
    }),
  }) as number;

  return async () => {
    const eventPlugin = (window as unknown as {
      __TAURI_EVENT_PLUGIN_INTERNALS__?: {
        unregisterListener?: (event: string, eventId: number) => void;
      };
    }).__TAURI_EVENT_PLUGIN_INTERNALS__;
    eventPlugin?.unregisterListener?.(DESKTOP_UPDATE_STATE_EVENT, eventId);
    await invoke('plugin:event|unlisten', {
      event: DESKTOP_UPDATE_STATE_EVENT,
      eventId,
    });
  };
}

/** A System Settings privacy pane the Computer Use guide can open. */
export type ComputerUsePermissionPane = 'accessibility' | 'screen_recording';

/**
 * The native side of the Computer Use guide, or `null` when this page cannot
 * drive it: a browser tab, a remote browser, or an older shell. The shell
 * re-checks that the caller is its own Workbench.
 */
export function computerUseShellActions(): {
  enable: () => Promise<void>;
  openPermission: (pane: ComputerUsePermissionPane) => Promise<void>;
} | null {
  const invoke = computerUseInvoke();
  if (!invoke) return null;
  return {
    enable: async () => { await invoke('computer_use_enable'); },
    openPermission: async (pane) => { await invoke('computer_use_open_permission', { pane }); },
  };
}
