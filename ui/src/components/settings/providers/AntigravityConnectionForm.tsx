import { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  AlertTriangle,
  CheckCircle2,
  ExternalLink,
  Eye,
  EyeOff,
  Info,
  KeyRound,
  LoaderCircle,
  Pencil,
  Terminal,
} from 'lucide-react';
import {
  useApi,
  type AntigravityAuthMode,
  type AntigravityAuthState,
  type OAuthWebMutationResult,
} from '@/context/ApiContext';
import { useToast } from '@/context/ToastContext';
import { errorMessage } from '@/lib/errorMessage';
import { isNativeAuthHubOwned } from '@/lib/nativeAuthOwnership';
import { Button } from '@/components/ui/button';
import { CopyButton } from '@/components/ui/copy-button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { MethodRadio } from './MethodRadio';
import { HubOwnedAuthNotice } from '../shared/HubOwnedAuthNotice';
import { surfaceBackendNotices } from '../shared/surfaceBackendNotices';
import { useBackendOAuth } from '../oauth/useBackendOAuth';
import { BackendOAuthPanel } from '../BackendOAuthPanel';
import { OAuthDeviceCodeRow, OAuthLinkRow } from '../oauth/OAuthFlowParts';
import './connection.css';

type DialogInitialMethod = 'oauth' | 'api_key';
type Editable = 'method' | 'api_key' | 'base_url' | 'gcp_project' | 'gcp_location';

const DEFAULT_ADC_COMMAND = 'gcloud auth application-default login';

const isAntigravityAuthMode = (value: string | null | undefined): value is AntigravityAuthMode =>
  value === 'oauth-personal'
  || value === 'oauth-business'
  || value === 'gemini-api-key'
  || value === 'agent-platform';

const mapInitialMethod = (initialMethod: DialogInitialMethod): AntigravityAuthMode =>
  initialMethod === 'api_key' ? 'gemini-api-key' : 'oauth-personal';

