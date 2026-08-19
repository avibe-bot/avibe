import { useCallback, useEffect, useMemo, useState, useSyncExternalStore } from 'react';
import { useTranslation } from 'react-i18next';

import { useApi } from '../context/ApiContext';
import { useToast } from '../context/ToastContext';
import {
  commitShowPagesInventoryStore,
  getShowPagesInventoryStore,
  type ShowPage,
} from '../lib/showPagesStore';

export { replaceShowPageTitleIfCurrent } from '../lib/showPagesStore';
export type { ShowPage, Visibility } from '../lib/showPagesStore';

// Stable hook adapter over the workbench-wide external store. The synchronous
// snapshot is what lets a remounted panel paint known icon_version values on its
// first render; activation then performs a shared background revalidation.
export function useShowPageInventory(enabled = true) {
  const api = useApi();
  const store = useMemo(() => getShowPagesInventoryStore(api), [api]);
  const { pages, loading, loaded } = useSyncExternalStore(
    store.subscribe,
    store.getSnapshot,
    store.getSnapshot,
  );

  // The tree has now committed to this identity's store, which is what makes the
  // one a previous identity handed out unreachable and its retained
  // subscription a leak. Runs whether or not this consumer activates: holding
  // the store is what supersedes, reading it is a separate question.
  useEffect(() => {
    commitShowPagesInventoryStore(api);
  }, [api]);

  useEffect(() => {
    if (enabled) return store.activate();
  }, [enabled, store]);

  const reload = useCallback(() => {
    if (enabled) void store.reload();
  }, [enabled, store]);

  return {
    pages,
    loading,
    loaded,
    mergePage: store.mergePage,
    removePage: store.removePage,
    replaceTitleIfCurrent: store.replaceTitleIfCurrent,
    reload,
  };
}

// The Show Pages inventory and availability mutations, with their toasts.
// Lifted out of the view so the App Library owns one copy of
// the pages state and projects it into both the Apps and Show Pages views (kept
// in a hook module so the view file exports only components — fast-refresh safe).
export function useShowPages() {
  const api = useApi();
  const { showToast } = useToast();
  const { t } = useTranslation();
  const { pages, loading, loaded, mergePage, replaceTitleIfCurrent, reload } =
    useShowPageInventory();
  const [busyId, setBusyId] = useState<string | null>(null);

  const setOffline = async (page: ShowPage, offline: boolean) => {
    if (page.offline === offline || busyId) return;
    setBusyId(page.session_id);
    try {
      const res = await api.setShowPageAvailability(page.session_id, offline);
      if (res?.session_id === page.session_id) mergePage(res as ShowPage);
      else reload();
      showToast(t('showPages.toast.updated'));
    } catch {
      // ApiContext surfaces a toast on failure.
    } finally {
      setBusyId(null);
    }
  };

  const rename = async (page: ShowPage, title: string | null) => {
    const previousTitle = page.title?.trim() || null;
    const nextTitle = title?.trim() || null;
    if (nextTitle === previousTitle) return;
    mergePage({ session_id: page.session_id, title: nextTitle });
    try {
      const updated = await api.updateSession(page.session_id, { title: nextTitle });
      replaceTitleIfCurrent(page.session_id, nextTitle, updated.title?.trim() || null);
    } catch (error) {
      replaceTitleIfCurrent(page.session_id, nextTitle, previousTitle);
      throw error;
    }
  };

  // Upload a new app icon (§7.1j). The server writes the workspace-root favicon and
  // returns the refreshed payload; merging it updates `icon_version`, so the
  // content-versioned icon URL changes and every avatar surface refetches on its own.
  const uploadIcon = async (page: ShowPage, file: File) => {
    if (busyId) return;
    setBusyId(page.session_id);
    try {
      const res = await api.uploadShowPageIcon(page.session_id, file);
      mergePage(res);
      showToast(t('showPages.icon.toast.updated'));
    } catch {
      // ApiContext surfaces a toast on failure.
    } finally {
      setBusyId(null);
    }
  };

  return { pages, loading, loaded, busyId, setOffline, rename, uploadIcon, reload };
}

export type ShowPagesController = ReturnType<typeof useShowPages>;
