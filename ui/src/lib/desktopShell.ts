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
    /** The shell supports native desktop notification settings IPC on the Workbench page. */
    readonly __AVIBE_DESKTOP_NOTIFICATIONS__?: true;
    /** The macOS shell answers the Computer Use guide's actions. */
    readonly __AVIBE_DESKTOP_COMPUTER_USE__?: true;
    /** The shell also turns Computer Use off from the page; older shells only turn it on. */
    readonly __AVIBE_DESKTOP_COMPUTER_USE_DISABLE__?: true;
    /** The macOS shell can run the vault sandbox's passkey steps in its own windows: on the macOS
     *  versions its window checks have passed on, signed with the sandbox association. A display
     *  capability, not a security decision. */
    readonly __AVIBE_DESKTOP_VAULT_PASSKEY__?: true;
    /** The shell brings its main window forward on the Workbench's request (`focus_main_window`). */
    readonly __AVIBE_DESKTOP_FOCUS__?: true;
    /** The shell owns App Lock and answers `app_lock_status` / `app_lock_set_policy` on the
     *  Workbench page (ui/src/lib/appLockContract.ts). */
    readonly __AVIBE_DESKTOP_APP_LOCK__?: true;
    /** The main Workbench can ask the shell to open standalone native app windows. */
    readonly __AVIBE_DESKTOP_APP_WINDOWS__?: true;
    /** Identity of a native App child window; grants no IPC capabilities. */
    readonly __AVIBE_NATIVE_APP_WINDOW__?: true;
    /** The macOS shell shares the Show this window displays through the system share menu
     *  (`desktop_show_share`, C1 v1 `c1.share_draft`): set on the Workbench and on a Show app
     *  window served by this computer, never on another app window or a page from elsewhere. */
    readonly __AVIBE_DESKTOP_SHOW_SHARE__?: 1;
    /** A Show app window also exports its own content as a PDF or image; the Workbench, which has
     *  no trusted tie to the pixels of a Show it embeds, shares links only. */
    readonly __AVIBE_DESKTOP_SHOW_EXPORT__?: 1;
    /** The current Workbench handled the shell's update-popover request. */
    __AVIBE_DESKTOP_UPDATES_HANDLED__?: boolean;
    /** Last native open request accepted in this document; not install authority. */
    __AVIBE_DESKTOP_UPDATES_DELIVERED__?: string;
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

