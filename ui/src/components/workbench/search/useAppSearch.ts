import { isDesktopViewport } from '../../../lib/useIsDesktop';
import { isNativeAppWindow } from '../../../lib/desktopShell';
import { useAppLaunch } from '../../../apps/useAppLaunch';
import { useCallback, useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { useLocation, useNavigate } from 'react-router-dom';

import { appLaunchIntent, type LaunchModifiers } from '../../../apps/appLaunch';
import { APP_LIST } from '../../../apps/registry';
import { useShowPageInventory } from '../../useShowPages';
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
  const launchApp = useAppLaunch('command-palette');
  const navigate = useNavigate();
  const location = useLocation();

  return useCallback(
    async (result: AppSearchResult, launch?: LaunchModifiers | null) => {
      const intent = launch ? appLaunchIntent(launch) : 'activate';
      const outcome = await launchApp({ appId: result.appId, title: result.title, sessionId: result.kind === 'showpage' ? result.sessionId : undefined }, intent);
      if (intent !== 'newTab' && (isDesktopViewport() || isNativeAppWindow()) && outcome !== 'stale' && outcome !== 'unknown' && location.pathname === '/search') {
        if (location.key === 'default') navigate('/inbox', { replace: true });
        else navigate(-1);
      }
    },
    [location.key, location.pathname, navigate, launchApp],
  );
}
