import { isDesktopShell } from '@/lib/desktopShell';
import { openLinkInNewContext } from '@/lib/pwaNavigation';

/** Native window/layout transport. pet_ready still parses legacy persisted
 * bindings for backwards compatibility; only GET /api/sessions/main selects
 * the conversation. The only binding write normalizes native storage to main. */

export type PetIntent = 'listen' | 'show' | 'release';

export type PetLayout = {
  panel_side: 'left' | 'right';
  panel_edge: 'top' | 'bottom';
  /** How far (CSS px) the column moves right (negative: left) of its place
   * centred on Vibey, so a pet near a display's side opens it whole there. */
  column_offset: number;
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
/** The shell resized the window while hiding it, `{ token, height }`: the page
 *  answers once it has drawn at that height, and the shell shows it again. */
export const PET_FRAME_EVENT = 'avibe:pet-frame';

/** A window-local rect that takes the mouse; `hover` marks Vibey's hover zone. */
export type PetHitRect = { x: number; y: number; width: number; height: number; hover?: boolean };

// Outside the shell, `{ binding, revision }` as JSON: the dev stand-in for
// `pet.json` and the shell's revision counter.
const DEV_BINDING_KEY = 'avibe.pet.devBinding';
const DEFAULT_LAYOUT: PetLayout = { panel_side: 'left', panel_edge: 'bottom', column_offset: 0 };

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
    // Older shells send none: the column stays centred on Vibey.
    column_offset: typeof layout?.column_offset === 'number' && Number.isFinite(layout.column_offset) ? Math.round(layout.column_offset) : 0,
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

  /** Clears native press-to-toggle state when the page cancels a conversation. */
  resetVoiceShortcut: async (): Promise<void> => {
    const invoke = shellInvoke();
    if (!invoke) return;
    await invoke('pet_reset_voice_shortcut');
  },

  /** Opens the native pet context menu inside the desktop shell. */
  contextMenu: async (): Promise<void> => {
    const invoke = shellInvoke();
    if (!invoke) return;
    await invoke('pet_context_menu');
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
  /** Where the pointer is in this window and whether the primary button is
   * down, as the shell sees it; `null` from a shell that cannot say. */
  pointer: async (): Promise<{ x: number; y: number; down: boolean | null } | null> => {
    const invoke = shellInvoke();
    if (!invoke) return null;
    const value = await invoke('pet_pointer') as { x?: unknown; y?: unknown; down?: unknown } | null;
    if (!value || typeof value.x !== 'number' || typeof value.y !== 'number') return null;
    return { x: value.x, y: value.y, down: typeof value.down === 'boolean' ? value.down : null };
  },

  /** `press`: where the press was, in the window's CSS px; the shell keeps
   * that point of Vibey under the pointer for the whole drag. */
  drag: async (press?: { x: number; y: number }): Promise<void> => {
    const invoke = shellInvoke();
    if (!invoke) return;
    if (!petBridge.shellDrags()) {
      void invoke('plugin:window|start_dragging');
      return;
    }
    await invoke('pet_drag', press ? { x: press.x, y: press.y } : undefined);
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

/** Answer each resize once a frame has been drawn at its height: two
 *  animation frames after the viewport reached it (the first may still paint
 *  the old size). Returns the unsubscribe function. */
export const answerPetResizes = (): (() => void) => {
  let pending: { token: number; height: number } | null = null;
  const drawn = (token: number) => requestAnimationFrame(() => requestAnimationFrame(() => {
    void shellInvoke()?.('pet_frame_drawn', { token }).catch(() => undefined);
  }));
  const check = () => {
    if (!pending || Math.abs(window.innerHeight - pending.height) >= 1) return;
    const { token } = pending;
    pending = null;
    drawn(token);
  };
  const frame = (event: Event) => {
    const detail = (event as CustomEvent<{ token?: unknown; height?: unknown }>).detail;
    if (typeof detail?.token !== 'number' || typeof detail?.height !== 'number') return;
    pending = { token: detail.token, height: detail.height };
    check();
  };
  window.addEventListener(PET_FRAME_EVENT, frame);
  window.addEventListener('resize', check);
  return () => {
    window.removeEventListener(PET_FRAME_EVENT, frame);
    window.removeEventListener('resize', check);
  };
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
