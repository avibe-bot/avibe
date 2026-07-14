import { useCallback, useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { useLocation, useNavigate } from 'react-router-dom';

import { APP_LIST } from '../../../apps/registry';
import { showAppRoutePath } from '../../apps/mobileDock';
import { useShowPageInventory } from '../../useShowPages';
import { useWindowManager } from '../../../context/WindowManagerContext';
import { filterAppSearchResults, type AppSearchResult } from './appSearch';

export function useAppSearchResults(query: string, enabled = true) {
  const { t } = useTranslation();
  const { pages, loading, loaded } = useShowPageInventory(enabled);
  const candidates = useMemo<AppSearchResult[]>(
    () => [
      ...APP_LIST.map((def) => {
        const title = t(def.titleKey);
        return {
          key: `builtin:${def.id}`,
          kind: 'builtin' as const,
          appId: def.id,
          title,
          searchTitle: title,
        };
      }),
      ...pages.map((page) => {
        const liveTitle = page.title?.trim() ?? '';
        return {
          key: `show:${page.session_id}`,
          kind: 'showpage' as const,
          appId: 'showpage' as const,
          title: liveTitle || t('chat.untitled'),
          searchTitle: liveTitle,
          sessionId: page.session_id,
          iconVersion: page.icon_version,
        };
      }),
    ],
    [pages, t],
  );
  const results = useMemo(() => filterAppSearchResults(candidates, query), [candidates, query]);

  return { results, loading: enabled && (loading || !loaded) };
}

export function useOpenSearchApp() {
  const wm = useWindowManager();
  const navigate = useNavigate();
  const location = useLocation();

  return useCallback(
    (result: AppSearchResult) => {
      const desktop = typeof window !== 'undefined' && !!window.matchMedia?.('(min-width: 768px)').matches;
      if (desktop) {
        wm.openApp(result.appId, {
          title: result.kind === 'showpage' ? result.title : undefined,
          params:
            result.kind === 'showpage'
              ? { sessionId: result.sessionId, title: result.title }
              : undefined,
        });
        if (location.pathname === '/search') {
          if (location.key === 'default') navigate('/inbox', { replace: true });
          else navigate(-1);
        }
        return;
      }
      if (result.kind === 'showpage') {
        if (result.sessionId) navigate(showAppRoutePath(result.sessionId));
      } else {
        navigate(`/apps/${result.appId}`);
      }
    },
    [location.key, location.pathname, navigate, wm],
  );
}
