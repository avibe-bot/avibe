import { useCallback, useEffect, useState } from 'react';
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

// The Show Pages inventory: fetch + the visibility / share-id / rotate mutations,
// with their toasts. Lifted out of the view so the App Library owns one copy of
// the pages state and projects it into both the Apps and Show Pages views (kept
// in a hook module so the view file exports only components — fast-refresh safe).
export function useShowPages() {
  const api = useApi();
  const { showToast } = useToast();
  const { t } = useTranslation();
  const [pages, setPages] = useState<ShowPage[]>([]);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [refreshTrigger, setRefreshTrigger] = useState(0);

  const load = useCallback(() => {
    api
      .getShowPages()
      .then((res: any) => setPages(Array.isArray(res?.pages) ? res.pages : []))
      .catch(() => {});
  }, [api]);

  useEffect(() => {
    load();
  }, [load, refreshTrigger]);

  const mergePage = (next: ShowPage) =>
    setPages((prev) => prev.map((page) => (page.session_id === next.session_id ? { ...page, ...next } : page)));

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

  const reload = useCallback(() => setRefreshTrigger((v) => v + 1), []);

  return { pages, busyId, setVisibility, rotate, onShareIdSaved, reload };
}

export type ShowPagesController = ReturnType<typeof useShowPages>;
