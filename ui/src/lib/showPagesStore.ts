import type { ApiContextType } from '../context/ApiContext';

export type Visibility = 'private' | 'public' | 'offline';

export interface ShowPage {
  session_id: string;
  visibility: Visibility;
  title: string | null;
  platform: string | null;
  agent: string | null;
  path: string;
  /** Opaque cache token for the page's own HTML icon (§7.1f): non-null iff a
   *  servable icon exists, and it changes when the icon file changes. Doubles as
   *  the has-icon signal and is appended to the icon URL as `?v=<token>`. */
  icon_version: string | null;
  active_url: string | null;
  private_url: string | null;
  public_url: string | null;
  url_available: boolean;
  url_guidance?: string | null;
  share_id: string | null;
  offline: boolean;
  offline_at: string | null;
  created_at: string;
  updated_at: string;
}

export type ShowPagePatch = Pick<ShowPage, 'session_id'> & Partial<ShowPage>;

export interface ShowPagesInventorySnapshot {
  pages: ShowPage[];
  loading: boolean;
  loaded: boolean;
}

export type ShowPagesInventoryApi = Pick<
  ApiContextType,
  'getShowPages' | 'connectWorkbenchEvents'
>;

type Listener = () => void;

export function replaceShowPageTitleIfCurrent(
  pages: ShowPage[],
  sessionId: string,
  expectedTitle: string | null,
  nextTitle: string | null,
): ShowPage[] {
  const index = pages.findIndex(
    (page) => page.session_id === sessionId && page.title === expectedTitle,
  );
  if (index < 0) return pages;
  const next = [...pages];
  next[index] = { ...next[index], title: nextTitle };
  return next;
}

// One store is shared by every inventory projection under an ApiProvider. It
// retains its last snapshot between panel mounts, while activation only owns one
// workbench-events subscription and every refresh joins the same in-flight work.
export class ShowPagesInventoryStore {
  private readonly api: ShowPagesInventoryApi;
  private snapshot: ShowPagesInventorySnapshot = {
    pages: [],
    loading: false,
    loaded: false,
  };
  private readonly listeners = new Set<Listener>();
  private activeConsumers = 0;
  private disconnectEvents: (() => void) | null = null;
  private inFlight: Promise<void> | null = null;
  private revision = 0;

  constructor(api: ShowPagesInventoryApi) {
    this.api = api;
  }

  getSnapshot = (): ShowPagesInventorySnapshot => this.snapshot;

  subscribe = (listener: Listener): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  activate = (): (() => void) => {
    this.activeConsumers += 1;
    if (this.activeConsumers === 1) this.connectEvents();

    // Every newly visible projection revalidates, but simultaneous activations
    // share one request. The retained snapshot remains readable while it runs.
    void this.reload();

    let active = true;
    return () => {
      if (!active) return;
      active = false;
      this.activeConsumers -= 1;
      if (this.activeConsumers === 0) {
        this.disconnectEvents?.();
        this.disconnectEvents = null;
      }
    };
  };

  reload = (): Promise<void> => {
    if (this.inFlight) return this.inFlight;
    this.updateSnapshot({ loading: true });
    this.inFlight = this.fetchCurrentRevision();
    return this.inFlight;
  };

  mergePage = (next: ShowPagePatch): void => {
    this.revision += 1;
    this.updateSnapshot({
      pages: this.snapshot.pages.map((page) =>
        page.session_id === next.session_id ? { ...page, ...next } : page,
      ),
    });
  };

  removePage = (sessionId: string): void => {
    this.revision += 1;
    this.updateSnapshot({
      pages: this.snapshot.pages.filter((page) => page.session_id !== sessionId),
    });
  };

  replaceTitleIfCurrent = (
    sessionId: string,
    expectedTitle: string | null,
    nextTitle: string | null,
  ): void => {
    this.revision += 1;
    this.updateSnapshot({
      pages: replaceShowPageTitleIfCurrent(
        this.snapshot.pages,
        sessionId,
        expectedTitle,
        nextTitle,
      ),
    });
  };

  private updateSnapshot(patch: Partial<ShowPagesInventorySnapshot>): void {
    const next = { ...this.snapshot, ...patch };
    if (
      next.pages === this.snapshot.pages &&
      next.loading === this.snapshot.loading &&
      next.loaded === this.snapshot.loaded
    ) {
      return;
    }
    this.snapshot = next;
    this.listeners.forEach((listener) => listener());
  }

  private async fetchCurrentRevision(): Promise<void> {
    try {
      // A mutation that lands during the read invalidates that response. Keep
      // the same single-flight promise alive and reconcile again so no stale
      // response can undo an optimistic or events-driven update.
      while (true) {
        const revision = this.revision;
        try {
          const res = (await this.api.getShowPages()) as { pages?: unknown };
          if (revision !== this.revision) continue;
          this.updateSnapshot({
            pages: Array.isArray(res.pages) ? (res.pages as ShowPage[]) : [],
            loaded: true,
          });
          return;
        } catch {
          if (revision !== this.revision) continue;
          this.updateSnapshot({ loaded: true });
          return;
        }
      }
    } finally {
      this.inFlight = null;
      this.updateSnapshot({ loading: false });
    }
  }

  private invalidateAndReload(): void {
    // An event can arrive after the server produced the response currently in
    // flight. Advancing the revision makes that response retry inside the same
    // single-flight promise instead of either accepting it or starting overlap.
    this.revision += 1;
    void this.reload();
  }

  private connectEvents(): void {
    if (this.disconnectEvents) return;
    let connected = false;
    this.disconnectEvents = this.api.connectWorkbenchEvents({
      onConnected: () => {
        // activate() already revalidates this subscription's initial connection.
        // Only later callbacks are reconnects that may cover a missed event gap.
        if (!connected) {
          connected = true;
          return;
        }
        this.invalidateAndReload();
      },
      onSessionActivity: (data) => {
        const hasPage = this.snapshot.pages.some(
          (page) => page.session_id === data.session_id,
        );
        if (data.event === 'archived') {
          if (hasPage) this.removePage(data.session_id);
          else if (!this.snapshot.loaded) void this.reload();
          return;
        }
        if (
          data.event === 'updated' &&
          Object.prototype.hasOwnProperty.call(data, 'title')
        ) {
          if (hasPage) {
            this.mergePage({
              session_id: data.session_id,
              title: data.title ?? null,
            });
          } else if (!this.snapshot.loaded) {
            void this.reload();
          }
          return;
        }
        // Runtime Show activity can materialize a page outside this browser.
        // Normal session/user-message events do not change this inventory.
        if (data.event === 'show_event') this.invalidateAndReload();
      },
    });
  }
}

const stores = new WeakMap<ApiContextType, ShowPagesInventoryStore>();

export function getShowPagesInventoryStore(api: ApiContextType): ShowPagesInventoryStore {
  let store = stores.get(api);
  if (!store) {
    store = new ShowPagesInventoryStore(api);
    stores.set(api, store);
  }
  return store;
}
