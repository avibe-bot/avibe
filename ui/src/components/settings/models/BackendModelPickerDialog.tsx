// Picking models to add to a backend's catalog.
//
// The whole dialog renders one server read. What a candidate is, which group it
// belongs to, which suppliers it has, and what its label and efforts should be
// are all answered by `GET .../models/candidates` (C4) — this file adds no rule
// of its own, because every rule it could add is one the server already applies
// to the write that follows. A client that re-derived them would disagree with
// the server the moment either changed.
//
// It writes nothing either. Picks go back into the catalog dialog's draft, so
// the list still settles through one `PUT .../models` with one baseline.
import * as React from 'react';
import { LoaderCircle, Plus, Search } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { cn } from '@/lib/utils';
import { EMPTY_PICKER_GROUPS, pickerGroups } from './backendCatalog';
import type { PickerGroups } from './backendCatalog';
import { modelsApi } from './modelsApi';
import type { AgentBackend, BackendModelCandidates, ModelCandidate } from './types';

type ReadState = 'loading' | 'ready' | 'error';

const NO_PICKS: ReadonlySet<string> = new Set();

/** Id, display name, and supplier name — the three things a row shows are the
 *  three things a query searches, so nothing on screen is unsearchable and
 *  nothing off it is matched. */
const matchesQuery = (candidate: ModelCandidate, needle: string): boolean =>
  needle === ''
  || candidate.id.toLowerCase().includes(needle)
  || (candidate.display_name ?? '').toLowerCase().includes(needle)
  || candidate.suppliers.some((supplier) => supplier.source_name.toLowerCase().includes(needle));

