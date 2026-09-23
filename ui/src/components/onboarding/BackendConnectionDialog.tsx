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
 *  its config file and its model identifiers use. The name is not the row's to
 *  compute: it is handed out once for the whole list by `allocate` below, because
 *  what a row may be called depends on what every other row is already called. */
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

/**
 * The name a row is allowed to show, given the names already handed out.
 *
 * The rule is that no two rows may read alike: a person choosing between them has
 * nothing else to act on, and the two are different endpoints that will be sent
 * different credentials. What a row asks for is `natural` — a brand's own name in
 * a brand slot, a custom provider's configured name anywhere else — and asking
 * first wins, which is why brand slots are allocated first.
 *
 * A row that loses falls back to the id OpenCode files it under, which is unique
 * within one catalog and is the thing that actually tells two entries of a brand
 * apart. But that id may itself already be on screen, because a custom provider's
 * *name* is whatever its author typed and may spell another row's id exactly. So
 * every candidate is checked before it is taken, including the fallback and the
 * composition after it — a candidate that is merely emitted rather than reserved
 * is how two rows come to read the same in the first place.
 *
 * The widening at the end terminates for the same reason the ladder is needed:
 * each attempt produces a string no earlier attempt did, and only finitely many
 * names have been handed out, so some attempt is free.
 */
const allocate = (natural: string, id: string, used: Set<string>): string => {
  let label = natural;
  for (let attempt = 0; used.has(label.toLowerCase()); attempt += 1) {
    label = attempt === 0 ? id : attempt === 1 ? `${natural} (${id})` : `${natural} (${id} ${attempt})`;
  }
  used.add(label.toLowerCase());
  return label;
};

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

// Which of the two layouts in `connection.css` the room can actually hold.
//
// Five fixed rows work until the room is smaller than what those rows cost plus
// something worth putting between them. Stopping at the rows alone would only
// move the defect: a middle of one pixel is as unusable as a middle of none, and
// a scroll container with no height scrolls nothing, so the fields inside it
// become unreachable rather than merely small. The floor is therefore the four
// fixed rows, the gaps between them, the padding on both sides — and one more
// control row, the height this dialog already gives every control it owns, so
// that what is left is a usable amount of room rather than a sliver.
//
// One control row is not one field, and this floor does not claim to be: a field
// is a label, an input taller than a control row, and a hint under it, so the
// reserve is deliberately the smaller, load-bearing half of that. What it buys is
// that the middle always has room worth scrolling, not that any one field fits.
//
// CSS cannot ask the question: media queries read the layout viewport, which is
// the very measurement a soft keyboard leaves wrong. So it is asked here, from
// the metrics the stylesheet resolved for the band actually in force, and the
// answer is written back as an attribute. Every term is a plain length in the
// stylesheet, so this needs nothing of the browser beyond reading them.
//
// Both sides of the comparison are independent of what the body holds: the
// metrics belong to the band, and the height belongs to the room. A method
// switch or an error cannot flip the layout — the same guarantee the fixed frame
// exists to give.
const ROOM_FLOOR: ReadonlyArray<readonly [string, number]> = [
  ['--connection-head', 1], ['--connection-desc', 1], ['--connection-footer', 1],
  ['--connection-gap', 4], ['--connection-pad', 2],
  // Twice: the method row, and one more for the middle to show a whole control in.
  ['--connection-controls', 2],
];

function useRoomFloor() {
  const stop = useRef<(() => void) | undefined>(undefined);
  // A callback ref, not an effect: the frame is rendered through a portal that
  // arrives a commit later than this component, so an effect would run while the
  // ref was still empty and never measure anything. This runs exactly when the
  // frame attaches, and again with `null` when it goes away.
  return useCallback((node: HTMLDivElement | null) => {
    stop.current?.();
    stop.current = undefined;
    if (!node || typeof ResizeObserver === 'undefined') return;
    const measure = () => {
      const style = getComputedStyle(node);
      let floor = 0;
      for (const [name, count] of ROOM_FLOOR) floor += (parseFloat(style.getPropertyValue(name)) || 0) * count;
      // `clientHeight` is the padding box, which is what the rows, the gaps and
      // the padding have to fit inside; the frame's border sits outside both, so
      // neither side counts it.
      const tight = floor > node.clientHeight;
      if (tight === (node.dataset.room === 'tight')) return;
      if (tight) node.dataset.room = 'tight'; else delete node.dataset.room;
    };
    measure();
    // The observer catches every room the frame is resized into — a rotation, a
    // window resize, the band changing underneath it. The visual viewport catches
    // the one thing that resizes nothing: a soft keyboard, which leaves the layout
    // viewport exactly as it was.
    //
    // Neither can feed back. The attribute changes which row absorbs the middle,
    // not the frame's box, so the observer is not re-armed by its own answer, and
    // a repeat answer returns above without touching the DOM at all.
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    const vv = window.visualViewport;
    vv?.addEventListener('resize', measure);
    vv?.addEventListener('scroll', measure);
    stop.current = () => {
      observer.disconnect();
      vv?.removeEventListener('resize', measure);
      vv?.removeEventListener('scroll', measure);
    };
  }, []);
}

