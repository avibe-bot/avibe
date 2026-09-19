import { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { CheckCircle2, ExternalLink, Eye, EyeOff, KeyRound, LoaderCircle, Pencil } from 'lucide-react';
import { useApi, type ClaudeAuthState, type CodexAuthState, type OpencodeProvider, type OAuthWebMutationResult } from '@/context/ApiContext';
import { useToast } from '@/context/ToastContext';
import { errorMessage } from '@/lib/errorMessage';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { MethodRadio } from './MethodRadio';
import { surfaceBackendNotices } from '../shared/surfaceBackendNotices';
import { useBackendOAuth, type OAuthBackend } from '../oauth/useBackendOAuth';
import { BackendOAuthPanel } from '../BackendOAuthPanel';
import { OAuthDeviceCodeRow, OAuthLinkRow } from '../oauth/OAuthFlowParts';
import './connection.css';

type Method = 'oauth' | 'api_key';
type Credential = 'api_key' | 'auth_token';
type NativeState = ClaudeAuthState | CodexAuthState;
export type ConnectionHeading = { method: Method; active: boolean; credential: Credential };

/**
 * One unsent secret, per credential type.
 *
 * An API key and an auth token are different credentials, not two spellings of
 * one: they are issued separately, formatted differently and sent under
 * different keys. A single draft therefore carried a half-typed key into the
 * Auth Token field the moment someone checked what the other option was — and
 * the same shared `editing` flag decided, for both, whether the stored mask was
 * showing. Keeping a draft per credential type means switching only ever
 * changes which draft is on screen, which is also what makes switching back
 * safe.
 */
type Draft = { value: string; editing: boolean };
const CREDENTIALS: readonly Credential[] = ['api_key', 'auth_token'];
const EMPTY_DRAFTS: Readonly<Record<Credential, Draft>> = {
  api_key: { value: '', editing: false },
  auth_token: { value: '', editing: false },
};
/** Typed, or asked to replace what is stored — either way, something is on screen to lose. */
const unsaved = (draft: Draft) => draft.value !== '' || draft.editing;

/**
 * What a confirmation is allowed to spend, and what it has to prove.
 *
 * A receipt settles one submission, so the only work it may release is the work
 * that submission actually carried: signing in sends no credential and no URL at
 * all, and a save sends at most one type's value alongside one URL. Carrying the
 * work itself as well as its name is what makes a late receipt safe — anything
 * changed since the write went out is no longer what was sent, so it outlives
 * its own submission's receipt rather than being erased by it. A deferred
 * confirmation therefore settles what was submitted rather than whatever happens
 * to be on screen when it lands.
 *
 * `sent` is the **whole draft** that went out, not its value, because a draft is
 * a value *and* whether someone is part-way through replacing what is stored.
 * Those two states can share a value: an empty field nobody has opened and a
 * field just opened by Replace both read `''`, and only one of them is the state
 * that was submitted. It is `null` when the payload omitted the credential
 * altogether — a save that changes only the address sends no key, so its receipt
 * has no credential work to spend, and modelling that as an empty draft would
 * make it spend one.
 *
 * `credential` is the type the write *declared*, which is not the same question:
 * Claude sends `credential_type` on every save, including one that carries no
 * key, so the type a receipt must prove outlives the draft it may or may not
 * have sent. `null` says this write declared no type at all.
 *
 * `baseUrl` is the URL exactly as it stood in the field, not the trimmed payload:
 * the question it answers is whether the person has moved on since, and `null`
 * says this write carried no URL to settle.
 */
type Submission = { method: Method; baseUrl: string | null; credential: Credential | null; sent: Draft | null };
const sameDraft = (left: Draft, right: Draft) => left.value === right.value && left.editing === right.editing;

/**
 * What one receipt releases, decided once.
 *
 * Every question here is the same question — did this submission carry this
 * editable, and does the screen still read the way it was sent — and the answers
 * are wanted in three places: the drafts to keep, the ownership to give back, and
 * whether the form may follow the server again. Answering it once and reading the
 * answer three times is the whole point of the type; computing it once by
 * comparison and again by assumption is how a retained draft lost its ownership.
 */
type Settlement = { drafts: Record<Credential, Draft>; baseUrl: boolean; method: boolean; credentialType: boolean };
const settle = (submission: Submission, showing: { baseUrl: string; method: Method; credential: Credential; drafts: Record<Credential, Draft> }): Settlement => {
  const { credential, sent } = submission;
  const released = credential !== null && sent !== null && sameDraft(showing.drafts[credential], sent);
  return {
    drafts: released && credential !== null ? { ...showing.drafts, [credential]: EMPTY_DRAFTS[credential] } : showing.drafts,
    baseUrl: submission.baseUrl !== null && showing.baseUrl === submission.baseUrl,
    method: showing.method === submission.method,
    credentialType: credential !== null && showing.credential === credential,
  };
};

/**
 * Something on this form a person can change, and an observation can seed over.
 *
 * The four are one list because they share one rule — a reseed follows the
 * server only where nobody has said otherwise — and they are tracked apart
 * because they are released apart. One shared flag would answer "has anything
 * been touched", which is the right question to ask before seeding and the wrong
 * one to answer after a receipt: settling a credential would clear a URL that
 * write never carried.
 */
type Editable = Credential | 'base_url' | 'method' | 'credential_type';

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
  const [credential, setCredential] = useState<Credential>('api_key');
  const [drafts, setDrafts] = useState<Record<Credential, Draft>>(EMPTY_DRAFTS);
  const { value: key, editing } = drafts[credential];
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
  const touched = useRef(new Set<Editable>());
  /**
   * What the person is looking at right now.
   *
   * A receipt is read in the render that dispatched the write, and the gap this
   * whole type closes is exactly the one where the field moved on while that
   * write was in flight. So the comparison a receipt makes is against the live
   * values, kept the same way `onConnectedRef` is.
   */
  const onScreen = useRef({ baseUrl, method, credential, drafts });
  onScreen.current = { baseUrl, method, credential, drafts };
  /**
   * Edits only ever reach the credential type currently on screen.
   *
   * Ownership is read off the draft the edit produces rather than asserted by the
   * act of editing, because not every edit leaves something to lose: Cancel puts
   * the field back exactly as an untouched one, and a form that still claimed it
   * would stop following the server for the rest of its life over nothing.
   */
  const patchDraft = useCallback((patch: Partial<Draft>) => {
    setDrafts((current) => {
      const next = { ...current[credential], ...patch };
      if (unsaved(next)) touched.current.add(credential);
      else touched.current.delete(credential);
      return { ...current, [credential]: next };
    });
  }, [credential]);
  const pendingConfirmation = useRef<Submission | null>(null);
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
  const observe = useCallback(async ({ expected, receiptError = '' }: {
    expected?: Submission; receiptError?: string;
  } = {}) => {
    const token = ++observation.current;
    const current = () => lifetime.current.mounted && token === observation.current;
    setConnected(false); setSavedDisabled(false); setError(receiptError);
    const pristine = () => touched.current.size === 0;
    const nativeRead = read().then((fresh) => {
      if (current()) {
        setNative(fresh.native); setCurrentProvider(fresh.provider); setAuthReadable(true);
        if (pristine()) {
          setBaseUrl(fresh.native?.base_url || fresh.provider?.base_url || '');
          if (fresh.native && 'credential_type' in fresh.native) setCredential(fresh.native.credential_type || 'api_key');
        }
        if (!compact && pristine()) {
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
      if (expected && ((!keylessSettings && effective !== expected.method) || uncertain)) messages.push(t('onboarding.connection.unconfirmed'));
      // Claude's two credential types are one method. `active_auth_mode` reads
      // `api_key` for a token exactly as it does for a key, so the method above
      // cannot tell a token write that landed from one that did not — only the
      // type the readback reports can. Absent or mismatched is unconfirmed: the
      // producer emits this field for every stored credential, including its
      // legacy API-key fallback, so nothing to compare means nothing observed,
      // not licence to settle on the weaker half of the question. Backends with
      // no such discriminator keep the method answer they have always had. The
      // claim is the type the write DECLARED, not the draft it happened to carry:
      // a save that only changes the address still declares one, and a receipt
      // that cannot see it land has still not seen the write land.
      if (expected?.credential && backend === 'claude') {
        const stored = fresh.native && 'credential_type' in fresh.native ? fresh.native.credential_type : null;
        if (stored !== expected.credential) messages.push(t('onboarding.connection.unconfirmed'));
      }
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

  // Release what this receipt paid for, and only that. One settlement decides it,
  // and the drafts it hands back are the same ones the ownership is then read off
  // — so a credential the comparison refused to release cannot lose its ownership
  // to a second, more optimistic answer. Anything the submission did not carry, or
  // that has changed since it went out, is newer work than the receipt and stays.
  //
  // It is done inside the updater because `current` is the only authoritative
  // draft state, and everything it writes is derived from the state it returns
  // rather than added to what was there before — so an updater React chooses to
  // run twice reaches the same answer both times.
  const consume = (submission: Submission) => {
    setDrafts((current) => {
      const settled = settle(submission, { ...onScreen.current, drafts: current });
      // A credential counts as work only while something is on screen to lose, so
      // one emptied by this receipt — or by the person — stops counting either way.
      for (const type of CREDENTIALS) if (!unsaved(settled.drafts[type])) touched.current.delete(type);
      if (settled.baseUrl) touched.current.delete('base_url');
      if (settled.method) touched.current.delete('method');
      if (settled.credentialType) touched.current.delete('credential_type');
      return settled.drafts;
    });
  };
  const confirm = async (submission: Submission, receiptError = '') => {
    pendingConfirmation.current = submission;
    if (!await observe({ expected: submission, receiptError })) return false;
    pendingConfirmation.current = null;
    consume(submission);
    await onConnectedRef.current?.();
    return true;
  };
  // Signing in settles the account, not the key someone was part-way through
  // typing, and not an address they were part-way through changing either.
  const OAUTH: Submission = { method: 'oauth', baseUrl: null, credential: null, sent: null };
  const confirmOAuth = async () => {
    if (!await confirm(OAUTH)) throw new Error(t('onboarding.connection.applyPending'));
  };
  const observeOAuthFailure = async (receiptError: string) => {
    pendingConfirmation.current = OAUTH;
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
  // What is stored is one credential of one type. A mask only answers for the
  // type it was saved under, so selecting Auth Token while an API key is stored
  // shows an empty field to fill rather than a mask that means something else —
  // and `canSave` then requires a real token instead of accepting "keep".
  const storedCredential: Credential = native && 'credential_type' in native ? native.credential_type || 'api_key' : 'api_key';
  const hasKey = authReadable && (backend === 'opencode' ? Boolean(currentProvider?.api_key_masked)
    : Boolean(native?.has_api_key) && (backend !== 'claude' || storedCredential === credential));
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
    // What this write carries, decided here rather than when the receipt lands: by
    // then the visible type may be the other one, and the draft or the address may
    // be newer work than the one that went out. `sent` follows the payload exactly
    // — an omitted key is no credential work, not empty credential work.
    const payload = { auth_mode: 'api_key' as const, api_key: key.trim() || undefined, base_url: baseUrl.trim() || null };
    const submitted: Submission = { method: 'api_key', baseUrl, credential, sent: payload.api_key ? drafts[credential] : null };
    try {
      const result = backend === 'claude' ? await api.saveClaudeAuth({ ...payload, credential_type: credential })
        : backend === 'codex' ? await api.saveCodexAuth(payload)
        : await api.setOpencodeProviderAuth(provider!.id, payload.api_key, payload.base_url);
      if (!lifetime.current.mounted) return;
      if (!result.ok) throw new Error(result.message || t('onboarding.connection.saveFailed'));
      if ('notices' in result) surfaceBackendNotices(result.notices, showToast, t);
      if ('partial' in result && result.partial) showToast(result.detail || result.warning || t('onboarding.connection.partial'), 'warning');
      await confirm(submitted, result.restart?.ok === false ? result.restart.message || t('onboarding.connection.applyFailed') : '');
    } catch (err) {
      if (lifetime.current.mounted) {
        pendingConfirmation.current = submitted;
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
  // Settings keeps its old content-sized loading row. The dialog does not: its
  // frame is anchored, so loading has to arrive inside the same three regions as
  // every other state or the tabs and the footer would appear late and move.
  if (loading && !compact) return <div className="connection-loading" role="status"><LoaderCircle className="animate-spin" size={16} />{t('common.loading')}</div>;
  // Exactly three children, always in this order: the method row, the scrolling
  // body, and the action footer. `connection.css` anchors the first and last to
  // their own rows of the dialog grid, which is what keeps the heading, tabs and
  // footer still while the body changes underneath them.
  return <div className="backend-connection-form">
    {!(compact && active && backend !== 'codex') && (backend !== 'opencode' || (!compact && provider?.oauth_available)) && <MethodRadio
      value={method} onChange={(value) => { if (!busy && !loading) { pendingConfirmation.current = null; touched.current.add('method'); setMethod(value); setError(''); setConnected(false); } }} disabled={busy || loading}
      ariaLabel={t('onboarding.connection.method')}
      options={[{ id: 'oauth', label: backend === 'claude' ? t('onboarding.connection.claudeLogin') : backend === 'codex' ? t('onboarding.connection.codexSignIn') : t('onboarding.connection.subscription') },
        { id: 'api_key', label: backend === 'claude' ? t('onboarding.connection.claudeCredentials') : backend === 'codex' ? t('onboarding.connection.openaiKey') : apiKeyLabel }]} />}
    <div className="connection-body">
    {loading && <div className="connection-loading" role="status"><LoaderCircle className="animate-spin" size={16} />{t('common.loading')}</div>}
    {/* The method's own instructions live here, not in the dialog heading: the
        heading and description are anchored and method-independent. */}
    {!loading && compact && <p className="connection-intro">{t(method === 'api_key'
      ? backend === 'claude' ? 'onboarding.connection.claudeKeyIntro' : backend === 'codex' ? 'onboarding.connection.codexKeyIntro' : 'onboarding.connection.providerKeyIntro'
      : backend === 'claude' ? active ? 'onboarding.connection.claudeCompleteIntro' : 'onboarding.connection.claudeIntro'
      : backend === 'codex' ? active ? 'onboarding.connection.codexCompleteIntro' : 'onboarding.connection.codexIntro'
      : 'onboarding.connection.providerIntro', { name: provider?.name })}</p>}
    {!loading && <>
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
      {backend === 'claude' && <div className="connection-field"><Label>{credentialLabel}</Label><MethodRadio value={credential} onChange={(value) => { touched.current.add('credential_type'); setCredential(value); setReveal(false); }} disabled={busy} ariaLabel={credentialLabel}
        options={[{ id: 'api_key', label: apiKeyLabel }, { id: 'auth_token', label: tokenLabel }]} /></div>}
      <div className="connection-field"><Label htmlFor={`${prefix}-connection-key`}>{secretLabel}</Label>
        {hasKey && !editing ? <div className="connection-secret"><KeyRound size={15} /><code>{mask}</code><Button variant="ghost" size="xs" disabled={busy} onClick={() => patchDraft({ editing: true, value: '' })}><Pencil size={14} />{t('settings.backends.replaceApiKey')}</Button></div>
          : <div className="connection-secret"><KeyRound size={15} /><Input id={`${prefix}-connection-key`} type={reveal ? 'text' : 'password'} value={key} onChange={(event) => patchDraft({ value: event.target.value })} disabled={busy} autoComplete="off" spellCheck={false} placeholder={credential === 'auth_token' ? t('settings.backends.claudeAuthTokenPlaceholder') : backend === 'claude' ? 'sk-ant-…' : 'sk-…'} />
            <Button variant="ghost" size="icon" aria-label={t(reveal ? 'onboarding.connection.hideKey' : 'onboarding.connection.showKey')} onClick={() => setReveal(!reveal)}>{reveal ? <EyeOff size={15} /> : <Eye size={15} />}</Button></div>}
        <p>{backend === 'opencode' ? t('onboarding.connection.providerCredentialHint', { name: provider?.name }) : t(backend === 'claude' && credential === 'auth_token' ? 'onboarding.connection.tokenHint' : 'onboarding.connection.keyHint')}</p>
        {editing && hasKey && <Button variant="link" size="xs" onClick={() => patchDraft({ editing: false, value: '' })}>{t('common.cancel')}</Button>}
      </div>
      <div className="connection-field"><Label htmlFor={`${prefix}-connection-url`}>{t('onboarding.connection.baseUrl')}</Label>
        <Input id={`${prefix}-connection-url`} type="url" value={baseUrl} onChange={(event) => { touched.current.add('base_url'); setBaseUrl(event.target.value); }} disabled={busy} autoComplete="off" placeholder={backend === 'claude' ? 'https://api.anthropic.com' : backend === 'codex' ? 'https://api.openai.com/v1' : t('onboarding.connection.providerDefault')} />
        <p>{t(backend === 'claude' ? 'onboarding.connection.claudeUrlHint' : 'onboarding.connection.urlHint')}</p>
        {!urlValid && <p className="connection-error">{t('onboarding.connection.invalidUrl')}</p>}
      </div>
    </>}
    </>}
    </div>
    {(compact || method === 'api_key') && <div className="connection-actions">
      {compact && <Button variant="secondary" onClick={onCancel}>{t('common.cancel')}</Button>}
      {!compact && hasKey && method === 'api_key' && <Button variant="ghost" disabled={busy} onClick={() => void remove(true)}>{t('settings.backends.claudeApiKeyRemove')}</Button>}
      {method === 'api_key' && <Button variant="brand" disabled={!canSave} onClick={() => void save()}>{saving ? t(compact ? 'onboarding.connection.connecting' : 'common.saving') : t(compact ? 'onboarding.connection.saveConnect' : 'common.save')}</Button>}
      {compact && needsCode && <Button variant="brand" disabled={!oauth.code.trim() || oauth.submitting} onClick={() => void oauth.submitCallback()}>{oauth.submitting ? t('onboarding.connection.connecting') : t('onboarding.connection.finishConnect')}</Button>}
    </div>}
  </div>;
}
