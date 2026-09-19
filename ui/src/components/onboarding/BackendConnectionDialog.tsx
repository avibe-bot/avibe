// The setup step's connection dialog.
//
// Its frame is fixed per assistant (`connection.css`): heading, description, the
// method row, a scrolling middle and the action footer each own a row of a grid
// whose height does not depend on what is inside it. That is the whole point —
// the reported defect was that switching Subscription/API Key re-measured the
// dialog and moved its top edge, because a centered box with content height
// moves by half of every content change. So the heading here names the assistant
// and nothing else, the description says what a connection is for regardless of
// method, and everything method-specific — including each method's own
// instructions — belongs to the scrolling middle, which is the only part allowed
// to change size.
//
// For OpenCode the dialog also has to choose a provider first. That step reuses
// the same five rows: the method row holds the search field, the middle holds
// the list, the footer holds Cancel. Picking a provider replaces the search with
// the chosen row and hands the middle to the form, and the frame never moves.
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ChevronDown, ChevronRight, Search } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { useApi, type OpencodeProvider } from '@/context/ApiContext';
import { errorMessage } from '@/lib/errorMessage';
import { getBackendUiMeta } from '@/lib/agentBackends';
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '../ui/dialog';
import { Button } from '../ui/button';
import { Input } from '../ui/input';
import { BackendIcon } from '../visual';
import { VendorGlyph } from '../settings/models/vendorGlyph';
import { providerLabel, providerVendorId, setupPrimaryRank } from '../settings/providers/providerIdentity';
import { BackendConnectionForm } from '../settings/providers/BackendConnectionForm';
import type { BackendId } from '../settings/shared/useBackendRuntime';

/** One provider choice: its brand mark, the name a person recognises, and the id
 *  OpenCode files it under — shown because that id is what the rest of OpenCode,
 *  its config file and its model identifiers use. */
function ProviderRow({ entry, onPick }: { entry: OpencodeProvider; onPick: () => void }) {
  const { t } = useTranslation();
  const label = providerLabel(entry.id, entry.name);
  return (
    <button type="button" className="connection-provider focus-visible:outline-2 focus-visible:outline-ring" onClick={onPick}>
      <span className="connection-provider-mark"><VendorGlyph vendor={providerVendorId(entry.id)} /></span>
      <span className="connection-provider-text">
        <strong>{label}</strong>
        <small>{label.toLowerCase() === entry.id.toLowerCase() ? entry.description || entry.id : entry.id}</small>
      </span>
      {entry.configured && <span className="connection-provider-configured">{t('onboarding.connection.providerConfigured')}</span>}
      <ChevronRight size={16} />
    </button>
  );
}

