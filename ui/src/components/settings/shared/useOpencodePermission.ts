import { useCallback, useEffect, useState } from 'react';
import { useApi } from '@/context/ApiContext';
import { useToast } from '@/context/ToastContext';

export type OpencodePermissionState = 'idle' | 'loading' | 'success' | 'error';

export interface UseOpencodePermissionOptions {
  // When true, fetch the cheap permission status on mount (and expose
  // ``refreshStatus`` for re-checks). The setup wizard sets this — it has no
  // provider probe and learns the state from GET /api/opencode/permission-status.
  // The Settings provider page leaves it false and feeds ``permissionAllowed``
  // from the value its provider load already returns.
  autoFetchStatus?: boolean;
}

export interface OpencodePermission {
  /** True once opencode.json carries ``permission: "allow"``. */
  permissionAllowed: boolean;
  /** Feed the value from an external source (e.g. the provider catalog load). */
  setPermissionAllowed: (value: boolean) => void;
  /** Write-action lifecycle state. */
  state: OpencodePermissionState;
  /** Latest success/error message from the write action. */
  message: string;
  /** True once an initial status fetch (or a successful write) has resolved. */
  statusLoaded: boolean;
  /** Cheap, read-only status re-check (no OpenCode server start). */
  refreshStatus: () => Promise<void>;
  /** Write ``permission: "allow"`` to opencode.json; flips state + the flag. */
  setupPermission: () => Promise<void>;
}

/**
 * Single source of truth for the OpenCode ``permission: "allow"`` affordance,
 * shared by the Settings provider page and the setup wizard so both behave
 * identically:
 *  - owns the write action (POST /api/opencode/setup-permission) plus its
 *    loading/success/error state and toast,
 *  - tracks ``permissionAllowed`` and flips it locally on a successful write so
 *    the affordance disappears immediately,
 *  - can cheaply fetch the current status (GET /api/opencode/permission-status,
 *    which reads opencode.json without starting the server) for callers that
 *    don't already have a provider probe to derive it from.
 */
export function useOpencodePermission(options: UseOpencodePermissionOptions = {}): OpencodePermission {
  const { autoFetchStatus = false } = options;
  const api = useApi();
  const { showToast } = useToast();
  const [permissionAllowed, setPermissionAllowed] = useState(false);
  const [state, setState] = useState<OpencodePermissionState>('idle');
  const [message, setMessage] = useState('');
  const [statusLoaded, setStatusLoaded] = useState(false);

  const refreshStatus = useCallback(async () => {
    try {
      const result = await api.opencodePermissionStatus();
      if (result.ok) {
        setPermissionAllowed(result.permission_allowed === true);
        // Mark loaded only on a definitive answer so callers can fail open: a
        // failed/unknown status must never satisfy a "permission resolved" gate
        // and trap the user — see the wizard's Continue gate.
        setStatusLoaded(true);
      }
    } catch {
      // Best-effort: keep the current value and leave statusLoaded false.
    }
  }, [api]);

  const setupPermission = useCallback(async () => {
    setState('loading');
    setMessage('');
    try {
      const result = await api.opencodeSetupPermission();
      setState(result.ok ? 'success' : 'error');
      setMessage(result.message);
      showToast(result.message, result.ok ? 'success' : 'error');
      if (result.ok) {
        // Flip locally so the affordance vanishes immediately rather than
        // waiting for the next status / provider refresh tick.
        setPermissionAllowed(true);
        setStatusLoaded(true);
      }
    } catch (e) {
      setState('error');
      const msg = e instanceof Error ? e.message : String(e);
      setMessage(msg);
      showToast(msg, 'error');
    }
  }, [api, showToast]);

  useEffect(() => {
    // Opt-in mount fetch (the setup wizard): read the current permission status
    // from opencode.json once so the affordance and the Continue gate reflect
    // reality. The setState lands after the network read inside refreshStatus —
    // a one-shot external-system sync, not a render loop.
    // eslint-disable-next-line react-hooks-js/set-state-in-effect
    if (autoFetchStatus) void refreshStatus();
  }, [autoFetchStatus, refreshStatus]);

  return {
    permissionAllowed,
    setPermissionAllowed,
    state,
    message,
    statusLoaded,
    refreshStatus,
    setupPermission,
  };
}
