import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react';
import clsx from 'clsx';
import {
  Building2,
  Check,
  ChevronDown,
  Globe2,
  Loader2,
  LockKeyhole,
  Mail,
  Plus,
  RefreshCw,
  Users,
  X,
} from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import { Switch } from '@/components/ui/switch';
import { useApi } from '@/context/ApiContext';
import { getPermissions } from '@/features/permissions/api';
import {
  normalizeShowAccessEmail,
  showAccessDirectoryOf,
  showAccessDraftChanged,
  showAccessEntriesKey,
  showAccessEntriesOf,
  showAccessEntryKey,
  showAccessSuggestions,
  showAccessApplyPayload,
  showAccessTargetEntries,
  withoutShowAccessEntry,
  withShowAccessEntry,
  SHOW_ACCESS_ENTRY_MAX_COUNT,
  type ShowAccess,
  type ShowAccessDirectory,
  type ShowAccessEntry,
  type ShowAccessMode,
  type ShowAccessSuggestion,
} from '@/lib/showPageAccess';
import { isValidShareId, SHARE_ID_MAX_LENGTH } from '@/lib/showPageLinks';

type Gate = 'idle' | 'loading' | 'ready' | 'conflict' | 'share_id_taken' | 'invalid' | 'error';

type DirectoryGate = 'idle' | 'loading' | 'ready' | 'unavailable';

const MODE_ICONS = {
  private: LockKeyhole,
  limited: Users,
  public: Globe2,
} satisfies Record<ShowAccessMode, typeof LockKeyhole>;

function AccessModeSelect({
  value,
  disabled,
  onChange,
  ownerWindowId,
}: {
  value: ShowAccessMode;
  disabled: boolean;
  onChange: (value: ShowAccessMode) => void;
  ownerWindowId?: string;
}) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const CurrentIcon = MODE_ICONS[value];
  const modes = (['private', 'limited', 'public'] as const).map((mode) => ({
    mode,
    label: t(`chat.showPage.sharingModes.${mode}`),
    description: t(`chat.showPage.sharingHelp.${mode}`),
  }));

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          type="button"
          disabled={disabled}
          aria-haspopup="listbox"
          aria-label={t('chat.showPage.sharingModeCurrent', {
            mode: t(`chat.showPage.sharingModes.${value}`),
          })}
          className="flex h-9 w-40 items-center gap-2 rounded-md border border-border bg-background px-3 text-left text-[13px] font-medium text-foreground outline-none transition hover:border-border-strong focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50"
        >
          <CurrentIcon data-access-icon={value} className="size-4 shrink-0 text-cyan-ink" />
          <span className="min-w-0 flex-1 truncate">{t(`chat.showPage.sharingModes.${value}`)}</span>
          <ChevronDown className="size-4 shrink-0 text-muted" />
        </button>
      </PopoverTrigger>
      <PopoverContent
        align="end"
        sideOffset={4}
        role="listbox"
        aria-label={t('chat.showPage.sharingAccess')}
        data-window-owner-id={ownerWindowId}
        className="w-64 space-y-0.5 p-1"
      >
        {modes.map(({ mode, label, description }) => {
          const Icon = MODE_ICONS[mode];
          const selected = value === mode;
          return (
            <button
              key={mode}
              type="button"
              role="option"
              aria-selected={selected}
              disabled={disabled}
              onClick={() => {
                setOpen(false);
                onChange(mode);
              }}
              className={clsx(
                'flex w-full items-start gap-2.5 rounded-sm px-2.5 py-2 text-left transition-colors',
                selected ? 'bg-mint/[0.1]' : 'hover:bg-foreground/[0.05]',
              )}
            >
              <Icon
                data-access-icon={mode}
                className={clsx(
                  'mt-0.5 size-4 shrink-0',
                  selected ? 'text-mint-ink' : 'text-muted',
                )}
              />
              <span className="min-w-0 flex-1">
                <span className="block text-[13px] font-medium text-foreground">{label}</span>
                <span className="mt-0.5 block text-[11px] leading-snug text-muted">{description}</span>
              </span>
              {selected ? <Check className="mt-0.5 size-4 shrink-0 text-mint-ink" /> : null}
            </button>
          );
        })}
      </PopoverContent>
    </Popover>
  );
}

