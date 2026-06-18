import React, { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  AlertTriangle,
  CheckCircle2,
  Info,
  KeyRound,
  Pencil,
  RotateCcw,
  Save,
  Sparkles,
  Trash2,
} from 'lucide-react';

import { Badge } from '../../ui/badge';
import { Button } from '../../ui/button';
import { Card, CardContent } from '../../ui/card';
import { Input } from '../../ui/input';
import { Label } from '../../ui/label';
import { BackendOAuthPanel } from '../BackendOAuthPanel';
import { BackendTestPanel } from '../BackendTestPanel';
import { BackendRuntimeCard } from '../shared/BackendRuntimeCard';
import { SegmentedRadio } from '../shared/SegmentedRadio';
import { useBackendRuntime } from '../shared/useBackendRuntime';
import { useOAuthFlowLock } from '../shared/useOAuthFlowLock';
import { useApi } from '@/context/ApiContext';
import type { ClaudeAuthMode, ClaudeAuthState } from '@/context/ApiContext';
import { useToast } from '@/context/ToastContext';

const BACKEND_ID = 'claude';
const DEFAULT_CLI = 'claude';

export const ClaudeProviderConfig: React.FC<{
  hideEnableToggle?: boolean;
  deferRestart?: boolean;
}> = ({ hideEnableToggle, deferRestart } = {}) => {
  const { t } = useTranslation();
  const api = useApi();
  const { showToast } = useToast();

  // Runtime state — CLI detection + lifecycle. Shared with Codex /
  // OpenCode pages via the ``useBackendRuntime`` hook.
  const runtime = useBackendRuntime({
    backend: BACKEND_ID,
    defaultCli: DEFAULT_CLI,
    deferRestart,
  });

  // Auth state — OAuth vs API-key.
  const [authState, setAuthState] = useState<ClaudeAuthState | null>(null);
  const [authLoading, setAuthLoading] = useState(true);
  const [authSaving, setAuthSaving] = useState(false);
  const [removingKey, setRemovingKey] = useState(false);
  const [authMode, setAuthMode] = useState<ClaudeAuthMode>('oauth');
  const [apiKey, setApiKey] = useState('');
  // ``editingKey`` mirrors the Codex page convention: false = show the
  // saved key as a read-only mask (``sk-ant-•••cd34``) with a pencil to
  // replace it; true = empty editable input ready for a fresh secret.
  const [editingKey, setEditingKey] = useState(false);
  const [baseUrl, setBaseUrl] = useState('');
  // Snapshot the last loaded/saved auth-mode + base_url so we can hide
  // the Save button when nothing has changed (page feedback: no-op
  // Save buttons are noise).
  const [savedAuthMode, setSavedAuthMode] = useState<ClaudeAuthMode>('oauth');
  const [savedBaseUrl, setSavedBaseUrl] = useState('');
  // Freeze the auth-mode segmented radio while the OAuth panel is mid-
  // handshake. Shared with Codex / OpenCode via ``useOAuthFlowLock``.
  const { oauthFlowActive, setOauthFlowActive, guardedSetAuthMode } =
    useOAuthFlowLock<ClaudeAuthMode>({
      setAuthMode,
      warnTag: 'claude-auth-mode',
    });

  useEffect(() => {
    // Runtime config (enabled + cli_path + initial detect) is owned by
    // ``useBackendRuntime``. This effect only loads auth state.
    let cancelled = false;
    api
      .getClaudeAuth()
      .then((data) => {
        if (cancelled) return;
        setAuthState(data);
        // Prefer the live-effective tab. V2Config defaults to ``"oauth"``
        // even when settings.json carries the actual key, so reading
        // ``auth_mode`` alone would land the user on the wrong tab. Fall
        // back to V2Config only when nothing on disk is configured.
        const initialMode =
          data.active_auth_mode !== 'none' ? data.active_auth_mode : data.auth_mode;
        const initialBase = data.base_url || '';
        setAuthMode(initialMode);
        setBaseUrl(initialBase);
        setSavedAuthMode(initialMode);
        setSavedBaseUrl(initialBase);
        // The masked preview lives in ``authState.api_key_masked``;
        // ``apiKey`` stays empty until the user clicks "Replace".
        setApiKey('');
        setEditingKey(false);
      })
      .catch(() => {
        // ApiContext already toasted; leave the page on its defaults.
      })
      .finally(() => {
        if (!cancelled) setAuthLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [api]);

  const modeOptions = useMemo(
    () =>
      [
        { id: 'oauth' as const, label: t('settings.backends.claudeAuthModeOauth') },
        { id: 'api_key' as const, label: t('settings.backends.claudeAuthModeApiKey') },
      ] as const,
    [t]
  );

  const apiKeyStatus = authState?.has_api_key
    ? t('settings.backends.claudeApiKeyConfigured', { length: authState.api_key_length })
    : t('settings.backends.claudeApiKeyMissing');

  const onRemoveApiKey = async () => {
    // Drop just the API key from V2Config; leaves Claude Code's own OAuth
    // token store alone. Lets the user clear a stale key without having to
    // also re-do OAuth.
    const confirmed = window.confirm(
      t('settings.backends.claudeApiKeyRemoveConfirm') as string,
    );
    if (!confirmed) return;
    setRemovingKey(true);
    try {
      const result = await api.removeBackendApiKey('claude');
      if (!result.ok) {
        showToast(
          t('settings.backends.claudeApiKeyRemoveFailed', {
            detail: result.error || result.detail || 'unknown',
          }),
          'error',
        );
        return;
      }
      const fresh = await api.getClaudeAuth();
      setAuthState(fresh);
      setBaseUrl(fresh.base_url || '');
      setSavedBaseUrl(fresh.base_url || '');
      setApiKey('');
      setEditingKey(false);
      showToast(t('settings.backends.claudeApiKeyRemoved'), 'success');
    } catch (err: any) {
      showToast(
        t('settings.backends.claudeApiKeyRemoveFailed', { detail: err?.message || 'unknown' }),
        'error',
      );
    } finally {
      setRemovingKey(false);
    }
  };

  const onSaveAuth = async () => {
    setAuthSaving(true);
    try {
      const payload: Record<string, unknown> = {
        auth_mode: authMode,
        api_key: authMode === 'api_key' ? (apiKey || undefined) : null,
      };
      if (authMode === 'api_key') {
        payload.base_url = baseUrl.trim() || null;
      }
      const result = await api.saveClaudeAuth(payload as any);
      if (result.ok === false) {
        showToast(result.message || t('settings.backends.claudeSaveFailed'), 'error');
        return;
      }
      setAuthState(result);
      const nextMode =
        result.active_auth_mode !== 'none' ? result.active_auth_mode : result.auth_mode;
      const nextBase = result.base_url || '';
      setAuthMode(nextMode);
      setBaseUrl(nextBase);
      setSavedAuthMode(nextMode);
      setSavedBaseUrl(nextBase);
      setApiKey('');
      setEditingKey(false);
      // Claude restart is synthetic (one-shot CLI) so result.restart.ok
      // is always true; treat any falsy state defensively just in case.
      if (result.partial) {
        showToast(
          t('settings.backends.claudeSavePartial', {
            detail: result.detail || result.warning || 'oauth_cleanup_failed',
          }),
          'warning',
        );
      } else if (result.restart?.ok === false) {
        showToast(result.restart.message || t('settings.backends.claudeSaveSuccess'), 'warning');
      } else {
        showToast(t('settings.backends.claudeSaveSuccess'), 'success');
      }
    } catch (err: any) {
      showToast(err?.message || t('settings.backends.claudeSaveFailed'), 'error');
    } finally {
      setAuthSaving(false);
    }
  };

  if (!runtime.loaded) {
    return <div className="text-sm text-muted">{t('common.loading')}</div>;
  }

  return (
    <div className="flex flex-col gap-4">
      <BackendRuntimeCard
        backend={BACKEND_ID}
        label="Claude Code"
        description={t('settings.backends.claudeDescription')}
        Icon={Sparkles}
        iconTileClassName="bg-cyan-soft"
        iconClassName="text-cyan"
        runtime={runtime}
        hideEnableToggle={hideEnableToggle}
      />

      {authLoading ? (
        <div className="text-sm text-muted">{t('common.loading')}</div>
      ) : (
        <Card>
          <CardContent className="flex flex-col gap-5 p-6">
            <div className="flex flex-col gap-2">
              <div className="flex items-center justify-between gap-3">
                <Label className="text-xs font-medium uppercase text-muted">
                  {t('settings.backends.claudeAuthModeLabel')}
                </Label>
                {authState?.active_auth_mode && authState.active_auth_mode !== 'none' && (
                  <Badge
                    variant={authState.active_auth_mode === 'oauth' ? 'success' : 'info'}
                    className="font-mono uppercase tracking-[0.06em]"
                  >
                    <CheckCircle2 className="size-3" />
                    {authState.active_auth_mode === 'oauth'
                      ? t('settings.backends.activeAuthOauth')
                      : t('settings.backends.activeAuthApiKey')}
                  </Badge>
                )}
                {authState?.active_auth_mode === 'none' && (
                  <Badge variant="secondary" className="font-mono uppercase tracking-[0.06em]">
                    {t('settings.backends.activeAuthNone')}
                  </Badge>
                )}
              </div>
              <SegmentedRadio
                value={authMode}
                onChange={guardedSetAuthMode}
                options={modeOptions}
                ariaLabel={t('settings.backends.claudeAuthModeLabel') as string}
                disabled={oauthFlowActive}
              />
              <p className="text-[12px] leading-relaxed text-muted">
                {authMode === 'api_key'
                  ? t('settings.backends.claudeAuthModeApiKeyHint')
                  : t('settings.backends.claudeAuthModeOauthHint')}
              </p>
            </div>

            {authMode === 'oauth' && (
              <BackendOAuthPanel
                backend={BACKEND_ID}
                // Claude Code may still have account tokens in its own
                // store after the user switches Avibe to API-key mode.
                // The Settings UI should show OAuth as signed in only when
                // OAuth is the currently effective Avibe auth source.
                signedIn={authState?.active_auth_mode === 'oauth'}
                canRemoveAuth={!!authState?.has_oauth_credentials}
                title={t('settings.backends.claudeOauthPanelTitle')}
                subtitle={t('settings.backends.claudeOauthPanelSubtitle')}
                onActiveChange={setOauthFlowActive}
                onSuccess={() => {
                  // Re-fetch the auth state so the "Signed in" pill and
                  // any masked-key indicators reflect the fresh login
                  // immediately rather than waiting for a page reload.
                  void api
                    .getClaudeAuth()
                    .then((data) => {
                      // Refresh the underlying state (active badge,
                      // masked key, settings.json conflict warning)
                      // but DO NOT clobber ``authMode``. The radio
                      // tab is a *user-controlled* affordance after
                      // first load — re-syncing it from the server
                      // here would force a tab change every time
                      // OAuth completes / Sign out fires, even when
                      // the user wants to stay on the OAuth tab to
                      // re-authenticate.
                      setAuthState(data);
                      setBaseUrl(data.base_url || '');
                    })
                    .catch(() => {
                      // Already toasted upstream; leave UI on previous state.
                    });
                }}
              />
            )}

            {authMode === 'api_key' && (
              <>
                <div className="flex flex-col gap-2">
                  <Label htmlFor="claude-api-key" className="text-xs font-medium uppercase text-muted">
                    {t('settings.backends.claudeApiKeyLabel')}
                  </Label>
                  {authState?.has_api_key && !editingKey ? (
                    // Same masked-preview affordance the Codex page uses;
                    // keeps the user from re-typing the secret when they
                    // are only changing the Base URL.
                    <div className="flex items-center gap-2 rounded-md border border-border bg-foreground/[0.04] px-3 py-2">
                      <KeyRound className="size-4 shrink-0 text-muted" />
                      <code className="flex-1 truncate font-mono text-[13px] text-foreground">
                        {authState.api_key_masked || '••••••••'}
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
                      {/* Symmetric to OpenCode's Remove affordance:
                          clear the saved API key while leaving OAuth
                          token store intact. Without this, a stuck key
                          keeps forcing the env-var path even after the
                          user signed in via OAuth. */}
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
                          : t('settings.backends.claudeApiKeyRemove')}
                      </Button>
                    </div>
                  ) : (
                    <div className="relative">
                      <KeyRound className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted" />
                      <Input
                        id="claude-api-key"
                        type="password"
                        autoComplete="off"
                        spellCheck={false}
                        placeholder={t('settings.backends.claudeApiKeyPlaceholder') as string}
                        value={apiKey}
                        onChange={(e) => setApiKey(e.target.value)}
                        className="pl-9 font-mono"
                        disabled={authSaving}
                        autoFocus={editingKey}
                      />
                    </div>
                  )}
                  <div className="flex items-center justify-between gap-2">
                    <p className="text-[12px] text-muted">{apiKeyStatus}</p>
                    {authState?.has_api_key && editingKey && (
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
                  <Label htmlFor="claude-base-url" className="text-xs font-medium uppercase text-muted">
                    {t('settings.backends.claudeBaseUrlLabel')}
                  </Label>
                  <div className="flex gap-2">
                    <Input
                      id="claude-base-url"
                      type="url"
                      autoComplete="off"
                      spellCheck={false}
                      placeholder={t('settings.backends.claudeBaseUrlPlaceholder') as string}
                      value={baseUrl}
                      onChange={(e) => setBaseUrl(e.target.value)}
                      className="font-mono"
                      disabled={authSaving}
                    />
                    <Button
                      type="button"
                      variant="secondary"
                      size="sm"
                      onClick={() => setBaseUrl('')}
                      disabled={!baseUrl || authSaving}
                    >
                      <RotateCcw className="size-3.5" />
                      {t('settings.backends.claudeBaseUrlReset')}
                    </Button>
                  </div>
                  <p className="text-[12px] text-muted">{t('settings.backends.claudeBaseUrlHint')}</p>
                </div>
              </>
            )}

            {/* Kept defensively for legacy states where V2Config still
                carries a key before the next save migrates it. New saves
                overwrite Claude's own settings.json directly. */}
            {authMode === 'api_key' && authState?.settings_conflict && (
              <div className="flex items-start gap-2 rounded-lg border border-gold/30 bg-gold/[0.08] px-3 py-2.5">
                <AlertTriangle className="mt-0.5 size-3.5 shrink-0 text-gold" />
                <div className="flex flex-col gap-1">
                  <p className="text-[12px] font-medium text-gold">
                    {t('settings.backends.claudeSettingsConflictTitle')}
                  </p>
                  <p className="text-[12px] leading-relaxed text-muted">
                    {t('settings.backends.claudeSettingsConflictBody', {
                      var: authState.settings_env_key_var || 'ANTHROPIC_API_KEY',
                      path: authState.settings_path || '~/.claude/settings.json',
                    })}
                  </p>
                </div>
              </div>
            )}

            {authMode === 'api_key' && (
              <div className="flex items-start gap-2 rounded-lg border border-border bg-surface-2/60 px-3 py-2.5">
                <Info className="mt-0.5 size-3.5 shrink-0 text-muted" />
                <p className="text-[12px] leading-relaxed text-muted">
                  {t('settings.backends.claudeInfoHint')}
                </p>
              </div>
            )}

            {/* OAuth mode persists ``auth_mode=oauth`` automatically on
                successful sign-in (see ``_invoke_post_web_success_hook``),
                so the Save button only needs to surface in API-key mode
                where the user still has to commit the key / Base URL —
                and even there, only when something has actually changed. */}
            {authMode === 'api_key' && (() => {
              const dirty =
                authMode !== savedAuthMode ||
                apiKey.trim().length > 0 ||
                baseUrl.trim() !== savedBaseUrl.trim();
              if (!dirty) return null;
              return (
                <div className="flex justify-end">
                  <Button variant="brand" size="default" onClick={onSaveAuth} disabled={authSaving}>
                    <Save className="size-3.5" />
                    {authSaving ? t('settings.backends.claudeSaving') : t('settings.backends.claudeSave')}
                  </Button>
                </div>
              );
            })()}
          </CardContent>
        </Card>
      )}

      {/* Connectivity probe — matches design.pen cdTest2 panel.
          Works in both OAuth and API-key modes because the underlying
          ``claude -p "Hi"`` subprocess inherits whichever auth source
          Claude Code reads at launch. */}
      {!authLoading && <BackendTestPanel backend={BACKEND_ID} />}
    </div>
  );
};
