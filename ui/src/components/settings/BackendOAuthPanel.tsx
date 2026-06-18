import React, { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { AlertTriangle, CheckCircle2, Copy, ExternalLink, LogIn, Trash2, X } from 'lucide-react';
import clsx from 'clsx';

import { Button } from '../ui/button';
import { Input } from '../ui/input';
import { Label } from '../ui/label';
import { useApi } from '@/context/ApiContext';
import type { OAuthWebState } from '@/context/ApiContext';
import { useToast } from '@/context/ToastContext';

type Backend = 'claude' | 'codex' | 'opencode';

type LocalState = 'idle' | OAuthWebState;

const POLL_INTERVAL_MS = 2000;
// Stop polling 16 minutes in (matches AgentAuthService.setup_timeout_seconds
// which gives the user 15 minutes to complete login). Anything still
// polling past that has been abandoned and just burns the UI server's
// background OAuth loop.
const POLL_DEADLINE_MS = 16 * 60 * 1000;

export type BackendOAuthPanelProps = {
  backend: Backend;
  /** Required when ``backend === "opencode"``; the OpenCode daemon needs
   *  to know which provider's OAuth flow to kick off (each provider has
   *  its own authorize endpoint). Ignored for claude / codex. */
  opencodeProviderId?: string;
  /** When ``true`` the user is already signed in (read from the backend's
   *  ``get*Auth`` endpoint). We still render a "re-authenticate" button so
   *  rotating credentials without dropping to a terminal stays one click. */
  signedIn: boolean;
  /** When ``true`` render a Claude-only cleanup affordance for stale OAuth
   *  tokens that are not the currently active auth source. */
  canRemoveAuth?: boolean;
  /** Heading text shown on the panel (e.g. "Claude account login"). */
  title: string;
  /** Short paragraph under the heading describing what login does. */
  subtitle: string;
  /** Optional one-line identifier rendered under the "signed in" banner
   *  (e.g. "alice@example.com · Pro" for Codex's ChatGPT account). When
   *  ``null`` / undefined the banner shows only the generic "signed in"
   *  copy. Plaintext only — never leak tokens here. */
  signedInDetail?: string | null;
  /** Hide the Sign out / Remove auth button (e.g. OpenCode providers
   *  expose their own DELETE-credentials affordance inline). */
  hideRemove?: boolean;
  /** Optional callback fired once after the flow lands on ``state === "success"``.
   *  The parent typically re-reads ``getClaudeAuth`` / ``getCodexAuth`` here so
   *  the on-screen "signed in" indicators move. */
  onSuccess?: () => void;
  /** Fires whenever the panel's internal flow is mid-handshake
   *  (``state`` ∈ {starting, awaiting_code, verifying}). The parent
   *  uses this to disable auth-mode switching: on iOS Safari the
   *  device-code "Copy" tap was bouncing the surrounding OAuth /
   *  API-Key radio, which would tear down the in-progress flow. The
   *  callback keeps the source of truth here in the panel and lets
   *  the parent freeze the tab without duplicating state. */
  onActiveChange?: (active: boolean) => void;
};

/**
 * Drives the Settings → Backends OAuth flow that mirrors the IM ``/setup``
 * command. Calls the four web endpoints added in PR #282 R5:
 *
 *   - POST /api/backend/<backend>/auth/oauth/start
 *   - GET  /api/backend/<backend>/auth/oauth/status/<flow_id>
 *   - POST /api/backend/<backend>/auth/oauth/submit-code  (Claude only)
 *   - POST /api/backend/<backend>/auth/oauth/cancel
 *
 * Claude returns a manual auth URL + asks for a callback code; Codex
 * returns a device URL + device code and self-completes when the user
 * finishes auth on OpenAI's side. The state machine + polling are shared.
 */
export const BackendOAuthPanel: React.FC<BackendOAuthPanelProps> = ({
  backend,
  opencodeProviderId,
  signedIn,
  canRemoveAuth,
  title,
  subtitle,
  signedInDetail,
  hideRemove,
  onSuccess,
  onActiveChange,
}) => {
  const { t } = useTranslation();
  const api = useApi();
  const { showToast } = useToast();

  const [state, setState] = useState<LocalState>('idle');
  const [flowId, setFlowId] = useState<string | null>(null);
  const [url, setUrl] = useState<string | null>(null);
  const [deviceCode, setDeviceCode] = useState<string | null>(null);
  const [code, setCode] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [starting, setStarting] = useState(false);
  const [removing, setRemoving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const pollTimer = useRef<number | null>(null);
  const pollDeadlineRef = useRef<number | null>(null);
  const onSuccessRef = useRef(onSuccess);
  onSuccessRef.current = onSuccess;

  const stopPolling = () => {
    if (pollTimer.current !== null) {
      window.clearTimeout(pollTimer.current);
      pollTimer.current = null;
    }
    pollDeadlineRef.current = null;
  };

  const resetToIdle = () => {
    stopPolling();
    setState('idle');
    setFlowId(null);
    setUrl(null);
    setDeviceCode(null);
    setCode('');
    setError(null);
  };

  // Broadcast in-progress flow state to the parent so it can lock the
  // auth-mode segmented radio. On iOS Safari the device-code "Copy" tap
  // was bouncing that radio mid-flow — we can't reproduce the exact
  // event path in code, but freezing the radio while in-flight makes
  // the bug impossible to trigger regardless of how the rogue event
  // gets there.
  const onActiveChangeRef = useRef(onActiveChange);
  onActiveChangeRef.current = onActiveChange;
  useEffect(() => {
    const active = state === 'starting' || state === 'awaiting_code' || state === 'verifying';
    onActiveChangeRef.current?.(active);
  }, [state]);

  // Poll the status endpoint until the flow lands on a terminal state.
  // The server holds the flow alive across requests on its own event loop,
  // so dropping the timer on success/failure is safe — we'll never miss a
  // transition by stopping early.
  useEffect(() => {
    return () => stopPolling();
  }, []);

  const scheduleNextPoll = (currentFlowId: string) => {
    if (pollDeadlineRef.current !== null && Date.now() > pollDeadlineRef.current) {
      setError(t('settings.backends.oauthPollTimedOut') as string);
      setState('failed');
      stopPolling();
      return;
    }
    pollTimer.current = window.setTimeout(() => {
      void pollOnce(currentFlowId);
    }, POLL_INTERVAL_MS);
  };

  const pollOnce = async (currentFlowId: string) => {
    try {
      const data = await api.getOAuthWebStatus(backend, currentFlowId);
      if (!data.ok) {
        setError(data.error || 'flow_not_found');
        setState('failed');
        stopPolling();
        return;
      }
      if (data.url) setUrl(data.url);
      if (data.device_code) setDeviceCode(data.device_code);
      if (data.state) setState(data.state);
      if (data.state === 'success') {
        stopPolling();
        showToast(t('settings.backends.oauthSuccess'), 'success');
        onSuccessRef.current?.();
        return;
      }
      if (data.state === 'failed' || data.state === 'cancelled') {
        setError(data.error || (data.state === 'cancelled' ? 'cancelled' : 'unknown_failure'));
        stopPolling();
        return;
      }
      scheduleNextPoll(currentFlowId);
    } catch (err: any) {
      setError(err?.message || 'poll_failed');
      setState('failed');
      stopPolling();
    }
  };

  const startFlow = async () => {
    setStarting(true);
    setError(null);
    setCode('');
    try {
      const result =
        backend === 'opencode'
          ? await api.startOAuthWebForOpencodeProvider(opencodeProviderId || '', true)
          : await api.startOAuthWeb(backend, true);
      if (!result.ok || !result.flow_id) {
        setError(result.error || result.detail || 'start_failed');
        setState('failed');
        return;
      }
      setFlowId(result.flow_id);
      setUrl(result.url || null);
      setDeviceCode(result.device_code || null);
      setState(result.state || 'starting');
      pollDeadlineRef.current = Date.now() + POLL_DEADLINE_MS;
      scheduleNextPoll(result.flow_id);
    } catch (err: any) {
      setError(err?.message || 'start_failed');
      setState('failed');
    } finally {
      setStarting(false);
    }
  };

  const cancelFlow = async () => {
    if (!flowId) {
      resetToIdle();
      return;
    }
    stopPolling();
    try {
      await api.cancelOAuthWeb(backend, flowId);
    } catch (err) {
      // Swallow — even if the cancel rountrip failed, the UI should still
      // return to the idle state so the user can retry without a stuck
      // panel. The server-side flow times out on its own at 900s.
    }
    resetToIdle();
  };

  const submitCallback = async () => {
    if (!flowId) return;
    const trimmed = code.trim();
    if (!trimmed) return;
    setSubmitting(true);
    setError(null);
    try {
      // Claude → ``code#state`` (Anthropic's callback fragment).
      // OpenCode browser-redirect → ``http://127.0.0.1:<port>/callback?...``
      // (the URL the provider redirected to). Both flow through the
      // same endpoint; the server side dispatches by backend type.
      const result = await api.submitOAuthWebCode(backend, flowId, trimmed);
      if (!result.ok) {
        setError(result.error || result.detail || 'submit_failed');
        return;
      }
      // Server transitioned the flow to "verifying"; reflect that locally
      // so the UI hides the code input immediately rather than waiting for
      // the next poll tick.
      setState('verifying');
    } catch (err: any) {
      setError(err?.message || 'submit_failed');
    } finally {
      setSubmitting(false);
    }
  };

  const removeAuth = async () => {
    if (backend === 'opencode') {
      // OpenCode providers expose their own Remove-key affordance inline
      // on the parent page (DELETE /api/backend/opencode/provider/<id>/auth);
      // the OAuth panel just shouldn't render this button there.
      return;
    }
    setRemoving(true);
    setError(null);
    try {
      const result =
        backend === 'claude' && canRemoveAuth && !signedIn
          ? await api.removeClaudeOAuthCredentials()
          : await api.removeBackendAuth(backend);
      if (!result.ok) {
        showToast(
          t('settings.backends.oauthRemoveFailed', {
            detail: result.error || result.detail || 'unknown',
          }),
          'error',
        );
        return;
      }
      resetToIdle();
      if (result.partial) {
        // V2Config got cleared, but the CLI logout subprocess failed —
        // credentials may still live on disk. Surface a warning toast
        // with the detail so the user knows to investigate (e.g.
        // ``codex logout`` returning "Not logged in" is harmless;
        // a real failure means the user has to run logout manually).
        showToast(
          t('settings.backends.oauthRemovePartial', {
            detail: result.detail || result.warning || 'logout_failed',
          }),
          'warning',
        );
      } else {
        showToast(t('settings.backends.oauthRemoved'), 'success');
      }
      onSuccessRef.current?.();
    } catch (err: any) {
      showToast(
        t('settings.backends.oauthRemoveFailed', { detail: err?.message || 'unknown' }),
        'error',
      );
    } finally {
      setRemoving(false);
    }
  };

  const copyUrl = async (e?: React.MouseEvent) => {
    // Defensively stop the click from bubbling. If a parent had an
    // accidental form / radio handler, copying the URL must never
    // also trigger a tab switch or a save.
    e?.preventDefault();
    e?.stopPropagation();
    if (!url) return;
    try {
      await navigator.clipboard.writeText(url);
      showToast(t('settings.backends.oauthUrlCopied'), 'success');
    } catch {
      showToast(t('common.copyFailed'), 'error');
    }
  };

  const copyDeviceCode = async (e?: React.MouseEvent) => {
    // Same defensive bubble-stop as copyUrl. Reported symptom: clicking
    // "Copy" on the Codex device code in Settings was bouncing the auth-
    // mode segmented radio from OAuth to API Key, interrupting the
    // login flow. Without seeing a repro path in code, guard the
    // click here so the side effect can't survive in any browser.
    e?.preventDefault();
    e?.stopPropagation();
    if (!deviceCode) return;
    try {
      await navigator.clipboard.writeText(deviceCode);
      showToast(t('settings.backends.oauthDeviceCodeCopied'), 'success');
    } catch {
      showToast(t('common.copyFailed'), 'error');
    }
  };

  const isActive = state !== 'idle' && state !== 'success' && state !== 'failed' && state !== 'cancelled';
  const showStartButton = state === 'idle' || state === 'success' || state === 'failed' || state === 'cancelled';
  const claudeAwaitingCode = backend === 'claude' && state === 'awaiting_code';
  // OpenCode browser-redirect providers (poe, gitlab, openai-browser)
  // return only ``url`` with no device code — the provider then redirects
  // to ``http://127.0.0.1:<port>/callback?…``. From a remote browser
  // that loopback is unreachable, so we ask the user to paste the URL
  // their browser landed on; the backend replays it from inside the
  // container so OpenCode's listener consumes it.
  const opencodeAwaitingCallback =
    backend === 'opencode' && state === 'awaiting_code' && url && !deviceCode;
  // OpenCode device flows (openai headless, github-copilot) carry the
  // user-facing code in the same payload as Codex; reuse the same UI
  // affordance. Browser-redirect flows (gitlab, poe, openai browser)
  // don't surface a code — only the URL.
  const codexShowDevice = backend === 'codex' && state === 'awaiting_code' && url && deviceCode;
  const opencodeShowDevice = backend === 'opencode' && state === 'awaiting_code' && url && deviceCode;
  const showDeviceBlock = codexShowDevice || opencodeShowDevice;

  const startLabel = signedIn
    ? t('settings.backends.oauthReauthenticate')
    : (() => {
        if (backend === 'claude') return t('settings.backends.claudeSignInButton');
        if (backend === 'codex') return t('settings.backends.codexSignInButton');
        return t('settings.backends.opencodeProviderSignIn');
      })();
  const showRemoveAuth = (canRemoveAuth ?? signedIn) || state === 'success';
  const removeLabel =
    backend === 'claude' && canRemoveAuth && !signedIn
      ? t('settings.backends.oauthCleanStoredCredentials')
      : t('settings.backends.oauthRemove');

  return (
    <div className="flex flex-col gap-4 rounded-lg border border-border bg-surface-2/60 p-4">
      <div className="flex flex-col gap-1">
        <p className="text-[13px] font-semibold text-foreground">{title}</p>
        <p className="text-[12px] leading-relaxed text-muted">{subtitle}</p>
      </div>

      {signedIn && state === 'idle' && (
        <div className="flex items-start gap-2 rounded-md border border-mint/30 bg-mint-soft/40 px-3 py-2">
          <CheckCircle2 className="mt-0.5 size-3.5 shrink-0 text-mint" />
          <div className="flex flex-col gap-0.5">
            <p className="text-[12px] text-mint">
              {(() => {
                if (backend === 'claude') return t('settings.backends.claudeOauthSignedIn');
                if (backend === 'codex') return t('settings.backends.codexOauthSignedIn');
                return t('settings.backends.opencodeProviderOauthSignedIn');
              })()}
            </p>
            {signedInDetail && (
              <p className="font-mono text-[11px] text-mint/80">{signedInDetail}</p>
            )}
          </div>
        </div>
      )}

      {state === 'success' && (
        <div className="flex items-start gap-2 rounded-md border border-mint/30 bg-mint-soft/40 px-3 py-2">
          <CheckCircle2 className="mt-0.5 size-3.5 shrink-0 text-mint" />
          <p className="text-[12px] text-mint">{t('settings.backends.oauthSuccess')}</p>
        </div>
      )}

      {error && (
        <div className="flex items-start gap-2 rounded-md border border-destructive/30 bg-destructive/[0.08] px-3 py-2">
          <AlertTriangle className="mt-0.5 size-3.5 shrink-0 text-destructive" />
          <p className="text-[12px] leading-relaxed text-destructive">
            {t('settings.backends.oauthFailed', { detail: error })}
          </p>
        </div>
      )}

      {state === 'starting' && (
        <p className="text-[12px] text-muted">{t('settings.backends.oauthStarting')}</p>
      )}

      {state === 'verifying' && (
        <p className="text-[12px] text-muted">{t('settings.backends.oauthVerifying')}</p>
      )}

      {url && (state === 'awaiting_code' || state === 'starting' || state === 'verifying') && (
        <div className="flex flex-col gap-2 rounded-md border border-border bg-background px-3 py-2.5">
          <Label className="text-[11px] font-medium uppercase tracking-wide text-muted">
            {t('settings.backends.oauthAuthUrlLabel')}
          </Label>
          <div className="flex flex-wrap items-center gap-2">
            <a
              href={url}
              target="_blank"
              rel="noopener noreferrer"
              className={clsx(
                'inline-flex max-w-full items-center gap-1.5 break-all rounded-md',
                'bg-cyan-soft/40 px-2 py-1 font-mono text-[12px] text-cyan',
                'transition-colors hover:bg-cyan-soft hover:text-cyan',
              )}
            >
              <ExternalLink className="size-3 shrink-0" />
              <span className="break-all">{url}</span>
            </a>
            <Button type="button" variant="secondary" size="xs" onClick={(e) => void copyUrl(e)}>
              <Copy className="size-3" />
              {t('common.copy')}
            </Button>
          </div>
        </div>
      )}

      {showDeviceBlock && (
        <div className="flex flex-col gap-2 rounded-md border border-border bg-background px-3 py-2.5">
          <Label className="text-[11px] font-medium uppercase tracking-wide text-muted">
            {t('settings.backends.codexDeviceCodeLabel')}
          </Label>
          <div className="flex flex-wrap items-center gap-2">
            <code className="rounded-md bg-cyan-soft/40 px-2.5 py-1 font-mono text-[14px] font-semibold tracking-[0.18em] text-cyan">
              {deviceCode}
            </code>
            <Button type="button" variant="secondary" size="xs" onClick={(e) => void copyDeviceCode(e)}>
              <Copy className="size-3" />
              {t('common.copy')}
            </Button>
          </div>
          <p className="text-[12px] leading-relaxed text-muted">
            {t('settings.backends.codexDeviceInstructions')}
          </p>
        </div>
      )}

      {claudeAwaitingCode && (
        <div className="flex flex-col gap-2">
          <Label htmlFor={`oauth-code-${backend}`} className="text-xs font-medium uppercase text-muted">
            {t('settings.backends.claudeCallbackCodeLabel')}
          </Label>
          <div className="flex gap-2">
            <Input
              id={`oauth-code-${backend}`}
              type="text"
              autoComplete="off"
              spellCheck={false}
              placeholder={t('settings.backends.claudeCallbackCodePlaceholder') as string}
              value={code}
              onChange={(e) => setCode(e.target.value)}
              className="font-mono"
              disabled={submitting}
            />
            <Button
              type="button"
              variant="brand"
              size="sm"
              onClick={() => void submitCallback()}
              disabled={submitting || !code.trim()}
            >
              {submitting ? t('common.submitting') : t('common.submit')}
            </Button>
          </div>
          <p className="text-[12px] leading-relaxed text-muted">
            {t('settings.backends.claudeCallbackCodeHint')}
          </p>
        </div>
      )}

      {opencodeAwaitingCallback && (
        <div className="flex flex-col gap-2">
          <Label htmlFor={`oauth-code-${backend}`} className="text-xs font-medium uppercase text-muted">
            {t('settings.backends.opencodeCallbackUrlLabel')}
          </Label>
          <div className="flex gap-2">
            <Input
              id={`oauth-code-${backend}`}
              type="text"
              autoComplete="off"
              spellCheck={false}
              placeholder="http://127.0.0.1:..../callback?code=..."
              value={code}
              onChange={(e) => setCode(e.target.value)}
              className="font-mono"
              disabled={submitting}
            />
            <Button
              type="button"
              variant="brand"
              size="sm"
              onClick={() => void submitCallback()}
              disabled={submitting || !code.trim()}
            >
              {submitting ? t('common.submitting') : t('common.submit')}
            </Button>
          </div>
          <p className="text-[12px] leading-relaxed text-muted">
            {t('settings.backends.opencodeCallbackUrlHint')}
          </p>
        </div>
      )}

      <div className="flex items-center justify-between gap-2">
        {showStartButton ? (
          <Button
            type="button"
            variant="brand"
            size="default"
            onClick={() => void startFlow()}
            disabled={starting || removing}
          >
            <LogIn className="size-3.5" />
            {starting ? t('common.loading') : startLabel}
          </Button>
        ) : (
          <span className="text-[12px] text-muted">
            {t('settings.backends.oauthInProgress')}
          </span>
        )}
        {!hideRemove && showRemoveAuth && state !== 'starting' && state !== 'awaiting_code' && state !== 'verifying' && (
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={() => void removeAuth()}
            disabled={removing || starting}
            className="text-destructive hover:bg-destructive/10 hover:text-destructive"
          >
            <Trash2 className="size-3.5" />
            {removing ? t('common.removing') : removeLabel}
          </Button>
        )}
        {isActive && (
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={() => void cancelFlow()}
          >
            <X className="size-3.5" />
            {t('common.cancel')}
          </Button>
        )}
      </div>
    </div>
  );
};
