import { isDesktopShell } from '@/lib/desktopShell';
import { openLinkInNewContext } from '@/lib/pwaNavigation';

/** Native window/layout transport. pet_ready still parses legacy persisted
 * bindings for backwards compatibility; only GET /api/sessions/main selects
 * the conversation. The only binding write normalizes native storage to main. */

export type PetIntent = 'listen' | 'show' | 'release';

export type PetLayout = {
  panel_side: 'left' | 'right';
  panel_edge: 'top' | 'bottom';
};

/** The shell's binding, and the revision it was at. */
export type PetBinding = {
  binding: string | null;
  revision: number;
};

/** The layout to draw and its revision; the shell is its only source. */
export type LayoutSnapshot = {
  revision: number;
  layout: PetLayout;
};

export type PetReady = PetBinding & {
  summon_pending: { intent: PetIntent } | null;
  /** Only shells that drag the pet send one. */
  snapshot?: LayoutSnapshot;
};

export const PET_SUMMON_EVENT = 'avibe:pet-summon';
export const PET_BOUND_EVENT = 'avibe:pet-bound';
export const PET_HOVER_EVENT = 'avibe:pet-hover';
/** The shell's layout snapshot `{ revision, layout }`: the page draws it. */
export const PET_LAYOUT_EVENT = 'avibe:pet-layout';

/** A window-local rect that takes the mouse; `hover` marks Vibey's hover zone. */
export type PetHitRect = { x: number; y: number; width: number; height: number; hover?: boolean };

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

const isIntent = (value: unknown): value is PetIntent => value === 'listen' || value === 'show' || value === 'release';

const parseLayout = (value: unknown): PetLayout => {
  const layout = value as Partial<PetLayout> | null;
  return {
    panel_side: layout?.panel_side === 'right' ? 'right' : 'left',
    panel_edge: layout?.panel_edge === 'top' ? 'top' : 'bottom',
  };
};

const parseSnapshot = (value: unknown): LayoutSnapshot | null => {
  const snapshot = value as { revision?: unknown; layout?: unknown } | null;
  const revision = snapshot?.revision;
  return typeof revision === 'number' && Number.isInteger(revision) && revision >= 0
    ? { revision, layout: parseLayout(snapshot?.layout) }
    : null;
};

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
// Whether this document's `pet_ready` has succeeded: only then does the shell
// know it draws the top layout, and only then may the shell drag it.
let shellKnowsLayouts = false;

const readDevBinding = (): PetBinding => {
  try {
    const stored = window.localStorage.getItem(DEV_BINDING_KEY);
    if (stored) devMemory = parseBinding(JSON.parse(stored));
  } catch {
    /* storage unavailable or unreadable: keep this page's own copy */
  }
  return devMemory;
};

export const petBridge = {
  /** True inside the native pet window. */
  native: (): boolean => shellInvoke() !== null,

  /**
   * Whether the shell drags the pet and owns its layout. It says so before
   * any page script runs (`__AVIBE_PET_DRAG__`); older shells have no marker.
   */
  shellOwnsLayout: (): boolean => shellInvoke() !== null
    && (window as unknown as { __AVIBE_PET_DRAG__?: unknown }).__AVIBE_PET_DRAG__ === true,

  /**
   * Whether `drag` goes through the shell, which shrinks the window to Vibey:
   * only once this page's `pet_ready` has told the shell the page draws the
   * top layout. Until then a press drags the window natively.
   */
  shellDrags: (): boolean => shellKnowsLayouts && petBridge.shellOwnsLayout(),

  ready: async (): Promise<PetReady> => {
    const invoke = shellInvoke();
    if (!invoke) return { ...readDevBinding(), summon_pending: null };
    // `layouts`: this page draws the column below Vibey (`panel_edge: top`).
    const result = (await invoke('pet_ready', { layouts: true })) as {
      summon_pending?: { intent?: unknown } | null; layout?: unknown; layout_revision?: unknown;
    } | null;
    const pending = result?.summon_pending;
    const snapshot = parseSnapshot({ revision: result?.layout_revision, layout: result?.layout });
    const ready = {
      ...parseBinding(result),
      summon_pending: pending && isIntent(pending.intent) ? { intent: pending.intent } : null,
      ...(snapshot ? { snapshot } : {}),
    };
    shellKnowsLayouts = true;
    return ready;
  },

  setExpanded: async (expanded: boolean): Promise<PetLayout> => {
    const invoke = shellInvoke();
    if (!invoke) return DEFAULT_LAYOUT;
    return parseLayout(await invoke('pet_set_expanded', { expanded }));
  },

  setHitRegions: async (rects: PetHitRect[]): Promise<void> => {
    const invoke = shellInvoke();
    if (!invoke) return;
    await invoke('pet_hit_regions', { rects });
  },

  /** Normalize the legacy binding; callers cannot choose another session. */
  bindMain: async (): Promise<PetBinding> => {
    const invoke = shellInvoke();
    if (invoke) return parseBinding(await invoke('pet_bind', { sessionId: 'ses_main' }));
    devMemory = { binding: 'ses_main', revision: readDevBinding().revision + 1 };
    try {
      window.localStorage.setItem(DEV_BINDING_KEY, JSON.stringify(devMemory));
    } catch {
      // Browser preview without storage keeps the normalized state in memory.
    }
    return devMemory;
  },

  /**
   * Moves the native window with the pointer: through the shell when it drags
   * the pet (resolves once the drop is placed; its layout arrives as a
   * snapshot), else with the window's own `start_dragging`.
   */
  drag: async (): Promise<void> => {
    const invoke = shellInvoke();
    if (!invoke) return;
    if (!petBridge.shellDrags()) {
      void invoke('plugin:window|start_dragging');
      return;
    }
    await invoke('pet_drag');
  },

  /**
   * Tells the shell this page drew `revision`; a frame waiting for it comes
   * back. Answers with the snapshot the page should draw now.
   */
  settle: async (revision: number): Promise<LayoutSnapshot | null> => {
    const invoke = shellInvoke();
    if (!invoke) return null;
    return parseSnapshot(await invoke('pet_settle', { revision }));
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

/** Subscribe to the shell's hover reports; returns the unsubscribe function. */
export const onPetHover = (handler: (hover: boolean) => void): (() => void) => {
  const listener = (event: Event) => handler((event as CustomEvent<{ hover?: unknown }>).detail?.hover === true);
  window.addEventListener(PET_HOVER_EVENT, listener);
  return () => window.removeEventListener(PET_HOVER_EVENT, listener);
};

/** Subscribe to shell events; returns the unsubscribe function. */
export const onPetEvents = (handlers: {
  summon?: (intent: PetIntent) => void;
  layout?: (snapshot: LayoutSnapshot) => void;
}): (() => void) => {
  const summon = (event: Event) => {
    const intent = (event as CustomEvent<{ intent?: unknown }>).detail?.intent;
    handlers.summon?.(isIntent(intent) ? intent : 'show');
  };
  const layout = (event: Event) => {
    const snapshot = parseSnapshot((event as CustomEvent<unknown>).detail);
    if (snapshot) handlers.layout?.(snapshot);
  };
  window.addEventListener(PET_SUMMON_EVENT, summon);
  window.addEventListener(PET_LAYOUT_EVENT, layout);
  return () => {
    window.removeEventListener(PET_SUMMON_EVENT, summon);
    window.removeEventListener(PET_LAYOUT_EVENT, layout);
  };
};
