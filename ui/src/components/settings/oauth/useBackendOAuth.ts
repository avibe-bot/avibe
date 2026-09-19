import { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useApi, type OAuthWebStartResult, type OAuthWebState } from '@/context/ApiContext';
import { useToast } from '@/context/ToastContext';
import { errorMessage } from '@/lib/errorMessage';
import { isNativeAuthHubOwned } from '@/lib/nativeAuthOwnership';

export type OAuthBackend = 'claude' | 'codex' | 'opencode';
const POLL_INTERVAL_MS = 2000;
const POLL_DEADLINE_MS = 16 * 60 * 1000;

/** One owner for Settings and onboarding: invalidation precedes cancellation. */
export function useBackendOAuth({ backend, opencodeProviderId, onSuccess, onFailure, onCancel, onActiveChange, onPendingChange }: {
  backend: OAuthBackend;
  opencodeProviderId?: string;
  onSuccess?: () => void | Promise<void>;
  /** Observe persisted credentials when a terminal server failure follows commit. */
  onFailure?: (error: string) => void | Promise<void>;
  /** Invalidate an in-flight confirmation before explicit cancellation. */
  onCancel?: () => void;
  onActiveChange?: (active: boolean) => void;
  onPendingChange?: (pending: boolean) => void;
}) {
  const api = useApi();
  const { t } = useTranslation();
  const { showToast } = useToast();
  const [state, setState] = useState<'idle' | OAuthWebState>('idle');
  const [url, setUrl] = useState<string | null>(null);
  const [deviceCode, setDeviceCode] = useState<string | null>(null);
  const [callbackKind, setCallbackKind] = useState<OAuthWebStartResult['callback_kind']>(null);
  const [code, setCode] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [hubOwnedAuth, setHubOwnedAuth] = useState(false);
  const owner = useRef({ generation: 0, mounted: true, flowId: '', busy: false, submitting: false, deadline: 0 });
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const callbacks = useRef({ onSuccess, onFailure, onCancel, onActiveChange, onPendingChange });
  callbacks.current = { onSuccess, onFailure, onCancel, onActiveChange, onPendingChange };
  const pending = useRef(new Set<number>());
  const settled = useCallback((generation: number) => {
    if (pending.current.delete(generation)) {
      const remaining = pending.current.size > 0;
      callbacks.current.onPendingChange?.(remaining);
      if (!remaining && owner.current.mounted) {
        owner.current.busy = false;
        setStarting(false);
        callbacks.current.onActiveChange?.(false);
      }
    }
  }, []);
  const current = (generation: number) => owner.current.mounted && owner.current.generation === generation;
  const stopPolling = () => { clearTimeout(timer.current); timer.current = undefined; };
  const cancelRemote = useCallback(async (id: string) => {
    try {
      const result = await api.cancelOAuthWeb(backend, id);
      if (!result.ok && result.error !== 'flow_not_found') throw new Error(result.detail || result.error);
    } catch (err) {
      // Keep a cancellation failure observable, including after the dialog closes.
      showToast(errorMessage(err) || t('settings.backends.oauthFailed', { detail: 'cancel_failed' }), 'warning');
    }
  }, [api, backend, showToast, t]);
  useEffect(() => {
    owner.current.mounted = true;
    const lifetime = owner.current;
    return () => {
      lifetime.mounted = false;
      const generation = lifetime.generation++;
      clearTimeout(timer.current);
      if (lifetime.flowId) void cancelRemote(lifetime.flowId).finally(() => settled(generation));
      // A pending start owns its settlement until its returned flow is cancelled.
      lifetime.flowId = '';
      lifetime.busy = false;
      callbacks.current.onActiveChange?.(false);
    };
  }, [cancelRemote, opencodeProviderId, settled]);

  const resetToIdle = () => {
    stopPolling();
    owner.current.generation += 1;
    owner.current.flowId = '';
    owner.current.busy = false;
    owner.current.submitting = false;
    setState('idle'); setUrl(null); setDeviceCode(null); setCode(''); setError(null); setHubOwnedAuth(false);
    setStarting(false); setSubmitting(false);
    callbacks.current.onActiveChange?.(false);
  };
  const settleHubOwnedAuth = (generation: number) => {
    stopPolling();
    owner.current.generation += 1;
    owner.current.flowId = '';
    owner.current.busy = false;
    owner.current.submitting = false;
    setStarting(false);
    setSubmitting(false);
    setHubOwnedAuth(true);
    setError(null);
    setState('failed');
    settled(generation);
    callbacks.current.onActiveChange?.(false);
  };
  const accept = async (data: OAuthWebStartResult, generation: number) => {
    if (!current(generation)) return;
    if (!data.ok) {
      if (isNativeAuthHubOwned(data)) {
        settleHubOwnedAuth(generation);
        return;
      }
      throw new Error(data.detail || data.error || 'flow_not_found');
    }
    if (data.callback_kind) setCallbackKind(data.callback_kind);
    if (data.url) setUrl(data.url);
    if (data.device_code) setDeviceCode(data.device_code);
    if (data.state === 'success') {
      stopPolling();
      // The success consumer must re-read effective auth/application before the
      // presentation can claim success. It may reject while application fails.
      setState('verifying');
      await callbacks.current.onSuccess?.();
      if (!current(generation)) return;
      owner.current.flowId = '';
      owner.current.busy = false;
      settled(generation);
      setState('success');
      callbacks.current.onActiveChange?.(false);
      return;
    }
    if (data.state === 'failed' || data.state === 'cancelled') {
      if (data.state === 'failed' && isNativeAuthHubOwned(data)) {
        settleHubOwnedAuth(generation);
        return;
      }
      if (data.state === 'failed') await callbacks.current.onFailure?.(data.error || data.state);
      if (!current(generation)) return;
      throw new Error(data.error || data.state);
    }
    setState(data.state || 'starting');
    if (Date.now() > owner.current.deadline) throw new Error(t('settings.backends.oauthPollTimedOut'));
    timer.current = setTimeout(() => { void poll(generation); }, POLL_INTERVAL_MS);
  };
  const fail = (err: unknown, generation: number) => {
    if (!current(generation)) return;
    stopPolling();
    setHubOwnedAuth(false);
    setError(errorMessage(err) || 'auth_failed'); setState('failed');
    // Cancel a failed local polling attempt as well; it must not finish later
    // behind a newly selected method. Server cancellation never logs out.
    const id = owner.current.flowId;
    owner.current.flowId = '';
    // Keep method/key writes locked until server cancellation settles.
    if (id) void cancelRemote(id).finally(() => settled(generation));
    else settled(generation);
  };
  const poll = async (generation: number) => {
    if (!current(generation)) return;
    try {
      const result = await api.getOAuthWebStatus(backend, owner.current.flowId);
      await accept({ ...result, error: result.error || undefined }, generation);
    } catch (err) { fail(err, generation); }
  };
  const startFlow = async () => {
    if (owner.current.busy || pending.current.size > 0) return;
    const generation = ++owner.current.generation;
    pending.current.add(generation);
    callbacks.current.onPendingChange?.(true);
    owner.current.busy = true;
    owner.current.deadline = Date.now() + POLL_DEADLINE_MS;
    setStarting(true); setState('starting'); setError(null); setHubOwnedAuth(false); setCode(''); setUrl(null); setDeviceCode(null); setCallbackKind(null);
    callbacks.current.onActiveChange?.(true);
    try {
      const result = backend === 'opencode'
        ? await api.startOAuthWebForOpencodeProvider(opencodeProviderId || '', false)
        : await api.startOAuthWeb(backend, false);
      if (!current(generation)) {
        if (result.flow_id) await cancelRemote(result.flow_id);
        settled(generation);
        return;
      }
      owner.current.flowId = result.flow_id || '';
      // Terminal ownership refusals do not create a local OAuth flow. Let the
      // shared response consumer classify that payload before applying the
      // ordinary missing-flow failure.
      if (!result.flow_id && !isNativeAuthHubOwned(result)) {
        throw new Error(result.detail || result.error || 'start_failed');
      }
      await accept(result, generation);
    } catch (err) { if (current(generation)) fail(err, generation); else settled(generation); }
    finally { if (current(generation)) setStarting(false); }
  };
  const cancelFlow = async () => {
    callbacks.current.onCancel?.();
    const id = owner.current.flowId;
    const generation = owner.current.generation;
    resetToIdle();
    if (pending.current.size > 0) {
      owner.current.busy = true; setStarting(true);
      callbacks.current.onActiveChange?.(true);
    }
    if (id) {
      owner.current.busy = true;
      setStarting(true);
      await cancelRemote(id);
      settled(generation);
      owner.current.busy = false;
      if (owner.current.mounted) setStarting(false);
    }
  };
  const submitCallback = async () => {
    if (!owner.current.flowId || owner.current.submitting || !code.trim()) return;
    const generation = owner.current.generation;
    owner.current.submitting = true; setSubmitting(true); setError(null);
    try {
      const result = await api.submitOAuthWebCode(backend, owner.current.flowId, code.trim());
      if (!current(generation)) return;
      if (!result.ok) {
        if (isNativeAuthHubOwned(result)) {
          settleHubOwnedAuth(generation);
          return;
        }
        throw new Error(result.detail || result.error || 'submit_failed');
      }
      setState('verifying');
    } catch (err) { if (current(generation)) setError(errorMessage(err) || 'submit_failed'); }
    finally {
      if (current(generation)) { owner.current.submitting = false; setSubmitting(false); }
    }
  };
  const copy = async (text: string | null, event?: React.MouseEvent) => {
    event?.preventDefault(); event?.stopPropagation();
    if (!text) return;
    try { await navigator.clipboard.writeText(text); showToast(t('common.copied'), 'success'); }
    catch { showToast(t('common.copyFailed'), 'error'); }
  };
  const isActive = state === 'starting' || state === 'awaiting_code' || state === 'verifying';
  return { state, url, deviceCode, callbackKind, code, setCode, submitting, starting, error, setError, hubOwnedAuth,
    isActive, startFlow, cancelFlow, submitCallback, resetToIdle,
    copyUrl: (e?: React.MouseEvent) => copy(url, e), copyDeviceCode: (e?: React.MouseEvent) => copy(deviceCode, e) };
}
