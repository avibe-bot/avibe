import React from 'react';
import { useTranslation } from 'react-i18next';
import { AlertCircle, Check, Download, RefreshCw, RotateCw, X } from 'lucide-react';
import clsx from 'clsx';
import { useApi, type BackendRuntimeInfo } from '../../context/ApiContext';
import { useToast } from '../../context/ToastContext';
import { Button } from '../ui/button';
import { badgeVariants } from '../ui/badge-variants';
import { cn } from '@/lib/utils';

type CliStatus = 'unknown' | 'ok' | 'missing';
type Phase = 'idle' | 'loading' | 'upgrading' | 'restarting';
type Visual = 'disabled' | 'ready' | 'updating' | 'update' | 'error' | 'loading';
type BadgeVariant = 'secondary' | 'success' | 'info' | 'warning' | 'destructive';

export type BackendChipChange = {
  // Path returned by ``install_agent`` when the chip just (re)installed the
  // backend. Parents should treat this as the new source of truth and update
  // their saved ``cli_path`` before re-running detection — otherwise a stale
  // path from the React closure keeps the row in a false ``missing`` state
  // even though the install succeeded.
  installedPath?: string | null;
};

interface BackendLifecycleChipProps {
  name: string;
  enabled: boolean;
  cliStatus: CliStatus;
  onChanged?: (info?: BackendChipChange) => void | Promise<void>;
}

// Map lifecycle visual states to canonical Badge variants from the design
// system. The chip is a clickable status pill so it shares Badge's shape;
// we apply badgeVariants() to a <button> rather than re-roll the styling.
const BADGE_VARIANT: Record<Visual, BadgeVariant> = {
  disabled: 'secondary',
  loading: 'secondary',
  ready: 'success',
  updating: 'info',
  update: 'warning',
  error: 'destructive',
};

const DOT_STYLES: Record<Visual, string> = {
  disabled: 'bg-muted/60',
  ready: 'bg-mint',
  updating: 'bg-cyan animate-pulse',
  update: 'bg-gold animate-pulse',
  error: 'bg-destructive',
  loading: 'bg-muted/60',
};

const deriveVisual = (
  enabled: boolean,
  cliStatus: CliStatus,
  runtime: BackendRuntimeInfo | null,
  phase: Phase,
): Visual => {
  // An in-flight upgrade outranks a stale "disabled" — if the user toggles a
  // backend off mid-install we still want the progress affordance visible.
  if (phase === 'upgrading') return 'updating';
  if (!enabled) return 'disabled';
  if (cliStatus === 'missing') return 'error';
  // ``runtime.installed`` is probed against the *persisted* cli_path. When
  // the user edits the row's path and runs Detect, ``cliStatus`` flips to
  // ``ok`` but the runtime probe is still inspecting the saved config. In
  // that window the local detection is the fresher signal — trust it and
  // do not flip the chip to error just because the saved config is stale.
  if (cliStatus !== 'ok' && runtime && runtime.installed === false) return 'error';
  // ``opencode serve`` and ``codex app-server`` are lazy-spawn daemons: they
  // stay stopped until a session needs them, so ``process_status === 'stopped'``
  // is the normal idle state, not an error. Restart still works on demand
  // from the popover.
  if (runtime?.has_update) return 'update';
  // After a successful upgrade, ``cliStatus`` can briefly hang at ``unknown``
  // — the chip's own ``loadRuntime`` already returned a fresh runtime
  // (``installed=true``) but the parent's separate ``detect()`` call (which
  // owns the prop) is still in flight. Without the second clause here,
  // ``deriveVisual`` falls through to ``loading`` and the user sees
  // "未检测" until the parent's detect resolves and triggers a re-render.
  // Treating ``runtime.installed=true`` as a ready signal closes that
  // race — the runtime probe is the authoritative source on whether the
  // binary is on disk and produces a version string.
  if (cliStatus === 'ok' || runtime?.installed === true) return 'ready';
  return 'loading';
};

