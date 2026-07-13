import { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';

import { useApi } from '../context/ApiContext';
import { useToast } from '../context/ToastContext';
import type { ShowPageLinkInfo } from '../lib/showPageLinks';

export type Visibility = 'private' | 'public' | 'offline';

export interface ShowPage {
  session_id: string;
  visibility: Visibility;
  title: string | null;
  platform: string | null;
  agent: string | null;
  path: string;
  active_url: string | null;
  private_url: string | null;
  public_url: string | null;
  url_available: boolean;
  share_id: string | null;
  offline: boolean;
  offline_at: string | null;
  created_at: string;
  updated_at: string;
}

type ShowPagePatch = Pick<ShowPage, 'session_id'> & Partial<ShowPage>;
const INVENTORY_REFRESH_EVENTS = new Set(['created', 'user_message', 'show_event']);

// Read-side Show Page inventory shared by the Library, Dock, and global search.
// Session-title edits already publish `session.activity`; subscribe here so each
// mounted projection prefers the live title while `title_snapshot` remains only
// an offline/missing-page fallback.
export function useShowPageInventory(enabled = true) {
  const api = useApi();
  const [pages, setPages] = useState<ShowPage[]>([]);
  const [loading, setLoading] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const loadSeqRef = useRef(0);
  const revisionRef = useRef(0);

  const mergePage = useCallback((next: ShowPagePatch) => {
    revisionRef.current += 1;
    setPages((prev) =>
      prev.map((page) => (page.session_id === next.session_id ? { ...page, ...next } : page)),
    );
  }, []);

  const removePage = useCallback((sessionId: string) => {
    revisionRef.current += 1;
    setPages((prev) => prev.filter((page) => page.session_id !== sessionId));
  }, []);

  const load = useCallback(async () => {
    if (!enabled) return;
    const seq = (loadSeqRef.current += 1);
    const revision = revisionRef.current;
    setLoading(true);
    try {
      const res = (await api.getShowPages()) as { pages?: unknown };
      if (seq !== loadSeqRef.current || revision !== revisionRef.current) return;
      setPages(Array.isArray(res.pages) ? (res.pages as ShowPage[]) : []);
      setLoaded(true);
    } catch {
      if (seq === loadSeqRef.current) setLoaded(true);
    } finally {
      if (seq === loadSeqRef.current) setLoading(false);
    }
  }, [api, enabled]);

  useEffect(() => {
    if (!enabled) return;
    void load();
    return api.connectWorkbenchEvents({
      onConnected: () => void load(),
      onSessionActivity: (data) => {
        if (data.event === 'archived') {
          removePage(data.session_id);
          return;
        }
        if (data.event === 'updated' && Object.prototype.hasOwnProperty.call(data, 'title')) {
          mergePage({ session_id: data.session_id, title: data.title ?? null });
          void load();
          return;
        }
        // A newly ensured Show Page is followed by the prompt's user_message;
        // show_event covers pages first materialized by runtime activity.
        if (INVENTORY_REFRESH_EVENTS.has(data.event)) void load();
      },
    });
  }, [api, enabled, load, mergePage, removePage]);

  const reload = useCallback(() => void load(), [load]);

  return { pages, loading, loaded, mergePage, reload };
}

// The Show Pages inventory: fetch + the visibility / share-id / rotate mutations,
// with their toasts. Lifted out of the view so the App Library owns one copy of
// the pages state and projects it into both the Apps and Show Pages views (kept
// in a hook module so the view file exports only components — fast-refresh safe).
export function useShowPages() {
  const api = useApi();
  const { showToast } = useToast();
  const { t } = useTranslation();
  const { pages, loading, loaded, mergePage, reload } = useShowPageInventory();
  const [busyId, setBusyId] = useState<string | null>(null);

  const setVisibility = async (page: ShowPage, visibility: Visibility) => {
    if (page.visibility === visibility || busyId) return;
    setBusyId(page.session_id);
    try {
      const res = await api.setShowPageVisibility(page.session_id, visibility);
      mergePage(res);
      showToast(t('showPages.toast.updated'));
    } catch {
      // ApiContext surfaces a toast on failure.
    } finally {
      setBusyId(null);
    }
  };

  const rotate = async (page: ShowPage) => {
    if (busyId) return;
    setBusyId(page.session_id);
    try {
      const res = await api.rotateShowPageShare(page.session_id);
      mergePage(res);
      showToast(t('showPages.toast.rotated'));
    } catch {
      // handled by ApiContext
    } finally {
      setBusyId(null);
    }
  };

  // The custom-link field owns its own request/validation; we only merge the
  // returned payload (new share_id, updated_at) and confirm.
  const onShareIdSaved = (next: ShowPageLinkInfo) => {
    mergePage(next as ShowPage);
    showToast(t('showPages.shareId.toast.saved'));
  };

  const rename = async (page: ShowPage, title: string | null) => {
    const previousTitle = page.title;
    const nextTitle = title?.trim() || null;
    if (nextTitle === previousTitle) return;
    mergePage({ session_id: page.session_id, title: nextTitle });
    try {
      const updated = await api.updateSession(page.session_id, { title: nextTitle });
      mergePage({ session_id: page.session_id, title: updated.title ?? null });
    } catch (error) {
      mergePage({ session_id: page.session_id, title: previousTitle });
      throw error;
    }
  };

  return { pages, loading, loaded, busyId, setVisibility, rotate, rename, onShareIdSaved, reload };
}

export type ShowPagesController = ReturnType<typeof useShowPages>;
