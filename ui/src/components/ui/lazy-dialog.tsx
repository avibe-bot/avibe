import { Suspense, useEffect, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';

import { useToast } from '../../context/ToastContext';
import { useLatestRef } from '../../lib/useLatestRef';
import { ErrorBoundary } from './error-boundary';

/**
 * A dialog whose code loads on demand (lib/lazyPage). While it loads, nothing is drawn; if its
 * code cannot load (offline, a tunnel blip, Avibe updated since the page opened), only the dialog
 * is lost: the shell and any half-filled form behind it stay as they are, a toast says to reload,
 * and `onFailed` lets the owner close what asked for the dialog, so the next request tries again.
 */
export function LazyDialog({ onFailed, children }: { onFailed: () => void; children: ReactNode }) {
  return (
    <ErrorBoundary variant="inline" fallback={() => <LazyDialogFailed onFailed={onFailed} />}>
      <Suspense fallback={null}>{children}</Suspense>
    </ErrorBoundary>
  );
}

function LazyDialogFailed({ onFailed }: { onFailed: () => void }) {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const latest = useLatestRef({ onFailed, showToast, t });
  // Once per failure: the owner's close unmounts this boundary, and a later request mounts a new one.
  useEffect(() => {
    const { onFailed: close, showToast: toast, t: translate } = latest.current;
    toast(translate('errorBoundary.chunkLoadBody'), 'error');
    close();
  }, [latest]);
  return null;
}
