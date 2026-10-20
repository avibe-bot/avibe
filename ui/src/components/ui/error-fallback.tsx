import * as React from 'react';
import { useTranslation } from 'react-i18next';
import { AlertTriangle, Loader2, RotateCcw, RotateCw } from 'lucide-react';
import clsx from 'clsx';

import { Button } from './button';

// A lazy module that failed to load, worded differently by Chromium, Firefox, and Safari, plus
// Vite's own message when a lazy chunk's CSS fails to preload. Retrying in place cannot recover
// it: React.lazy keeps the rejected import, and browsers keep a failed module fetch until the page
// reloads. The usual cause is a page left open across an Avibe update.
const CHUNK_LOAD_ERROR =
  /Failed to fetch dynamically imported module|error loading dynamically imported module|Importing a module script failed|Unable to preload CSS/i;

function isChunkLoadError(error: unknown): boolean {
  return error instanceof Error && CHUNK_LOAD_ERROR.test(error.message);
}

// Kept dependency-light on purpose: this renders precisely when something else just threw, so it must
// not itself rely on app data that might be the cause. i18n `t` returns the key if missing (no throw).
export const ErrorFallback: React.FC<{ error: unknown; reset: () => void; variant: 'page' | 'inline' }> = ({ error, reset, variant }) => {
  const { t } = useTranslation();
  // A reload keeps showing this page until the next one arrives, which can take a while over a
  // remote connection, so the button shows that it is working.
  const [reloading, setReloading] = React.useState(false);
  const chunkLoadFailed = isChunkLoadError(error);
  const detail = error instanceof Error ? error.message : typeof error === 'string' ? error : '';
  const reload = () => {
    setReloading(true);
    window.location.reload();
  };
  return (
    <div className={clsx('grid h-full w-full place-items-center bg-surface p-6 text-center', variant === 'page' && 'min-h-[60vh]')}>
      <div className="flex max-w-sm flex-col items-center gap-3">
        <span className="grid size-12 shrink-0 place-items-center rounded-2xl border border-gold/40 bg-gold/[0.08]">
          <AlertTriangle className="size-6 text-gold-ink" />
        </span>
        <div className="text-[15px] font-semibold text-foreground">
          {t(chunkLoadFailed ? 'errorBoundary.chunkLoadTitle' : 'errorBoundary.title')}
        </div>
        <div className="text-[12.5px] text-muted">{t(chunkLoadFailed ? 'errorBoundary.chunkLoadBody' : 'errorBoundary.body')}</div>
        {detail && (
          <div className="max-w-full truncate rounded bg-surface-3 px-2 py-1 font-mono text-[11px] text-muted" title={detail}>
            {detail}
          </div>
        )}
        <div className="mt-1 flex items-center gap-2">
          {!chunkLoadFailed && (
            <Button type="button" size="sm" variant="brand" className="gap-1.5" onClick={reset} disabled={reloading}>
              <RotateCcw className="size-3.5" /> {t('errorBoundary.retry')}
            </Button>
          )}
          <Button
            type="button"
            size="sm"
            variant={chunkLoadFailed ? 'brand' : 'outline'}
            className="gap-1.5"
            onClick={reload}
            disabled={reloading}
            aria-busy={reloading}
          >
            {reloading ? (
              <Loader2 className="size-3.5 animate-spin" />
            ) : (
              chunkLoadFailed && <RotateCw className="size-3.5" />
            )}
            {t(reloading ? 'errorBoundary.reloading' : 'errorBoundary.reload')}
          </Button>
        </div>
      </div>
    </div>
  );
};
