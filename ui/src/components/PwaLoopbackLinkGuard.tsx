import { useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router-dom';

import { useToast } from '@/context/ToastContext';
import { isIosDevice, isStandalonePwa } from '@/lib/platform';
import { internalPwaLinkTarget, shouldBlockPwaLoopbackLink } from '@/lib/pwaNavigation';

// iOS opens `_blank` links from a Home-Screen app in a secondary browser context
// and may restore that context after evicting the PWA process. Keep internal App
// and Show Page links in this context, and keep blocking loopback URLs that point
// at the phone rather than the machine running Avibe. One capture boundary also
// covers Markdown links, so individual renderers cannot drift.
export const PwaLoopbackLinkGuard = () => {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const navigate = useNavigate();

  useEffect(() => {
    if (!(isIosDevice() && isStandalonePwa())) return;

    const onClick = (event: MouseEvent) => {
      if (!(event.target instanceof Element)) return;
      const anchor = event.target.closest<HTMLAnchorElement>('a[href]');
      if (!anchor) return;

      const internalTarget =
        anchor.target.toLowerCase() === '_blank'
          ? internalPwaLinkTarget(anchor.href, window.location.href)
          : null;
      if (internalTarget) {
        event.preventDefault();
        event.stopPropagation();
        if (internalTarget.navigation === 'spa') navigate(internalTarget.path);
        else window.location.assign(internalTarget.path);
        return;
      }

      if (!shouldBlockPwaLoopbackLink(anchor.href, window.location.href)) return;

      event.preventDefault();
      event.stopPropagation();
      showToast(t('common.localLinkUnavailable'), 'warning');
    };

    document.addEventListener('click', onClick, true);
    return () => document.removeEventListener('click', onClick, true);
  }, [navigate, showToast, t]);

  return null;
};