export function isNativeAppWindow(): boolean {
  return typeof window !== 'undefined' && window.__AVIBE_NATIVE_APP_WINDOW__ === true;
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

/**
 * The shell can host the vault sandbox's passkey steps itself: `window.open` from this page returns
 * a window that only the sandbox may load, the sandbox frame may open its own, and WebAuthn reaches
 * the sandbox's passkey. Older shells, Windows, Linux, macOS versions the shell has not been
 * verified on, and unsigned builds never say so.
 */
export function canRunDesktopVaultPasskey(): boolean {
  return isDesktopShell() && window.__AVIBE_DESKTOP_VAULT_PASSKEY__ === true;
}

/** Probe the native desktop notification settings contract before rendering its switch. */
export function canConfigureDesktopNotifications(): boolean {
  return isDesktopShell() && window.__AVIBE_DESKTOP_NOTIFICATIONS__ === true;
}

export const DESKTOP_OPEN_SHORTCUT_SETTINGS_EVENT = 'avibe:desktop-open-shortcut-settings';
export const DESKTOP_PET_SETTINGS_EVENT = 'avibe:desktop-pet-settings';
export const DESKTOP_NOTIFICATIONS_EVENT = 'avibe:desktop-notifications';

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
  errors: DesktopPetShortcutError[];
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
  resetShortcuts: () => Promise<DesktopPetSettings>;
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
    resetShortcuts: async () => (await invoke('pet_reset_shortcuts')) as DesktopPetSettings,
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

/** What the system allows the shell to post. `unavailable`: no system permission to report (not macOS). */
export type DesktopNotificationAuthorization = 'authorized' | 'denied' | 'not_determined' | 'unavailable';

export type DesktopNotificationsActions = {
  get: () => Promise<boolean>;
  set: (enabled: boolean) => Promise<boolean>;
  /** Rejects on a shell that predates the command; the page then shows no system state. */
  authorization: () => Promise<DesktopNotificationAuthorization>;
  openSystemSettings: () => Promise<void>;
};

const AUTHORIZATIONS = new Set<DesktopNotificationAuthorization>(['authorized', 'denied', 'not_determined', 'unavailable']);

const desktopNotificationsInvoke = (): ShellInvoke | null => {
  if (!canConfigureDesktopNotifications()) return null;
  const internals = (window as unknown as { __TAURI_INTERNALS__?: { invoke?: ShellInvoke } }).__TAURI_INTERNALS__;
  return internals?.invoke ? internals.invoke.bind(internals) : null;
};

export function desktopNotificationsActions(): DesktopNotificationsActions | null {
  const invoke = desktopNotificationsInvoke();
  if (!invoke) return null;
  return {
    get: async () => {
      const value = await invoke('desktop_notifications_state');
      if (typeof value !== 'boolean') throw new Error('Desktop notification preference is unavailable');
      return value;
    },
    set: async (enabled) => {
      const value = await invoke('desktop_notifications_set_enabled', { enabled });
      if (typeof value !== 'boolean') throw new Error('Desktop notification preference was not confirmed');
      return value;
    },
    authorization: async () => {
      const value = await invoke('desktop_notifications_authorization');
      if (!AUTHORIZATIONS.has(value as DesktopNotificationAuthorization)) throw new Error('Desktop notification authorization is unavailable');
      return value as DesktopNotificationAuthorization;
    },
    openSystemSettings: async () => {
      await invoke('desktop_notifications_open_settings');
    },
  };
}

export function listenDesktopNotifications(handler: (enabled: boolean) => void): () => void {
  const listener = (event: Event) => {
    const detail = (event as CustomEvent<{ enabled?: unknown }>).detail;
    if (detail && typeof detail.enabled === 'boolean') handler(detail.enabled);
  };
  window.addEventListener(DESKTOP_NOTIFICATIONS_EVENT, listener);
  return () => window.removeEventListener(DESKTOP_NOTIFICATIONS_EVENT, listener);
}

type ShellInvoke = (command: string, args?: Record<string, unknown>) => Promise<unknown>;
type ShellTransformCallback = (callback: (event: unknown) => void, once?: boolean) => number;

export type DesktopAppWindowTarget = {
  appId: string;
  sessionId?: string | null;
  title?: string;
  newWindow: boolean;
};

const desktopAppWindowInvoke = (): ShellInvoke | null => {
  const supported = isDesktopShell() && window.__AVIBE_DESKTOP_APP_WINDOWS__ === true;
  if (!supported) return null;
  const internals = (window as unknown as { __TAURI_INTERNALS__?: { invoke?: ShellInvoke } }).__TAURI_INTERNALS__;
  return internals?.invoke ? internals.invoke.bind(internals) : null;
};

/**
 * Ask the native shell to open a validated standalone app page. A missing
 * marker, old shell, or rejected command is a compatibility signal: callers
 * must continue with the existing in-Workbench WindowManager path.
 */
export async function tryOpenDesktopAppWindow(target: DesktopAppWindowTarget): Promise<boolean> {
  const invoke = desktopAppWindowInvoke();
  if (!invoke) return false;
  try {
    await invoke('open_app_window', {
      request: {
        appId: target.appId,
        sessionId: target.sessionId ?? null,
        title: target.title,
        newWindow: target.newWindow,
      },
    });
    return true;
  } catch {
    return false;
  }
}

const desktopFocusInvoke = (): ShellInvoke | null => {
  const supported = isDesktopShell() && window.__AVIBE_DESKTOP_FOCUS__ === true;
  if (!supported) return null;
  const internals = (window as unknown as { __TAURI_INTERNALS__?: { invoke?: ShellInvoke } }).__TAURI_INTERNALS__;
  return internals?.invoke ? internals.invoke.bind(internals) : null;
};

/** Whether this shell can bring its own window forward; older shells and browsers cannot. */
export function canBringDesktopAppForward(): boolean {
  return desktopFocusInvoke() !== null;
}

/**
 * Ask the shell to show and focus its main window. The shell does nothing when
 * the window is already in front, and asks for attention instead where the
 * platform refuses a background activation. A refusal here (a remote instance's
 * page, an older shell) is not an error the user needs to see.
 */
export async function bringDesktopAppForward(): Promise<void> {
  const invoke = desktopFocusInvoke();
  if (!invoke) return;
  try {
    await invoke('focus_main_window');
  } catch {
    // Not granted on this page: the browser tab still tells the user they can return.
  }
}

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
  /** Whether the shell offers pre-releases too; absent from shells without the Workbench switch. */
  prereleases?: boolean;
  skippedVersion: string | null;
  interactive: boolean;
  /** When the last check finished (RFC 3339); absent from shells that do not report it. */
  checkedAt?: string | null;
  /** Install found versions when idle, without asking; absent from shells without the setting. */
  autoUpdate?: boolean;
  /** Updater commands this shell takes from the Workbench (`check`, `prereleases`, `auto_update`). */
  hostActions?: string[];
  remoteInstallAllowed?: boolean;
  remoteRequest?: {
    requestId: string;
    target: string;
    phase:
      | 'requested'
      | 'acknowledged'
      | 'checking'
      | 'awaiting_host'
      | 'downloading'
      | 'installing'
      | 'restarting'
      | 'completed'
      | 'failed'
      | 'declined'
      | 'expired';
    progress: number | null;
    errorCode: string | null;
    updatedAt: string;
  } | null;
};

