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
import { useViewportHeightVar } from '@/lib/useViewportHeightVar';
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '../ui/dialog';
import { Button } from '../ui/button';
import { Input } from '../ui/input';
import { BackendIcon } from '../visual';
import { VendorGlyph } from '../settings/models/vendorGlyph';
import { CUSTOM_VENDOR } from '../settings/models/apiKeyVendors';
import { providerBrandLabel, providerLabel, providerVendorId, setupPrimaryRank, SETUP_PRIMARY_VENDORS } from '../settings/providers/providerIdentity';
import { BackendConnectionForm } from '../settings/providers/BackendConnectionForm';
import type { BackendId } from '../settings/shared/useBackendRuntime';

/** One provider choice: its brand mark, the name a person recognises, and the id
 *  OpenCode files it under — shown because that id is what the rest of OpenCode,
 *  its config file and its model identifiers use. The caller supplies the name,
 *  because what a row should be called depends on where it stands: a prioritised
 *  brand slot has to say the brand, and a row under More has to say which of
 *  that brand's entries it is. */
type PickerRow = { entry: OpencodeProvider; label: string };

// A custom provider's id is whatever its author typed into Settings, and OpenCode
// only reserves the ids it ships itself — so someone's own relay can be filed
// under `dashscope`, or under a brand's own name. That id is not evidence of a
// brand, and the catalog alias must not read it as one: a relay that inherited
// Qwen's slot, name and mark would be the row a person picks to connect Qwen,
// and the credential would be written to the relay. So identity for these rows
// comes from what they were configured as, and they are never brand candidates.

/** The mark a row draws. 自定义 has never had a logo — it takes the field's own
 *  subject, an address you supply — and that is exactly what a custom provider
 *  is, so it draws the same thing rather than borrowing a brand's. */
const providerMark = (entry: OpencodeProvider) => entry.custom ? CUSTOM_VENDOR : providerVendorId(entry.id);

/** What a row is called where it is not standing in a brand slot. `providerLabel`
 *  would read a relay filed under `dashscope` as "Qwen" the moment its author
 *  left the name blank, because the server echoes the id back as the name. */
const providerTitle = (entry: OpencodeProvider) => entry.custom
  ? entry.name?.trim() || entry.id
  : providerLabel(entry.id, entry.name);

