import React, { useEffect, useRef, useState } from 'react';
import { useBackendOAuth } from './oauth/useBackendOAuth';
import { useTranslation } from 'react-i18next';
import { AlertTriangle, CheckCircle2, LogIn, Trash2, X } from 'lucide-react';

import { Button } from '../ui/button';
import { Label } from '../ui/label';
import { OAuthDeviceCodeRow, OAuthLinkRow, OAuthSubmitRow } from './oauth/OAuthFlowParts';
import { useApi, type OAuthWebMutationResult } from '@/context/ApiContext';
import { useToast } from '@/context/ToastContext';
import { surfaceBackendNotices } from './shared/surfaceBackendNotices';
import { errorMessage } from '@/lib/errorMessage';

type Backend = 'claude' | 'codex' | 'opencode';

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
  onSuccess?: () => void | Promise<void>;
  onRemoved?: (result: OAuthWebMutationResult) => boolean | void | Promise<boolean | void>;
  onFailure?: (error: string) => void | Promise<void>;
  onCancel?: () => void;
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
  onRemoved,
  onFailure,
  onCancel,
  onActiveChange,
}) => {
  const { t } = useTranslation();
  const api = useApi();
  const { showToast } = useToast();

  const { state, url, deviceCode, callbackKind, code, setCode, submitting, starting, error, setError,
    startFlow, cancelFlow, submitCallback, resetToIdle, copyUrl, copyDeviceCode, isActive } =
    useBackendOAuth({ backend, opencodeProviderId, onSuccess, onFailure, onCancel, onActiveChange });
  const [removing, setRemoving] = useState(false);
  const removalOwner = useRef({ mounted: true, busy: false });
  useEffect(() => {
    const owner = removalOwner.current; owner.mounted = true;
    return () => { owner.mounted = false; };
  }, []);

  const removeAuth = async () => {
    if (backend === 'opencode') {
      // OpenCode providers expose their own Remove-key affordance inline
      // on the parent page (DELETE /api/backend/opencode/provider/<id>/auth);
      // the OAuth panel just shouldn't render this button there.
      return;
    }
    if (removalOwner.current.busy) return;
    removalOwner.current.busy = true;
    setRemoving(true);
    onActiveChange?.(true);
    setError(null);
    try {
      const result =
        backend === 'claude' && canRemoveAuth && !signedIn
          ? await api.removeClaudeOAuthCredentials()
          : await api.removeBackendAuth(backend);
      if (!removalOwner.current.mounted) return;
      if (!result.ok) {
        showToast(
          t('settings.backends.oauthRemoveFailed', {
            detail: result.error || result.detail || 'unknown',
          }),
          'error',
        );
        await onRemoved?.(result);
        return;
      }
      resetToIdle();
      onActiveChange?.(true);
      if (result.notices) surfaceBackendNotices(result.notices, showToast, t);
      // Report partial persistence before readback can change the effective mode
      // and unmount this panel. The parent owns subsequent application errors.
      if (result.partial) {
        showToast(t('settings.backends.oauthRemovePartial', {
          detail: result.detail || result.warning || 'logout_failed',
        }), 'warning');
      }
      const observed = onRemoved ? await onRemoved(result) : await onSuccess?.();
      if (!removalOwner.current.mounted) return;
      if (result.restart?.ok === false) {
        setError(result.restart.message || t('onboarding.connection.applyFailed'));
      } else if (!result.partial && observed !== false) {
        showToast(t('settings.backends.oauthRemoved'), 'success');
      }
    } catch (err) {
      if (!removalOwner.current.mounted) return;
      showToast(
        t('settings.backends.oauthRemoveFailed', { detail: errorMessage(err) || 'unknown' }),
        'error',
      );
      await onRemoved?.({ ok: false, error: errorMessage(err) || 'unknown' });
    } finally {
      removalOwner.current.busy = false;
      if (removalOwner.current.mounted) { setRemoving(false); onActiveChange?.(false); }
    }
  };

  const showStartButton = state === 'idle' || state === 'success' || state === 'failed' || state === 'cancelled';
  const claudeAwaitingCode = backend === 'claude' && state === 'awaiting_code';
  // OpenCode browser-redirect providers (poe, gitlab, openai-browser)
  // return only ``url`` with no device code — the provider then redirects
  // to ``http://127.0.0.1:<port>/callback?…``. From a remote browser
  // that loopback is unreachable, so we ask the user to paste the URL
  // their browser landed on; the backend replays it from inside the
  // container so OpenCode's listener consumes it.
  const opencodeAwaitingCallback =
    backend === 'opencode' && state === 'awaiting_code' && (callbackKind === 'code' || callbackKind === 'redirect' || (!callbackKind && url && !deviceCode));
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
  const showRemoveAuth = canRemoveAuth ?? (signedIn || state === 'success');
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
          <CheckCircle2 className="mt-0.5 size-3.5 shrink-0 text-mint-ink" />
          <div className="flex flex-col gap-0.5">
            <p className="text-[12px] text-mint-ink">
              {(() => {
                if (backend === 'claude') return t('settings.backends.claudeOauthSignedIn');
                if (backend === 'codex') return t('settings.backends.codexOauthSignedIn');
                return t('settings.backends.opencodeProviderOauthSignedIn');
              })()}
            </p>
            {signedInDetail && (
              <p className="font-mono text-[11px] text-mint-ink/80">{signedInDetail}</p>
            )}
          </div>
        </div>
      )}

      {state === 'success' && signedIn && (
        <div className="flex items-start gap-2 rounded-md border border-mint/30 bg-mint-soft/40 px-3 py-2">
          <CheckCircle2 className="mt-0.5 size-3.5 shrink-0 text-mint-ink" />
          <p className="text-[12px] text-mint-ink">{t('settings.backends.oauthSuccess')}</p>
        </div>
      )}

      {error && (
        <div className="flex items-start gap-2 rounded-md border border-destructive/30 bg-destructive/[0.08] px-3 py-2">
          <AlertTriangle className="mt-0.5 size-3.5 shrink-0 text-destructive-ink" />
          <p className="text-[12px] leading-relaxed text-destructive-ink">
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
          <OAuthLinkRow
            url={url}
            onCopy={(e) => void copyUrl(e)}
            copyLabel={t('common.copy') as string}
          />
        </div>
      )}

      {showDeviceBlock && (
        <div className="flex flex-col gap-2 rounded-md border border-border bg-background px-3 py-2.5">
          <Label className="text-[11px] font-medium uppercase tracking-wide text-muted">
            {t('settings.backends.codexDeviceCodeLabel')}
          </Label>
          <OAuthDeviceCodeRow
            code={deviceCode ?? ''}
            onCopy={(e) => void copyDeviceCode(e)}
            copyLabel={t('common.copy') as string}
          />
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
          <OAuthSubmitRow
            id={`oauth-code-${backend}`}
            value={code}
            onChange={setCode}
            onSubmit={() => void submitCallback()}
            submitting={submitting}
            placeholder={t('settings.backends.claudeCallbackCodePlaceholder') as string}
            submitLabel={t('common.submit') as string}
            submittingLabel={t('common.submitting') as string}
          />
          <p className="text-[12px] leading-relaxed text-muted">
            {t('settings.backends.claudeCallbackCodeHint')}
          </p>
        </div>
      )}

      {opencodeAwaitingCallback && (
        <div className="flex flex-col gap-2">
          <Label htmlFor={`oauth-code-${backend}`} className="text-xs font-medium uppercase text-muted">
            {t(callbackKind === 'code' ? 'onboarding.connection.manualCode' : 'settings.backends.opencodeCallbackUrlLabel')}
          </Label>
          <OAuthSubmitRow
            id={`oauth-code-${backend}`}
            value={code}
            onChange={setCode}
            onSubmit={() => void submitCallback()}
            submitting={submitting}
            placeholder={callbackKind === 'code' ? t('onboarding.connection.manualCodePlaceholder') : 'http://127.0.0.1:..../callback?code=...'}
            submitLabel={t('common.submit') as string}
            submittingLabel={t('common.submitting') as string}
          />
          <p className="text-[12px] leading-relaxed text-muted">
            {t(callbackKind === 'code' ? 'onboarding.connection.manualCodeHint' : 'settings.backends.opencodeCallbackUrlHint')}
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
            className="text-destructive-ink hover:bg-destructive/10 hover:text-destructive-ink"
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
