import { useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router-dom';

import { useToast } from '@/context/ToastContext';
import { isNativeShell } from '@/lib/native';
import { isIosDevice, isStandalonePwa } from '@/lib/platform';
import {
  internalPwaLinkTarget,
  pwaNavigationPolicy,
  shouldBlockPwaLoopbackLink,
} from '@/lib/pwaNavigation';

// iOS opens `_blank` links from a Home-Screen app in a secondary browser context
// and may restore that context after evicting the PWA process. Keep every
// same-origin navigation in this context, and keep blocking loopback URLs that
// point at the phone rather than the machine running Avibe. The global bridge is
// used by same-origin Show Page frames, whose clicks cannot bubble to this document.
export const PwaLoopbackLinkGuard = () => {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const navigate = useNavigate();

  useEffect(() => {
    const nativeShell = isNativeShell();
    const policy = pwaNavigationPolicy({
      iosStandalone: isIosDevice() && isStandalonePwa(),
      nativeShell,
    });
    if (!policy.guardLoopback) return;

    const handleNewContext = (href: string) => {
      if (shouldBlockPwaLoopbackLink(href, window.location.href)) {
        showToast(t('common.localLinkUnavailable'), 'warning');
        return true;
      }
      if (!policy.rewriteSameOriginNewContext) return false;
      const target = internalPwaLinkTarget(href, window.location.href);
      if (!target) return false;
      if (target.navigation === 'spa') navigate(target.path);
      else window.location.assign(target.path);
      return true;
    };

    const onClick = (event: MouseEvent) => {
      if (!(event.target instanceof Element)) return;
      const anchor = event.target.closest<HTMLAnchorElement>('a[href]');
      if (!anchor) return;

      const internalTarget =
        anchor.target.toLowerCase() === '_blank' && !anchor.hasAttribute('download')
          ? internalPwaLinkTarget(anchor.href, window.location.href)
          : null;
      if (internalTarget && handleNewContext(anchor.href)) {
        event.preventDefault();
        event.stopPropagation();
        return;
      }

      if (!shouldBlockPwaLoopbackLink(anchor.href, window.location.href)) return;

      event.preventDefault();
      event.stopPropagation();
      showToast(t('common.localLinkUnavailable'), 'warning');
    };

    window.__AVIBE_PWA_NAVIGATE_SAME_ORIGIN__ = handleNewContext;
    document.addEventListener('click', onClick, true);
    return () => {
      document.removeEventListener('click', onClick, true);
      if (window.__AVIBE_PWA_NAVIGATE_SAME_ORIGIN__ === handleNewContext) {
        delete window.__AVIBE_PWA_NAVIGATE_SAME_ORIGIN__;
      }
    };
  }, [navigate, showToast, t]);

  return null;
};