export function BackendConnectionDialog({ backend, method, onClose, onConnected, onWriteState }: {
  backend: BackendId;
  method: 'oauth' | 'api_key';
  onClose: () => void;
  onConnected: () => Promise<void>;
  onWriteState: (pending: boolean) => void;
}) {
  const api = useApi(); const { t } = useTranslation();
  // The frame is bound and centred in the USABLE viewport — a rectangle, not a
  // height: on a phone that is what the soft keyboard leaves, and iOS also pans
  // it sideways to keep a focused field in sight. `dvh` and `vw` cannot see
  // either, and this route is the setup wizard rather than the app shell that
  // normally keeps those four variables current, so the dialog keeps them
  // current itself. It is not the only consumer, and the hook is written for
  // that: both may be mounted at once.
  useViewportHeightVar();
  const frame = useRoomFloor();
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

  // What every row is called, decided once for the whole catalog and before any
  // query narrows it.
  //
  // Two things have to hold at the same time, and only one place can hold both.
  // No two rows may read alike, and a row must read the same whether or not
  // somebody typed in the search box — otherwise searching for the name you can
  // see is the one way to make it disappear. So names are handed out here, over
  // every eligible provider, and the search below matches the name that was handed
  // out rather than recomputing one of its own.
  //
  // Order is the whole of the policy: a brand slot asks first, so the eight always
  // read as their brand; everything else asks in catalog order, which no sort or
  // query can disturb. What each row asks for is its own, and `allocate` decides
  // what it actually gets.
  const naming = useMemo(() => {
    // Subscriptions follow OpenCode's real OAuth capability; the eight brands
    // below are an API-key presentation order, not an authentication catalog.
    const eligible = providers.filter((entry) => (method === 'api_key' ? !entry.local : entry.oauth_available));
    const byVendor = new Map<string, OpencodeProvider[]>();
    for (const entry of eligible) {
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
    const chosen = method !== 'api_key' ? [] : SETUP_PRIMARY_VENDORS.flatMap((vendor) => {
      const group = byVendor.get(vendor);
      if (!group?.length) return [];
      const exact = (entry: OpencodeProvider) => entry.id.trim().toLowerCase() === vendor;
      return [[...group].sort((left, right) => {
        if (left.configured !== right.configured) return left.configured ? -1 : 1;
        if (exact(left) !== exact(right)) return exact(left) ? -1 : 1;
        return left.id.localeCompare(right.id);
      })[0]];
    });
    const slots = new Set(chosen.map((entry) => entry.id));
    const used = new Set<string>();
    const labels = new Map<string, string>();
    for (const entry of [...chosen, ...eligible.filter((entry) => !slots.has(entry.id))]) {
      labels.set(entry.id, allocate(slots.has(entry.id) ? providerBrandLabel(entry.id, entry.name) : providerTitle(entry), entry.id, used));
    }
    // A provider this allocation never saw — the one already chosen, when the
    // method has since changed under it — still has to be callable something.
    return { eligible, chosen, slots, label: (entry: OpencodeProvider) => labels.get(entry.id) ?? providerTitle(entry) };
  }, [providers, method]);

  const { shortlist, more } = useMemo(() => {
    const { eligible, chosen, slots, label } = naming;
    const term = query.trim().toLowerCase();
    // Every name this row answers to is searchable: the one it shows, the one
    // OpenCode gave it, its id, its models — so "qwen" finds the row the catalog
    // calls "Alibaba (China)" and "alibaba" still finds it too. A custom provider
    // is searched by what it is and not by the brand its id happens to spell.
    const haystack = (entry: OpencodeProvider) => [label(entry), providerTitle(entry), entry.name, entry.id, entry.models.join(' '),
      entry.custom ? '' : `${providerBrandLabel(entry.id, entry.name)} ${providerLabel(entry.id, entry.name)}`].join(' ').toLowerCase();
    const matching = eligible.filter((entry) => !term || haystack(entry).includes(term));
    const rank = (entry: OpencodeProvider) => method === 'api_key' && !entry.custom ? setupPrimaryRank(entry.id) : null;
    const row = (entry: OpencodeProvider): PickerRow => ({ entry, label: label(entry) });
    // A search has to reach every provider, so it collapses nothing. Neither
    // does the subscription list: it is already only what can sign in, and
    // there is no shortlist to be the remainder of. Both keep the brand order
    // in front, then whatever already holds credentials.
    if (term || method !== 'api_key') return {
      shortlist: [...matching].sort((left, right) => {
        const [a, b] = [rank(left), rank(right)];
        if (a !== null && b !== null) return a - b;
        if (a !== null) return -1;
        if (b !== null) return 1;
        if (left.configured !== right.configured) return left.configured ? -1 : 1;
        return label(left).localeCompare(label(right));
      }).map(row),
      more: [] as PickerRow[],
    };
    // Holding credentials is a reason to be easy to find, not a reason to take
    // a brand's slot — so a configured provider opens More instead of growing
    // the eight. Everything the shortlist did not take is here, which is what
    // keeps every native provider reachable without a search.
    return { shortlist: chosen.map(row), more: matching.filter((entry) => !slots.has(entry.id)).map(row).sort((left, right) => {
      if (left.entry.configured !== right.entry.configured) return left.entry.configured ? -1 : 1;
      return left.label.localeCompare(right.label);
    }) };
  }, [naming, method, query]);

  return <Dialog open onOpenChange={(open) => { if (!open) onClose(); }}>
    <DialogContent ref={frame} className="connection-dialog" data-backend={backend} closeLabel={t('common.close')}>
      <div className="connection-heading"><div className="connection-logo"><BackendIcon backend={backend} variant="brand" brandFit="mark" size={28} aria-hidden="true" /></div>
        <div><DialogTitle>{title}</DialogTitle><p>{t('onboarding.setup.forNamed', { name: getBackendUiMeta(backend).label })}</p></div>
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
          {/* The row that was picked, still reading exactly as it read when it was
              picked — one row, one name, everywhere it appears. */}
          <span className="connection-provider-text"><strong>{naming.label(provider)}</strong><small>{t('onboarding.connection.providerDescription')}</small></span>
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
