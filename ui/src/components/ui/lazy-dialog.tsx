import { Suspense, useEffect, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';

import { useToast } from '../../context/ToastContext';
import { isChunkLoadError } from '../../lib/chunkLoadError';
import { useLatestRef } from '../../lib/useLatestRef';
import { ErrorBoundary } from './error-boundary';

/**
 * A dialog whose code loads on demand (lib/lazyPage). While it loads, nothing is drawn; if its
 * code cannot load (offline, a tunnel blip, Avibe updated since the page opened), only the dialog
 * is lost: the shell and any half-filled form behind it stay as they are, a toast says to reload
 * (or, for an error of the dialog's own once loaded, that something went wrong),
 * and `onFailed` lets the owner close what asked for the dialog, so the next request tries again.
 */
export function LazyDialog({ onFailed, children }: { onFailed: () => void; children: ReactNode }) {
  return (
    <ErrorBoundary variant="inline" fallback={({ error }) => <LazyDialogFailed error={error} onFailed={onFailed} />}>
      <Suspense fallback={null}>{children}</Suspense>
    </ErrorBoundary>
  );
}

function LazyDialogFailed({ error, onFailed }: { error: unknown; onFailed: () => void }) {
  const { t } = useTranslation();
  const { showToast } = useToast();
  // Code that did not load needs a reload; any other error is the dialog's own, and says so.
  const message = isChunkLoadError(error) ? 'errorBoundary.chunkLoadBody' as const : 'errorBoundary.body' as const;
  const latest = useLatestRef({ onFailed, showToast, t, message });
  // Once per failure: the owner's close unmounts this boundary, and a later request mounts a new one.
  useEffect(() => {
    const { onFailed: close, showToast: toast, t: translate, message: key } = latest.current;
    toast(translate(key), 'error');
    close();
  }, [latest]);
  return null;
}
