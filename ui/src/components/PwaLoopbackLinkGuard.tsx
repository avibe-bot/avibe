import { useEffect } from 'react';
import { useTranslation } from 'react-i18next';

import { useToast } from '@/context/ToastContext';
import { isIosDevice, isStandalonePwa } from '@/lib/platform';
import { shouldBlockPwaLoopbackLink } from '@/lib/pwaNavigation';

// iOS opens out-of-scope links from a Home-Screen app in a dismissible browser
// sheet, and may restore that sheet after evicting the PWA process. A loopback
// URL in a chat reply can therefore strand the user on a dead "localhost" page:
// localhost is the iPhone, not the machine running Avibe. Catch every ordinary
// anchor at the app boundary so individual renderers cannot drift.
export const PwaLoopbackLinkGuard = () => {
  const { t } = useTranslation();
  const { showToast } = useToast();

  useEffect(() => {
    if (!(isIosDevice() && isStandalonePwa())) return;

    const onClick = (event: MouseEvent) => {
      if (!(event.target instanceof Element)) return;
      const anchor = event.target.closest<HTMLAnchorElement>('a[href]');
      if (!anchor || !shouldBlockPwaLoopbackLink(anchor.href, window.location.href)) return;

      event.preventDefault();
      event.stopPropagation();
      showToast(t('common.localLinkUnavailable'), 'warning');
    };

    document.addEventListener('click', onClick, true);
    return () => document.removeEventListener('click', onClick, true);
  }, [showToast, t]);

  return null;
};