export function BackendConnectionDialog({ backend, method, onClose, onConnected, onWriteState }: {
  backend: BackendId;
  method: 'oauth' | 'api_key';
  onClose: () => void;
  onConnected: () => Promise<void>;
  onWriteState: (pending: boolean) => void;
}) {
  const api = useApi(); const { t } = useTranslation();
  const [providers, setProviders] = useState<OpencodeProvider[]>([]);
  const [provider, setProvider] = useState<OpencodeProvider>();
  const [loading, setLoading] = useState(backend === 'opencode');
  const [error, setError] = useState('');
  const [query, setQuery] = useState('');
  const [showMore, setShowMore] = useState(false);
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

  const picking = backend === 'opencode' && !provider;
  const name = getBackendUiMeta(backend).label;
  // Method-independent, by contract. Whatever the person is about to do, this
  // dialog is for this assistant and says so the same way in every state.
  const title = t('onboarding.connection.dialogTitle', { name });
  const intro = t(backend === 'claude' ? 'onboarding.connection.claudeDialogIntro'
    : backend === 'codex' ? 'onboarding.connection.codexDialogIntro'
    : 'onboarding.connection.opencodeDialogIntro');

  const { shortlist, more } = useMemo(() => {
    const term = query.trim().toLowerCase();
    // Subscriptions follow OpenCode's real OAuth capability; the eight brands
    // below are an API-key presentation order, not an authentication catalog.
    const eligible = providers.filter((entry) => (method === 'api_key' ? !entry.local : entry.oauth_available));
    const matching = eligible.filter((entry) => !term
      || `${providerLabel(entry.id, entry.name)} ${entry.name} ${entry.id} ${entry.models.join(' ')}`.toLowerCase().includes(term));
    const rank = (entry: OpencodeProvider) => method === 'api_key' ? setupPrimaryRank(entry.id) : null;
    const ordered = [...matching].sort((left, right) => {
      const [a, b] = [rank(left), rank(right)];
      if (a !== null && b !== null) return a - b;
      if (a !== null) return -1;
      if (b !== null) return 1;
      // Providers already holding credentials stay reachable without More.
      if (left.configured !== right.configured) return left.configured ? -1 : 1;
      return providerLabel(left.id, left.name).localeCompare(providerLabel(right.id, right.name));
    });
    // A search has to reach every provider, so it collapses nothing. Neither
    // does the subscription list: it is already only what can sign in, and
    // there is no shortlist to be the remainder of.
    if (term || method !== 'api_key') return { shortlist: ordered, more: [] as OpencodeProvider[] };
    const promoted = (entry: OpencodeProvider) => rank(entry) !== null || entry.configured;
    return { shortlist: ordered.filter(promoted), more: ordered.filter((entry) => !promoted(entry)) };
  }, [providers, method, query]);

  return <Dialog open onOpenChange={(open) => { if (!open) onClose(); }}>
    <DialogContent className="connection-dialog" data-backend={backend} closeLabel={t('common.close')}>
      <div className="connection-heading"><div className="connection-logo"><BackendIcon backend={backend} variant="brand" brandFit="mark" size={28} aria-hidden="true" /></div>
        <div><DialogTitle>{title}</DialogTitle><p>{t(`onboarding.setup.${backend}Description`)}</p></div>
      </div>
      <DialogDescription className="connection-intro connection-description">{intro}</DialogDescription>
      {picking ? (
        <div className="connection-controls connection-search">
          <Search size={15} aria-hidden="true" />
          <Input aria-label={t('settings.backends.opencodeSearchPlaceholder')} placeholder={t('settings.backends.opencodeSearchPlaceholder')}
            value={query} onChange={(event) => setQuery(event.target.value)} />
        </div>
      ) : provider ? (
        <div className="connection-controls connection-chosen">
          <span className="connection-provider-mark"><VendorGlyph vendor={providerVendorId(provider.id)} /></span>
          <span className="connection-provider-text"><strong>{providerLabel(provider.id, provider.name)}</strong><small>{t('onboarding.connection.providerDescription')}</small></span>
          <Button variant="ghost" size="xs" disabled={busy} onClick={() => setProvider(undefined)}>{t('onboarding.connection.changeProvider')}</Button>
        </div>
      ) : null}
      {picking ? <>
        <div className="connection-body connection-pick">
          {loading && <p role="status">{t('common.loading')}</p>}
          {error && <div className="connection-error" role="alert">{error}<Button variant="secondary" onClick={() => void load()}>{t('common.retry')}</Button></div>}
          <p className="connection-intro">{t(method === 'api_key' ? 'onboarding.connection.pickKey' : 'onboarding.connection.pickSubscription')}</p>
          <div className="connection-provider-list">
            {shortlist.map((entry) => <ProviderRow key={entry.id} entry={entry} onPick={() => setProvider(entry)} />)}
            {more.length > 0 && <>
              <Button type="button" variant="secondary" className="connection-more" aria-expanded={showMore} onClick={() => setShowMore((open) => !open)}>
                {showMore ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
                {t('onboarding.connection.moreProviders')} ({more.length})
              </Button>
              {showMore && more.map((entry) => <ProviderRow key={entry.id} entry={entry} onPick={() => setProvider(entry)} />)}
            </>}
          </div>
          {!loading && !error && shortlist.length === 0 && more.length === 0 && <p className="connection-intro">{t('onboarding.connection.noProviders')}</p>}
          <p className="connection-intro">{t('onboarding.connection.catalogHint')}</p>
        </div>
        <div className="connection-actions"><Button variant="secondary" onClick={onClose}>{t('common.cancel')}</Button></div>
      </> : (
        <BackendConnectionForm key={provider?.id || backend} backend={backend} provider={provider} initialMethod={method} compact
          onBusyChange={setBusy} onWriteState={onWriteState} onCancel={onClose}
          onConnected={async () => { await onConnected(); if (mounted.current) onClose(); }} />
      )}
    </DialogContent>
  </Dialog>;
}