const desktopUpdateInvoke = (): ShellInvoke | null => {
  if (!isDesktopShell()) return null;
  const internals = (window as unknown as { __TAURI_INTERNALS__?: { invoke?: ShellInvoke } }).__TAURI_INTERNALS__;
  return internals?.invoke ? internals.invoke.bind(internals) : null;
};

/** The App Lock commands, when this Workbench's shell offers them. */
export function desktopAppLockInvoke(): ShellInvoke | null {
  if (typeof window === 'undefined' || window.__AVIBE_DESKTOP_APP_LOCK__ !== true) return null;
  return desktopUpdateInvoke();
}

export const DESKTOP_OPEN_UPDATES_EVENT = 'avibe:desktop-open-updates';
export const DESKTOP_UPDATE_STATE_EVENT = 'avibe:desktop-update-state';

type DesktopUpdateStateEvent = {
  payload: unknown;
};

type DesktopUpdateStateListener = (state: DesktopUpdateState) => void;

/** The desktop updater controls available to the Workbench's version popover. */
export type DesktopUpdateShellActions = {
  getState: () => Promise<DesktopUpdateState>;
  /** Notify the shell only after the panel listener can consume an open event. */
  panelReady: () => Promise<void>;
  check: () => Promise<void>;
  /** Install exactly the version the panel shows; an older shell ignores the argument. */
  install: (targetVersion: string) => Promise<void>;
  skip: () => Promise<void>;
  declineRemoteRequest: () => Promise<void>;
  /** Offer pre-releases (the shell's `test` channel) as well as stable releases. */
  setPrereleases: (enabled: boolean) => Promise<void>;
  setAutoUpdate: (enabled: boolean) => Promise<void>;
};

export function desktopUpdateShellActions(): DesktopUpdateShellActions | null {
  const invoke = desktopUpdateInvoke();
  if (!invoke) return null;
  return {
    getState: async () => (await invoke('desktop_update_state')) as DesktopUpdateState,
    panelReady: async () => { await invoke('desktop_update_panel_ready'); },
    check: async () => { await invoke('desktop_update_check'); },
    install: async (targetVersion) => { await invoke('desktop_update_install', { targetVersion }); },
    skip: async () => { await invoke('desktop_update_skip'); },
    declineRemoteRequest: async () => { await invoke('desktop_update_remote_decline'); },
    setPrereleases: async (enabled) => { await invoke('desktop_update_prereleases_set', { enabled }); },
    setAutoUpdate: async (enabled) => { await invoke('desktop_update_auto_update_set', { enabled }); },
  };
}

export type DesktopRemoteInstallActions = {
  get: () => Promise<boolean>;
  set: (enabled: boolean) => Promise<boolean>;
};

/**
 * The remote-install preference is shell-owned. The command probe is also the
 * compatibility boundary: Web pages and older shells simply have no setting.
 */
export function desktopRemoteInstallActions(): DesktopRemoteInstallActions | null {
  const invoke = desktopUpdateInvoke();
  if (!invoke) return null;
  return {
    get: async () => {
      const value = await invoke('desktop_remote_install_allowed');
      if (typeof value !== 'boolean') throw new Error('Remote install preference is unavailable');
      return value;
    },
    set: async (enabled) => {
      const value = await invoke('desktop_remote_install_set', { enabled });
      if (typeof value !== 'boolean') throw new Error('Remote install preference was not confirmed');
      return value;
    },
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
  /** `null` on a shell that cannot turn Computer Use off from the page. */
  disable: (() => Promise<void>) | null;
  openPermission: (pane: ComputerUsePermissionPane) => Promise<void>;
} | null {
  const invoke = computerUseInvoke();
  if (!invoke) return null;
  return {
    enable: async () => { await invoke('computer_use_enable'); },
    disable: window.__AVIBE_DESKTOP_COMPUTER_USE_DISABLE__ === true
      ? async () => { await invoke('computer_use_disable'); }
      : null,
    openPermission: async (pane) => { await invoke('computer_use_open_permission', { pane }); },
  };
}