function ProviderRow({ entry, label, onPick }: PickerRow & { onPick: () => void }) {
  const { t } = useTranslation();
  return (
    <button type="button" className="connection-provider focus-visible:outline-2 focus-visible:outline-ring" onClick={onPick}>
      <span className="connection-provider-mark"><VendorGlyph vendor={providerMark(entry)} /></span>
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
  // The frame is sized to the USABLE viewport, which on a phone is what the soft
  // keyboard leaves. `dvh` cannot see that on iOS — only the visual viewport
  // shrinks — and this route is the setup wizard, not the app shell that
  // normally keeps `--app-vvh` current, so the dialog keeps it current itself.
  useViewportHeightVar();
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
    // Both names are searchable, so "qwen" finds the row OpenCode calls
    // "Alibaba (China)" and "alibaba" still finds it too. A custom provider is
    // searched by what it is — its own name, its id, its models — and not by the
    // brand its id happens to spell.
    const haystack = (entry: OpencodeProvider) => [providerTitle(entry), entry.name, entry.id, entry.models.join(' '),
      entry.custom ? '' : `${providerBrandLabel(entry.id, entry.name)} ${providerLabel(entry.id, entry.name)}`].join(' ').toLowerCase();
    const matching = eligible.filter((entry) => !term || haystack(entry).includes(term));
    const rank = (entry: OpencodeProvider) => method === 'api_key' && !entry.custom ? setupPrimaryRank(entry.id) : null;
    const row = (entry: OpencodeProvider, brand = false): PickerRow => ({ entry,
      label: brand ? providerBrandLabel(entry.id, entry.name) : providerTitle(entry) });
    // No two rows on screen may read the same, or there is nothing to act on
    // the difference with. The first row to use a title keeps it — and a brand
    // slot is always first — so the eight always read as their brand, and a
    // second entry of that brand falls back to the id it is filed under, which
    // is exactly what tells one of them from another.
    const distinct = (rows: PickerRow[]) => {
      const used = new Set<string>();
      return rows.map((entry) => {
        if (!used.has(entry.label)) { used.add(entry.label); return entry; }
        return { ...entry, label: entry.entry.id };
      });
    };
    // A search has to reach every provider, so it collapses nothing. Neither
    // does the subscription list: it is already only what can sign in, and
    // there is no shortlist to be the remainder of. Both keep the brand order
    // in front, then whatever already holds credentials.
    if (term || method !== 'api_key') return {
      shortlist: distinct([...matching].sort((left, right) => {
        const [a, b] = [rank(left), rank(right)];
        if (a !== null && b !== null) return a - b;
        if (a !== null) return -1;
        if (b !== null) return 1;
        if (left.configured !== right.configured) return left.configured ? -1 : 1;
        return providerTitle(left).localeCompare(providerTitle(right));
      }).map((entry) => row(entry))),
      more: [] as PickerRow[],
    };
    const byVendor = new Map<string, OpencodeProvider[]>();
    for (const entry of matching) {
      if (entry.custom) continue;
      const vendor = providerVendorId(entry.id);
      const group = byVendor.get(vendor);
      if (group) group.push(entry); else byVendor.set(vendor, [entry]);
    }
    // Exactly one row per prioritised brand, in the approved order, and only
    // for the brands this runtime actually offers — an absent brand leaves no
    // slot behind rather than a row that cannot be connected. Which entry
    // represents a brand is decided rather than incidental: one already holding
    // credentials, else the id that IS the brand, else the lowest id. So two
    // aliases of one brand can never both take the shortlist, and two reads of
    // the same catalog can never trade them. Only entries OpenCode ships are
    // candidates: a brand with nothing but a custom relay behind it gets no row
    // rather than a row that would name someone's endpoint after it.
    const chosen = SETUP_PRIMARY_VENDORS.flatMap((vendor) => {
      const group = byVendor.get(vendor);
      if (!group?.length) return [];
      const exact = (entry: OpencodeProvider) => entry.id.trim().toLowerCase() === vendor;
      return [[...group].sort((left, right) => {
        if (left.configured !== right.configured) return left.configured ? -1 : 1;
        if (exact(left) !== exact(right)) return exact(left) ? -1 : 1;
        return left.id.localeCompare(right.id);
      })[0]];
    });
    // Holding credentials is a reason to be easy to find, not a reason to take
    // a brand's slot — so a configured provider opens More instead of growing
    // the eight. Everything the shortlist did not take is here, which is what
    // keeps every native provider reachable without a search.
    const taken = new Set(chosen.map((entry) => entry.id));
    const rows = distinct([...chosen.map((entry) => row(entry, true)),
      ...matching.filter((entry) => !taken.has(entry.id)).map((entry) => row(entry))]);
    return { shortlist: rows.slice(0, chosen.length), more: rows.slice(chosen.length).sort((left, right) => {
      if (left.entry.configured !== right.entry.configured) return left.entry.configured ? -1 : 1;
      return left.label.localeCompare(right.label);
    }) };
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
          <span className="connection-provider-mark"><VendorGlyph vendor={providerMark(provider)} /></span>
          <span className="connection-provider-text"><strong>{providerTitle(provider)}</strong><small>{t('onboarding.connection.providerDescription')}</small></span>
          <Button variant="ghost" size="xs" disabled={busy} onClick={() => setProvider(undefined)}>{t('onboarding.connection.changeProvider')}</Button>
        </div>
      ) : null}
      {picking ? <>
        <div className="connection-body connection-pick">
          {loading && <p role="status">{t('common.loading')}</p>}
          {error && <div className="connection-error" role="alert">{error}<Button variant="secondary" onClick={() => void load()}>{t('common.retry')}</Button></div>}
          <p className="connection-intro">{t(method === 'api_key' ? 'onboarding.connection.pickKey' : 'onboarding.connection.pickSubscription')}</p>
          <div className="connection-provider-list">
            {shortlist.map(({ entry, label }) => <ProviderRow key={entry.id} entry={entry} label={label} onPick={() => setProvider(entry)} />)}
            {more.length > 0 && <>
              <Button type="button" variant="secondary" className="connection-more" aria-expanded={showMore} onClick={() => setShowMore((open) => !open)}>
                {showMore ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
                {t('onboarding.connection.moreProviders')} ({more.length})
              </Button>
              {showMore && more.map(({ entry, label }) => <ProviderRow key={entry.id} entry={entry} label={label} onPick={() => setProvider(entry)} />)}
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