/** Settings and onboarding connection form for Antigravity's four auth methods. */
export function AntigravityConnectionForm({
  initialMethod = 'oauth',
  compact = false,
  connectionRevision = 0,
  onConnected,
  onCancel,
  onBusyChange,
  onWriteState,
}: {
  initialMethod?: DialogInitialMethod;
  compact?: boolean;
  connectionRevision?: number;
  onConnected?: () => void | Promise<void>;
  onCancel?: () => void;
  onBusyChange?: (busy: boolean) => void;
  onWriteState?: (pending: boolean) => void;
}) {
  const api = useApi();
  const { t } = useTranslation();
  const { showToast } = useToast();

  const [auth, setAuth] = useState<AntigravityAuthState | null>(null);
  const [method, setMethod] = useState<AntigravityAuthMode>(mapInitialMethod(initialMethod));
  const [apiKey, setApiKey] = useState('');
  const [editingKey, setEditingKey] = useState(false);
  const [reveal, setReveal] = useState(false);
  const [baseUrl, setBaseUrl] = useState('');
  const [gcpProject, setGcpProject] = useState('');
  const [gcpLocation, setGcpLocation] = useState('');

  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [connected, setConnected] = useState(false);
  const [savedDisabled, setSavedDisabled] = useState(false);
  const [active, setActive] = useState(false);
  const [applyPending, setApplyPending] = useState(false);
  const [authReadable, setAuthReadable] = useState(false);
  const [hubOwnedAuth, setHubOwnedAuth] = useState(false);

  const observation = useRef(0);
  const touched = useRef(new Set<Editable>());
  const writeState = useRef(onWriteState);
  writeState.current = onWriteState;
  const lifetime = useRef({ mounted: true, busy: false });
  const onConnectedRef = useRef(onConnected);
  onConnectedRef.current = onConnected;

  useEffect(() => {
    const owner = lifetime.current;
    owner.mounted = true;
    return () => {
      owner.mounted = false;
      observation.current += 1;
    };
  }, []);

  const observe = useCallback(async ({ receiptError = '' }: { receiptError?: string } = {}) => {
    const token = ++observation.current;
    const current = () => lifetime.current.mounted && token === observation.current;
    setConnected(false);
    setSavedDisabled(false);
    setError(receiptError);

    const [application] = await Promise.allSettled([api.getBackendConnection('antigravity')]);
    if (!current()) return false;

    const messages = receiptError ? [receiptError] : [];
    const connection = application.status === 'fulfilled' ? application.value : null;
    const applied = connection?.ok && ['applied', 'stopped'].includes(connection.application);
    if (!applied) {
      messages.push(
        application.status === 'rejected'
          ? errorMessage(application.reason) || t('onboarding.connection.readFailed')
          : connection?.message || t('onboarding.connection.applyPending'),
      );
    }

    const supplyMode = connection?.ok === true ? connection.supply_mode : undefined;
    setHubOwnedAuth(supplyMode === 'hub');
    if (supplyMode === 'hub') {
      setLoading(false);
      setAuthReadable(false);
      setAuth(null);
      setError([...new Set(messages)].join(' '));
      setApplyPending(messages.length > 0);
      setConnected(false);
      setSavedDisabled(false);
      return messages.length === 0;
    }

    const [authResult] = await Promise.allSettled([api.getAntigravityAuth()]);
    if (!current()) return false;
    setLoading(false);

    let hasAuth = false;
    if (authResult.status === 'rejected') {
      setAuthReadable(false);
      messages.push(errorMessage(authResult.reason) || t('onboarding.connection.readFailed'));
    } else if (!authResult.value.ok) {
      setAuthReadable(false);
      messages.push(authResult.value.message || t('onboarding.connection.readFailed'));
    } else {
      const fresh = authResult.value;
      setAuth(fresh);
      setAuthReadable(true);
      if (!touched.current.has('base_url')) setBaseUrl(fresh.base_url || '');
      if (!touched.current.has('gcp_project')) setGcpProject(fresh.gcp_project || '');
      if (!touched.current.has('gcp_location')) setGcpLocation(fresh.gcp_location || '');
      if (!compact && !touched.current.has('method') && isAntigravityAuthMode(fresh.auth_mode)) {
        setMethod(fresh.auth_mode);
      }
      hasAuth = Boolean(fresh.authenticated || fresh.credentials_present);
    }

    setError([...new Set(messages)].join(' '));
    setApplyPending(messages.length > 0);
    const confirmed = messages.length === 0;
    setConnected(confirmed && hasAuth && Boolean(connection?.ready));
    setSavedDisabled(confirmed && hasAuth && connection?.enabled === false);
    return confirmed;
  }, [api, compact, t]);

  useEffect(() => {
    void observe();
    return () => {
      observation.current += 1;
    };
  }, [observe]);

  const observedRevision = useRef(connectionRevision);
  useEffect(() => {
    if (observedRevision.current === connectionRevision) return;
    observedRevision.current = connectionRevision;
    void observe();
    return () => {
      observation.current += 1;
    };
  }, [connectionRevision, observe]);

  const confirmOAuth = async (applicationError = '') => {
    const confirmed = await observe({ receiptError: applicationError });
    if (!confirmed) throw new Error(t('onboarding.connection.applyPending'));
    touched.current.delete('method');
    await onConnectedRef.current?.();
  };

  const observeOAuthFailure = async (receiptError: string) => {
    await observe({ receiptError });
  };

  const changeOAuthActive = (value: boolean) => {
    if (value) observation.current += 1;
    setActive(value);
  };

  const oauthAuthMode = method === 'oauth-business' ? 'oauth-business' : 'oauth-personal';
  const oauth = useBackendOAuth({
    backend: 'antigravity',
    authMode: oauthAuthMode,
    onSuccess: confirmOAuth,
    onFailure: observeOAuthFailure,
    onCancel: () => {
      observation.current += 1;
    },
    onActiveChange: changeOAuthActive,
    onPendingChange: onWriteState,
  });

  const removed = async (result: OAuthWebMutationResult) => {
    if (isNativeAuthHubOwned(result)) {
      setHubOwnedAuth(true);
      setError('');
      setApplyPending(false);
      return false;
    }
    return observe({
      receiptError: !result.ok
        ? result.error || result.detail || t('onboarding.connection.saveFailed')
        : result.restart?.ok === false
          ? result.restart.message || t('onboarding.connection.applyFailed')
          : '',
    });
  };

  const busy = saving || active;
  useEffect(() => {
    onBusyChange?.(busy);
  }, [busy, onBusyChange]);

  const hasKey = authReadable && Boolean(auth?.has_api_key);
  const mask = auth?.api_key_masked || '••••••••';

  const urlValid = (() => {
    if (!baseUrl.trim()) return true;
    try {
      const parsed = new URL(baseUrl.trim());
      return ['http:', 'https:'].includes(parsed.protocol) && Boolean(parsed.hostname);
    } catch {
      return false;
    }
  })();

  const isOAuthMethod = method === 'oauth-personal' || method === 'oauth-business';
  const canSaveApiKey =
    !loading
    && !hubOwnedAuth
    && authReadable
    && !busy
    && urlValid
    && Boolean(apiKey.trim() || (hasKey && !editingKey));
  const canSaveAgentPlatform =
    !loading
    && !hubOwnedAuth
    && authReadable
    && !busy
    && Boolean(gcpProject.trim());
  const canSave = method === 'gemini-api-key' ? canSaveApiKey : method === 'agent-platform' ? canSaveAgentPlatform : false;

  const save = async () => {
    if (!canSave || lifetime.current.busy) return;
    lifetime.current.busy = true;
    observation.current += 1;
    writeState.current?.(true);
    setSaving(true);
    setError('');
    setConnected(false);
    setSavedDisabled(false);
    setHubOwnedAuth(false);

    const payload =
      method === 'gemini-api-key'
        ? {
            auth_mode: 'gemini-api-key' as const,
            api_key: apiKey.trim() || undefined,
            base_url: baseUrl.trim() || null,
          }
        : {
            auth_mode: 'agent-platform' as const,
            gcp_project: gcpProject.trim(),
            gcp_location: gcpLocation.trim() || null,
          };

    try {
      const result = await api.saveAntigravityAuth(payload);
      if (!lifetime.current.mounted) return;
      if (!result.ok) {
        if (isNativeAuthHubOwned(result)) {
          setHubOwnedAuth(true);
          setApplyPending(false);
          return;
        }
        throw new Error(result.message || result.detail || result.error || t('onboarding.connection.saveFailed'));
      }
      if (result.notices) surfaceBackendNotices(result.notices, showToast, t);
      if (result.partial) showToast(result.detail || result.warning || t('onboarding.connection.partial'), 'warning');

      const receiptError =
        result.restart?.ok === false
          ? result.restart.message || t('onboarding.connection.applyFailed')
          : '';
      const confirmed = await observe({ receiptError });
      if (!lifetime.current.mounted || !confirmed) return;

      if (method === 'gemini-api-key') {
        setApiKey('');
        setEditingKey(false);
        touched.current.delete('api_key');
        touched.current.delete('base_url');
      } else {
        touched.current.delete('gcp_project');
        touched.current.delete('gcp_location');
      }
      touched.current.delete('method');
      await onConnectedRef.current?.();
    } catch (err) {
      if (lifetime.current.mounted) {
        await observe({ receiptError: errorMessage(err) || t('onboarding.connection.saveFailed') });
      }
    } finally {
      lifetime.current.busy = false;
      writeState.current?.(false);
      if (lifetime.current.mounted) setSaving(false);
    }
  };

  const removeApiKey = async () => {
    if (lifetime.current.busy || !window.confirm(t('onboarding.connection.removeConfirm'))) return;
    lifetime.current.busy = true;
    observation.current += 1;
    writeState.current?.(true);
    setSaving(true);
    setError('');
    setConnected(false);
    setSavedDisabled(false);
    setHubOwnedAuth(false);
    try {
      const result = await api.removeBackendApiKey('antigravity');
      if (!lifetime.current.mounted) return;
      if (!result.ok) {
        if (isNativeAuthHubOwned(result)) {
          setHubOwnedAuth(true);
          setApplyPending(false);
          return;
        }
        throw new Error(result.detail || result.error || t('onboarding.connection.saveFailed'));
      }
      if (result.notices) surfaceBackendNotices(result.notices, showToast, t);
      setApiKey('');
      setEditingKey(false);
      touched.current.delete('api_key');
      await removed(result);
    } catch (err) {
      if (lifetime.current.mounted) {
        await observe({ receiptError: errorMessage(err) || t('onboarding.connection.readFailed') });
      }
    } finally {
      lifetime.current.busy = false;
      writeState.current?.(false);
      if (lifetime.current.mounted) setSaving(false);
    }
  };

  const personalSignedIn = authReadable && Boolean(auth?.authenticated) && auth?.auth_mode === 'oauth-personal';
  const businessSignedIn = authReadable && Boolean(auth?.authenticated) && auth?.auth_mode === 'oauth-business';
  const canRemoveOAuth = authReadable && Boolean(auth?.authenticated || auth?.credentials_present);
  const needsCode =
    oauth.state === 'awaiting_code'
    && (oauth.callbackKind === 'code'
      || oauth.callbackKind === 'redirect'
      || (!oauth.callbackKind && Boolean(oauth.url) && !oauth.deviceCode));

  const adcCommand = auth?.adc_command?.trim() || DEFAULT_ADC_COMMAND;
  const enterpriseTerminalCommand = oauth.terminalCommand?.trim() || 'agy';

  const personalAutomationNotice = (
    <div
      className="flex items-start gap-2.5 rounded-lg border border-gold/40 bg-gold/[0.08] px-3.5 py-2.5 text-[12px] leading-relaxed text-foreground"
      role="note"
    >
      <Info className="mt-0.5 size-4 shrink-0 text-gold-ink" />
      <p className="text-[12px] leading-relaxed text-foreground">
        {t('settings.backends.antigravityPersonalAutomationNotice')}
      </p>
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

  const methodOptions = [
    {
      id: 'oauth-personal' as const,
      label: t(compact ? 'settings.backends.antigravityMethodGoogleCompact' : 'settings.backends.antigravityMethodGoogle'),
    },
    {
      id: 'oauth-business' as const,
      label: t(compact ? 'settings.backends.antigravityMethodEnterpriseCompact' : 'settings.backends.antigravityMethodEnterprise'),
    },
    {
      id: 'gemini-api-key' as const,
      label: t(compact ? 'settings.backends.antigravityMethodApiKeyCompact' : 'settings.backends.antigravityMethodApiKey'),
    },
    {
      id: 'agent-platform' as const,
      label: t(compact ? 'settings.backends.antigravityMethodAgentPlatformCompact' : 'settings.backends.antigravityMethodAgentPlatform'),
    },
  ];

  const compactIntroKey =
    method === 'oauth-personal'
      ? 'onboarding.connection.antigravityGoogleIntro'
      : method === 'oauth-business'
        ? 'onboarding.connection.antigravityEnterpriseIntro'
        : method === 'gemini-api-key'
          ? 'onboarding.connection.antigravityKeyIntro'
          : 'onboarding.connection.antigravityPlatformIntro';

  if (loading && !compact) {
    return (
      <div className="connection-loading" role="status">
        <LoaderCircle className="animate-spin" size={16} />
        {t('common.loading')}
      </div>
    );
  }

  return (
    <div className="backend-connection-form">
      {!hubOwnedAuth && !(compact && active) && (
        <MethodRadio
          value={method}
          onChange={(value) => {
            if (!busy && !loading) {
              observation.current += 1;
              oauth.resetToIdle();
              touched.current.add('method');
              setMethod(value);
              setError('');
              setConnected(false);
            }
          }}
          disabled={busy || loading}
          ariaLabel={t('onboarding.connection.method')}
          className={compact ? undefined : 'h-auto flex-wrap sm:h-9 sm:flex-nowrap'}
          options={methodOptions}
        />
      )}

      <div className="connection-body">
        {loading && (
          <div className="connection-loading" role="status">
            <LoaderCircle className="animate-spin" size={16} />
            {t('common.loading')}
          </div>
        )}

        {!loading && !hubOwnedAuth && compact && (
          <p className="connection-intro">{t(compactIntroKey)}</p>
        )}

        {!loading && hubOwnedAuth && <HubOwnedAuthNotice onNavigate={compact ? onCancel : undefined} />}

        {!loading && !hubOwnedAuth && (
          <>
            {applyPending && (
              <div>
                <Button variant="secondary" disabled={busy} onClick={() => void observe()}>
                  {t('onboarding.connection.refresh')}
                </Button>
              </div>
            )}

            {connected && (
              <p className="connection-confirmed" role="status">
                <CheckCircle2 size={16} />
                {t('onboarding.connection.connected')}
              </p>
            )}

            {savedDisabled && (
              <p className="text-xs text-muted break-words" role="status">
                {t('onboarding.connection.savedDisabled')}
              </p>
            )}

            {authReadable && auth?.credentials_present && !auth.authenticated && (
              <p className="connection-notice" role="status">
                {t('settings.backends.antigravityCredentialsUnverified')}
              </p>
            )}

            {error && (
              <div role="alert" className="connection-error">
                {error}
                {!auth && (
                  <Button variant="secondary" onClick={() => void observe()}>
                    {t('common.retry')}
                  </Button>
                )}
              </div>
            )}

            {method === 'oauth-personal' && (!compact ? (
              <BackendOAuthPanel
                key="oauth-personal"
                backend="antigravity"
                authMode="oauth-personal"
                signedIn={personalSignedIn}
                canRemoveAuth={canRemoveOAuth}
                title={t('settings.backends.antigravityOauthPersonalTitle')}
                subtitle={t('settings.backends.antigravityOauthPersonalSubtitle')}
                notice={personalAutomationNotice}
                startButtonLabel={t('settings.backends.antigravitySignInGoogle')}
                signedInLabel={t('settings.backends.antigravityOauthPersonalSignedIn')}
                onSuccess={confirmOAuth}
                onRemoved={removed}
                onFailure={observeOAuthFailure}
                onCancel={() => {
                  observation.current += 1;
                }}
                onActiveChange={changeOAuthActive}
              />
            ) : (
              <>
                {personalAutomationNotice}
                {oauth.hubOwnedAuth && <HubOwnedAuthNotice onNavigate={onCancel} />}
                {oauth.error && !oauth.hubOwnedAuth && (
                  <p className="connection-error" role="alert">{oauth.error}</p>
                )}
                {!oauth.isActive ? (
                  <div className="connection-account">
                    <h3>{t('settings.backends.antigravityOauthPersonalTitle')}</h3>
                    <p>{t('settings.backends.antigravityOauthPersonalSubtitle')}</p>
                    {personalSignedIn && (
                      <p className="connection-confirmed">{t('settings.backends.antigravityOauthPersonalSignedIn')}</p>
                    )}
                    <div>
                      <Button variant="brand" onClick={() => void oauth.startFlow()} disabled={oauth.starting}>
                        <ExternalLink size={15} />
                        {t('settings.backends.antigravitySignInGoogle')}
                      </Button>
                    </div>
                  </div>
                ) : (
                  <div className="connection-auth-steps">
                    {oauth.deviceCode && (
                      <>
                        <Label>{t('onboarding.connection.copyDevice')}</Label>
                        <OAuthDeviceCodeRow code={oauth.deviceCode} />
                      </>
                    )}
                    {oauth.url && (
                      <>
                        <Label>{t(oauth.deviceCode ? 'onboarding.connection.openDevice' : 'onboarding.connection.authorizeBrowser')}</Label>
                        <OAuthLinkRow url={oauth.url} />
                      </>
                    )}
                    {needsCode && (
                      <div className="connection-field">
                        <Label htmlFor="antigravity-connection-code">
                          {t('settings.backends.antigravityCallbackCodeLabel')}
                        </Label>
                        <Input
                          id="antigravity-connection-code"
                          value={oauth.code}
                          onChange={(event) => oauth.setCode(event.target.value)}
                          disabled={oauth.submitting}
                          placeholder={t('settings.backends.antigravityCallbackCodePlaceholder')}
                          autoComplete="off"
                        />
                        <p>{t('settings.backends.antigravityCallbackCodeHint')}</p>
                      </div>
                    )}
                    <p className="connection-waiting" role="status">
                      <LoaderCircle size={15} className="motion-safe:animate-spin" />
                      {t(
                        oauth.state === 'starting'
                          ? 'settings.backends.oauthStarting'
                          : oauth.state === 'verifying'
                            ? 'settings.backends.oauthVerifying'
                            : 'onboarding.connection.waiting',
                      )}
                    </p>
                  </div>
                )}
              </>
            ))}

            {method === 'oauth-business' && (!compact ? (
              <div className="flex flex-col gap-3">
                <BackendOAuthPanel
                  key="oauth-business"
                  backend="antigravity"
                  authMode="oauth-business"
                  signedIn={businessSignedIn}
                  canRemoveAuth={canRemoveOAuth}
                  title={t('settings.backends.antigravityOauthBusinessTitle')}
                  subtitle={t('settings.backends.antigravityOauthBusinessSubtitle')}
                  startButtonLabel={t('settings.backends.antigravitySignInEnterprise')}
                  signedInLabel={t('settings.backends.antigravityOauthBusinessSignedIn')}
                  onSuccess={confirmOAuth}
                  onRemoved={removed}
                  onFailure={observeOAuthFailure}
                  onCancel={() => {
                    observation.current += 1;
                  }}
                  onActiveChange={changeOAuthActive}
                />
                {enterpriseTerminalBlock}
              </div>
            ) : (
              <>
                {oauth.hubOwnedAuth && <HubOwnedAuthNotice onNavigate={onCancel} />}
                {oauth.error && !oauth.hubOwnedAuth && (
                  <p className="connection-error" role="alert">{oauth.error}</p>
                )}
                {!oauth.isActive ? (
                  <div className="connection-account">
                    <h3>{t('settings.backends.antigravityOauthBusinessTitle')}</h3>
                    <p>{t('settings.backends.antigravityOauthBusinessSubtitle')}</p>
                    {businessSignedIn && (
                      <p className="connection-confirmed">{t('settings.backends.antigravityOauthBusinessSignedIn')}</p>
                    )}
                    <div>
                      <Button variant="brand" onClick={() => void oauth.startFlow()} disabled={oauth.starting}>
                        <ExternalLink size={15} />
                        {t('settings.backends.antigravitySignInEnterprise')}
                      </Button>
                    </div>
                  </div>
                ) : (
                  <div className="connection-auth-steps">
                    {oauth.deviceCode && (
                      <>
                        <Label>{t('onboarding.connection.copyDevice')}</Label>
                        <OAuthDeviceCodeRow code={oauth.deviceCode} />
                      </>
                    )}
                    {oauth.url && (
                      <>
                        <Label>{t(oauth.deviceCode ? 'onboarding.connection.openDevice' : 'onboarding.connection.authorizeBrowser')}</Label>
                        <OAuthLinkRow url={oauth.url} />
                      </>
                    )}
                    {needsCode && (
                      <div className="connection-field">
                        <Label htmlFor="antigravity-enterprise-code">
                          {t('settings.backends.antigravityCallbackCodeLabel')}
                        </Label>
                        <Input
                          id="antigravity-enterprise-code"
                          value={oauth.code}
                          onChange={(event) => oauth.setCode(event.target.value)}
                          disabled={oauth.submitting}
                          placeholder={t('settings.backends.antigravityCallbackCodePlaceholder')}
                          autoComplete="off"
                        />
                        <p>{t('settings.backends.antigravityCallbackCodeHint')}</p>
                      </div>
                    )}
                    <p className="connection-waiting" role="status">
                      <LoaderCircle size={15} className="motion-safe:animate-spin" />
                      {t(
                        oauth.state === 'starting'
                          ? 'settings.backends.oauthStarting'
                          : oauth.state === 'verifying'
                            ? 'settings.backends.oauthVerifying'
                            : 'onboarding.connection.waiting',
                      )}
                    </p>
                  </div>
                )}
                {enterpriseTerminalBlock}
              </>
            ))}

            {method === 'gemini-api-key' && (
              <>
                <div className="connection-field">
                  <Label htmlFor="antigravity-connection-key">
                    {t('settings.backends.antigravityApiKeyLabel')}
                  </Label>
                  {hasKey && !editingKey ? (
                    <div className="connection-secret">
                      <KeyRound size={15} />
                      <code>{mask}</code>
                      <Button
                        variant="ghost"
                        size="xs"
                        disabled={busy}
                        onClick={() => {
                          setEditingKey(true);
                          setApiKey('');
                          touched.current.add('api_key');
                        }}
                      >
                        <Pencil size={14} />
                        {t('settings.backends.replaceApiKey')}
                      </Button>
                    </div>
                  ) : (
                    <div className="connection-secret">
                      <KeyRound size={15} />
                      <Input
                        id="antigravity-connection-key"
                        type={reveal ? 'text' : 'password'}
                        value={apiKey}
                        onChange={(event) => {
                          const next = event.target.value;
                          setApiKey(next);
                          if (next || editingKey) touched.current.add('api_key');
                          else touched.current.delete('api_key');
                        }}
                        disabled={busy}
                        autoComplete="off"
                        spellCheck={false}
                        placeholder={t('settings.backends.antigravityApiKeyPlaceholder')}
                      />
                      <Button
                        variant="ghost"
                        size="icon"
                        aria-label={t(reveal ? 'onboarding.connection.hideKey' : 'onboarding.connection.showKey')}
                        onClick={() => setReveal(!reveal)}
                      >
                        {reveal ? <EyeOff size={15} /> : <Eye size={15} />}
                      </Button>
                    </div>
                  )}
                  <p>{t('settings.backends.antigravityApiKeyHint')}</p>
                  {editingKey && hasKey && (
                    <Button
                      variant="link"
                      size="xs"
                      onClick={() => {
                        setEditingKey(false);
                        setApiKey('');
                        touched.current.delete('api_key');
                      }}
                    >
                      {t('common.cancel')}
                    </Button>
                  )}
                </div>

                <div className="connection-field">
                  <Label htmlFor="antigravity-connection-url">
                    {t('settings.backends.antigravityBaseUrlLabel')}
                  </Label>
                  <Input
                    id="antigravity-connection-url"
                    type="url"
                    value={baseUrl}
                    onChange={(event) => {
                      touched.current.add('base_url');
                      setBaseUrl(event.target.value);
                    }}
                    disabled={busy}
                    autoComplete="off"
                    placeholder={t('settings.backends.antigravityBaseUrlPlaceholder')}
                  />
                  <p>{t('settings.backends.antigravityBaseUrlHint')}</p>
                  {!urlValid && <p className="connection-error">{t('onboarding.connection.invalidUrl')}</p>}
                </div>
              </>
            )}

            {method === 'agent-platform' && (
              <>
                <div className="connection-field">
                  <Label htmlFor="antigravity-gcp-project">
                    {t('settings.backends.antigravityGcpProjectLabel')}
                  </Label>
                  <Input
                    id="antigravity-gcp-project"
                    type="text"
                    value={gcpProject}
                    onChange={(event) => {
                      touched.current.add('gcp_project');
                      setGcpProject(event.target.value);
                    }}
                    disabled={busy}
                    autoComplete="off"
                    spellCheck={false}
                    placeholder={t('settings.backends.antigravityGcpProjectPlaceholder')}
                  />
                  <p>{t('settings.backends.antigravityGcpProjectHint')}</p>
                </div>

                <div className="connection-field">
                  <Label htmlFor="antigravity-gcp-location">
                    {t('settings.backends.antigravityGcpLocationLabel')}
                  </Label>
                  <Input
                    id="antigravity-gcp-location"
                    type="text"
                    value={gcpLocation}
                    onChange={(event) => {
                      touched.current.add('gcp_location');
                      setGcpLocation(event.target.value);
                    }}
                    disabled={busy}
                    autoComplete="off"
                    spellCheck={false}
                    placeholder={t('settings.backends.antigravityGcpLocationPlaceholder')}
                  />
                  <p>{t('settings.backends.antigravityGcpLocationHint')}</p>
                </div>

                <div className="flex flex-col gap-2.5 rounded-lg border border-edge bg-surface-2/60 px-3.5 py-3 shadow-elevation-0">
                  <span className="text-[12px] font-semibold text-foreground">
                    {t('settings.backends.antigravityAdcStatusLabel')}
                  </span>
                  {auth?.adc_present ? (
                    <div className="flex items-start gap-2 text-[12px] text-mint-ink">
                      <CheckCircle2 className="mt-0.5 size-3.5 shrink-0" />
                      <span>{t('settings.backends.antigravityAdcPresent')}</span>
                    </div>
                  ) : (
                    <div className="flex items-start gap-2 text-[12px] text-gold-ink">
                      <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />
                      <span>{t('settings.backends.antigravityAdcMissing')}</span>
                    </div>
                  )}
                  <p className="text-[12px] leading-relaxed text-muted">
                    {t('settings.backends.antigravityAdcCommandHint')}
                  </p>
                  <div className="flex items-center justify-between gap-2 rounded-md border border-edge bg-background px-3 py-2">
                    <code className="overflow-x-auto font-mono text-[12px] text-foreground">{adcCommand}</code>
                    <CopyButton value={adcCommand} />
                  </div>
                </div>
              </>
            )}
          </>
        )}
      </div>

      {(compact || (!hubOwnedAuth && !isOAuthMethod)) && (
        <div className="connection-actions">
          {compact && (
            <Button variant="secondary" onClick={onCancel}>
              {t('common.cancel')}
            </Button>
          )}
          {!hubOwnedAuth && !compact && hasKey && method === 'gemini-api-key' && (
            <Button variant="ghost" disabled={busy} onClick={() => void removeApiKey()}>
              {t('settings.backends.antigravityApiKeyRemove')}
            </Button>
          )}
          {!hubOwnedAuth && !isOAuthMethod && (
            <Button variant="brand" disabled={!canSave} onClick={() => void save()}>
              {saving
                ? t(compact ? 'onboarding.connection.connecting' : 'common.saving')
                : t(compact ? 'onboarding.connection.saveConnect' : 'common.save')}
            </Button>
          )}
          {!hubOwnedAuth && compact && isOAuthMethod && needsCode && (
            <Button
              variant="brand"
              disabled={!oauth.code.trim() || oauth.submitting}
              onClick={() => void oauth.submitCallback()}
            >
              {oauth.submitting ? t('onboarding.connection.connecting') : t('onboarding.connection.finishConnect')}
            </Button>
          )}
        </div>
      )}
    </div>
  );
}
