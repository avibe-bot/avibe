import { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';

import { useApi } from '@/context/ApiContext';
import { useToast } from '@/context/ToastContext';
import { setConfigField } from '@/lib/configMutations';
import { errorMessage } from '@/lib/errorMessage';

export type CliStatus = 'unknown' | 'ok' | 'missing';

export type BackendId = 'claude' | 'codex' | 'opencode';

export interface InstallResult {
  ok: boolean;
  message: string;
  output?: string | null;
}

export interface UseBackendRuntimeOptions {
  /** Backend identifier used in V2Config keys and install_agent dispatch. */
  backend: BackendId;
  /** Fallback CLI binary name when V2Config carries no override. */
  defaultCli: string;
}

export interface BackendRuntimeState {
  /** Initial V2Config load attempt has finished (success or failure). */
  loaded: boolean;
  /**
   * ``true`` when the initial ``getConfig()`` rejected. In that state,
   * ``enabled`` / ``cliPath`` are still their pre-load defaults rather
   * than reflecting persisted state, so consumers MUST treat
   * ``loaded && !configError`` — not just ``loaded`` — as the gate for
   * any side-effect that depends on actual backend state (e.g.
   * OpenCode's providers fan-out).
   */
  configError: boolean;
  enabled: boolean;
  cliPath: string;
  cliStatus: CliStatus;
  detecting: boolean;
  installing: boolean;
  installResult: InstallResult | null;
  installOutputOpen: boolean;
  savingRuntime: boolean;
  /** True once the user has typed a path different from the saved one. */
  runtimeDirty: boolean;
  /** Changes after a runtime mutation settles; consumers must read fresh state. */
  connectionRevision: number;

  setCliPath: (next: string) => void;
  setInstallOutputOpen: (open: boolean | ((prev: boolean) => boolean)) => void;
  /** Runs ``detectCli`` and updates ``cliPath`` + ``cliStatus``. */
  detect: (binary?: string) => Promise<void>;
  /** Calls ``installAgent`` then re-runs detect with the resolved path. */
  install: () => Promise<void>;
  /** Persists this backend's CLI path; enablement has its own mutation. */
  onSaveRuntime: () => Promise<void>;
  /** Optimistically flip enabled, then reconcile the latest persisted intent. */
  toggleEnabled: () => void;
  /**
   * Pass to ``BackendLifecycleChip.onChanged``. Updates ``cliPath`` when
   * the chip reports a fresh install path and re-runs detect. A null
   * notification from onOperationChange(false) observes every settlement,
   * including rejected restart/upgrade, without modifying a path draft.
   */
  handleLifecycleChanged: (info: { installedPath?: string | null } | undefined | null) => Promise<void>;
}

type BackendRuntimeApplyResult = {
  hot_reconciled?: boolean;
  restart_scheduled?: boolean;
  apply_on_next_start?: boolean;
  restart_error?: string;
  error?: string;
};

const assertBackendRuntimeApplied = (savedConfig: unknown, fallbackMessage: string) => {
  const runtime = (
    savedConfig as { agent_backend_runtime?: BackendRuntimeApplyResult } | null
  )?.agent_backend_runtime;
  if (
    !runtime ||
    runtime.hot_reconciled === true ||
    runtime.restart_scheduled === true ||
    runtime.apply_on_next_start === true
  ) {
    return;
  }
  throw new Error(runtime.restart_error || runtime.error || fallbackMessage);
};

/**
 * Encapsulates the runtime (CLI lifecycle) state shared by every
 * Settings → Backends provider page. Previously each page (Claude /
 * OpenCode / Codex) duplicated ~120 lines of state declarations + the
 * same detect / install / save / toggle handlers; differences were the
 * backend id and the fallback CLI name. Pulling it into a hook means
 * the next backend gets the same lifecycle for free, and any bug fix
 * lands once.
 */
export function useBackendRuntime({
  backend,
  defaultCli,
}: UseBackendRuntimeOptions): BackendRuntimeState {
  const api = useApi();
  const { showToast } = useToast();
  const { t } = useTranslation();

  const [loaded, setLoaded] = useState(false);
  const [configError, setConfigError] = useState(false);
  const [enabled, setEnabled] = useState(true);
  const [cliPath, setCliPath] = useState(defaultCli);
  const [savedCliPath, setSavedCliPath] = useState(defaultCli);
  const [cliStatus, setCliStatus] = useState<CliStatus>('unknown');
  const [detecting, setDetecting] = useState(false);
  const [installing, setInstalling] = useState(false);
  const [installResult, setInstallResult] = useState<InstallResult | null>(null);
  const [installOutputOpen, setInstallOutputOpen] = useState(false);
  const [savingRuntime, setSavingRuntime] = useState(false);
  const [connectionRevision, setConnectionRevision] = useState(0);
  const mutationQueue = useRef(Promise.resolve());
  const enabledIntent = useRef(0);
  const pathIntent = useRef(0);
  const detectionToken = useRef(0);
  const pathState = useRef({ cliPath, savedCliPath });
  pathState.current = { cliPath, savedCliPath };
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; enabledIntent.current += 1; detectionToken.current += 1; };
  }, []);

  const detect = useCallback(
    async (binary?: string) => {
      const token = ++detectionToken.current;
      const intent = pathIntent.current;
      setDetecting(true);
      try {
        const result = await api.detectCli(binary || cliPath || defaultCli);
        if (!mounted.current || detectionToken.current !== token || pathIntent.current !== intent) return;
        const nextPath = result.path || cliPath || defaultCli;
        setCliPath(nextPath);
        setCliStatus(result.found ? 'ok' : 'missing');
      } catch (e) {
        if (!mounted.current || detectionToken.current !== token || pathIntent.current !== intent) return;
        setCliStatus('missing');
        showToast(errorMessage(e) || t('common.saveFailed'), 'error');
      } finally {
        if (mounted.current && detectionToken.current === token) setDetecting(false);
      }
    },
    [api, cliPath, defaultCli, showToast, t],
  );

  // Initial load: read V2Config for ``enabled`` + ``cli_path`` and then
  // probe the CLI. Independent of any auth-state fetch the page also
  // performs — that's owned by the page, not the runtime hook.
  useEffect(() => {
    let cancelled = false;
    api
      .getConfig()
      .then((config) => {
        if (cancelled) return;
        const agent = config?.agents?.[backend];
        const initialEnabled = typeof agent?.enabled === 'boolean' ? agent.enabled : true;
        const initialPath = agent?.cli_path || defaultCli;
        setEnabled(initialEnabled);
        setCliPath(initialPath);
        setSavedCliPath(initialPath);
        setConfigError(false);
        setLoaded(true);
        void detect(initialPath);
      })
      .catch((e: any) => {
        if (cancelled) return;
        // We still flip ``loaded`` so the page can drop its loading
        // skeleton, but ``configError`` tells consumers the persisted
        // state was never read — gate any side-effect that depends on
        // ``enabled`` on ``!configError`` to avoid acting on a default
        // we never confirmed (e.g. firing provider fetches when the
        // backend may actually be disabled).
        setConfigError(true);
        setLoaded(true);
        showToast(e?.message || t('common.saveFailed'), 'error');
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [api, backend, defaultCli]);

  const install = useCallback(async () => {
    const intent = pathIntent.current;
    setInstalling(true);
    setInstallResult(null);
    setInstallOutputOpen(false);
    try {
      const result = await api.installAgent(backend);
      if (!mounted.current) return;
      const installedPath =
        typeof result.path === 'string' && result.path ? result.path : null;
      setInstallResult({ ok: result.ok, message: result.message, output: result.output });
      if (result.ok) {
        if (installedPath) setSavedCliPath(installedPath);
        if (pathIntent.current === intent) {
          if (installedPath) setCliPath(installedPath);
          await detect(installedPath || cliPath);
        }
        showToast(result.message || t('agentDetection.installAgent'), 'success');
      } else {
        showToast(result.message || t('common.saveFailed'), 'error');
      }
    } catch (e) {
      if (!mounted.current) return;
      setInstallResult({ ok: false, message: String(e), output: null });
      showToast(errorMessage(e) || String(e), 'error');
    } finally {
      if (mounted.current) {
        setConnectionRevision((revision) => revision + 1);
        setInstalling(false);
      }
    }
  }, [api, backend, cliPath, detect, showToast, t]);

  const onSaveRuntime = useCallback(async () => {
    setSavingRuntime(true);
    const path = cliPath || defaultCli;
    // Save edits only the CLI field. Enablement has its own serialized toggle;
    // including it here could overwrite a newer optimistic on/off intent.
    const operation = mutationQueue.current.then(async () => {
      try {
        const saved = await api.mutateConfig([setConfigField(['agents', backend, 'cli_path'], path)]);
        if (!mounted.current) return;
        setSavedCliPath(path); // persistence succeeded even if application failed
        assertBackendRuntimeApplied(saved, t('common.saveFailed'));
        showToast(t('common.saved'), 'success');
      } catch (e) {
        if (mounted.current) showToast(errorMessage(e) || t('common.saveFailed'), 'error');
      } finally {
        if (mounted.current) {
          setSavingRuntime(false);
          setConnectionRevision((revision) => revision + 1);
        }
      }
    });
    mutationQueue.current = operation;
    await operation;
  }, [api, backend, cliPath, defaultCli, showToast, t]);

  const toggleEnabled = useCallback(() => {
    const next = !enabled;
    const intent = ++enabledIntent.current;
    setEnabled(next);
    mutationQueue.current = mutationQueue.current.then(async () => {
      let saved = false;
      try {
        const savedConfig = await api.mutateConfig([setConfigField(['agents', backend, 'enabled'], next)]);
        saved = true;
        if (!mounted.current || enabledIntent.current !== intent) return;
        const persisted = savedConfig?.agents?.[backend]?.enabled;
        setEnabled(typeof persisted === 'boolean' ? persisted : next);
        assertBackendRuntimeApplied(savedConfig, t('common.saveFailed'));
      } catch (e) {
        if (!mounted.current || enabledIntent.current !== intent) return;
        showToast(errorMessage(e) || t('common.saveFailed'), 'error');
        if (!saved) {
          // A rejected request may have lost its response after persistence.
          // Reconcile the uncached config projection instead of guessing rollback.
          try {
            const fresh = await api.getBackendConnection(backend);
            if (!fresh.ok) throw new Error(fresh.message || t('common.saveFailed'));
            if (mounted.current && enabledIntent.current === intent) setEnabled(fresh.enabled);
          } catch (cause) {
            if (mounted.current && enabledIntent.current === intent) showToast(errorMessage(cause) || t('common.saveFailed'), 'error');
          }
        }
      } finally {
        if (mounted.current && enabledIntent.current === intent) setConnectionRevision((revision) => revision + 1);
      }
    });
  }, [api, backend, enabled, showToast, t]);

  const handleLifecycleChanged = useCallback(
    async (info: { installedPath?: string | null } | undefined | null) => {
      if (!mounted.current) return;
      if (!info) {
        if (mounted.current) setConnectionRevision((revision) => revision + 1);
        return;
      }
      const installedPath = info?.installedPath || null;
      if (installedPath) setSavedCliPath(installedPath);
      // The lifecycle chip may still hold the callback from before a user edit.
      // Read the current draft before accepting its installed path.
      if (pathState.current.cliPath === pathState.current.savedCliPath) {
        if (installedPath) setCliPath(installedPath);
        await detect(installedPath || pathState.current.cliPath);
      }
    },
    [detect],
  );

  const runtimeDirty = cliPath !== savedCliPath;

  return {
    loaded,
    configError,
    enabled,
    cliPath,
    cliStatus,
    detecting,
    installing,
    installResult,
    installOutputOpen,
    savingRuntime,
    runtimeDirty,
    connectionRevision,
    setCliPath: (next) => { pathIntent.current += 1; setCliPath(next); },
    setInstallOutputOpen,
    detect,
    install,
    onSaveRuntime,
    toggleEnabled,
    handleLifecycleChanged,
  };
}
