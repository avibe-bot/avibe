import { useEffect, useRef } from 'react';
import { useTranslation } from 'react-i18next';

import { useToast, type ToastType } from '@/context/ToastContext';
import type { TranslationKey } from '@/i18n/types';

/** Raises one toast on mount, as the page would at that moment: a frame's way to show what the
 *  App's own toast says (ToastProvider draws it bottom right, or above the tab bar on a phone). */
export function RaiseToast({ i18nKey, values, type = 'success', persist = false }: {
  i18nKey: TranslationKey; values?: Record<string, string>; type?: ToastType; persist?: boolean;
}) {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const raised = useRef(false);
  useEffect(() => {
    if (raised.current) return;
    raised.current = true;
    showToast(t(i18nKey, values), type, undefined, persist ? { persist: true } : undefined);
  }, [i18nKey, values, type, persist, showToast, t]);
  return null;
}
