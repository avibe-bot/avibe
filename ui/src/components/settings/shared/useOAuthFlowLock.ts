import { useCallback, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';

import { useToast } from '@/context/ToastContext';

export interface UseOAuthFlowLockOptions<TMode extends string> {
  /** The real auth-mode setter that the page owns. */
  setAuthMode: (next: TMode) => void;
  /**
   * Tag used in the ``console.warn`` when a guarded write is rejected.
   * Pick something page-specific (e.g. ``claude-auth-mode``) so
   * future regressions are attributable in DevTools.
   */
  warnTag: string;
}

export interface OAuthFlowLock<TMode extends string> {
  oauthFlowActive: boolean;
  setOauthFlowActive: (active: boolean) => void;
  /**
   * Drop-in replacement for the page's ``setAuthMode``. When an OAuth
   * flow is mid-handshake, calls become no-ops with a console.warn +
   * warning toast — so an iOS Safari quirk that smuggles a click past
   * a ``disabled`` button can't tear down the in-progress login.
   */
  guardedSetAuthMode: (next: TMode) => void;
}

/**
 * Guard the auth-mode switcher while a backend OAuth flow is in flight.
 *
 * Background: on iOS Safari, tapping the device-code "Copy" button on the
 * Codex / Claude OAuth panel was flipping the surrounding segmented radio
 * from OAuth to API Key, tearing down the login. The eventual root-cause
 * fix lives in ApiContext memoization (so the page's mount-time
 * ``useEffect([api])`` no longer re-fires on toast renders), but as belt-
 * and-suspenders we ALSO refuse the state mutation while a flow is mid-
 * handshake. The ``disabled`` HTML attribute is not enough on its own —
 * the user has reproduced the flip with the radio rendered disabled.
 *
 * Pages call this once and pass ``guardedSetAuthMode`` to the
 * ``SegmentedRadio`` and ``setOauthFlowActive`` to the
 * ``BackendOAuthPanel.onActiveChange`` prop.
 */
export function useOAuthFlowLock<TMode extends string>({
  setAuthMode,
  warnTag,
}: UseOAuthFlowLockOptions<TMode>): OAuthFlowLock<TMode> {
  const { showToast } = useToast();
  const { t } = useTranslation();

  const [oauthFlowActive, setOauthFlowActive] = useState(false);
  // Ref so the guard always sees the latest value without rebuilding the
  // callback (and therefore the SegmentedRadio's ``onChange`` reference)
  // on every render.
  const oauthFlowActiveRef = useRef(oauthFlowActive);
  oauthFlowActiveRef.current = oauthFlowActive;

  const guardedSetAuthMode = useCallback(
    (next: TMode) => {
      if (oauthFlowActiveRef.current) {
        console.warn(
          '[%s] rejected change to %s while OAuth flow active',
          warnTag,
          next,
        );
        showToast(t('settings.backends.oauthFlowLockedToast'), 'warning');
        return;
      }
      setAuthMode(next);
    },
    [setAuthMode, showToast, t, warnTag],
  );

  return { oauthFlowActive, setOauthFlowActive, guardedSetAuthMode };
}
