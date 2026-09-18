import { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { CheckCircle2, ExternalLink, Eye, EyeOff, KeyRound, LoaderCircle, Pencil } from 'lucide-react';
import { useApi, type ClaudeAuthState, type CodexAuthState, type OpencodeProvider, type OAuthWebMutationResult } from '@/context/ApiContext';
import { useToast } from '@/context/ToastContext';
import { errorMessage } from '@/lib/errorMessage';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { SegmentedRadio } from '../shared/SegmentedRadio';
import { surfaceBackendNotices } from '../shared/surfaceBackendNotices';
import { useBackendOAuth, type OAuthBackend } from '../oauth/useBackendOAuth';
import { BackendOAuthPanel } from '../BackendOAuthPanel';
import { OAuthDeviceCodeRow, OAuthLinkRow } from '../oauth/OAuthFlowParts';
import './connection.css';

type Method = 'oauth' | 'api_key';
type NativeState = ClaudeAuthState | CodexAuthState;
export type ConnectionHeading = { method: Method; active: boolean; credential: 'api_key' | 'auth_token' };

/** Settings and onboarding share persistence, validation, effective readback and cancellation. */
export function BackendConnectionForm({ backend, provider, initialMethod = 'oauth', compact = false,
  onConnected, onCancel, onHeading, onBusyChange, onWriteState, connectionRevision = 0 }: {
  backend: OAuthBackend;
  provider?: OpencodeProvider;
  initialMethod?: Method;
  compact?: boolean;
  /** Runtime mutation settlement: refresh observations without replacing input drafts. */
  connectionRevision?: number;
  onConnected?: () => void | Promise<void>;
  onCancel?: () => void;
  onHeading?: (heading: ConnectionHeading) => void;
  onBusyChange?: (busy: boolean) => void;
  onWriteState?: (pending: boolean) => void;
}) {
  const api = useApi();
  const { t } = useTranslation();
  const { showToast } = useToast();
  const [native, setNative] = useState<NativeState | null>(null);
  const [currentProvider, setCurrentProvider] = useState(provider);
  const [method, setMethod] = useState<Method>(backend === 'opencode' && !provider?.oauth_available ? 'api_key' : initialMethod);
  const [credential, setCredential] = useState<'api_key' | 'auth_token'>('api_key');
  const [key, setKey] = useState('');
  const [editing, setEditing] = useState(false);
  const [reveal, setReveal] = useState(false);
  const [baseUrl, setBaseUrl] = useState('');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [connected, setConnected] = useState(false);
  const [savedDisabled, setSavedDisabled] = useState(false);
  const [active, setActive] = useState(false);
  const [applyPending, setApplyPending] = useState(false);
  const [authReadable, setAuthReadable] = useState(false);
  const observation = useRef(0);
  const draftTouched = useRef(false);
  const pendingConfirmation = useRef<Method | null>(null);
  const writeState = useRef(onWriteState); writeState.current = onWriteState;
  const lifetime = useRef({ mounted: true, busy: false });
  const onConnectedRef = useRef(onConnected); onConnectedRef.current = onConnected;
  useEffect(() => {
    const owner = lifetime.current; owner.mounted = true;
    return () => { owner.mounted = false; observation.current += 1; };
  }, []);
  const read = useCallback(async () => {
    if (backend === 'opencode') {
      const result = await api.getOpencodeProviders();
      if (!result.ok) throw new Error(result.message || t('onboarding.connection.readFailed'));
      const fresh = result.providers?.find((entry) => entry.id === provider?.id);
      if (!fresh) throw new Error(t('onboarding.connection.readFailed'));
      return { provider: fresh, native: null };
    }
    const fresh = await (backend === 'claude' ? api.getClaudeAuth() : api.getCodexAuth());
    if (!fresh.ok) throw new Error(fresh.message || t('onboarding.connection.readFailed'));
    return { native: fresh, provider: undefined };
  }, [api, backend, provider?.id, t]);
  // Native persistence and controller application are independent observations.
  // A failed IPC read must not discard a successful post-commit credential read.
  const observe = useCallback(async ({ expectedMethod, receiptError = '' }: {
    expectedMethod?: Method; receiptError?: string;
  } = {}) => {
    const token = ++observation.current;
    const current = () => lifetime.current.mounted && token === observation.current;
    setConnected(false); setSavedDisabled(false); setError(receiptError);
    const nativeRead = read().then((fresh) => {
      if (current()) {
        setNative(fresh.native); setCurrentProvider(fresh.provider); setAuthReadable(true);
        if (!draftTouched.current) {
          setBaseUrl(fresh.native?.base_url || fresh.provider?.base_url || '');
          if (fresh.native && 'credential_type' in fresh.native) setCredential(fresh.native.credential_type || 'api_key');
        }
        if (!compact && !draftTouched.current) {
          const effective = fresh.native?.active_auth_mode;
          setMethod(effective && effective !== 'none' ? effective : fresh.provider?.active_auth_type === 'api' ? 'api_key' : backend === 'opencode' && !provider?.oauth_available ? 'api_key' : initialMethod);
        }
      }
      return fresh;
    }, (cause: unknown) => {
      if (current()) setAuthReadable(false);
      throw cause;
    });
    const [auth, application] = await Promise.allSettled([nativeRead, api.getBackendConnection(backend)]);
    if (!current()) return false;
    setLoading(false);
    const messages = receiptError ? [receiptError] : [];
    const connection = application.status === 'fulfilled' ? application.value : null;
    const applied = connection?.ok && ['applied', 'stopped'].includes(connection.application);
    if (!applied) messages.push(application.status === 'rejected' ? errorMessage(application.reason) || t('onboarding.connection.readFailed') : connection?.message || t('onboarding.connection.applyPending'));
    let hasAuth = false;
    let keylessSettings = false;
    if (auth.status === 'rejected') messages.push(errorMessage(auth.reason) || t('onboarding.connection.readFailed'));
    else {
      const fresh = auth.value;
      const effective = fresh.native?.active_auth_mode || (fresh.provider?.active_auth_type === 'oauth' ? 'oauth' : fresh.provider?.active_auth_type === 'api' ? 'api_key' : 'none');
      const uncertain = fresh.native && 'auth_mode_uncertain' in fresh.native && fresh.native.auth_mode_uncertain;
      hasAuth = ['oauth', 'api_key'].includes(effective) && !uncertain;
      keylessSettings = !compact && Boolean(fresh.provider?.custom && fresh.provider.configured && effective === 'none');
      if (expectedMethod && ((!keylessSettings && effective !== expectedMethod) || uncertain)) messages.push(t('onboarding.connection.unconfirmed'));
    }
    setError([...new Set(messages)].join(' '));
    setApplyPending(messages.length > 0);
    const confirmed = messages.length === 0;
    setConnected(confirmed && hasAuth && Boolean(connection?.ready));
    setSavedDisabled(confirmed && hasAuth && connection?.enabled === false && !keylessSettings);
    return confirmed;
  }, [read, api, backend, compact, provider?.oauth_available, initialMethod, t]);
  useEffect(() => {
    void observe();
    return () => { observation.current += 1; };
  }, [observe]);
  const observedRevision = useRef(connectionRevision);
  useEffect(() => {
    if (observedRevision.current === connectionRevision) return;
    observedRevision.current = connectionRevision;
    void observe();
    return () => { observation.current += 1; };
  }, [connectionRevision, observe]);

  const confirm = async (expectedMethod: Method = method, receiptError = '') => {
    pendingConfirmation.current = expectedMethod;
    if (!await observe({ expectedMethod, receiptError })) return false;
    pendingConfirmation.current = null;
    setKey(''); setEditing(false); draftTouched.current = false;
    await onConnectedRef.current?.();
    return true;
  };
  const confirmOAuth = async () => {
    if (!await confirm('oauth')) throw new Error(t('onboarding.connection.applyPending'));
  };
  const observeOAuthFailure = async (receiptError: string) => {
    pendingConfirmation.current = 'oauth';
    await observe({ receiptError });
  };
  const oauth = useBackendOAuth({ backend, opencodeProviderId: provider?.id, onSuccess: confirmOAuth,
    onFailure: observeOAuthFailure, onCancel: () => { pendingConfirmation.current = null; observation.current += 1; }, onActiveChange: setActive, onPendingChange: onWriteState });
  const refresh = async () => {
    const expected = pendingConfirmation.current;
    if (expected) await confirm(expected);
    else await observe();
  };
  const removed = async (result: OAuthWebMutationResult) => {
    pendingConfirmation.current = null;
    return observe({ receiptError: !result.ok ? result.error || result.detail || t('onboarding.connection.saveFailed')
      : result.restart?.ok === false ? result.restart.message || t('onboarding.connection.applyFailed') : '' });
  };
  const busy = saving || active;
  useEffect(() => { onBusyChange?.(busy); }, [busy, onBusyChange]);
  useEffect(() => { onHeading?.({ method, active, credential }); }, [method, active, credential, onHeading]);
  const hasKey = authReadable && (backend === 'opencode' ? Boolean(currentProvider?.api_key_masked) : Boolean(native?.has_api_key));
  const mask = native?.api_key_masked || currentProvider?.api_key_masked || '••••••••';
  const uncertain = native && 'auth_mode_uncertain' in native && native.auth_mode_uncertain;
  const signedIn = authReadable && (native?.active_auth_mode === 'oauth' || currentProvider?.active_auth_type === 'oauth');
  const urlValid = (() => {
    if (!baseUrl.trim()) return !provider?.custom;
    try { const url = new URL(baseUrl.trim()); return ['http:', 'https:'].includes(url.protocol) && Boolean(url.hostname); }
    catch { return false; }
  })();
  const canSave = !loading && authReadable && !busy && urlValid && Boolean(key.trim() || (hasKey && !editing) || (!compact && currentProvider?.custom && currentProvider.configured && !editing));
  const save = async () => {
    if (!canSave || lifetime.current.busy) return;
    lifetime.current.busy = true; observation.current += 1; writeState.current?.(true); setSaving(true); setError(''); setConnected(false); setSavedDisabled(false);
    try {
      const payload = { auth_mode: 'api_key' as const, api_key: key.trim() || undefined, base_url: baseUrl.trim() || null };
      const result = backend === 'claude' ? await api.saveClaudeAuth({ ...payload, credential_type: credential })
        : backend === 'codex' ? await api.saveCodexAuth(payload)
        : await api.setOpencodeProviderAuth(provider!.id, payload.api_key, payload.base_url);
      if (!lifetime.current.mounted) return;
      if (!result.ok) throw new Error(result.message || t('onboarding.connection.saveFailed'));
      if ('notices' in result) surfaceBackendNotices(result.notices, showToast, t);
      if ('partial' in result && result.partial) showToast(result.detail || result.warning || t('onboarding.connection.partial'), 'warning');
      await confirm('api_key', result.restart?.ok === false ? result.restart.message || t('onboarding.connection.applyFailed') : '');
    } catch (err) {
      if (lifetime.current.mounted) {
        pendingConfirmation.current = 'api_key';
        await observe({ receiptError: errorMessage(err) || t('onboarding.connection.saveFailed') });
      }
    }
    finally { lifetime.current.busy = false; writeState.current?.(false); if (lifetime.current.mounted) setSaving(false); }
  };
  const remove = async (onlyKey: boolean) => {
    if (lifetime.current.busy || !window.confirm(t('onboarding.connection.removeConfirm'))) return;
    lifetime.current.busy = true; observation.current += 1; writeState.current?.(true); setSaving(true); setError(''); setConnected(false); setSavedDisabled(false);
    try {
      const result = backend === 'opencode' ? await api.deleteOpencodeProviderAuth(provider!.id)
        : onlyKey ? await api.removeBackendApiKey(backend) : await api.removeBackendAuth(backend);
      if (!lifetime.current.mounted) return;
      if (!result.ok) throw new Error(('message' in result ? result.message : 'detail' in result ? result.detail : undefined) || t('onboarding.connection.saveFailed'));
      if ('notices' in result) surfaceBackendNotices(result.notices, showToast, t);
      await removed(result);
    } catch (err) {
      if (lifetime.current.mounted) {
        pendingConfirmation.current = null;
        await observe({ receiptError: errorMessage(err) || t('onboarding.connection.readFailed') });
      }
    }
    finally { lifetime.current.busy = false; writeState.current?.(false); if (lifetime.current.mounted) setSaving(false); }
  };
  const prefix = backend === 'claude' ? 'claude' : backend === 'codex' ? 'codex' : 'opencode';
  const title = backend === 'claude' ? t('onboarding.connection.claudeAccount') : backend === 'codex' ? t('onboarding.connection.codexAccount') : provider?.name || '';
  const hint = backend === 'claude' ? t('onboarding.connection.claudeHint') : t('onboarding.connection.deviceHint');
  const needsCode = oauth.state === 'awaiting_code' && (backend === 'claude' || (backend === 'opencode' && (oauth.callbackKind === 'code' || oauth.callbackKind === 'redirect' || (!oauth.callbackKind && Boolean(oauth.url) && !oauth.deviceCode))));
  const apiKeyLabel = t(compact || backend !== 'claude' ? 'onboarding.connection.apiKeyLabel' : 'settings.backends.claudeCredentialTypeApiKey');
  const tokenLabel = t(compact ? 'onboarding.connection.authTokenLabel' : 'settings.backends.claudeCredentialTypeAuthToken');
  const credentialLabel = t(compact ? 'onboarding.connection.credentialType' : 'settings.backends.claudeCredentialTypeLabel');
  const secretLabel = credential === 'auth_token' && backend === 'claude' ? tokenLabel : apiKeyLabel;
  const startLabel = backend === 'claude' ? t('onboarding.connection.claudeSignIn') : backend === 'codex' ? t('onboarding.connection.codexSignIn') : t('settings.backends.opencodeProviderSignIn');
  if (loading) return <div className="connection-loading" role="status"><LoaderCircle className="animate-spin" size={16} />{t('common.loading')}</div>;
  return <div className="backend-connection-form">
    {!(compact && active && backend !== 'codex') && (backend !== 'opencode' || (!compact && provider?.oauth_available)) && <SegmentedRadio
      value={method} onChange={(value) => { if (!busy) { pendingConfirmation.current = null; draftTouched.current = true; setMethod(value); setError(''); setConnected(false); } }} disabled={busy}
      ariaLabel={t('onboarding.connection.method')}
      options={[{ id: 'oauth', label: backend === 'claude' ? t('onboarding.connection.claudeLogin') : backend === 'codex' ? t('onboarding.connection.codexSignIn') : t('onboarding.connection.subscription') },
        { id: 'api_key', label: backend === 'claude' ? t('onboarding.connection.claudeCredentials') : backend === 'codex' ? t('onboarding.connection.openaiKey') : apiKeyLabel }]} />}
    {uncertain && <p className="connection-notice">{t('onboarding.connection.uncertain')}</p>}
    {native && 'file_store_active' in native && method === 'api_key' && !native.file_store_active && !uncertain && <p className="connection-notice">{t('settings.backends.codexCredentialsStoreKeyringWarn', { store: native.credentials_store })}</p>}
    {native && 'settings_conflict' in native && native.settings_conflict && <p className="connection-notice">{t('settings.backends.claudeSettingsConflictTitle')}: {t('settings.backends.claudeSettingsConflictBody', { var: native.settings_env_key_var || 'ANTHROPIC_API_KEY', path: native.settings_path })}</p>}
    {applyPending && <Button variant="secondary" disabled={busy} onClick={() => void refresh()}>{t('onboarding.connection.refresh')}</Button>}
    {connected && <p className="connection-confirmed" role="status"><CheckCircle2 size={16} />{t('onboarding.connection.connected')}</p>}
    {savedDisabled && <p className="text-xs text-muted break-words" role="status">{t('onboarding.connection.savedDisabled')}</p>}
    {error && <div role="alert" className="connection-error">{error}{!native && !currentProvider && <Button variant="secondary" onClick={() => void observe()}>{t('common.retry')}</Button>}</div>}
    {method === 'oauth' && (!compact ? <BackendOAuthPanel backend={backend} opencodeProviderId={provider?.id} signedIn={signedIn}
      title={title} subtitle={hint} hideRemove={backend === 'opencode'} onSuccess={confirmOAuth} onRemoved={removed} onFailure={observeOAuthFailure} onCancel={() => { pendingConfirmation.current = null; observation.current += 1; }} onActiveChange={setActive}
      canRemoveAuth={authReadable && Boolean(native && ('has_oauth_credentials' in native ? native.has_oauth_credentials : native.has_chatgpt_tokens))}
      signedInDetail={native && 'chatgpt_account' in native && native.chatgpt_account ? [native.chatgpt_account.email, native.chatgpt_account.plan_type, (native.chatgpt_account.organizations?.find((org) => org.is_default) || native.chatgpt_account.organizations?.[0])?.title].filter(Boolean).join(' · ') : undefined} /> : <>
      {oauth.error && <p className="connection-error" role="alert">{oauth.error}</p>}
      {!oauth.isActive ? <div className="connection-account"><h3>{title}</h3><p>{hint}</p>
        {signedIn && <p className="connection-confirmed">{t('onboarding.connection.savedSubscription')}</p>}
        <div><Button variant="brand" onClick={() => void oauth.startFlow()} disabled={oauth.starting}><ExternalLink size={15} />{startLabel}</Button></div>
      </div> : <div className="connection-auth-steps">
        {oauth.deviceCode && <><Label>{t('onboarding.connection.copyDevice')}</Label><OAuthDeviceCodeRow code={oauth.deviceCode} onCopy={(event) => void oauth.copyDeviceCode(event)} copyLabel={t('common.copy')} /></>}
        {oauth.url && <><Label>{t(oauth.deviceCode ? 'onboarding.connection.openDevice' : 'onboarding.connection.authorizeBrowser')}</Label><OAuthLinkRow url={oauth.url} onCopy={(event) => void oauth.copyUrl(event)} copyLabel={t('common.copy')} /></>}
        {oauth.deviceCode && oauth.url && <div><Button variant="brand" asChild><a href={oauth.url} target="_blank" rel="noopener noreferrer"><ExternalLink size={15} />{t(backend === 'codex' ? 'onboarding.connection.openChatGPT' : 'onboarding.connection.openProvider')}</a></Button></div>}
        {needsCode && <div className="connection-field"><Label htmlFor="connection-code">{t(backend === 'claude' ? 'settings.backends.claudeCallbackCodeLabel' : oauth.callbackKind === 'code' ? 'onboarding.connection.manualCode' : 'settings.backends.opencodeCallbackUrlLabel')}</Label>
          <Input id="connection-code" value={oauth.code} onChange={(event) => oauth.setCode(event.target.value)} disabled={oauth.submitting} placeholder={backend === 'claude' ? 'code#state' : oauth.callbackKind === 'code' ? t('onboarding.connection.manualCodePlaceholder') : 'http://127.0.0.1:…/callback?code=…'} autoComplete="off" />
          <p>{t(backend === 'claude' ? 'onboarding.connection.codeHint' : oauth.callbackKind === 'code' ? 'onboarding.connection.manualCodeHint' : 'settings.backends.opencodeCallbackUrlHint')}</p></div>}
        <p className="connection-waiting" role="status"><LoaderCircle size={15} className="motion-safe:animate-spin" />{t(oauth.state === 'starting' ? 'settings.backends.oauthStarting' : oauth.state === 'verifying' ? 'settings.backends.oauthVerifying' : 'onboarding.connection.waiting')}</p>
      </div>}
    </>)}
    {method === 'api_key' && <>
      {backend === 'claude' && <div className="connection-field"><Label>{credentialLabel}</Label><SegmentedRadio value={credential} onChange={(value) => { draftTouched.current = true; setCredential(value); }} disabled={busy} ariaLabel={credentialLabel}
        options={[{ id: 'api_key', label: apiKeyLabel }, { id: 'auth_token', label: tokenLabel }]} /></div>}
      <div className="connection-field"><Label htmlFor={`${prefix}-connection-key`}>{secretLabel}</Label>
        {hasKey && !editing ? <div className="connection-secret"><KeyRound size={15} /><code>{mask}</code><Button variant="ghost" size="xs" disabled={busy} onClick={() => { draftTouched.current = true; setEditing(true); setKey(''); }}><Pencil size={14} />{t('settings.backends.replaceApiKey')}</Button></div>
          : <div className="connection-secret"><KeyRound size={15} /><Input id={`${prefix}-connection-key`} type={reveal ? 'text' : 'password'} value={key} onChange={(event) => { draftTouched.current = true; setKey(event.target.value); }} disabled={busy} autoComplete="off" spellCheck={false} placeholder={credential === 'auth_token' ? t('settings.backends.claudeAuthTokenPlaceholder') : backend === 'claude' ? 'sk-ant-…' : 'sk-…'} />
            <Button variant="ghost" size="icon" aria-label={t(reveal ? 'onboarding.connection.hideKey' : 'onboarding.connection.showKey')} onClick={() => setReveal(!reveal)}>{reveal ? <EyeOff size={15} /> : <Eye size={15} />}</Button></div>}
        <p>{backend === 'opencode' ? t('onboarding.connection.providerCredentialHint', { name: provider?.name }) : t(backend === 'claude' && credential === 'auth_token' ? 'onboarding.connection.tokenHint' : 'onboarding.connection.keyHint')}</p>
        {editing && hasKey && <Button variant="link" size="xs" onClick={() => { setEditing(false); setKey(''); }}>{t('common.cancel')}</Button>}
      </div>
      <div className="connection-field"><Label htmlFor={`${prefix}-connection-url`}>{t('onboarding.connection.baseUrl')}</Label>
        <Input id={`${prefix}-connection-url`} type="url" value={baseUrl} onChange={(event) => { draftTouched.current = true; setBaseUrl(event.target.value); }} disabled={busy} autoComplete="off" placeholder={backend === 'claude' ? 'https://api.anthropic.com' : backend === 'codex' ? 'https://api.openai.com/v1' : t('onboarding.connection.providerDefault')} />
        <p>{t(backend === 'claude' ? 'onboarding.connection.claudeUrlHint' : 'onboarding.connection.urlHint')}</p>
        {!urlValid && <p className="connection-error">{t('onboarding.connection.invalidUrl')}</p>}
      </div>
    </>}
    {(compact || method === 'api_key') && <div className="connection-actions">
      {compact && <Button variant="secondary" onClick={onCancel}>{t('common.cancel')}</Button>}
      {!compact && hasKey && method === 'api_key' && <Button variant="ghost" disabled={busy} onClick={() => void remove(true)}>{t('settings.backends.claudeApiKeyRemove')}</Button>}
      {method === 'api_key' && <Button variant="brand" disabled={!canSave} onClick={() => void save()}>{saving ? t(compact ? 'onboarding.connection.connecting' : 'common.saving') : t(compact ? 'onboarding.connection.saveConnect' : 'common.save')}</Button>}
      {compact && needsCode && <Button variant="brand" disabled={!oauth.code.trim() || oauth.submitting} onClick={() => void oauth.submitCallback()}>{oauth.submitting ? t('onboarding.connection.connecting') : t('onboarding.connection.finishConnect')}</Button>}
    </div>}
  </div>;
}
