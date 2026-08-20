import type { ApiContextType } from '../context/ApiContext';

export type Visibility = 'private' | 'limited' | 'public' | 'offline';

export interface ShowPage {
  session_id: string;
  visibility: Visibility;
  access_mode: 'private' | 'limited' | 'public';
  access_revision: number;
  can_manage: boolean;
  can_publish_public: boolean;
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
  private retired = false;

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
    this.syncEventSubscription();

    // Every newly visible projection revalidates, but simultaneous activations
    // share one request. The retained snapshot remains readable while it runs.
    void this.reload();

    let active = true;
    return () => {
      if (!active) return;
      active = false;
      this.activeConsumers -= 1;
      this.syncEventSubscription();
    };
  };

  // The subscription is keyed on whether there is anything to protect, not on
  // whether anyone is reading. Those are different questions: a consumer needs
  // revalidation, but a RETAINED snapshot needs invalidation — and a store that
  // unsubscribed when its last window closed cannot hear the revocation it would
  // have to act on. Reopening then renders the retained pages synchronously
  // (titles, paths, share URLs) while ``activate()``'s reload is still in flight,
  // and ``fetchCurrentRevision`` keeps them when that reload fails, so revoked
  // metadata can stay on screen indefinitely.
  //
  // A read in flight counts: it will produce a snapshot, so the invalidation
  // window has to stay open across it. This adds no request on any route — while
  // no consumer reads the inventory the subscription handles invalidation only
  // (below), and the shared events stream is already open for the shell-wide
  // badges wherever a snapshot can exist.
  private shouldWatchEvents(): boolean {
    return this.activeConsumers > 0 || this.snapshot.pages.length > 0 || this.inFlight !== null;
  }

  private syncEventSubscription(): void {
    if (!this.retired && this.shouldWatchEvents()) {
      this.connectEvents();
      return;
    }
    this.disconnectEvents?.();
    this.disconnectEvents = null;
  }

  // A newer ApiContext identity has superseded this store (see the factory
  // below). "Temporarily unread" and "unreachable" are different states, and
  // only the retained-snapshot rule above makes the difference matter: keeping a
  // subscription alive to protect a snapshot is right while a consumer can still
  // render it, and a leak once none can — the registration is what keeps the
  // store, its pages and the old api value alive, so every later identity change
  // would strand another one.
  //
  // The consumers that could reach it are switching to the new store in this same
  // commit, so there is nothing left to protect now rather than later. Fencing
  // the read in flight matters as much as the disconnect: without it the response
  // would repopulate `pages` and resubscribe through the ``finally`` below.
  retire = (): void => {
    this.retired = true;
    this.revision += 1;
    this.syncEventSubscription();
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

  // Access to Show Pages was revoked or re-granted. Advancing the revision fences
  // any read already in flight — the single-flight loop discards that response and
  // reconciles again — and the snapshot drops to pre-first-read state, so neither
  // the consumer reading it now nor the next one to reopen can render revoked
  // titles, paths or share URLs while a replacement read is slow, or after one
  // fails. Stronger than revalidating, which depended on a fetch succeeding.
  private discardAuthorizedPages(): void {
    this.revision += 1;
    this.updateSnapshot({ pages: [], loaded: false });
    this.syncEventSubscription();
  }

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
          // Retirement fences the read through the same revision bump a mutation
          // uses, so it has to be distinguished here: a mutation wants the read
          // repeated, a disposal wants it abandoned.
          if (this.retired) return;
          if (revision !== this.revision) continue;
          this.updateSnapshot({
            pages: Array.isArray(res.pages) ? (res.pages as ShowPage[]) : [],
            loaded: true,
          });
          return;
        } catch {
          if (this.retired) return;
          if (revision !== this.revision) continue;
          this.updateSnapshot({ loaded: true });
          return;
        }
      }
    } finally {
      this.inFlight = null;
      this.updateSnapshot({ loading: false });
      // A consumer may have detached mid-read; now that the snapshot is settled
      // it is decidable whether anything is left to keep watching for.
      this.syncEventSubscription();
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
    let handshook = false;
    this.disconnectEvents = this.api.connectWorkbenchEvents({
      onConnected: (data) => {
        // Keyed off what the signal says, not off how many times it has been
        // called. `null` is a gap declared with no handshake behind it, so it is
        // never this subscription's initial connection however early it arrives
        // -- and it can arrive first, because a reactivation announces the gap
        // itself rather than waiting on the stream it is replacing. Counting
        // calls instead would spend that first announcement on a read
        // activate() had already done, and leave the real gap unreconciled
        // until a handshake that may never come.
        if (data && !handshook) {
          // activate() already revalidates this subscription's initial
          // connection; only a later one is a reconnect covering a gap.
          handshook = true;
          return;
        }
        // Revalidation, so it may wait for a consumer: activation re-reads
        // anyway. Only invalidation has to act with nobody reading.
        if (this.activeConsumers === 0) return;
        this.invalidateAndReload();
      },
      onSessionActivity: (data) => {
        // Same rule: keeping a retained snapshot merged is revalidation, and the
        // reload below would be a request on a route that reads nothing.
        if (this.activeConsumers === 0) return;
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
      // Invalidation, so unlike the two above it is not the consumer count that
      // decides whether it acts — only whether a replacement READ follows.
      // Revalidating for an active consumer left the revoked pages in the
      // snapshot until the re-read landed, and ``fetchCurrentRevision``
      // deliberately keeps them when it fails, so a failed replacement kept
      // revoked titles, paths and share URLs readable indefinitely.
      onAuthorizationChanged: () => {
        this.discardAuthorizedPages();
        if (this.activeConsumers > 0) void this.reload();
      },
    });
  }
}

const stores = new WeakMap<ApiContextType, ShowPagesInventoryStore>();

// The store the mounted tree has committed to. One slot, because a document has
// one ``ApiProvider``: a new context identity (its value is memoized on ``t``, so
// a locale switch rebuilds it) means the previous store became unreachable, not
// that a second one went live.
let committed: { api: ApiContextType; store: ShowPagesInventoryStore } | null = null;

export function getShowPagesInventoryStore(api: ApiContextType): ShowPagesInventoryStore {
  let store = stores.get(api);
  if (!store) {
    store = new ShowPagesInventoryStore(api);
    stores.set(api, store);
  }
  return store;
}

/** Record the identity the tree has committed to, retiring the store a previous
 *  identity handed out.
 *
 *  Disposal belongs to the commit phase, not to the render that creates the new
 *  store: a re-render can be double-invoked or discarded, and closing the
 *  subscription of a store the mounted tree is still reading would lose the
 *  invalidation it is kept open for. Every consumer of a changed context value
 *  re-renders in one commit, so by the time any of their effects runs, all of
 *  them hold this store — which makes the call idempotent and its ordering among
 *  them irrelevant. A document with no inventory consumer never calls it and
 *  never needs to: nothing was created, so nothing was superseded.
 */
export function commitShowPagesInventoryStore(api: ApiContextType): void {
  if (committed?.api === api) return;
  if (committed) {
    // Dropping the WeakMap entry with it keeps the factory from ever handing a
    // retired store to a consumer, should an identity somehow come back.
    stores.delete(committed.api);
    committed.store.retire();
  }
  committed = { api, store: getShowPagesInventoryStore(api) };
}
