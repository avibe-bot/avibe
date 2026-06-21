import React, { useEffect, useMemo, useState } from 'react';
import { Bot, CheckCircle2, Info, KeyRound, Pencil, RotateCcw, Save, Trash2 } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Badge } from '../../ui/badge';
import { Button } from '../../ui/button';
import { Card, CardContent } from '../../ui/card';
import { Input } from '../../ui/input';
import { Label } from '../../ui/label';
import { useApi } from '@/context/ApiContext';
import type { CodexAuthMode, CodexAuthState } from '@/context/ApiContext';
import { useToast } from '@/context/ToastContext';
import { BackendOAuthPanel } from '../BackendOAuthPanel';
import { BackendTestPanel } from '../BackendTestPanel';
import { BackendRuntimeCard } from '../shared/BackendRuntimeCard';
import { SegmentedRadio } from '../shared/SegmentedRadio';
import { surfaceBackendNotices } from '../shared/surfaceBackendNotices';
import { useBackendRuntime } from '../shared/useBackendRuntime';
import { useOAuthFlowLock } from '../shared/useOAuthFlowLock';

const BACKEND_ID = 'codex';
const DEFAULT_CLI = 'codex';

export const CodexProviderConfig: React.FC<{
  hideEnableToggle?: boolean;
  deferRestart?: boolean;
}> = ({ hideEnableToggle, deferRestart } = {}) => {
  const { t } = useTranslation();
  const api = useApi();
  const { showToast } = useToast();

  // Runtime state — shared across all backend pages via the hook. Adds
  // the Runtime card affordances (lifecycle chip / toggle / CLI path
  // detect / install) that #282 omitted from the Codex page.
  const runtime = useBackendRuntime({
    backend: BACKEND_ID,
    defaultCli: DEFAULT_CLI,
    deferRestart,
  });

  const [state, setState] = useState<CodexAuthState | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [removingKey, setRemovingKey] = useState(false);
  const [authMode, setAuthMode] = useState<CodexAuthMode>('oauth');
  const [apiKey, setApiKey] = useState('');
  // ``editingKey`` distinguishes "showing the saved masked key" (false,
  // input is read-only and pre-filled with ``api_key_masked``) from "user
  // is typing a fresh secret" (true, input is editable + empty). The
  // pencil button flips it on; saving or reloading flips it back off.
  const [editingKey, setEditingKey] = useState(false);
  const [baseUrl, setBaseUrl] = useState('');
  // ``savedAuthMode`` / ``savedBaseUrl`` snapshot the last successfully
  // loaded (or saved) values so we can show the Save button only when
  // the user has actually changed something. Page feedback: a Save
  // button that never has a no-op state is noisy and confusing.
  const [savedAuthMode, setSavedAuthMode] = useState<CodexAuthMode>('oauth');
  const [savedBaseUrl, setSavedBaseUrl] = useState('');
  // Freeze the auth-mode segmented radio while the OAuth panel is mid-
  // handshake. Shared with Claude / OpenCode via ``useOAuthFlowLock``.
  const { oauthFlowActive, setOauthFlowActive, guardedSetAuthMode } =
    useOAuthFlowLock<CodexAuthMode>({
      setAuthMode,
      warnTag: 'codex-auth-mode',
    });

  useEffect(() => {
    let cancelled = false;
    api
      .getCodexAuth()
      .then((data) => {
        if (cancelled) return;
        setState(data);
        // Default the radio to whichever auth source the running CLI is
        // *actually* using (``active_auth_mode``). V2Config's
        // ``auth_mode`` defaults to ``"oauth"`` on fresh installs, which
        // would otherwise force the OAuth tab even when the user has a
        // working API key on disk (e.g. they pre-configured ``auth.json``
        // by hand, or a prior Sign out wiped V2Config but the relay
        // config in ``config.toml`` still points at a custom endpoint).
        // ``active_auth_mode === "none"`` means we have nothing on disk
        // either, so honour V2Config's saved intent in that fallback case.
        const initialMode =
          data.active_auth_mode !== 'none' ? data.active_auth_mode : data.auth_mode;
        const initialBase = data.base_url || '';
        setAuthMode(initialMode);
        setBaseUrl(initialBase);
        setSavedAuthMode(initialMode);
        setSavedBaseUrl(initialBase);
        // Empty + read-only input + masked preview rendered separately
        // (see below) reflects the saved state without leaking plaintext.
        setApiKey('');
        setEditingKey(false);
      })
      .catch(() => {
        // Errors are already surfaced via ToastContext by ApiContext;
        // leave the page on the default oauth state so the user can still
        // make a choice rather than seeing a broken UI.
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [api]);

  const modeOptions = useMemo(
    () =>
      [
        { id: 'oauth' as const, label: t('settings.backends.codexAuthModeOauth') },
        { id: 'api_key' as const, label: t('settings.backends.codexAuthModeApiKey') },
      ] as const,
    [t]
  );

  const apiKeyStatus = state?.has_api_key
    ? t('settings.backends.codexApiKeyConfigured', { length: state.api_key_length })
    : t('settings.backends.codexApiKeyMissing');

  const onRemoveApiKey = async () => {
    // Drop just the API key (V2Config + auth.json's OPENAI_API_KEY),
    // leave OAuth tokens intact. Codex's CLI prefers the API key when
    // both are present — without this affordance a stale/invalid key
    // keeps forcing 401s even after the user signed in via OAuth.
    const confirmed = window.confirm(
      t('settings.backends.codexApiKeyRemoveConfirm') as string,
    );
    if (!confirmed) return;
    setRemovingKey(true);
    try {
      const result = await api.removeBackendApiKey('codex');
      if (!result.ok) {
        showToast(
          t('settings.backends.codexApiKeyRemoveFailed', {
            detail: result.error || result.detail || 'unknown',
          }),
          'error',
        );
        return;
      }
      const fresh = await api.getCodexAuth();
      setState(fresh);
      setBaseUrl(fresh.base_url || '');
      setSavedBaseUrl(fresh.base_url || '');
      setApiKey('');
      setEditingKey(false);
      showToast(t('settings.backends.codexApiKeyRemoved'), 'success');
      surfaceBackendNotices(result.notices, showToast, t);
    } catch (err: any) {
      showToast(
        t('settings.backends.codexApiKeyRemoveFailed', { detail: err?.message || 'unknown' }),
        'error',
      );
    } finally {
      setRemovingKey(false);
    }
  };

  const onSave = async () => {
    setSaving(true);
    try {
      // In OAuth mode base_url does not apply, so omit it entirely — the
      // backend's three-state payload semantics preserve whatever is
      // already stored. This keeps a relay URL the user had configured in
      // api_key mode intact when they toggle to OAuth and back.
      const payload: Record<string, unknown> = {
        auth_mode: authMode,
        // Send a fresh key only when the user typed one; an empty string
        // lets the server reuse the stored key (useful when the user is
        // just updating the base URL).
        api_key: authMode === 'api_key' ? (apiKey || undefined) : null,
      };
      if (authMode === 'api_key') {
        payload.base_url = baseUrl.trim() || null;
      }
      const result = await api.saveCodexAuth(payload as any);
      if (result.ok === false) {
        // The server returns ok:false for validation/persist failures
        // (e.g. missing api_key when auth_mode is "api_key") with HTTP 200,
        // so we must not advance into the success branch here — applying
        // ``result`` into state would overwrite the user's in-progress
        // edits with a malformed response that omits auth_mode / base_url.
        showToast(result.message || t('settings.backends.codexSaveFailed'), 'error');
        return;
      }
      setState(result);
      const nextMode =
        result.active_auth_mode !== 'none' ? result.active_auth_mode : result.auth_mode;
      const nextBase = result.base_url || '';
      setAuthMode(nextMode);
      setBaseUrl(nextBase);
      setSavedAuthMode(nextMode);
      setSavedBaseUrl(nextBase);
      setApiKey('');
      setEditingKey(false);
      if (result.restart?.ok === false) {
        // Config saved, restart failed — make the partial success visible.
        showToast(result.restart.message || result.message || t('settings.backends.codexSaveSuccess'), 'warning');
      } else {
        showToast(t('settings.backends.codexSaveSuccess'), 'success');
      }
      surfaceBackendNotices(result.notices, showToast, t);
    } catch (err: any) {
      showToast(err?.message || t('settings.backends.codexSaveFailed'), 'error');
    } finally {
      setSaving(false);
    }
  };

  if (loading || !runtime.loaded) {
    return <div className="text-sm text-muted">{t('common.loading')}</div>;
  }

  return (
    <div className="flex flex-col gap-4">
      <BackendRuntimeCard
        backend={BACKEND_ID}
        label="Codex"
        description={t('settings.backends.codexDescription')}
        Icon={Bot}
        iconTileClassName="bg-gold"
        iconClassName="text-gold-foreground"
        runtime={runtime}
        hideEnableToggle={hideEnableToggle}
      />

      <Card>
        <CardContent className="flex flex-col gap-5 p-6">
          <div className="flex flex-col gap-2">
            <div className="flex items-center justify-between gap-3">
              <Label className="text-xs font-medium uppercase text-muted">
                {t('settings.backends.codexAuthModeLabel')}
            </Label>
            {state?.active_auth_mode && state.active_auth_mode !== 'none' && (
              <Badge
                variant={state.active_auth_mode === 'oauth' ? 'success' : 'info'}
                className="font-mono uppercase tracking-[0.06em]"
              >
                <CheckCircle2 className="size-3" />
                {state.active_auth_mode === 'oauth'
                  ? t('settings.backends.activeAuthOauth')
                  : t('settings.backends.activeAuthApiKey')}
              </Badge>
            )}
            {state?.active_auth_mode === 'none' && (
              <Badge variant="secondary" className="font-mono uppercase tracking-[0.06em]">
                {t('settings.backends.activeAuthNone')}
              </Badge>
            )}
          </div>
          <SegmentedRadio
            value={authMode}
            onChange={guardedSetAuthMode}
            options={modeOptions}
            ariaLabel={t('settings.backends.codexAuthModeLabel') as string}
            disabled={oauthFlowActive}
          />
          <p className="text-[12px] leading-relaxed text-muted">
            {authMode === 'api_key'
              ? t('settings.backends.codexAuthModeApiKeyHint')
              : t('settings.backends.codexAuthModeOauthHint')}
          </p>
          {state?.auth_mode_uncertain && (
            // Codex stores credentials in the OS keychain by default
            // (``cli_auth_credentials_store=auto``). When there's no
            // disk key/tokens we cannot tell whether the user is
            // signed in via keychain or not — say so honestly rather
            // than rendering "no key configured" with an oauth toggle.
            <p className="text-[12px] text-gold">
              {t('settings.backends.codexAuthModeUncertain', { store: state.credentials_store })}
            </p>
          )}
          {authMode === 'api_key' && state && !state.file_store_active && !state.auth_mode_uncertain && (
            // Codex's documented default is ``auto`` (keyring-preferred), so
            // the API-key UX is honest about what saving will do: we pin
            // ``cli_auth_credentials_store = "file"`` on save, otherwise
            // Codex would ignore ``auth.json`` and keep reading the keychain.
            <p className="text-[12px] text-gold">
              {t('settings.backends.codexCredentialsStoreKeyringWarn', { store: state.credentials_store })}
            </p>
          )}
        </div>

        {authMode === 'oauth' && (
          <BackendOAuthPanel
            backend="codex"
            signedIn={state?.active_auth_mode === 'oauth'}
            signedInDetail={(() => {
              // Compose a single-line identity (``email · plan ·
              // org``) from the JWT-decoded ``chatgpt_account``
              // bundle. We render only the pieces present so a
              // partial JWT still surfaces something useful.
              const acct = state?.chatgpt_account;
              if (!acct) return null;
              const parts: string[] = [];
              if (acct.email) parts.push(acct.email);
              if (acct.plan_type) parts.push(acct.plan_type);
              const defaultOrg = acct.organizations?.find((o) => o.is_default)
                || acct.organizations?.[0];
              if (defaultOrg?.title) parts.push(defaultOrg.title);
              return parts.length ? parts.join(' · ') : null;
            })()}
            title={t('settings.backends.codexOauthPanelTitle')}
            subtitle={t('settings.backends.codexOauthPanelSubtitle')}
            onActiveChange={setOauthFlowActive}
            onSuccess={() => {
              // Re-read Codex auth state so the "ChatGPT tokens detected"
              // line and any keychain hints catch up with the freshly
              // minted credential without a manual page reload.
              void api
                .getCodexAuth()
                .then((data) => {
                  // Refresh underlying state but DO NOT clobber the
                  // radio tab — that's a user-controlled affordance
                  // after first load. Forcing a re-sync here would
                  // bounce the user back to whatever
                  // ``active_auth_mode`` reports every time OAuth
                  // completes / Sign out runs.
                  setState(data);
                  setBaseUrl(data.base_url || '');
                })
                .catch(() => {
                  /* ApiContext already toasted; leave existing state. */
                });
            }}
          />
        )}

        {authMode === 'api_key' && (
          <>
            <div className="flex flex-col gap-2">
              <Label htmlFor="codex-api-key" className="text-xs font-medium uppercase text-muted">
                {t('settings.backends.codexApiKeyLabel')}
              </Label>
              {state?.has_api_key && !editingKey ? (
                // Saved-key preview: render the server-masked value
                // (``sk-proj-•••H8mN``) read-only with a pencil to swap
                // in a fresh key. Same affordance shown in design.pen
                // ``cxApiVal`` — saves the user from typing the secret
                // again when they only want to change the Base URL.
                <div className="flex items-center gap-2 rounded-md border border-border bg-foreground/[0.04] px-3 py-2">
                  <KeyRound className="size-4 shrink-0 text-muted" />
                  <code className="flex-1 truncate font-mono text-[13px] text-foreground">
                    {state.api_key_masked || '••••••••'}
                  </code>
                  <Button
                    type="button"
                    variant="ghost"
                    size="xs"
                    onClick={() => {
                      setEditingKey(true);
                      setApiKey('');
                    }}
                    disabled={removingKey}
                  >
                    <Pencil className="size-3" />
                    {t('settings.backends.replaceApiKey')}
                  </Button>
                  {/* Symmetric to OpenCode's Remove affordance: lets
                      the user drop a stale / invalid key without
                      re-signing in. Important for Codex specifically
                      because the CLI prefers ``OPENAI_API_KEY`` when
                      both api_key and OAuth tokens live in
                      ``auth.json`` — a stuck key keeps forcing 401s
                      even after a successful OAuth sign-in. */}
                  <Button
                    type="button"
                    variant="ghost"
                    size="xs"
                    onClick={() => void onRemoveApiKey()}
                    disabled={removingKey || editingKey}
                    className="text-destructive hover:bg-destructive/10 hover:text-destructive"
                  >
                    <Trash2 className="size-3" />
                    {removingKey
                      ? t('common.removing')
                      : t('settings.backends.codexApiKeyRemove')}
                  </Button>
                </div>
              ) : (
                <div className="relative">
                  <KeyRound className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted" />
                  <Input
                    id="codex-api-key"
                    type="password"
                    autoComplete="off"
                    spellCheck={false}
                    placeholder={t('settings.backends.codexApiKeyPlaceholder') as string}
                    value={apiKey}
                    onChange={(e) => setApiKey(e.target.value)}
                    className="pl-9 font-mono"
                    autoFocus={editingKey}
                  />
                </div>
              )}
              <div className="flex items-center justify-between gap-2">
                <p className="text-[12px] text-muted">{apiKeyStatus}</p>
                {state?.has_api_key && editingKey && (
                  <button
                    type="button"
                    className="text-[12px] text-muted underline-offset-2 transition hover:text-foreground hover:underline"
                    onClick={() => {
                      setEditingKey(false);
                      setApiKey('');
                    }}
                  >
                    {t('common.cancel')}
                  </button>
                )}
              </div>
            </div>

            <div className="flex flex-col gap-2">
              <Label htmlFor="codex-base-url" className="text-xs font-medium uppercase text-muted">
                {t('settings.backends.codexBaseUrlLabel')}
              </Label>
              <div className="flex gap-2">
                <Input
                  id="codex-base-url"
                  type="url"
                  autoComplete="off"
                  spellCheck={false}
                  placeholder={t('settings.backends.codexBaseUrlPlaceholder') as string}
                  value={baseUrl}
                  onChange={(e) => setBaseUrl(e.target.value)}
                  className="font-mono"
                />
                <Button
                  type="button"
                  variant="secondary"
                  size="sm"
                  onClick={() => setBaseUrl('')}
                  disabled={!baseUrl}
                >
                  <RotateCcw className="size-3.5" />
                  {t('settings.backends.codexBaseUrlReset')}
                </Button>
              </div>
              <p className="text-[12px] text-muted">{t('settings.backends.codexBaseUrlHint')}</p>
            </div>
          </>
        )}

        {/* Info hint describes ``~/.codex/{auth.json,config.toml}``
            writes that only happen on api_key saves. Skip it under
            OAuth so the panel doesn't carry irrelevant copy. */}
        {authMode === 'api_key' && (
          <div className="flex items-start gap-2 rounded-lg border border-border bg-surface-2/60 px-3 py-2.5">
            <Info className="mt-0.5 size-3.5 shrink-0 text-muted" />
            <p className="text-[12px] leading-relaxed text-muted">
              {t('settings.backends.codexInfoHint')}
            </p>
          </div>
        )}

        {/* OAuth mode persists ``auth_mode=oauth`` automatically on
            successful sign-in, so the Save button only surfaces for
            api_key mode — and even then only when something has
            actually changed (page feedback: a permanent Save button
            with no dirty signal is noisy). ``dirty`` watches the
            three things this card mutates against their saved
            snapshot. */}
        {authMode === 'api_key' && (() => {
          const dirty =
            authMode !== savedAuthMode ||
            apiKey.trim().length > 0 ||
            baseUrl.trim() !== savedBaseUrl.trim();
          if (!dirty) return null;
          return (
            <div className="flex justify-end">
              <Button variant="brand" size="default" onClick={onSave} disabled={saving}>
                <Save className="size-3.5" />
                {saving ? t('settings.backends.codexSaving') : t('settings.backends.codexSave')}
              </Button>
            </div>
          );
        })()}
        </CardContent>
      </Card>

      <BackendTestPanel backend="codex" />
    </div>
  );
};
