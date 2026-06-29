import * as React from 'react';
import { useTranslation } from 'react-i18next';
import { AlertTriangle, RotateCcw } from 'lucide-react';
import clsx from 'clsx';

import { Button } from './button';

// A React error boundary: catches a render error in its subtree and shows a recoverable card instead
// of letting the throw unmount the whole app (a blank white screen). Wrap independent regions — each
// windowed app, the routed page area, and the app root — so one crashing component only takes down
// its own region, and the rest of the workbench stays usable.
//
// Error boundaries only catch errors thrown during render/lifecycle of their children — not in event
// handlers or async callbacks (those should be try/caught at the call site).

type FallbackRender = (args: { error: Error; reset: () => void }) => React.ReactNode;

type Props = {
  children: React.ReactNode;
  /** Custom fallback; defaults to a recoverable card that fills its container. */
  fallback?: FallbackRender;
  /**
   * When any value here changes (compared with Object.is), a caught error auto-clears. Pass STABLE
   * values only — e.g. the route path so navigating away from a crashed page recovers. An unstable
   * value (a fresh object/array each render) would reset → re-throw → reset in an infinite loop.
   */
  resetKeys?: unknown[];
  /** `page` fills a tall content area; `inline` is compact for an in-window app body. */
  variant?: 'page' | 'inline';
  onError?: (error: Error, info: React.ErrorInfo) => void;
};

type State = { error: Error | null };

function resetKeysChanged(a: unknown[] | undefined, b: unknown[] | undefined): boolean {
  if (a === b) return false;
  if (!a || !b || a.length !== b.length) return true;
  return a.some((value, i) => !Object.is(value, b[i]));
}

export class ErrorBoundary extends React.Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: React.ErrorInfo): void {
    // The boundary swallows the throw so it can never white-screen the app — but the detail must not
    // be lost, so always surface it to the console (and any wired reporter via onError).
    // eslint-disable-next-line no-console
    console.error('ErrorBoundary caught a render error:', error, info.componentStack);
    this.props.onError?.(error, info);
  }

  componentDidUpdate(prev: Props): void {
    if (this.state.error && resetKeysChanged(prev.resetKeys, this.props.resetKeys)) {
      this.reset();
    }
  }

  reset = (): void => this.setState({ error: null });

  render(): React.ReactNode {
    const { error } = this.state;
    if (error) {
      if (this.props.fallback) return this.props.fallback({ error, reset: this.reset });
      return <ErrorFallback error={error} reset={this.reset} variant={this.props.variant ?? 'page'} />;
    }
    return this.props.children;
  }
}

// Kept dependency-light on purpose: this renders precisely when something else just threw, so it must
// not itself rely on app data that might be the cause. i18n `t` returns the key if missing (no throw).
const ErrorFallback: React.FC<{ error: Error; reset: () => void; variant: 'page' | 'inline' }> = ({ error, reset, variant }) => {
  const { t } = useTranslation();
  return (
    <div className={clsx('grid h-full w-full place-items-center bg-surface p-6 text-center', variant === 'page' && 'min-h-[60vh]')}>
      <div className="flex max-w-sm flex-col items-center gap-3">
        <span className="grid size-12 shrink-0 place-items-center rounded-2xl border border-gold/40 bg-gold/[0.08]">
          <AlertTriangle className="size-6 text-gold" />
        </span>
        <div className="text-[15px] font-semibold text-foreground">{t('errorBoundary.title')}</div>
        <div className="text-[12.5px] text-muted">{t('errorBoundary.body')}</div>
        {error.message && (
          <div className="max-w-full truncate rounded bg-surface-3 px-2 py-1 font-mono text-[11px] text-muted" title={error.message}>
            {error.message}
          </div>
        )}
        <div className="mt-1 flex items-center gap-2">
          <Button type="button" size="sm" variant="brand" className="gap-1.5" onClick={reset}>
            <RotateCcw className="size-3.5" /> {t('errorBoundary.retry')}
          </Button>
          <Button type="button" size="sm" variant="outline" onClick={() => window.location.reload()}>
            {t('errorBoundary.reload')}
          </Button>
        </div>
      </div>
    </div>
  );
};