export const BackendLifecycleChip: React.FC<BackendLifecycleChipProps> = ({
  name,
  enabled,
  cliStatus,
  onChanged,
}) => {
  const { t } = useTranslation();
  const api = useApi();
  const { showToast } = useToast();
  const [isOpen, setIsOpen] = React.useState(false);
  const [runtime, setRuntime] = React.useState<BackendRuntimeInfo | null>(null);
  const [phase, setPhase] = React.useState<Phase>('idle');
  const popupRef = React.useRef<HTMLDivElement>(null);
  const isMountedRef = React.useRef(true);
  // Monotonic token guards against stale async writes when toggle/detect
  // changes fire faster than the runtime probe completes.
  const loadTokenRef = React.useRef(0);

  React.useEffect(() => {
    isMountedRef.current = true;
    return () => {
      isMountedRef.current = false;
    };
  }, []);

  // Close on outside click — same pattern as VersionBadge.
  React.useEffect(() => {
    if (!isOpen) return;
    const handleClickOutside = (event: MouseEvent) => {
      if (popupRef.current && !popupRef.current.contains(event.target as Node)) {
        setIsOpen(false);
      }
    };
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, [isOpen]);

  const loadRuntime = React.useCallback(async () => {
    const myToken = ++loadTokenRef.current;
    setPhase('loading');
    let info: BackendRuntimeInfo | null = null;
    try {
      info = await api.getBackendRuntime(name);
    } catch (e) {
      console.error(`Failed to load runtime for ${name}:`, e);
    } finally {
      // Drop the result if a newer request superseded us, or the component
      // unmounted while we were in flight.
      if (isMountedRef.current && loadTokenRef.current === myToken) {
        setRuntime(info);
        setPhase('idle');
      }
    }
  }, [api, name]);

  // Refresh when the chip opens, when the user toggles enabled, or when the CLI
  // path is freshly detected. Keeps the chip in sync with the surrounding card.
  React.useEffect(() => {
    if (!enabled) {
      // Bump the token so any in-flight probe drops its result.
      loadTokenRef.current += 1;
      setRuntime(null);
      return;
    }
    if (cliStatus === 'ok' || isOpen) {
      void loadRuntime();
    }
  }, [enabled, cliStatus, isOpen, loadRuntime]);

  const visual = deriveVisual(enabled, cliStatus, runtime, phase);

  const handleUpgrade = async () => {
    setPhase('upgrading');
    try {
      const result = await api.installAgent(name);
      if (result.ok) {
        showToast(t('backendLifecycle.upgradeSuccess'), 'success');
        await loadRuntime();
        const installedPath = typeof result.path === 'string' && result.path ? result.path : null;
        await onChanged?.({ installedPath });
      } else {
        showToast(result.message || t('backendLifecycle.upgradeFailed'), 'error');
      }
    } catch (e) {
      showToast(String(e), 'error');
    } finally {
      if (isMountedRef.current) setPhase('idle');
    }
  };

  const handleRestart = async () => {
    setPhase('restarting');
    try {
      const result = await api.restartBackend(name);
      showToast(
        result.message || (result.ok ? t('backendLifecycle.restartSuccess') : t('backendLifecycle.restartFailed')),
        result.ok ? 'success' : 'error',
      );
      if (result.ok) await loadRuntime();
    } catch (e) {
      showToast(String(e), 'error');
    } finally {
      if (isMountedRef.current) setPhase('idle');
    }
  };

  const chipLabel = (() => {
    switch (visual) {
      case 'disabled':
        return t('backendLifecycle.statusDisabled');
      case 'ready':
        return t('backendLifecycle.statusReady');
      case 'updating':
        return t('backendLifecycle.statusUpdating');
      case 'update':
        return t('backendLifecycle.statusUpdateAvailable');
      case 'error':
        return t('backendLifecycle.statusError');
      case 'loading':
      default:
        return t('common.notChecked');
    }
  })();

  return (
    <div className="relative" ref={popupRef}>
      <button
        type="button"
        onClick={() => setIsOpen((prev) => !prev)}
        className={cn(
          badgeVariants({ variant: BADGE_VARIANT[visual] }),
          'cursor-pointer font-medium tracking-normal hover:brightness-110 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background',
        )}
        aria-label={chipLabel}
      >
        <span className={clsx('size-2 rounded-full', DOT_STYLES[visual])} />
        {chipLabel}
      </button>

      {isOpen && (
        <div
          className={clsx(
            'z-50 rounded-lg border border-border bg-popover text-popover-foreground shadow-xl',
            'fixed inset-x-3 top-[4.5rem] max-h-[calc(100dvh-5.5rem)] overflow-auto',
            'md:absolute md:inset-x-auto md:right-0 md:top-full md:mt-2 md:w-72 md:max-h-none md:overflow-visible',
          )}
        >
          <div className="flex items-center justify-between border-b border-border px-4 py-3">
            <span className="text-sm font-medium text-foreground">{t('backendLifecycle.title')}</span>
            <div className="flex items-center gap-1">
              <Button
                variant="ghost"
                size="icon"
                className="h-7 w-7 text-muted hover:text-foreground"
                onClick={() => void loadRuntime()}
                disabled={phase !== 'idle'}
                aria-label={t('common.refresh')}
                title={t('common.refresh')}
              >
                <RefreshCw size={14} className={phase === 'loading' ? 'animate-spin' : ''} />
              </Button>
              <Button
                variant="ghost"
                size="icon"
                className="h-7 w-7 text-muted hover:text-foreground"
                onClick={() => setIsOpen(false)}
                aria-label={t('common.close')}
              >
                <X size={14} />
              </Button>
            </div>
          </div>

          <div className="space-y-3 p-4">
            <ChipPopoverBody
              visual={visual}
              runtime={runtime}
              phase={phase}
              name={name}
            />
          </div>

          {visual !== 'disabled' && visual !== 'updating' && (
            <div className="flex flex-wrap justify-end gap-2 border-t border-border px-4 py-3">
              {visual === 'update' && (
                <Button variant="brand" size="xs" onClick={() => void handleUpgrade()} disabled={phase !== 'idle'}>
                  <Download size={14} />
                  {t('backendLifecycle.upgradeNow')}
                </Button>
              )}
              {visual === 'error' && (
                <Button variant="brand" size="xs" onClick={() => void handleUpgrade()} disabled={phase !== 'idle'}>
                  <Download size={14} />
                  {t('backendLifecycle.reinstall')}
                </Button>
              )}
              {visual !== 'update' && cliStatus === 'ok' && runtime?.supports_restart && (
                <Button
                  variant="secondary"
                  size="xs"
                  onClick={() => void handleRestart()}
                  disabled={phase !== 'idle'}
                >
                  <RotateCw size={14} className={phase === 'restarting' ? 'animate-spin' : ''} />
                  {t('backendLifecycle.restart')}
                </Button>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
};

const ChipPopoverBody: React.FC<{
  visual: Visual;
  runtime: BackendRuntimeInfo | null;
  phase: Phase;
  name: string;
}> = ({ visual, runtime, phase, name }) => {
  const { t } = useTranslation();

  if (visual === 'disabled') {
    return (
      <p className="rounded-md border border-border bg-surface-2/40 px-3 py-2 text-xs text-muted">
        {t('backendLifecycle.disabledHint')}
      </p>
    );
  }

  return (
    <>
      <div className="flex items-center justify-between text-sm">
        <span className="text-muted">{t('backendLifecycle.currentVersion')}</span>
        <span className="font-mono font-medium text-foreground">{runtime?.current_version || '—'}</span>
      </div>
      {runtime?.latest_version && (
        <div className="flex items-center justify-between text-sm">
          <span className="text-muted">{t('backendLifecycle.latestVersion')}</span>
          <span className="font-mono font-medium text-foreground">{runtime.latest_version}</span>
        </div>
      )}
      <StateBlock visual={visual} phase={phase} runtime={runtime} name={name} />
    </>
  );
};

const StateBlock: React.FC<{
  visual: Visual;
  phase: Phase;
  runtime: BackendRuntimeInfo | null;
  name: string;
}> = ({ visual, phase, runtime, name }) => {
  const { t } = useTranslation();

  if (phase === 'upgrading') {
    return (
      <div className="flex items-center gap-2 rounded-md border border-cyan/25 bg-cyan/10 px-3 py-2 text-sm text-cyan-ink">
        <RefreshCw size={16} className="shrink-0 animate-spin" />
        <span>{t('backendLifecycle.upgrading')}</span>
      </div>
    );
  }
  if (visual === 'update') {
    return (
      <div className="flex items-center gap-2 rounded-md border border-gold/30 bg-gold/10 px-3 py-2 text-sm text-gold-ink">
        <AlertCircle size={16} className="shrink-0" />
        <span>
          {t('backendLifecycle.updateHint', {
            from: runtime?.current_version || '—',
            to: runtime?.latest_version || '—',
          })}
        </span>
      </div>
    );
  }
  if (visual === 'error') {
    return (
      <div className="flex items-center gap-2 rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive-ink">
        <AlertCircle size={16} className="shrink-0" />
        <span>{t('backendLifecycle.errorHint', { name })}</span>
      </div>
    );
  }
  if (visual === 'ready') {
    return (
      <div className="flex items-center gap-2 rounded-md border border-mint/25 bg-mint/10 px-3 py-2 text-sm text-mint-ink">
        <Check size={16} className="shrink-0" />
        <span>{t('backendLifecycle.readyHint')}</span>
      </div>
    );
  }
  return null;
};