/** The audience field. Focusing or clicking it opens the Organization directory
 *  (members + groups); a partial query narrows it; anything that normalizes to an
 *  email can be typed in full. The dropdown is rendered in flow — not in a
 *  portal — because this field itself lives inside the share popover, where a
 *  nested portal would fight it for focus and dismissal. */
function AudienceCombobox({
  disabled,
  directoryLoading,
  suggestions,
  truncated,
  placeholder,
  query,
  invalid,
  onQueryChange,
  onSelect,
  onSubmitTypedEmail,
}: {
  disabled: boolean;
  directoryLoading: boolean;
  suggestions: ShowAccessSuggestion[];
  truncated: boolean;
  placeholder: string;
  query: string;
  invalid: boolean;
  onQueryChange: (value: string) => void;
  onSelect: (suggestion: ShowAccessSuggestion) => void;
  onSubmitTypedEmail: () => void;
}) {
  const { t } = useTranslation();
  const listId = useId();
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [focused, setFocused] = useState(false);
  // The keyboard cursor tracks an option KEY, not an index: the option list is
  // recomputed from the query on every keystroke, so an index would silently
  // point at a different row (and a reset effect would cascade renders).
  const [activeKey, setActiveKey] = useState<string | null>(null);

  const typedEmail = normalizeShowAccessEmail(query);
  const typedOption: ShowAccessSuggestion | null = typedEmail
    && !suggestions.some((option) => option.kind === 'email' && option.value === typedEmail)
    ? { kind: 'email', value: typedEmail, label: typedEmail }
    : null;
  const options = typedOption ? [...suggestions, typedOption] : suggestions;
  // A focused unmatched query (not a complete email) has zero options and is
  // not loading, so the empty-state copy inside the listbox is unreachable
  // unless the list stays open for that case too.
  const open = focused && !disabled && (
    options.length > 0 || directoryLoading || query.trim().length > 0
  );
  const activeIndex = options.findIndex((option) => showAccessEntryKey(option) === activeKey);
  const active = activeIndex >= 0 ? options[activeIndex] : null;

  const moveActive = (delta: number) => {
    if (!options.length) return;
    const next = activeIndex < 0
      ? (delta > 0 ? 0 : options.length - 1)
      : (activeIndex + delta + options.length) % options.length;
    setActiveKey(showAccessEntryKey(options[next]));
  };

  const commit = (option: ShowAccessSuggestion) => {
    setActiveKey(null);
    onSelect(option);
  };

  return (
    <div
      ref={containerRef}
      className="relative w-full max-w-[17.5rem]"
      onBlur={(event) => {
        if (containerRef.current?.contains(event.relatedTarget as Node | null)) return;
        setFocused(false);
        setActiveKey(null);
      }}
    >
      <div className="flex items-start gap-1.5">
        <div className="min-w-0 flex-1">
          <Input
            role="combobox"
            aria-expanded={open}
            aria-controls={open ? listId : undefined}
            aria-autocomplete="list"
            aria-label={t('chat.showPage.shareAudience')}
            aria-invalid={invalid || undefined}
            autoComplete="off"
            spellCheck={false}
            value={query}
            disabled={disabled}
            placeholder={placeholder}
            className={clsx('h-8 text-xs', invalid && 'border-destructive')}
            onFocus={() => setFocused(true)}
            onClick={() => setFocused(true)}
            onBlur={(event) => {
              // Clicking an option or the add button moves focus inside the field;
              // only leaving the field entirely closes the list. The container's
              // own onBlur is what actually closes it when focus then leaves
              // from those child controls.
              if (containerRef.current?.contains(event.relatedTarget as Node | null)) return;
              setFocused(false);
              setActiveKey(null);
            }}
            onChange={(event) => {
              setFocused(true);
              setActiveKey(null);
              onQueryChange(event.target.value);
            }}
            onKeyDown={(event) => {
              if (event.key === 'Escape') {
                setFocused(false);
                setActiveKey(null);
                return;
              }
              if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
                if (!options.length) return;
                event.preventDefault();
                setFocused(true);
                moveActive(event.key === 'ArrowDown' ? 1 : -1);
                return;
              }
              if (event.key !== 'Enter') return;
              event.preventDefault();
              if (active) {
                commit(active);
                return;
              }
              onSubmitTypedEmail();
            }}
          />
        </div>
        <Button
          type="button"
          size="icon"
          variant="outline"
          className="size-8 shrink-0"
          disabled={disabled || !query}
          onClick={onSubmitTypedEmail}
          aria-label={t('chat.showPage.addEmail')}
        >
          <Plus className="size-3.5" />
        </Button>
      </div>

      {open ? (
        <div
          id={listId}
          role="listbox"
          aria-label={t('chat.showPage.shareAudienceOptions')}
          className="absolute left-0 top-9 z-50 max-h-44 w-[calc(100%-2.375rem)] overflow-y-auto rounded-md border border-border bg-background p-1 shadow-lg"
        >
          {directoryLoading ? (
            <div className="flex items-center gap-1.5 px-2 py-1.5 text-[11px] text-muted">
              <Loader2 className="size-3 animate-spin" />
              {t('chat.showPage.loadingShareDirectory')}
            </div>
          ) : null}
          {options.map((option, index) => {
            const OptionIcon = option.kind === 'group' ? Users : Mail;
            return (
              <button
                key={showAccessEntryKey(option)}
                type="button"
                role="option"
                aria-selected={index === activeIndex}
                // Keeps the field from blurring before the click lands.
                onMouseDown={(event) => event.preventDefault()}
                onClick={() => commit(option)}
                className={clsx(
                  'flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-left text-xs transition-colors',
                  index === activeIndex ? 'bg-mint/[0.1]' : 'hover:bg-foreground/[0.05]',
                )}
              >
                <OptionIcon className="size-3.5 shrink-0 text-muted" />
                <span className="min-w-0 flex-1 truncate text-foreground">{option.label}</span>
                {option.kind === 'group' ? (
                  <span className="shrink-0 text-[10px] uppercase tracking-wide text-muted">
                    {t('chat.showPage.shareGroupBadge')}
                  </span>
                ) : null}
              </button>
            );
          })}
          {!directoryLoading && options.length === 0 ? (
            <p className="px-2 py-1.5 text-[11px] leading-snug text-muted">
              {t('chat.showPage.shareSuggestionEmpty')}
            </p>
          ) : null}
          {truncated ? (
            <p className="px-2 py-1.5 text-[11px] leading-snug text-muted">
              {t('chat.showPage.shareSuggestionNarrow')}
            </p>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

export function ShowPageSharingSettings({
  active,
  canManage,
  sessionId,
  onApplied,
  ownerWindowId,
}: {
  active: boolean;
  canManage: boolean;
  sessionId: string;
  onApplied?: (showAccess: ShowAccess) => void;
  ownerWindowId?: string;
}) {
  const { t } = useTranslation();
  const api = useApi();
  const [gate, setGate] = useState<Gate>('idle');
  const [saved, setSaved] = useState<ShowAccess | null>(null);
  const [mode, setMode] = useState<ShowAccessMode>('private');
  const [shareId, setShareId] = useState('');
  const [entries, setEntries] = useState<ShowAccessEntry[]>([]);
  const [query, setQuery] = useState('');
  const [emailInvalid, setEmailInvalid] = useState(false);
  const [saving, setSaving] = useState(false);
  const [directory, setDirectory] = useState<ShowAccessDirectory | null>(null);
  const [directoryGate, setDirectoryGate] = useState<DirectoryGate>('idle');
  const generationRef = useRef(0);
  const savingRef = useRef(false);
  const savedRef = useRef<ShowAccess | null>(null);
  const directoryRequestedRef = useRef(false);
  // Directory is instance-level (`getPermissions`), not page-revision-level.
  // Tying it to generationRef let a CAS `load()` discard an in-flight
  // directory response while the latch stayed true, so the combobox spun
  // until remount. This counter only advances with the session effect.
  const directoryGenerationRef = useRef(0);
  const sessionIdRef = useRef(sessionId);
  sessionIdRef.current = sessionId;

  const adopt = useCallback(
    (showAccess: ShowAccess, preserveShareIdDraft = false) => {
      if (showAccess.page_id !== sessionId) throw new Error('ShowAccess page identity mismatch');
      savedRef.current = showAccess;
      setSaved(showAccess);
      setMode(showAccess.access_mode);
      if (!preserveShareIdDraft) setShareId(showAccess.share_id ?? '');
      setEntries(showAccessEntriesOf(showAccess));
      setQuery('');
      setEmailInvalid(false);
    },
    [sessionId],
  );

  const load = useCallback(async (
    settledGate: Gate = 'ready',
    preserveShareIdDraft = false,
  ) => {
    const generation = ++generationRef.current;
    setGate('loading');
    try {
      const result = await api.getShowAccessSettings(sessionId);
      if (generation !== generationRef.current) return;
      adopt(result.show_access, preserveShareIdDraft);
      setGate(settledGate);
    } catch {
      if (generation !== generationRef.current) return;
      setGate('error');
    }
  }, [adopt, api, sessionId]);

  useEffect(() => {
    generationRef.current += 1;
    directoryGenerationRef.current += 1;
    savingRef.current = false;
    savedRef.current = null;
    setSaved(null);
    setMode('private');
    setShareId('');
    setEntries([]);
    setQuery('');
    setEmailInvalid(false);
    setSaving(false);
    setGate('idle');
    setDirectory(null);
    setDirectoryGate('idle');
    directoryRequestedRef.current = false;
    if (active && canManage) void load();
    return () => {
      generationRef.current += 1;
      directoryGenerationRef.current += 1;
      savingRef.current = false;
    };
  }, [active, canManage, load, sessionId]);

  // The Organization directory is only needed to search an audience, so it is
  // read when Limited is actually in play. A failure degrades to email-only
  // entry instead of blocking the audience the owner can still type out.
  // The "already requested" latch is a ref, not the gate state: keying it off
  // the gate would let this effect's own `loading` write re-run and cancel the
  // request it just started.
  useEffect(() => {
    if (!active || !canManage || mode !== 'limited' || directoryRequestedRef.current) return;
    directoryRequestedRef.current = true;
    const generation = directoryGenerationRef.current;
    setDirectoryGate('loading');
    void (async () => {
      try {
        const permissions = await getPermissions();
        if (generation !== directoryGenerationRef.current) return;
        const next = showAccessDirectoryOf(permissions);
        setDirectory(next);
        setDirectoryGate(next ? 'ready' : 'unavailable');
      } catch {
        if (generation !== directoryGenerationRef.current) return;
        setDirectory(null);
        setDirectoryGate('unavailable');
      }
    })();
  }, [active, canManage, mode]);

  const normalizedShareId = shareId.trim() || null;
  const sharedMode = mode === 'limited' || mode === 'public';
  const shareIdInvalid = sharedMode && (!normalizedShareId || !isValidShareId(normalizedShareId));
  const accessDirty = Boolean(
    saved && showAccessDraftChanged(saved, mode, saved.share_id, entries),
  );
  const shareIdDirty = Boolean(saved && normalizedShareId !== saved.share_id);
  const entryLimitReached = entries.length >= SHOW_ACCESS_ENTRY_MAX_COUNT;
  const editable = canManage && gate !== 'loading' && !saving;
  const lastEntryPinned = mode === 'limited' && entries.length <= 1;

  const { suggestions, truncated } = useMemo(
    () => showAccessSuggestions(directory, query, entries),
    [directory, entries, query],
  );
  const organizationEntry = entries.find((entry) => entry.kind === 'organization') ?? null;
  const organizationId = organizationEntry?.value ?? directory?.organization_id ?? null;
  const groupName = (groupId: string) => (
    directory?.groups.find((group) => group.id === groupId)?.name ?? groupId
  );

  const commit = async (
    nextMode: ShowAccessMode,
    nextShareId: string,
    nextEntries: ShowAccessEntry[],
    preserveShareIdDraftOnConflict = false,
    preserveShareIdDraftOnSuccess = preserveShareIdDraftOnConflict,
  ) => {
    const current = savedRef.current;
    const targetShareId = nextShareId.trim() || null;
    const targetEntries = showAccessTargetEntries(nextMode, nextEntries);
    const nextInvalid =
      (nextMode !== 'private' && (!targetShareId || !isValidShareId(targetShareId)))
      || (nextMode === 'limited' && targetEntries.length === 0);
    if (
      !current
      || savingRef.current
      || nextInvalid
      || !canManage
      || gate === 'loading'
      || !showAccessDraftChanged(current, nextMode, targetShareId, nextEntries)
    ) {
      return;
    }
    const generation = generationRef.current;
    const requestSessionId = sessionId;
    const isCurrent = () =>
      generation === generationRef.current && requestSessionId === sessionIdRef.current;
    savingRef.current = true;
    setSaving(true);
    try {
      const result = await api.applyShowAccess(
        requestSessionId,
        showAccessApplyPayload(current.revision, nextMode, targetShareId, nextEntries),
      );
      if (result.show_access.page_id !== requestSessionId) {
        throw new Error('ShowAccess page identity mismatch');
      }
      if (!isCurrent()) {
        // Collapsing the row unmounts this editor, but the successful write still
        // has to reconcile the shared inventory so its copied link is not stale.
        // A real session change updates sessionIdRef and remains discarded.
        if (result.status === 'applied' && requestSessionId === sessionIdRef.current) {
          onApplied?.(result.show_access);
        }
        return;
      }
      if (result.status === 'conflict') {
        savingRef.current = false;
        setSaving(false);
        await load('conflict', preserveShareIdDraftOnConflict);
        return;
      }
      if (result.status === 'share_id_taken' || result.status === 'invalid') {
        setGate(result.status);
        return;
      }
      adopt(result.show_access, preserveShareIdDraftOnSuccess);
      setGate('ready');
      onApplied?.(result.show_access);
    } catch {
      if (!isCurrent()) return;
      setGate('error');
    } finally {
      if (isCurrent()) {
        savingRef.current = false;
        setSaving(false);
      }
    }
  };

  const changeMode = (nextMode: ShowAccessMode) => {
    if (!editable || nextMode === mode) return;
    setMode(nextMode);
    setEmailInvalid(false);
    if (nextMode === 'limited' && entries.length === 0) return;
    void commit(nextMode, savedRef.current?.share_id ?? shareId, entries, shareIdDirty);
  };

  const applyEntries = (nextEntries: ShowAccessEntry[]) => {
    if (showAccessEntriesKey(nextEntries) === showAccessEntriesKey(entries)) return;
    setEntries(nextEntries);
    if (mode === 'limited') {
      void commit(mode, savedRef.current?.share_id ?? shareId, nextEntries, shareIdDirty);
    }
  };

  const addEntry = (entry: ShowAccessEntry) => {
    if (!editable || entryLimitReached) return;
    setQuery('');
    setEmailInvalid(false);
    applyEntries(withShowAccessEntry(entries, entry));
  };

  const submitTypedEmail = () => {
    if (!editable || entryLimitReached) return;
    const normalized = normalizeShowAccessEmail(query);
    if (!normalized) {
      setEmailInvalid(true);
      return;
    }
    addEntry({ kind: 'email', value: normalized });
  };

  const removeEntry = (entry: ShowAccessEntry) => {
    if (!editable) return;
    if (lastEntryPinned) return;
    applyEntries(withoutShowAccessEntry(entries, entry));
  };

  const toggleOrganization = (next: boolean) => {
    if (!editable || !organizationId) return;
    if (next) {
      addEntry({ kind: 'organization', value: organizationId });
      return;
    }
    removeEntry({ kind: 'organization', value: organizationId });
  };

  const saveShareId = () => {
    if (!editable || shareIdInvalid) return;
    void commit(mode, shareId, entries, true, false);
  };

  if (!canManage) return null;

  const audienceVariant = directory ? 'organization' : 'email';

  return (
    <div className="space-y-2.5">
      {gate === 'loading' || gate === 'idle' ? (
        <div className="flex min-h-9 items-center justify-between gap-3">
          <div className="text-sm font-medium">{t('chat.showPage.sharingAccess')}</div>
          <div className="flex items-center gap-1.5 text-[11px] text-muted">
            <Loader2 className="size-3.5 animate-spin" />
            {t('chat.showPage.loadingSharingAccess')}
          </div>
        </div>
      ) : saved ? (
        <>
          {sharedMode ? (
            <section
              className="space-y-1.5"
              aria-label={t('showPages.shareId.label')}
            >
              <div className="text-sm font-medium">{t('showPages.shareId.label')}</div>
              <div className="grid w-full max-w-[17.5rem] grid-cols-[auto_minmax(0,1fr)_2rem] items-center gap-1.5">
                <span className="shrink-0 font-mono text-xs text-muted">/p/</span>
                <Input
                  value={shareId}
                  spellCheck={false}
                  autoCapitalize="none"
                  autoCorrect="off"
                  maxLength={SHARE_ID_MAX_LENGTH}
                  disabled={!editable}
                  onChange={(event) => {
                    setShareId(event.target.value);
                    if (gate === 'share_id_taken') setGate('ready');
                  }}
                  aria-label={t('showPages.shareId.label')}
                  aria-invalid={shareIdInvalid || gate === 'share_id_taken' || undefined}
                  className={clsx(
                    'h-8 min-w-0 w-full font-mono text-xs',
                    (shareIdInvalid || gate === 'share_id_taken') && 'border-destructive',
                  )}
                />
                <div className="size-8 shrink-0">
                  {shareIdDirty ? (
                    <Button
                      type="button"
                      size="icon"
                      variant="outline"
                      className="size-8"
                      disabled={
                        !editable
                        || shareIdInvalid
                        || (mode === 'limited' && entries.length === 0)
                      }
                      onClick={saveShareId}
                      aria-label={t('common.save')}
                      title={t('common.save')}
                    >
                      <Check className="size-3.5" />
                    </Button>
                  ) : null}
                </div>
              </div>
              {shareIdInvalid || gate === 'share_id_taken' ? (
                <p className="text-[11px] text-destructive-ink">
                  {gate === 'share_id_taken'
                    ? t('showPages.shareId.errors.taken')
                    : t('showPages.shareId.errors.invalid')}
                </p>
              ) : null}
            </section>
          ) : null}

          <section
            aria-label={t('chat.showPage.sharingAccess')}
            className={clsx('space-y-2.5', sharedMode && 'border-t border-border pt-3')}
          >
            <div className="flex items-center justify-between gap-3">
              <div className="min-w-0">
                <div className="text-sm font-medium">{t('chat.showPage.sharingAccess')}</div>
                <div className="mt-0.5 flex items-center gap-1 text-[11px] text-muted" role="status">
                  {saving ? (
                    <>
                      <Loader2 className="size-3 animate-spin" />
                      {t('common.saving')}
                    </>
                  ) : (
                    <>
                      <Check className="size-3 text-mint-ink" />
                      {t('chat.showPage.sharingAutoSave')}
                    </>
                  )}
                </div>
              </div>
              <AccessModeSelect
                value={mode}
                disabled={!editable}
                onChange={changeMode}
                ownerWindowId={ownerWindowId}
              />
            </div>

            {mode === 'limited' ? (
              <div className="space-y-2">
                <div className="flex items-center justify-between gap-2">
                  <span className="text-[11px] font-medium text-foreground">
                    {t('chat.showPage.shareAudience')}
                  </span>
                  {saved.access_mode === 'limited' && !accessDirty && !saving ? (
                    <span className="flex items-center gap-1 text-[11px] text-mint-ink">
                      <Check className="size-3" />
                      {t('common.saved')}
                    </span>
                  ) : null}
                </div>

                <AudienceCombobox
                  disabled={!editable || entryLimitReached}
                  directoryLoading={directoryGate === 'loading'}
                  suggestions={suggestions}
                  truncated={truncated}
                  placeholder={t(`chat.showPage.shareAudiencePlaceholder.${audienceVariant}`)}
                  query={query}
                  invalid={emailInvalid}
                  onQueryChange={(value) => {
                    setQuery(value);
                    setEmailInvalid(false);
                  }}
                  onSelect={(suggestion) => addEntry({
                    kind: suggestion.kind,
                    value: suggestion.value,
                  })}
                  onSubmitTypedEmail={submitTypedEmail}
                />
                {emailInvalid ? (
                  <p className="text-[11px] text-destructive-ink">
                    {t('chat.showPage.emailInvalid')}
                  </p>
                ) : null}

                {/* One flat audience: the Organization switch sits at the top of
                    the very list it belongs to, level with the group and email
                    entries it neither replaces nor absorbs. */}
                <div className="max-h-40 space-y-1 overflow-y-auto">
                  {organizationId ? (
                    <div className="flex min-h-8 items-center gap-2 rounded-md border border-border bg-foreground/[0.03] px-2 py-1.5">
                      <Building2 className="size-3.5 shrink-0 text-cyan-ink" />
                      <span className="min-w-0 flex-1 truncate text-[11px] text-foreground">
                        {directory?.organization_name
                          ? t('chat.showPage.shareOrganizationNamed', {
                            name: directory.organization_name,
                          })
                          : t('chat.showPage.shareOrganizationGeneric')}
                      </span>
                      <Switch
                        checked={Boolean(organizationEntry)}
                        disabled={
                          !editable
                          || (Boolean(organizationEntry) && lastEntryPinned)
                          || (!organizationEntry && entryLimitReached)
                        }
                        onCheckedChange={toggleOrganization}
                        label={t('chat.showPage.shareOrganizationGeneric')}
                      />
                    </div>
                  ) : null}

                  {entries.filter((entry) => entry.kind !== 'organization').map((entry) => {
                    const label = entry.kind === 'group' ? groupName(entry.value) : entry.value;
                    const EntryIcon = entry.kind === 'group' ? Users : Mail;
                    return (
                      <div
                        key={showAccessEntryKey(entry)}
                        className="flex min-h-8 items-center gap-2 rounded-md border border-border bg-foreground/[0.03] px-2 py-1.5"
                      >
                        <EntryIcon className="size-3.5 shrink-0 text-muted" />
                        <span
                          className={clsx(
                            'min-w-0 flex-1 truncate text-[11px] text-foreground',
                            entry.kind === 'email' && 'font-mono',
                          )}
                        >
                          {label}
                        </span>
                        {entry.kind === 'group' ? (
                          <span className="shrink-0 text-[10px] uppercase tracking-wide text-muted">
                            {t('chat.showPage.shareGroupBadge')}
                          </span>
                        ) : null}
                        <Button
                          type="button"
                          size="icon"
                          variant="ghost"
                          className="size-5 shrink-0"
                          disabled={!editable || lastEntryPinned}
                          onClick={() => removeEntry(entry)}
                          aria-label={t('chat.showPage.removeShareEntry', { label })}
                          title={lastEntryPinned
                            ? t('chat.showPage.keepOneShareEntry')
                            : t('chat.showPage.removeShareEntry', { label })}
                        >
                          <X className="size-3" />
                        </Button>
                      </div>
                    );
                  })}
                </div>

                {entries.length === 0 ? (
                  <p className="text-[11px] text-destructive-ink">
                    {t('chat.showPage.shareAudienceRequired')}
                  </p>
                ) : null}
                <p className="text-[11px] text-muted">
                  {t(`chat.showPage.shareAudienceHint.${audienceVariant}`, {
                    count: SHOW_ACCESS_ENTRY_MAX_COUNT,
                  })}
                </p>
              </div>
            ) : null}
          </section>

          {['conflict', 'invalid', 'error'].includes(gate) ? (
            <div className="flex items-center justify-between gap-2 border-t border-border pt-2">
              <span className="text-[11px] leading-snug text-destructive-ink">
                {t(`chat.showPage.sharingErrors.${gate}`)}
              </span>
              <Button
                type="button"
                size="icon"
                variant="ghost"
                className="size-7 shrink-0"
                onClick={() => void load('ready', shareIdDirty)}
                aria-label={t('common.retry')}
              >
                <RefreshCw className="size-3.5" />
              </Button>
            </div>
          ) : null}
        </>
      ) : (
        <div className="flex items-center justify-between gap-2">
          <span className="text-[11px] text-destructive-ink">
            {t('chat.showPage.sharingErrors.error')}
          </span>
          <Button
            type="button"
            size="icon"
            variant="ghost"
            className="size-7"
            onClick={() => void load('ready', shareIdDirty)}
            aria-label={t('common.retry')}
          >
            <RefreshCw className="size-3.5" />
          </Button>
        </div>
      )}
    </div>
  );
}
