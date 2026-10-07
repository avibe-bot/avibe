import { isDesktopShell } from '@/lib/desktopShell';
import { openLinkInNewContext } from '@/lib/pwaNavigation';

/**
 * The desktop pet's channel to the native shell. This file is the contract the
 * shell implements (docs/plans/2026-10-01-desktop-pet.md, "Shell ↔ pet IPC"):
 *
 * Page → shell, Tauri commands granted only to the `pet` window:
 *   pet_ready() -> PetBinding & { summon_pending: { intent } | null }
 *   pet_set_expanded({ expanded }) -> PetLayout
 *   pet_bind({ sessionId }) -> PetBinding & { shown: boolean }
 *   pet_unbind({ sessionId }) -> PetBinding   compare-and-clear
 *   pet_open({ link })                   any link the deep-link parser accepts
 *   plugin:window|start_dragging         the `start-dragging` permission
 *
 * Shell → page, DOM events dispatched on `window` by a shell-evaluated script
 * (the same channel as the Settings… menu, so no event permission is needed):
 *   avibe:pet-summon  detail { intent }  only after pet_ready(); earlier summons
 *                                        are returned by pet_ready() instead
 *   avibe:pet-bound   detail { session_id: string | null, revision }
 *
 * `revision` is the shell's binding revision: it grows by one on every change
 * to the binding, and every answer and event carries the revision it reflects.
 * Answers and events travel on different channels and may arrive out of
 * order, so the page applies only a revision newer than the last it applied.
 *
 * Outside the shell (a browser tab during development) the bridge keeps the
 * binding in localStorage and lays the panel out to the right, so the route
 * stays usable without the native window.
 */

export type PetIntent = 'listen' | 'show';

export type PetLayout = {
  panel_side: 'left' | 'right';
  panel_edge: 'top' | 'bottom';
};

/** The shell's binding, and the revision it was at. */
export type PetBinding = {
  binding: string | null;
  revision: number;
};

export type PetReady = PetBinding & {
  summon_pending: { intent: PetIntent } | null;
};

export const PET_SUMMON_EVENT = 'avibe:pet-summon';
export const PET_BOUND_EVENT = 'avibe:pet-bound';

// Outside the shell, `{ binding, revision }` as JSON: the dev stand-in for
// `pet.json` and the shell's revision counter.
const DEV_BINDING_KEY = 'avibe.pet.devBinding';
const DEFAULT_LAYOUT: PetLayout = { panel_side: 'left', panel_edge: 'bottom' };

type TauriInternals = { invoke: (command: string, args?: Record<string, unknown>) => Promise<unknown> };

const shellInvoke = (): TauriInternals['invoke'] | null => {
  if (!isDesktopShell()) return null;
  const internals = (window as unknown as { __TAURI_INTERNALS__?: TauriInternals }).__TAURI_INTERNALS__;
  return internals?.invoke ? internals.invoke.bind(internals) : null;
};

const isIntent = (value: unknown): value is PetIntent => value === 'listen' || value === 'show';

const parseBinding = (value: unknown): PetBinding => {
  const result = value as { binding?: unknown; revision?: unknown } | null;
  const revision = result?.revision;
  if (typeof revision !== 'number' || !Number.isInteger(revision) || revision < 0) {
    throw new Error('pet shell answered without a binding revision');
  }
  return {
    binding: typeof result?.binding === 'string' && result.binding ? result.binding : null,
    revision,
  };
};

let devMemory: PetBinding = { binding: null, revision: 0 };

const readDevBinding = (): PetBinding => {
  try {
    const stored = window.localStorage.getItem(DEV_BINDING_KEY);
    if (stored) devMemory = parseBinding(JSON.parse(stored));
  } catch {
    /* storage unavailable or unreadable: keep this page's own copy */
  }
  return devMemory;
};

const writeDevBinding = (sessionId: string | null): PetBinding => {
  devMemory = { binding: sessionId, revision: readDevBinding().revision + 1 };
  try {
    window.localStorage.setItem(DEV_BINDING_KEY, JSON.stringify(devMemory));
  } catch {
    /* storage unavailable: the binding lasts for this page only */
  }
  return devMemory;
};

