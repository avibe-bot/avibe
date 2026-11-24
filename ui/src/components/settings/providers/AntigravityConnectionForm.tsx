import { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { AlertTriangle, CheckCircle2, ExternalLink, Eye, EyeOff, Info, KeyRound, LoaderCircle, Pencil, Terminal } from 'lucide-react';
import { useApi, type AntigravityAuthMode, type AntigravityAuthState, type OAuthWebMutationResult } from '@/context/ApiContext';
import { useToast } from '@/context/ToastContext';
import { getBackendUiMeta } from '@/lib/agentBackends';
import { errorMessage } from '@/lib/errorMessage';
import { Button } from '@/components/ui/button';
import { CopyButton } from '@/components/ui/copy-button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { MethodRadio } from './MethodRadio';
import { surfaceBackendNotices } from '../shared/surfaceBackendNotices';
import { useBackendOAuth } from '../oauth/useBackendOAuth';
import { BackendOAuthPanel } from '../BackendOAuthPanel';
import { OAuthDeviceCodeRow, OAuthLinkRow } from '../oauth/OAuthFlowParts';
import './connection.css';

type DialogInitialMethod = 'oauth' | 'api_key';
type OAuthMethod = 'oauth-personal' | 'oauth-business';
type Editable = 'method' | 'api_key' | 'base_url' | 'gcp_project' | 'gcp_location';

const ANTIGRAVITY_MODES: readonly AntigravityAuthMode[] = ['oauth-personal', 'oauth-business', 'gemini-api-key', 'agent-platform'];

const METHOD_META = {
  'oauth-personal': { label: 'settings.backends.antigravityMethodGoogle', compactLabel: 'settings.backends.antigravityMethodGoogleCompact', intro: 'onboarding.connection.antigravityGoogleIntro' },
  'oauth-business': { label: 'settings.backends.antigravityMethodEnterprise', compactLabel: 'settings.backends.antigravityMethodEnterpriseCompact', intro: 'onboarding.connection.antigravityEnterpriseIntro' },
  'gemini-api-key': { label: 'settings.backends.antigravityMethodApiKey', compactLabel: 'settings.backends.antigravityMethodApiKeyCompact', intro: 'onboarding.connection.antigravityKeyIntro' },
  'agent-platform': { label: 'settings.backends.antigravityMethodAgentPlatform', compactLabel: 'settings.backends.antigravityMethodAgentPlatformCompact', intro: 'onboarding.connection.antigravityPlatformIntro' },
} as const satisfies Record<AntigravityAuthMode, { label: string; compactLabel: string; intro: string }>;

const OAUTH_META = {
  'oauth-personal': { title: 'settings.backends.antigravityOauthPersonalTitle', subtitle: 'settings.backends.antigravityOauthPersonalSubtitle', signIn: 'settings.backends.antigravitySignInGoogle', signedIn: 'settings.backends.antigravityOauthPersonalSignedIn', codeInputId: 'antigravity-connection-code' },
  'oauth-business': { title: 'settings.backends.antigravityOauthBusinessTitle', subtitle: 'settings.backends.antigravityOauthBusinessSubtitle', signIn: 'settings.backends.antigravitySignInEnterprise', signedIn: 'settings.backends.antigravityOauthBusinessSignedIn', codeInputId: 'antigravity-enterprise-code' },
} as const satisfies Record<OAuthMethod, { title: string; subtitle: string; signIn: string; signedIn: string; codeInputId: string }>;

const isAntigravityAuthMode = (value: string | null | undefined): value is AntigravityAuthMode =>
  ANTIGRAVITY_MODES.includes(value as AntigravityAuthMode);

const isValidBaseUrl = (value: string): boolean => {
  const trimmed = value.trim();
  if (!trimmed) return true;
  try {
    const parsed = new URL(trimmed);
    return (parsed.protocol === 'http:' || parsed.protocol === 'https:') && Boolean(parsed.hostname);
  } catch {
    return false;
  }
};

function ConnectionTextField({ id, label, value, onChange, disabled, placeholder, hint, type = 'text', spellCheck, error }: {
  id: string; label: string; value: string; onChange: (value: string) => void;
  disabled?: boolean; placeholder: string; hint: string; type?: string; spellCheck?: boolean; error?: string;
}) {
  return (
    <div className="connection-field">
      <Label htmlFor={id}>{label}</Label>
      <Input
        id={id} type={type} value={value} disabled={disabled} autoComplete="off" spellCheck={spellCheck}
        placeholder={placeholder} onChange={(event) => onChange(event.target.value)}
      />
      <p>{hint}</p>
      {error && <p className="connection-error">{error}</p>}
    </div>
  );
}

/** Settings and onboarding connection form for Antigravity's four auth methods. */
export function AntigravityConnectionForm({
  initialMethod = 'oauth', compact = false, connectionRevision = 0, onConnected, onCancel, onBusyChange, onWriteState,
}: {
  initialMethod?: DialogInitialMethod; compact?: boolean; connectionRevision?: number;
  onConnected?: () => void | Promise<void>; onCancel?: () => void;
  onBusyChange?: (busy: boolean) => void; onWriteState?: (pending: boolean) => void;
}) {
  const api = useApi();
  const { t } = useTranslation();
  const { showToast } = useToast();

  const [auth, setAuth] = useState<AntigravityAuthState | null>(null);
  const [method, setMethod] = useState<AntigravityAuthMode>(initialMethod === 'api_key' ? 'gemini-api-key' : 'oauth-personal');
  const [apiKey, setApiKey] = useState('');
  const [editingKey, setEditingKey] = useState(false);
  const [reveal, setReveal] = useState(false);
  const [fields, setFields] = useState({ base_url: '', gcp_project: '', gcp_location: '' });
  const [active, setActive] = useState(false);
  const [status, setStatus] = useState({
    loading: true, saving: false, error: '', connected: false, savedDisabled: false, applyPending: false, authReadable: false,
  });

  const observation = useRef(0);
  const touched = useRef(new Set<Editable>());
  const lifetime = useRef({ mounted: true, busy: false });
  const callbacks = useRef({ onConnected, onWriteState });

  useEffect(() => {
    callbacks.current = { onConnected, onWriteState };
  }, [onConnected, onWriteState]);

  useEffect(() => {
    const owner = lifetime.current;
    owner.mounted = true;
    return () => { owner.mounted = false; observation.current += 1; };
  }, []);

  const invalidateObservation = () => { observation.current += 1; };

  const observe = useCallback(({ receiptError = '' }: { receiptError?: string } = {}) => {
    const token = ++observation.current;
    const current = () => lifetime.current.mounted && token === observation.current;

    return Promise.allSettled([
      api.getBackendConnection('antigravity'),
      api.getAntigravityAuth(),
    ]).then(([application, authResult]) => {
      if (!current()) return false;

      const messages = receiptError ? [receiptError] : [];
      const connection = application.status === 'fulfilled' ? application.value : null;
      if (!(connection?.ok && ['applied', 'stopped'].includes(connection.application))) {
        messages.push(
          application.status === 'rejected'
            ? errorMessage(application.reason) || t('onboarding.connection.readFailed')
            : connection?.message || t('onboarding.connection.applyPending'),
        );
      }

      let hasAuth = false;
      let nextAuthReadable = false;
      if (authResult.status === 'rejected') {
        messages.push(errorMessage(authResult.reason) || t('onboarding.connection.readFailed'));
      } else if (!authResult.value.ok) {
        messages.push(authResult.value.message || t('onboarding.connection.readFailed'));
      } else {
        const fresh = authResult.value;
        setAuth(fresh);
        nextAuthReadable = true;
        setFields((prev) => ({
          base_url: touched.current.has('base_url') ? prev.base_url : fresh.base_url || '',
          gcp_project: touched.current.has('gcp_project') ? prev.gcp_project : fresh.gcp_project || '',
          gcp_location: touched.current.has('gcp_location') ? prev.gcp_location : fresh.gcp_location || '',
        }));
        if (!compact && !touched.current.has('method') && isAntigravityAuthMode(fresh.auth_mode)) {
          setMethod(fresh.auth_mode);
        }
        hasAuth = Boolean(fresh.authenticated || fresh.credentials_present);
      }

      const confirmed = messages.length === 0;
      setStatus((s) => ({
        ...s,
        loading: false,
        authReadable: nextAuthReadable,
        error: [...new Set(messages)].join(' '),
        applyPending: !confirmed,
        connected: confirmed && hasAuth && Boolean(connection?.ready),
        savedDisabled: confirmed && hasAuth && connection?.enabled === false,
      }));
      return confirmed;
    });
  }, [api, compact, t]);

  useEffect(() => {
    void connectionRevision;
    void observe();
    return () => { observation.current += 1; };
  }, [connectionRevision, observe]);

  const refreshObservation = (options: { receiptError?: string } = {}) => {
    setStatus((s) => ({ ...s, connected: false, savedDisabled: false, error: options.receiptError ?? '' }));
    return observe(options);
  };

  const confirmOAuth = async (applicationError = '') => {
    const confirmed = await refreshObservation({ receiptError: applicationError });
    if (!confirmed) throw new Error(t('onboarding.connection.applyPending'));
    touched.current.delete('method');
    await callbacks.current.onConnected?.();
  };

  const observeOAuthFailure = async (receiptError: string) => { await refreshObservation({ receiptError }); };
  const changeOAuthActive = (value: boolean) => { if (value) invalidateObservation(); setActive(value); };

  const oauthAuthMode: OAuthMethod = method === 'oauth-business' ? 'oauth-business' : 'oauth-personal';
  const oauth = useBackendOAuth({
    backend: 'antigravity',
    authMode: oauthAuthMode,
    onSuccess: confirmOAuth,
    onFailure: observeOAuthFailure,
    onCancel: invalidateObservation,
    onActiveChange: changeOAuthActive,
    onPendingChange: onWriteState,
  });

  const removed = (result: OAuthWebMutationResult) => refreshObservation({
    receiptError: !result.ok
      ? result.error || result.detail || t('onboarding.connection.saveFailed')
      : result.restart?.ok === false
        ? result.restart.message || t('onboarding.connection.applyFailed')
        : '',
  });

  const busy = status.saving || active;
  useEffect(() => { onBusyChange?.(busy); }, [busy, onBusyChange]);

  const hasKey = status.authReadable && Boolean(auth?.has_api_key);
  const urlValid = isValidBaseUrl(fields.base_url);
  const isOAuthMethod = method === 'oauth-personal' || method === 'oauth-business';
  const canSave = !status.loading && status.authReadable && !busy && (
    method === 'gemini-api-key'
      ? urlValid && Boolean(apiKey.trim() || (hasKey && !editingKey))
      : method === 'agent-platform' && Boolean(fields.gcp_project.trim())
  );

  const runMutation = async (fallbackError: string, action: () => Promise<void>) => {
    lifetime.current.busy = true;
    invalidateObservation();
    callbacks.current.onWriteState?.(true);
    setStatus((s) => ({ ...s, saving: true, error: '', connected: false, savedDisabled: false }));
    try {
      await action();
    } catch (err) {
      if (lifetime.current.mounted) await observe({ receiptError: errorMessage(err) || fallbackError });
    } finally {
      lifetime.current.busy = false;
      callbacks.current.onWriteState?.(false);
      if (lifetime.current.mounted) setStatus((s) => ({ ...s, saving: false }));
    }
  };

  const save = () => {
    if (!canSave || lifetime.current.busy) return;
    const payload = method === 'gemini-api-key'
      ? { auth_mode: 'gemini-api-key' as const, api_key: apiKey.trim() || undefined, base_url: fields.base_url.trim() || null }
      : { auth_mode: 'agent-platform' as const, gcp_project: fields.gcp_project.trim(), gcp_location: fields.gcp_location.trim() || null };
    return runMutation(t('onboarding.connection.saveFailed'), async () => {
      const result = await api.saveAntigravityAuth(payload);
      if (!lifetime.current.mounted) return;
      if (!result.ok) throw new Error(result.message || result.detail || result.error || t('onboarding.connection.saveFailed'));
      if (result.notices) surfaceBackendNotices(result.notices, showToast, t);
      if (result.partial) showToast(result.detail || result.warning || t('onboarding.connection.partial'), 'warning');

      const receiptError = result.restart?.ok === false ? result.restart.message || t('onboarding.connection.applyFailed') : '';
      const confirmed = await observe({ receiptError });
      if (!lifetime.current.mounted || !confirmed) return;

      if (method === 'gemini-api-key') { setApiKey(''); setEditingKey(false); }
      const keys: Editable[] = method === 'gemini-api-key' ? ['api_key', 'base_url', 'method'] : ['gcp_project', 'gcp_location', 'method'];
      keys.forEach((key) => touched.current.delete(key));
      await callbacks.current.onConnected?.();
    });
  };

  const removeApiKey = () => {
    if (lifetime.current.busy || !window.confirm(t('onboarding.connection.removeConfirm'))) return;
    return runMutation(t('onboarding.connection.readFailed'), async () => {
      const result = await api.removeBackendApiKey('antigravity');
      if (!lifetime.current.mounted) return;
      if (!result.ok) throw new Error(result.detail || result.error || t('onboarding.connection.saveFailed'));
      if (result.notices) surfaceBackendNotices(result.notices, showToast, t);
      setApiKey('');
      setEditingKey(false);
      touched.current.delete('api_key');
      await removed(result);
    });
  };

  const selectMethod = (value: AntigravityAuthMode) => {
    if (busy || status.loading) return;
    invalidateObservation();
    oauth.resetToIdle();
    touched.current.add('method');
    setMethod(value);
    setStatus((s) => ({ ...s, error: '', connected: false }));
  };

  const updateField = (key: 'base_url' | 'gcp_project' | 'gcp_location', value: string) => {
    touched.current.add(key);
    setFields((prev) => ({ ...prev, [key]: value }));
  };

  const oauthMeta = OAUTH_META[oauthAuthMode];
  const oauthSignedIn = status.authReadable && Boolean(auth?.authenticated) && auth?.auth_mode === oauthAuthMode;
  const canRemoveOAuth = status.authReadable && Boolean(auth?.authenticated || auth?.credentials_present);
  const needsCode = oauth.state === 'awaiting_code' && (
    oauth.callbackKind === 'code' || oauth.callbackKind === 'redirect' || (!oauth.callbackKind && Boolean(oauth.url) && !oauth.deviceCode)
  );
  const adcCommand = auth?.adc_command?.trim() || '';
  const enterpriseTerminalCommand = oauth.terminalCommand?.trim() || getBackendUiMeta('antigravity').defaultCli || '';
  const waitingKey = oauth.state === 'starting'
    ? 'settings.backends.oauthStarting'
    : oauth.state === 'verifying'
      ? 'settings.backends.oauthVerifying'
      : 'onboarding.connection.waiting';

  const loadingView = (
    <div className="connection-loading" role="status">
      <LoaderCircle className="animate-spin" size={16} />{t('common.loading')}
    </div>
  );
  if (status.loading && !compact) return loadingView;

  const personalAutomationNotice = (
    <div className="flex items-start gap-2.5 rounded-lg border border-gold/40 bg-gold/[0.08] px-3.5 py-2.5 text-[12px] leading-relaxed text-foreground" role="note">
      <Info className="mt-0.5 size-4 shrink-0 text-gold-ink" />
      <p className="text-[12px] leading-relaxed text-foreground">{t('settings.backends.antigravityPersonalAutomationNotice')}</p>
    </div>
  );

  const enterpriseTerminalBlock = (
    <div className="flex flex-col gap-2 rounded-lg border border-edge bg-surface px-3.5 py-3 shadow-elevation-0">
      <div className="flex items-center gap-2 text-[12px] font-semibold text-foreground">
        <Terminal className="size-3.5 shrink-0 text-muted" />
        <span>{t('settings.backends.antigravityEnterpriseTerminalTitle')}</span>
      </div>
      <p className="text-[12px] leading-relaxed text-muted">
        {oauth.instructions || t('settings.backends.antigravityEnterpriseTerminalHint')}
      </p>
      <div className="flex items-center justify-between gap-2 rounded-md border border-edge bg-background px-3 py-2">
        <code className="font-mono text-[12px] text-foreground">{enterpriseTerminalCommand}</code>
        <CopyButton value={enterpriseTerminalCommand} />
      </div>
    </div>
  );

  return (
    <div className="backend-connection-form">
      {!(compact && active) && (
        <MethodRadio
          value={method} onChange={selectMethod} disabled={busy || status.loading}
          ariaLabel={t('onboarding.connection.method')}
          className={compact ? undefined : 'h-auto flex-wrap sm:h-9 sm:flex-nowrap'}
          options={ANTIGRAVITY_MODES.map((id) => ({ id, label: t(compact ? METHOD_META[id].compactLabel : METHOD_META[id].label) }))}
        />
      )}

      <div className="connection-body">
        {status.loading ? loadingView : (
          <>
            {compact && <p className="connection-intro">{t(METHOD_META[method].intro)}</p>}
            {status.applyPending && (
              <div><Button variant="secondary" disabled={busy} onClick={() => void refreshObservation()}>{t('onboarding.connection.refresh')}</Button></div>
            )}
            {status.connected && (
              <p className="connection-confirmed" role="status"><CheckCircle2 size={16} />{t('onboarding.connection.connected')}</p>
            )}
            {status.savedDisabled && (
              <p className="text-xs text-muted break-words" role="status">{t('onboarding.connection.savedDisabled')}</p>
            )}
            {status.authReadable && auth?.credentials_present && !auth.authenticated && (
              <p className="connection-notice" role="status">{t('settings.backends.antigravityCredentialsUnverified')}</p>
            )}
            {status.error && (
              <div role="alert" className="connection-error">
                {status.error}
                {!auth && <Button variant="secondary" onClick={() => void refreshObservation()}>{t('common.retry')}</Button>}
              </div>
            )}

            {isOAuthMethod && (!compact ? (
              <div className="flex flex-col gap-3">
                <BackendOAuthPanel
                  key={oauthAuthMode} backend="antigravity" authMode={oauthAuthMode}
                  signedIn={oauthSignedIn} canRemoveAuth={canRemoveOAuth}
                  title={t(oauthMeta.title)} subtitle={t(oauthMeta.subtitle)}
                  notice={method === 'oauth-personal' ? personalAutomationNotice : undefined}
                  startButtonLabel={t(oauthMeta.signIn)} signedInLabel={t(oauthMeta.signedIn)}
                  onSuccess={confirmOAuth} onRemoved={removed} onFailure={observeOAuthFailure}
                  onCancel={invalidateObservation} onActiveChange={changeOAuthActive}
                />
                {method === 'oauth-business' && enterpriseTerminalBlock}
              </div>
            ) : (
              <>
                {method === 'oauth-personal' && personalAutomationNotice}
                {oauth.error && <p className="connection-error" role="alert">{oauth.error}</p>}
                {!oauth.isActive ? (
                  <div className="connection-account">
                    <h3>{t(oauthMeta.title)}</h3>
                    <p>{t(oauthMeta.subtitle)}</p>
                    {oauthSignedIn && <p className="connection-confirmed">{t(oauthMeta.signedIn)}</p>}
                    <div>
                      <Button variant="brand" onClick={() => void oauth.startFlow()} disabled={oauth.starting}>
                        <ExternalLink size={15} />{t(oauthMeta.signIn)}
                      </Button>
                    </div>
                  </div>
                ) : (
                  <div className="connection-auth-steps">
                    {oauth.deviceCode && <><Label>{t('onboarding.connection.copyDevice')}</Label><OAuthDeviceCodeRow code={oauth.deviceCode} /></>}
                    {oauth.url && (
                      <>
                        <Label>{t(oauth.deviceCode ? 'onboarding.connection.openDevice' : 'onboarding.connection.authorizeBrowser')}</Label>
                        <OAuthLinkRow url={oauth.url} />
                      </>
                    )}
                    {needsCode && (
                      <ConnectionTextField
                        id={oauthMeta.codeInputId} label={t('settings.backends.antigravityCallbackCodeLabel')}
                        value={oauth.code} onChange={oauth.setCode} disabled={oauth.submitting}
                        placeholder={t('settings.backends.antigravityCallbackCodePlaceholder')}
                        hint={t('settings.backends.antigravityCallbackCodeHint')}
                      />
                    )}
                    <p className="connection-waiting" role="status">
                      <LoaderCircle size={15} className="motion-safe:animate-spin" />{t(waitingKey)}
                    </p>
                  </div>
                )}
                {method === 'oauth-business' && enterpriseTerminalBlock}
              </>
            ))}

            {method === 'gemini-api-key' && (
              <>
                <div className="connection-field">
                  <Label htmlFor="antigravity-connection-key">{t('settings.backends.antigravityApiKeyLabel')}</Label>
                  {hasKey && !editingKey ? (
                    <div className="connection-secret">
                      <KeyRound size={15} />
                      <code>{auth?.api_key_masked || t('settings.backends.antigravityApiKeyMasked')}</code>
                      <Button variant="ghost" size="xs" disabled={busy} onClick={() => { setEditingKey(true); setApiKey(''); touched.current.add('api_key'); }}>
                        <Pencil size={14} />{t('settings.backends.replaceApiKey')}
                      </Button>
                    </div>
                  ) : (
                    <div className="connection-secret">
                      <KeyRound size={15} />
                      <Input
                        id="antigravity-connection-key" type={reveal ? 'text' : 'password'} value={apiKey}
                        disabled={busy} autoComplete="off" spellCheck={false}
                        placeholder={t('settings.backends.antigravityApiKeyPlaceholder')}
                        onChange={(event) => {
                          const next = event.target.value;
                          setApiKey(next);
                          if (next || editingKey) touched.current.add('api_key');
                          else touched.current.delete('api_key');
                        }}
                      />
                      <Button variant="ghost" size="icon" aria-label={t(reveal ? 'onboarding.connection.hideKey' : 'onboarding.connection.showKey')} onClick={() => setReveal((prev) => !prev)}>
                        {reveal ? <EyeOff size={15} /> : <Eye size={15} />}
                      </Button>
                    </div>
                  )}
                  <p>{t('settings.backends.antigravityApiKeyHint')}</p>
                  {editingKey && hasKey && (
                    <Button variant="link" size="xs" onClick={() => { setEditingKey(false); setApiKey(''); touched.current.delete('api_key'); }}>
                      {t('common.cancel')}
                    </Button>
                  )}
                </div>

                <ConnectionTextField
                  id="antigravity-connection-url" type="url" label={t('settings.backends.antigravityBaseUrlLabel')}
                  value={fields.base_url} onChange={(value) => updateField('base_url', value)} disabled={busy}
                  placeholder={t('settings.backends.antigravityBaseUrlPlaceholder')}
                  hint={t('settings.backends.antigravityBaseUrlHint')}
                  error={!urlValid ? t('onboarding.connection.invalidUrl') : undefined}
                />
              </>
            )}

            {method === 'agent-platform' && (
              <>
                <ConnectionTextField
                  id="antigravity-gcp-project" label={t('settings.backends.antigravityGcpProjectLabel')}
                  value={fields.gcp_project} onChange={(value) => updateField('gcp_project', value)} disabled={busy} spellCheck={false}
                  placeholder={t('settings.backends.antigravityGcpProjectPlaceholder')}
                  hint={t('settings.backends.antigravityGcpProjectHint')}
                />
                <ConnectionTextField
                  id="antigravity-gcp-location" label={t('settings.backends.antigravityGcpLocationLabel')}
                  value={fields.gcp_location} onChange={(value) => updateField('gcp_location', value)} disabled={busy} spellCheck={false}
                  placeholder={t('settings.backends.antigravityGcpLocationPlaceholder')}
                  hint={t('settings.backends.antigravityGcpLocationHint')}
                />
                <div className="flex flex-col gap-2.5 rounded-lg border border-edge bg-surface-2/60 px-3.5 py-3 shadow-elevation-0">
                  <span className="text-[12px] font-semibold text-foreground">{t('settings.backends.antigravityAdcStatusLabel')}</span>
                  <div className={`flex items-start gap-2 text-[12px] ${auth?.adc_present ? 'text-mint-ink' : 'text-gold-ink'}`}>
                    {auth?.adc_present ? <CheckCircle2 className="mt-0.5 size-3.5 shrink-0" /> : <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />}
                    <span>{t(auth?.adc_present ? 'settings.backends.antigravityAdcPresent' : 'settings.backends.antigravityAdcMissing')}</span>
                  </div>
                  {adcCommand && (
                    <>
                      <p className="text-[12px] leading-relaxed text-muted">{t('settings.backends.antigravityAdcCommandHint')}</p>
                      <div className="flex items-center justify-between gap-2 rounded-md border border-edge bg-background px-3 py-2">
                        <code className="overflow-x-auto font-mono text-[12px] text-foreground">{adcCommand}</code>
                        <CopyButton value={adcCommand} />
                      </div>
                    </>
                  )}
                </div>
              </>
            )}
          </>
        )}
      </div>

      {(compact || !isOAuthMethod) && (
        <div className="connection-actions">
          {compact && <Button variant="secondary" onClick={onCancel}>{t('common.cancel')}</Button>}
          {!compact && hasKey && method === 'gemini-api-key' && (
            <Button variant="ghost" disabled={busy} onClick={() => void removeApiKey()}>{t('settings.backends.antigravityApiKeyRemove')}</Button>
          )}
          {!isOAuthMethod && (
            <Button variant="brand" disabled={!canSave} onClick={() => void save()}>
              {status.saving ? t(compact ? 'onboarding.connection.connecting' : 'common.saving') : t(compact ? 'onboarding.connection.saveConnect' : 'common.save')}
            </Button>
          )}
          {compact && isOAuthMethod && needsCode && (
            <Button variant="brand" disabled={!oauth.code.trim() || oauth.submitting} onClick={() => void oauth.submitCallback()}>
              {oauth.submitting ? t('onboarding.connection.connecting') : t('onboarding.connection.finishConnect')}
            </Button>
          )}
        </div>
      )}
    </div>
  );
}