export const BackendModelPickerDialog: React.FC<{
  open: boolean;
  backend: AgentBackend;
  /** Ids the catalog draft already holds — saved rows and unsaved additions alike. */
  listedIds: ReadonlySet<string>;
  /** Ids to open with already picked, for a re-ask: the server refused an
   *  addition because its suppliers had moved (C1), so the same models come back
   *  selected with their current chips and one confirmation answers again. Keep
   *  the value stable while the dialog is open — it is applied on open. */
  seedPicked?: ReadonlySet<string>;
  onCancel: () => void;
  onAdd: (chosen: ModelCandidate[]) => void;
  /** Hand off to the custom-model editor, seeding the id with the query when the
   *  action the user pressed named it. */
  onCustom: (seedId: string) => void;
}> = ({ open, backend, listedIds, seedPicked, onCancel, onAdd, onCustom }) => {
  const { t } = useTranslation();
  const [readState, setReadState] = React.useState<ReadState>('loading');
  const [candidates, setCandidates] = React.useState<BackendModelCandidates | null>(null);
  const [query, setQuery] = React.useState('');
  const [picked, setPicked] = React.useState<ReadonlySet<string>>(new Set());
  const readAttempt = React.useRef(0);

  const load = React.useCallback(() => {
    const attempt = ++readAttempt.current;
    setReadState('loading');
    void (async () => {
      try {
        const read = await modelsApi.getAgentModelCandidates(backend);
        if (attempt !== readAttempt.current) return;
        setCandidates(read);
        setReadState('ready');
      } catch {
        if (attempt === readAttempt.current) setReadState('error');
      }
    })();
  }, [backend]);

  React.useEffect(() => {
    if (!open) return;
    setQuery('');
    setPicked(seedPicked ?? NO_PICKS);
    load();
    return () => { readAttempt.current += 1; };
  }, [load, open, seedPicked]);

  const groups = candidates ? pickerGroups(candidates, listedIds) : EMPTY_PICKER_GROUPS;
  const needle = query.trim().toLowerCase();
  const filtering = needle !== '';
  const shown: PickerGroups = {
    builtin: groups.builtin.filter((candidate) => matchesQuery(candidate, needle)),
    providers: groups.providers.filter((candidate) => matchesQuery(candidate, needle)),
    // With no query the list shows only what can be added, so the already-added
    // group exists exactly when a search could otherwise dead-end on it.
    listed: filtering ? groups.listed.filter((candidate) => matchesQuery(candidate, needle)) : [],
  };
  const nothingShown = shown.builtin.length === 0 && shown.providers.length === 0 && shown.listed.length === 0;

  const toggle = (modelId: string) => {
    setPicked((current) => {
      const next = new Set(current);
      if (!next.delete(modelId)) next.add(modelId);
      return next;
    });
  };

  /** Resolved against the unfiltered groups, in group order: a pick survives
   *  clearing or retyping the query, and additions land in the order they were
   *  offered rather than the order they were clicked. The count comes from the
   *  same list the confirmation sends, so a seeded id the current read no longer
   *  offers cannot be counted in a label that would then add fewer. */
  const chosen = [...groups.builtin, ...groups.providers].filter((candidate) => picked.has(candidate.id));

  const group = (key: 'builtin' | 'providers' | 'listed', label: string, rows: ModelCandidate[]) => (
    rows.length === 0 ? null : (
      <div key={key} role="group" aria-label={label} className="model-hub-picker-group">
        <div className="model-hub-picker-group-head">
          <span>{label}</span>
          <span>{rows.length}</span>
        </div>
        {rows.map((candidate) => (
          <PickerRow
            key={candidate.id}
            candidate={candidate}
            picked={picked.has(candidate.id)}
            listed={key === 'listed'}
            onToggle={() => toggle(candidate.id)}
          />
        ))}
      </div>
    )
  );

  return (
    <Dialog open={open} onOpenChange={(next) => { if (!next) onCancel(); }}>
      <DialogContent
        mobileSheetHeight="tall"
        closeLabel={t('settings.models.gateway.catalog.cancel') as string}
        className="model-hub-catalog-dialog model-hub-picker flex h-[min(642px,calc(100dvh-32px))] w-[min(680px,calc(100vw-32px))] max-w-[680px] flex-col gap-0 overflow-hidden rounded-[14px] border-border-strong bg-surface p-0 shadow-[var(--model-hub-dialog-shadow)] max-md:w-full max-md:max-w-none max-md:rounded-t-2xl max-md:p-0 max-md:pt-2"
      >
        <DialogHeader className="model-hub-catalog-head shrink-0 justify-center border-b border-border">
          <DialogTitle className="model-hub-catalog-title">
            {t('settings.models.gateway.picker.title', { backend: t(`settings.models.backends.${backend}`) })}
          </DialogTitle>
          <DialogDescription className="sr-only">{t('settings.models.gateway.picker.description')}</DialogDescription>
        </DialogHeader>

        <div className="model-hub-catalog-body flex min-h-0 flex-1 flex-col">
          <div className="model-hub-catalog-control model-hub-catalog-search flex min-w-0 shrink-0 items-center gap-2">
            <Search className="size-4 shrink-0 text-muted" aria-hidden="true" />
            <Input
              variant="bare"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder={t('settings.models.gateway.picker.search') as string}
              aria-label={t('settings.models.gateway.picker.search') as string}
              className="min-w-0 flex-1 text-[12.5px]"
              disabled={readState !== 'ready'}
            />
          </div>

          <div className="flex min-h-0 flex-1 flex-col overflow-y-auto">
            {readState === 'loading' ? (
              <div className="model-hub-picker-empty">
                <LoaderCircle className="size-5 animate-spin" aria-hidden="true" />
              </div>
            ) : readState === 'error' ? (
              <div className="model-hub-picker-empty">
                <p>{t('settings.models.gateway.picker.unavailable')}</p>
                <Button type="button" variant="outline" size="sm" onClick={load}>
                  {t('settings.models.gateway.retry')}
                </Button>
              </div>
            ) : nothingShown ? (
              <div className="model-hub-picker-empty">
                <p>{t('settings.models.gateway.picker.noMatch')}</p>
                {/* Only when the query is the thing the action would name. With an
                    empty one the footer already offers the same editor, and the
                    label would quote nothing. */}
                {filtering && (
                  <Button
                    type="button"
                    variant="ghost"
                    className="model-hub-catalog-control rounded-md px-2.5 text-[12.5px] font-semibold text-muted-foreground"
                    onClick={() => onCustom(query.trim())}
                  >
                    <Plus aria-hidden="true" />
                    {t('settings.models.gateway.picker.customFromQuery', { query: query.trim() })}
                  </Button>
                )}
              </div>
            ) : (
              <div className="model-hub-picker-list">
                {group('builtin', t('settings.models.gateway.picker.groupBuiltin', { backend: t(`settings.models.backends.${backend}`) }) as string, shown.builtin)}
                {group('providers', t('settings.models.gateway.picker.groupProviders') as string, shown.providers)}
                {group('listed', t('settings.models.gateway.picker.groupListed') as string, shown.listed)}
              </div>
            )}
          </div>
        </div>

        <DialogFooter className="model-hub-catalog-foot shrink-0 items-center border-t border-border sm:justify-between">
          <Button
            type="button"
            variant="ghost"
            className="model-hub-catalog-control shrink-0 rounded-md px-2.5 text-[12.5px] font-semibold text-muted-foreground max-sm:w-full"
            onClick={() => onCustom('')}
          >
            <Plus aria-hidden="true" />
            {t('settings.models.gateway.picker.custom')}
          </Button>
          <div className="flex w-full gap-2 sm:w-auto">
            <Button
              type="button"
              variant="outline"
              className="model-hub-catalog-control flex-1 rounded-md px-5 text-[12.5px] font-semibold sm:flex-none"
              onClick={onCancel}
            >
              {t('settings.models.gateway.catalog.cancel')}
            </Button>
            <Button
              type="button"
              variant="brand"
              className="model-hub-catalog-control flex-1 rounded-md px-5 text-[12.5px] font-semibold sm:flex-none"
              disabled={chosen.length === 0}
              onClick={() => onAdd(chosen)}
            >
              {/* The empty state borrows the catalog's own action label rather
                  than a count of zero, so the footer keeps its width and the
                  button keeps naming what it does. */}
              {chosen.length === 0
                ? t('settings.models.gateway.catalog.add')
                : t('settings.models.gateway.picker.confirm', { count: chosen.length })}
            </Button>
          </div>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
};

/**
 * One candidate.
 *
 * The row owns the `checkbox` role and the box inside it is presentational —
 * one target, one toggle, and the whole line is clickable. An already-listed row
 * renders checked and disabled: it answers 「this exists」 without offering a
 * duplicate.
 */
const PickerRow: React.FC<{
  candidate: ModelCandidate;
  picked: boolean;
  listed: boolean;
  onToggle: () => void;
}> = ({ candidate, picked, listed, onToggle }) => {
  const checked = picked || listed;
  const supplied = candidate.suppliers.length > 0;
  return (
    <button
      type="button"
      role="checkbox"
      aria-checked={checked}
      disabled={listed}
      onClick={onToggle}
      className={cn(
        'model-hub-picker-row min-w-0',
        picked && 'is-picked',
        listed && 'model-hub-picker-row--listed cursor-not-allowed',
        supplied && 'model-hub-picker-row--suppliers',
      )}
    >
      <Checkbox presentational checked={checked} disabled={listed} />
      <span className="model-hub-picker-line flex-1">
        {candidate.display_name ? (
          <>
            <span className="model-hub-picker-name truncate">{candidate.display_name}</span>
            <span className="model-hub-picker-id truncate font-mono">{candidate.id}</span>
          </>
        ) : (
          // No display name, so the id IS the label and sits where one would.
          <span className="model-hub-picker-name model-hub-picker-name--id truncate font-mono">{candidate.id}</span>
        )}
      </span>
      {supplied && (
        <span className="model-hub-picker-chips shrink-0">
          {candidate.suppliers.map((supplier) => (
            <span
              key={`${supplier.source_id} ${supplier.model_id}`}
              className="model-hub-pill model-hub-fill-0a border border-border text-muted"
            >
              {supplier.source_name}
            </span>
          ))}
        </span>
      )}
    </button>
  );
};

export default BackendModelPickerDialog;