export const petBridge = {
  /** True inside the native pet window. */
  native: (): boolean => shellInvoke() !== null,

  ready: async (): Promise<PetReady> => {
    const invoke = shellInvoke();
    if (!invoke) return { ...readDevBinding(), summon_pending: null };
    const result = (await invoke('pet_ready')) as { summon_pending?: { intent?: unknown } | null } | null;
    const pending = result?.summon_pending;
    return {
      ...parseBinding(result),
      summon_pending: pending && isIntent(pending.intent) ? { intent: pending.intent } : null,
    };
  },

  setExpanded: async (expanded: boolean): Promise<PetLayout> => {
    const invoke = shellInvoke();
    if (!invoke) return DEFAULT_LAYOUT;
    const layout = (await invoke('pet_set_expanded', { expanded })) as Partial<PetLayout> | null;
    return {
      panel_side: layout?.panel_side === 'right' ? 'right' : 'left',
      panel_edge: layout?.panel_edge === 'top' ? 'top' : 'bottom',
    };
  },

  bind: async (sessionId: string): Promise<PetBinding> => {
    const invoke = shellInvoke();
    if (!invoke) return writeDevBinding(sessionId);
    return parseBinding(await invoke('pet_bind', { sessionId }));
  },

  /** Clears the binding only if it is still `sessionId`. */
  unbind: async (sessionId: string): Promise<PetBinding> => {
    const invoke = shellInvoke();
    if (!invoke) {
      const current = readDevBinding();
      return current.binding === sessionId ? writeDevBinding(null) : current;
    }
    return parseBinding(await invoke('pet_unbind', { sessionId }));
  },

  /** Moves the native window with the pointer (`start-dragging`). */
  startDragging: (): void => {
    void shellInvoke()?.('plugin:window|start_dragging');
  },

  /** Opens an `avibe://` link in the main window. */
  open: async (link: string): Promise<void> => {
    const invoke = shellInvoke();
    if (invoke) {
      await invoke('pet_open', { link });
      return;
    }
    openLinkInNewContext(devPathForLink(link), 'noopener');
  },
};

/** The Workbench path for a deep link, used only outside the shell. */
export const devPathForLink = (link: string): string => {
  const match = /^avibe:\/\/(session|show)\/([^/?#]+)$/.exec(link);
  if (match) return `${match[1] === 'session' ? '/chat' : '/apps/show'}/${match[2]}`;
  const vault = /^avibe:\/\/vaults\/request\/([^/?#]+)$/.exec(link);
  if (vault) return `/vaults?request_id=${vault[1]}`;
  return '/settings/general';
};

export const sessionLink = (sessionId: string): string => `avibe://session/${sessionId}`;
export const vaultRequestLink = (requestId: string): string => `avibe://vaults/request/${requestId}`;
export const SETTINGS_LINK = 'avibe://settings';

/** Subscribe to shell events; returns the unsubscribe function. */
export const onPetEvents = (handlers: {
  summon?: (intent: PetIntent) => void;
  bound?: (binding: PetBinding) => void;
}): (() => void) => {
  const summon = (event: Event) => {
    const intent = (event as CustomEvent<{ intent?: unknown }>).detail?.intent;
    handlers.summon?.(isIntent(intent) ? intent : 'show');
  };
  const bound = (event: Event) => {
    const detail = (event as CustomEvent<{ session_id?: unknown; revision?: unknown }>).detail;
    let binding: PetBinding;
    try {
      binding = parseBinding({ binding: detail?.session_id, revision: detail?.revision });
    } catch {
      return; // Without a revision the event cannot be ordered, so it is not applied.
    }
    handlers.bound?.(binding);
  };
  window.addEventListener(PET_SUMMON_EVENT, summon);
  window.addEventListener(PET_BOUND_EVENT, bound);
  return () => {
    window.removeEventListener(PET_SUMMON_EVENT, summon);
    window.removeEventListener(PET_BOUND_EVENT, bound);
  };
};
