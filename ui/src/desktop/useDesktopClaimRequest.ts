import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';

import { desktopLocale, hasDesktopClient, presentDesktopClaim } from './bridge';

/** How long a request the shell did not act on keeps the button from asking again. */
const REQUEST_HOLD_MS = 3000;

/**
 * 「开启远程访问」 in the Desktop's Workbench (Settings › Devices, Setup's optional step): asks the
 * shell to show its claim dialog (avibe://desktop/claim), which signs in if needed, binds, and
 * returns to this same page. After a press the button is off until the page is shown again or a
 * moment passes, so requests never stack (native reviewer, 2026-11-23). The page learns the
 * result as it learns any binding: by reading the status again when it is shown.
 */
export function useDesktopClaimRequest() {
  const { i18n } = useTranslation();
  const [requested, setRequested] = useState(false);
  useEffect(() => {
    if (!requested) return;
    const timer = window.setTimeout(() => setRequested(false), REQUEST_HOLD_MS);
    const shown = () => { if (document.visibilityState === 'visible') setRequested(false); };
    document.addEventListener('visibilitychange', shown);
    return () => { window.clearTimeout(timer); document.removeEventListener('visibilitychange', shown); };
  }, [requested]);
  const request = useCallback(() => {
    if (requested || !hasDesktopClient()) return;
    if (presentDesktopClaim(desktopLocale(i18n.resolvedLanguage ?? i18n.language))) setRequested(true);
  }, [requested, i18n]);
  return { requested, request };
}
