import { useCallback, useEffect, useRef, useState } from 'react';
import { ChevronRight, Search } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { useApi, type OpencodeProvider } from '@/context/ApiContext';
import { errorMessage } from '@/lib/errorMessage';
import { getBackendUiMeta } from '@/lib/agentBackends';
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '../ui/dialog';
import { Button } from '../ui/button';
import { Input } from '../ui/input';
import { BackendIcon } from '../visual';
import { BackendConnectionForm, type ConnectionHeading } from '../settings/providers/BackendConnectionForm';
import type { BackendId } from '../settings/shared/useBackendRuntime';

export function BackendConnectionDialog({ backend, method, onClose, onConnected, onWriteState }: {
  backend: BackendId;
  method: 'oauth' | 'api_key';
  onClose: () => void;
  onConnected: () => Promise<void>;
  onWriteState: (pending: boolean) => void;
}) {
  const api = useApi(); const { t } = useTranslation();
  const [heading, setHeading] = useState<ConnectionHeading>({ method, active: false, credential: 'api_key' });
  const [providers, setProviders] = useState<OpencodeProvider[]>([]);
  const [provider, setProvider] = useState<OpencodeProvider>();
  const [loading, setLoading] = useState(backend === 'opencode');
  const [error, setError] = useState('');
  const [query, setQuery] = useState('');
  const [busy, setBusy] = useState(false);
  const readToken = useRef(0);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const load = useCallback(async () => {
    const token = ++readToken.current;
    setLoading(true); setError('');
    try {
      const result = await api.getOpencodeProviders();
      if (token !== readToken.current) return;
      if (!result.ok) throw new Error(result.message || t('onboarding.connection.readFailed'));
      setProviders(result.providers || []);
    } catch (err) { if (token === readToken.current) setError(errorMessage(err) || t('onboarding.connection.readFailed')); }
    finally { if (token === readToken.current) setLoading(false); }
  }, [api, t]);
  useEffect(() => { if (backend === 'opencode') void load(); return () => { readToken.current++; }; }, [backend, load]);
  const isKey = heading.method === 'api_key';
  const title = backend === 'claude' ? t(heading.active ? 'onboarding.connection.claudeComplete' : isKey ? heading.credential === 'auth_token' ? 'onboarding.connection.claudeTokenTitle' : 'onboarding.connection.claudeKeyTitle' : 'onboarding.connection.claudeTitle')
    : backend === 'codex' ? t(isKey ? 'onboarding.connection.codexKeyTitle' : heading.active ? 'onboarding.connection.codexComplete' : 'onboarding.connection.codexTitle')
    : provider ? t(isKey ? 'onboarding.connection.providerKeyTitle' : 'onboarding.connection.providerTitle', { name: provider.name })
      : t(isKey ? 'onboarding.connection.opencodeKeyTitle' : 'onboarding.connection.opencodeTitle');
  const intro = backend === 'claude' ? t(heading.active ? 'onboarding.connection.claudeCompleteIntro' : isKey ? 'onboarding.connection.claudeKeyIntro' : 'onboarding.connection.claudeIntro')
    : backend === 'codex' ? t(isKey ? 'onboarding.connection.codexKeyIntro' : heading.active ? 'onboarding.connection.codexCompleteIntro' : 'onboarding.connection.codexIntro')
      : provider ? t('onboarding.connection.providerIntro', { name: provider.name }) : t(isKey ? 'onboarding.connection.pickKey' : 'onboarding.connection.pickSubscription');
  const filtered = providers.filter((entry) => (method === 'api_key' ? !entry.local : entry.oauth_available)
    && `${entry.name} ${entry.id}`.toLowerCase().includes(query.toLowerCase()));
  return <Dialog open onOpenChange={(open) => { if (!open) onClose(); }}><DialogContent className="connection-dialog" closeLabel={t('common.close')}>
    <div className="connection-heading"><div className="connection-logo"><BackendIcon backend={backend} variant="brand" brandFit="mark" size={28} aria-hidden="true" /></div>
      <div><DialogTitle>{title}</DialogTitle><p>{t('onboarding.connection.target', { name: getBackendUiMeta(backend).label })}</p></div>
    </div>
    <DialogDescription className="connection-intro">{intro}</DialogDescription>
    {backend === 'opencode' && !provider ? <>
      {loading && <p role="status">{t('common.loading')}</p>}
      {error && <div className="connection-error" role="alert">{error}<Button variant="secondary" onClick={() => void load()}>{t('common.retry')}</Button></div>}
      {method === 'api_key' && <div className="relative"><Search size={15} className="absolute left-3 top-3 text-muted" /><Input className="h-[38px] pl-9" aria-label={t('settings.backends.opencodeSearchPlaceholder')} placeholder={t('settings.backends.opencodeSearchPlaceholder')} value={query} onChange={(event) => setQuery(event.target.value)} /></div>}
      <div className="connection-provider-list">{filtered.map((entry) => <button type="button" key={entry.id} className="connection-provider focus-visible:outline-2 focus-visible:outline-ring" onClick={() => setProvider(entry)}>
        <span><strong>{entry.name}</strong><small>{entry.description || entry.id}</small></span><ChevronRight size={16} /></button>)}</div>
      {!loading && !error && filtered.length === 0 && <p className="connection-intro">{t('onboarding.connection.noProviders')}</p>}
      <p className="connection-intro">{t('onboarding.connection.catalogHint')}</p>
      <div className="connection-actions"><Button variant="secondary" onClick={onClose}>{t('common.cancel')}</Button></div>
    </> : <>
      {provider && <div className="connection-provider"><span><strong>{provider.name}</strong><small>{t('onboarding.connection.providerDescription')}</small></span><Button variant="ghost" size="xs" disabled={busy} onClick={() => setProvider(undefined)}>{t('onboarding.connection.changeProvider')}</Button></div>}
      <BackendConnectionForm key={provider?.id || backend} backend={backend} provider={provider} initialMethod={method} compact
        onHeading={setHeading} onBusyChange={setBusy} onWriteState={onWriteState} onCancel={onClose}
        onConnected={async () => { await onConnected(); if (mounted.current) onClose(); }} />
    </>}
  </DialogContent></Dialog>;
}
