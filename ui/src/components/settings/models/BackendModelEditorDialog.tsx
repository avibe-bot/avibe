// One editor for every backend catalog row, add and edit alike.
//
// It writes nothing. The row it commits goes back into the catalog dialog's
// draft, so the whole list still settles through one `PUT .../models` with one
// baseline — an editor that saved on its own would make every row its own
// mutation and leave no way to cancel a list still being arranged.
//
// models.dev is a metadata source here, not an authority: the first field is a
// search, and choosing a suggestion fills the model id along with every fact the
// catalog knows about it — all of them still editable, because what the user
// wanted may be the model next to the one they found.
import * as React from 'react';
import { Check, Plus } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { SegmentedRadio } from '@/components/ui/segmented';
import { isComposingKey } from '@/lib/imeComposition';
import { cn } from '@/lib/utils';
import { applyModelsDevMatch, backendModelId, blankBackendModel, draftWithId, opencodeMenuIdentity, retireModelsDevMatch } from './backendCatalog';
import { Field } from './dialogFields';
import { formatTokensCompact } from './format';
import type { StandardVendors } from './menus/identifiers';
import { apiFailure, modelsApi } from './modelsApi';
import { modelsDevFillFailureKey } from './serverCopy';
import {
  BACKEND_MODEL_EFFORT_MAX_LENGTH,
  BACKEND_MODEL_ID_MAX_LENGTH,
  BACKEND_MODEL_INPUT_MODALITIES,
  BACKEND_MODEL_OUTPUT_MODALITIES,
  type AgentBackend,
  type BackendModel,
  type BackendModelInputModality,
  type BackendModelOutputModality,
  type ModelsDevMatch,
} from './types';

type FillState = 'idle' | 'loading' | 'error';

/** Long enough that a typed word is one read rather than one per keystroke,
 *  short enough that the list is there when the typing stops. */
const LOOKUP_DEBOUNCE_MS = 250;

/** Digits only. A pasted 「163,840」 is the same number as 「163840」, and the
 *  field renders the grouped form itself, so refusing the separator would refuse
 *  the value the field just showed. */
const parseTokens = (text: string): { ok: boolean; value: number | null } => {
  const trimmed = text.replace(/[\s,]/g, '');
  if (trimmed === '') return { ok: true, value: null };
  if (!/^\d+$/.test(trimmed)) return { ok: false, value: null };
  const value = Number(trimmed);
  return Number.isSafeInteger(value) && value >= 1 ? { ok: true, value } : { ok: false, value: null };
};

const groupTokens = (value: number | null): string => (value === null ? '' : value.toLocaleString('en-US'));

const ChipButton: React.FC<{
  on: boolean;
  disabled?: boolean;
  onClick: () => void;
  children: React.ReactNode;
}> = ({ on, disabled, onClick, children }) => (
  <button
    type="button"
    role="checkbox"
    aria-checked={on}
    disabled={disabled}
    onClick={onClick}
    className={cn('model-hub-model-chip', on && 'is-on')}
  >
    {on && <Check className="size-[13px] shrink-0" aria-hidden="true" />}
    {children}
  </button>
);

const SectionLabel: React.FC<{ children: React.ReactNode }> = ({ children }) => (
  <p className="model-hub-model-section-label">{children}</p>
);

/**
 * A capability is a three-valued fact, and `null` — 「nobody has said」 — is the
 * value every shipped row arrives with, because the backend projection simply
 * omits the flag.
 *
 * A switch can only spend that value: the first touch turns an unanswered
 * question into a stated `false`, and nothing on the surface gives it back. So
 * all three states are options here, and 「Not set」 is an answer the user can
 * return to as easily as they left it.
 */
const CAPABILITY_CHOICES = ['unset', 'no', 'yes'] as const;
type CapabilityChoice = (typeof CAPABILITY_CHOICES)[number];

const CapabilityField: React.FC<{
  label: string;
  value: boolean | null;
  onChange: (next: boolean | null) => void;
}> = ({ label, value, onChange }) => {
  const { t } = useTranslation();
  return (
    <div className="flex min-w-0 flex-col gap-1.5">
      <p className="model-hub-model-field-label">{label}</p>
      <SegmentedRadio
        value={value === null ? 'unset' : value ? 'yes' : 'no'}
        onChange={(next: CapabilityChoice) => onChange(next === 'unset' ? null : next === 'yes')}
        options={CAPABILITY_CHOICES.map((id) => ({
          id,
          label: t(`settings.models.gateway.modelEditor.capability.${id}`) as string,
        }))}
        ariaLabel={label}
      />
    </div>
  );
};

export const BackendModelEditorDialog: React.FC<{
  open: boolean;
  backend: AgentBackend;
  /** null opens the editor in add mode. */
  model: BackendModel | null;
  /** What the picker's 「add as a custom model」 action carried over, so the
   *  editor opens on that query instead of asking for it a second time. */
  seedId?: string;
  /** Ids the draft catalog already holds; a second row may not claim one. */
  takenIds: ReadonlySet<string>;
  /** Effort values this backend's other rows already use. Suggestions, never a
   *  vocabulary — every effort is sent verbatim and any string is equally valid. */
  effortSuggestions: readonly string[];
  /** OpenCode's standard vendor ids, as the server projects them — the input to
   *  the shared id rule both typeahead rows resolve through (`backendModelId`).
   *  Empty for every other backend, and for a server too old to project them:
   *  `custom/` is then the answer for every query, which OpenCode still accepts. */
  standardVendors: StandardVendors;
  onCancel: () => void;
  onCommit: (model: BackendModel) => void;
}> = ({ open, backend, model, seedId, takenIds, effortSuggestions, standardVendors, onCancel, onCommit }) => {
  const { t } = useTranslation();
  const creating = model === null;
  const [draft, setDraft] = React.useState<BackendModel>(() => model ?? blankBackendModel());
  /** Held as text, like the token fields: the schema's name is a non-empty
   *  string or null, and only `commit` gets to decide which an empty box is. */
  const [nameText, setNameText] = React.useState('');
  const [contextText, setContextText] = React.useState('');
  const [outputText, setOutputText] = React.useState('');
  const [fillState, setFillState] = React.useState<FillState>('idle');
  /** Which sentence `fillState === 'error'` shows, resolved while the server's
   *  own answer is still in hand. */
  const [fillFailedKey, setFillFailedKey] = React.useState('settings.models.gateway.modelEditor.fillFailed');
  const [matches, setMatches] = React.useState<ModelsDevMatch[]>([]);
  /** The query the typeahead is answering; empty closes it. Held apart from
   *  `draft.id` because choosing a suggestion writes the id, and a list keyed on
   *  the id would reopen on the answer it just gave. */
  const [lookup, setLookup] = React.useState('');
  const [active, setActive] = React.useState(0);
  const [customOpen, setCustomOpen] = React.useState(false);
  const [customEffort, setCustomEffort] = React.useState('');
  const [submitted, setSubmitted] = React.useState(false);
  const fillAttempt = React.useRef(0);
  /** The whole editable surface exactly as the last models.dev fill left it —
   *  the draft and the three fields held as text. A value that still matches it
   *  is models.dev's answer; a value that differs is the user's own typing. Null
   *  once no answer is outstanding, which is the only state `changeId` needs to
   *  tell apart. A ref, not state: nothing renders from it. */
  const filled = React.useRef<{ draft: BackendModel; name: string; context: string; output: string } | null>(null);
  const listId = React.useId();

  const seed = React.useCallback((next: BackendModel) => {
    setDraft(next);
    setNameText(next.display_name ?? '');
    setContextText(groupTokens(next.context_window));
    setOutputText(groupTokens(next.max_output_tokens));
    setFillState('idle');
    setMatches([]);
    setActive(0);
    setCustomOpen(false);
    setCustomEffort('');
    setSubmitted(false);
    filled.current = null;
  }, []);

  React.useEffect(() => {
    if (!open) {
      fillAttempt.current += 1;
      return;
    }
    // A seeded add opens with the query already running: the user typed it in
    // the picker, and asking for it again would be the surface forgetting. The
    // id it lands as goes through the one chokepoint like every other produced
    // id — a seed that skipped it would be an id the backend refuses, arriving
    // by the one route nobody watches. The query stays what was typed: it is a
    // models.dev search, and searching for the resolved id would search for the
    // vendor prefix the resolver just added.
    const next = model ?? draftWithId(blankBackendModel(), seedId?.trim() ?? '', backend, standardVendors);
    seed(next);
    setLookup(model === null ? seedId?.trim() ?? '' : '');
  }, [backend, model, open, seed, seedId, standardVendors]);

  /**
   * The typeahead: what has been typed so far, answered by models.dev.
   *
   * Debounced, because the query is a keystroke and the answer is a network
   * hop. Aborted on the way out, so a reply to a query the user has already
   * replaced never lands — and the attempt counter is bumped on every exit,
   * because aborting raises inside the request, and a stale attempt that still
   * matched would report 「models.dev could not be reached」 about a search
   * nobody is waiting for.
   */
  React.useEffect(() => {
    if (!creating || lookup === '') {
      fillAttempt.current += 1;
      setFillState('idle');
      setMatches([]);
      return;
    }
    const attempt = ++fillAttempt.current;
    setFillState('loading');
    setMatches([]);
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      void (async () => {
        try {
          const found = await modelsApi.searchModelsDev(lookup, controller.signal);
          if (attempt !== fillAttempt.current) return;
          setMatches(found);
          setActive(0);
          setFillState('idle');
        } catch (error) {
          if (attempt !== fillAttempt.current) return;
          setFillFailedKey(modelsDevFillFailureKey(apiFailure(error)?.detail));
          setFillState('error');
        }
      })();
    }, LOOKUP_DEBOUNCE_MS);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
      fillAttempt.current += 1;
    };
  }, [creating, lookup]);

  const context = parseTokens(contextText);
  const output = parseTokens(outputText);
  const trimmedId = draft.id.trim();
  /**
   * The row as it would be stored.
   *
   * Add mode resolves the id through the one chokepoint here, so the rules below
   * and the row `commit` sends judge and write the same value — the one the
   * backend receives. Checking the raw box instead would measure a length and a
   * collision against an id that does not exist yet: `custom/` is part of what
   * gets stored, and a row already holding `custom/foo` collides with a freshly
   * typed `foo`.
   *
   * An edit mints nothing. The id came from the server and is held read-only, so
   * resolving it again would rename a saved row whose id predates the rule.
   */
  const resolved = creating ? draftWithId(draft, trimmedId, backend, standardVendors) : draft;
  // Every id rule is a rule about what the user may type, and only add mode lets
  // them type it — an edit shows the id read-only. Judging a value the dialog
  // itself locks is what made a persisted row whose id predates the length
  // ceiling uneditable: its metadata is the part the backend still accepts, yet
  // Save refused it and pointed at the one field nobody could shorten.
  const idError = !creating
    ? null
    : trimmedId === ''
      ? 'required'
      : resolved.id.length > BACKEND_MODEL_ID_MAX_LENGTH
        ? 'tooLong'
        // Asked of the resolved id for the same reason the length and the
        // collision are: `custom/` is part of what gets stored, so the shape
        // rule has to judge the value the backend will actually parse. It is
        // the last rule that can be answered without the list, and the
        // collision check below is the one that needs it.
        : !opencodeMenuIdentity(resolved.id, backend)
          ? 'invalid'
          : takenIds.has(resolved.id)
            ? 'duplicate'
            : null;
  const valid = idError === null && context.ok && output.ok;

  const patch = (next: Partial<BackendModel>) => setDraft((current) => ({ ...current, ...next }));

  const toggleInput = (value: BackendModelInputModality) => patch({
    input_modalities: draft.input_modalities.includes(value)
      ? draft.input_modalities.filter((entry) => entry !== value)
      : [...draft.input_modalities, value],
  });
  const toggleOutput = (value: BackendModelOutputModality) => patch({
    output_modalities: draft.output_modalities.includes(value)
      ? draft.output_modalities.filter((entry) => entry !== value)
      : [...draft.output_modalities, value],
  });
  const toggleEffort = (value: string) => patch({
    reasoning_efforts: draft.reasoning_efforts.includes(value)
      ? draft.reasoning_efforts.filter((entry) => entry !== value)
      : [...draft.reasoning_efforts, value],
  });

  const applyMatch = (match: ModelsDevMatch) => {
    // The id comes with it — the row is the model that was chosen, under the id
    // its provider publishes it as. `origin` is `models_dev` unconditionally
    // because the typeahead is add mode's field: a saved row's own creation path
    // is never re-decided here.
    const next = applyModelsDevMatch(draft, match, 'models_dev', backend, standardVendors);
    const name = match.display_name ?? '';
    const context = groupTokens(match.context_window);
    const output = groupTokens(match.max_output_tokens);
    setDraft(next);
    setNameText(name);
    setContextText(context);
    setOutputText(output);
    filled.current = { draft: next, name, context, output };
    setLookup('');
  };

  /** Take the query as the id itself, resolved to one this backend accepts. The
   *  draft's other fields are the user's own by construction: whatever an
   *  earlier fill still owned was retired by `changeId` the moment they typed
   *  again, and what survived that is what they typed themselves. */
  const takeLookupAsId = () => {
    setDraft((current) => draftWithId(current, lookup.trim(), backend, standardVendors));
    setLookup('');
  };

  /**
   * Retype the id, and every models.dev answer to the old one is retired: the
   * request still in flight, the match list it would open, and the metadata an
   * earlier fill already poured in.
   *
   * How far that reaches is decided field by field against the fill itself, not
   * by one flag over the whole row — see `retireModelsDevMatch`. A draft that
   * owes models.dev nothing keeps every field, since correcting a typo in the id
   * is not a decision to retype the rest, and after one retirement there is
   * nothing left to retire.
   */
  const changeId = (nextId: string) => {
    setActive(0);
    setLookup(nextId.trim());
    const fill = filled.current;
    if (fill === null) {
      patch({ id: nextId });
      return;
    }
    setDraft((current) => retireModelsDevMatch(current, fill.draft, nextId));
    if (nameText === fill.name) setNameText('');
    if (contextText === fill.context) setContextText('');
    if (outputText === fill.output) setOutputText('');
    filled.current = null;
  };

  // The escape is the last row in every open state, including 「searching」 and
  // 「unavailable」: models.dev being slow or down is not a reason the user may
  // not name their own model.
  const lookupOpen = creating && lookup !== '';
  const rowCount = matches.length + 1;
  const activeRow = Math.min(active, rowCount - 1);
  const chooseRow = (index: number) => {
    if (index < matches.length) applyMatch(matches[index]);
    else takeLookupAsId();
  };

  const onIdKeyDown = (event: React.KeyboardEvent<HTMLInputElement>) => {
    // Every key below is one an IME candidate window uses too — Enter accepts
    // the characters being composed, the arrows move through the candidates —
    // so while one is open none of them are aimed at this list.
    if (isComposingKey(event)) return;
    if (!lookupOpen) return;
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      const step = event.key === 'ArrowDown' ? 1 : rowCount - 1;
      setActive((current) => (Math.min(current, rowCount - 1) + step) % rowCount);
    } else if (event.key === 'Home' || event.key === 'End') {
      event.preventDefault();
      setActive(event.key === 'Home' ? 0 : rowCount - 1);
    } else if (event.key === 'Enter') {
      event.preventDefault();
      chooseRow(activeRow);
    } else if (event.key === 'Escape') {
      // Only the list closes. Without this the dialog would take the same key
      // and discard a row the user is still filling in.
      event.preventDefault();
      event.stopPropagation();
      setLookup('');
    }
  };

  const addCustomEffort = () => {
    const value = customEffort.trim();
    if (value === '' || value.length > BACKEND_MODEL_EFFORT_MAX_LENGTH) return;
    if (!draft.reasoning_efforts.includes(value)) patch({ reasoning_efforts: [...draft.reasoning_efforts, value] });
    setCustomEffort('');
    setCustomOpen(false);
  };

  const commit = () => {
    setSubmitted(true);
    if (!valid) return;
    onCommit({
      ...resolved,
      // An empty box is 「no name」, not an empty name: the schema takes null and
      // the list falls back to the id.
      display_name: nameText.trim() || null,
      context_window: context.value,
      max_output_tokens: output.value,
      // The efforts are the row's own metadata, never a consequence of the
      // capability answer above them: 「no reasoning」 hides the list, and
      // dropping it would make that answer destructive — take it back and the
      // efforts would be gone with nothing to restore them from. Which efforts
      // a `false` row projects is the backend's question, not the editor's.
    });
  };

  const efforts = [...new Set([...effortSuggestions, ...draft.reasoning_efforts])];
  const backendName = t(`settings.models.backends.${backend}`, { defaultValue: backend });
  const idHint = submitted && idError ? t(`settings.models.gateway.modelEditor.id.${idError}`) as string : null;

  return (
    <Dialog open={open} onOpenChange={(next) => { if (!next) onCancel(); }}>
      <DialogContent
        mobileSheetHeight="tall"
        closeLabel={t('common.close') as string}
        className="model-hub-model-editor flex h-[min(660px,calc(100dvh-32px))] w-[min(720px,calc(100vw-32px))] max-w-[720px] flex-col gap-0 overflow-hidden rounded-[14px] border-border-strong bg-surface p-0 shadow-[var(--model-hub-dialog-shadow)] max-md:w-full max-md:max-w-none max-md:rounded-t-2xl max-md:p-0 max-md:pt-2"
      >
        <DialogHeader className="model-hub-model-editor-head shrink-0 justify-center border-b border-border">
          <p className="model-hub-model-editor-eyebrow truncate">{t('settings.models.gateway.modelEditor.eyebrow', { backend: backendName })}</p>
          <DialogTitle className="model-hub-model-editor-title">
            {t(creating ? 'settings.models.gateway.modelEditor.addTitle' : 'settings.models.gateway.modelEditor.editTitle')}
          </DialogTitle>
          <DialogDescription className="sr-only">{t('settings.models.gateway.modelEditor.description')}</DialogDescription>
        </DialogHeader>

        <div className="model-hub-model-editor-body min-h-0 flex-1 overflow-y-auto">
          <section className="model-hub-model-section">
            <SectionLabel>{t('settings.models.gateway.modelEditor.section.basic')}</SectionLabel>
            <Field
              label={t('settings.models.gateway.modelEditor.id.label')}
              className="min-w-0 gap-1.5"
              labelClassName="model-hub-model-field-label"
            >
              {(id) => (
                <div
                  className="relative min-w-0"
                  // The list is an overlay over the fields below it, so it lives
                  // exactly as long as the user is in it: focus moving anywhere
                  // outside closes it, the same containment check
                  // `SourceDetailPanel` uses. Its own rows never trigger this —
                  // they refuse focus on mousedown to keep the caret in the
                  // field — so choosing one still runs.
                  onBlur={(event) => { if (!event.currentTarget.contains(event.relatedTarget)) setLookup(''); }}
                >
                  <Input
                    id={id}
                    value={draft.id}
                    readOnly={!creating}
                    role="combobox"
                    aria-expanded={lookupOpen}
                    aria-controls={listId}
                    aria-autocomplete="list"
                    aria-activedescendant={lookupOpen ? `${listId}-${activeRow}` : undefined}
                    aria-invalid={Boolean(idHint)}
                    // The ceiling belongs to the field that can still be typed
                    // into; a read-only legacy id is shown in full, not clipped.
                    maxLength={creating ? BACKEND_MODEL_ID_MAX_LENGTH : undefined}
                    spellCheck={false}
                    autoComplete="off"
                    onChange={(event) => changeId(event.target.value)}
                    onKeyDown={onIdKeyDown}
                    placeholder={t('settings.models.gateway.modelEditor.id.placeholder') as string}
                    className="model-hub-model-control w-full font-mono text-[12.5px] read-only:text-muted"
                  />
                  {lookupOpen && (
                    // Over the form, not in it: the fields below keep their
                    // places while the list opens and closes on every keystroke.
                    <div className="model-hub-model-match-list absolute inset-x-0 top-full z-30 mt-1.5 flex flex-col">
                      {fillState !== 'idle' && (
                        <p className="model-hub-model-match-state" role="status">
                          {t(fillState === 'loading' ? 'settings.models.gateway.modelEditor.lookupLoading' : fillFailedKey)}
                        </p>
                      )}
                      <div
                        id={listId}
                        role="listbox"
                        aria-label={t('settings.models.gateway.modelEditor.matches') as string}
                        className="flex min-w-0 flex-col"
                      >
                        {matches.map((match, index) => {
                          const context = formatTokensCompact(match.context_window);
                          return (
                            <button
                              key={match.models_dev_id}
                              type="button"
                              role="option"
                              id={`${listId}-${index}`}
                              aria-selected={activeRow === index}
                              // The field keeps the caret: a click that moved
                              // focus would close the list before it fired.
                              onMouseDown={(event) => event.preventDefault()}
                              onMouseEnter={() => setActive(index)}
                              onClick={() => applyMatch(match)}
                              className={cn('model-hub-model-match flex min-w-0 items-center gap-2.5 text-left', activeRow === index && 'is-selected')}
                            >
                              <span className="flex min-w-0 flex-1 items-center gap-2">
                                {/* No `shrink-0`: pinning the name at its full
                                  * width is what its own `truncate` is there
                                  * to prevent, and a name long enough to need
                                  * cutting would instead squeeze the id and
                                  * the provider out of the row before running
                                  * past its edge. Left to shrink, the three
                                  * yield in proportion to what they show, so
                                  * the longest gives up the most. */}
                                <span className="model-hub-model-match-name truncate">{match.display_name ?? match.model_id}</span>
                                <span className="model-hub-model-match-id truncate font-mono">{match.model_id}</span>
                                <span className="model-hub-model-match-meta truncate">{match.provider_name}</span>
                              </span>
                              {context && <span className="model-hub-model-match-meta shrink-0">{context}</span>}
                            </button>
                          );
                        })}
                        <button
                          type="button"
                          role="option"
                          id={`${listId}-${matches.length}`}
                          aria-selected={activeRow === matches.length}
                          onMouseDown={(event) => event.preventDefault()}
                          onMouseEnter={() => setActive(matches.length)}
                          onClick={takeLookupAsId}
                          className={cn(
                            'model-hub-model-match model-hub-model-match--literal flex min-w-0 items-center text-left',
                            activeRow === matches.length && 'is-selected',
                          )}
                        >
                          {/* The id it will create, not the raw query: on
                              OpenCode those differ, and the row that names the
                              other one would be describing a different model. */}
                          <span className="model-hub-model-match-literal min-w-0 truncate">
                            {t('settings.models.gateway.modelEditor.useAsId', {
                              query: backendModelId(lookup.trim(), backend, standardVendors),
                            })}
                          </span>
                        </button>
                      </div>
                    </div>
                  )}
                </div>
              )}
            </Field>
            {idHint && <p className="model-hub-model-error" role="alert">{idHint}</p>}

            <Field
              label={t('settings.models.gateway.modelEditor.displayName.label')}
              className="min-w-0 gap-1.5"
              labelClassName="model-hub-model-field-label"
            >
              {(id) => (
                <Input
                  id={id}
                  value={nameText}
                  onChange={(event) => setNameText(event.target.value)}
                  placeholder={t('settings.models.gateway.modelEditor.displayName.placeholder') as string}
                  className="model-hub-model-control w-full text-[12.5px]"
                />
              )}
            </Field>
          </section>

          <section className="model-hub-model-section">
            <SectionLabel>{t('settings.models.gateway.modelEditor.section.parameters')}</SectionLabel>
            <div className="grid gap-3 sm:grid-cols-2 sm:gap-6">
              <Field
                label={t('settings.models.gateway.modelEditor.contextWindow')}
                className="min-w-0 gap-1.5"
                labelClassName="model-hub-model-field-label"
              >
                {(id) => (
                  <TokenInput
                    id={id}
                    value={contextText}
                    invalid={!context.ok}
                    onChange={setContextText}
                    onBlur={() => context.ok && setContextText(groupTokens(context.value))}
                  />
                )}
              </Field>
              <Field
                label={t('settings.models.gateway.modelEditor.maxOutput')}
                className="min-w-0 gap-1.5"
                labelClassName="model-hub-model-field-label"
              >
                {(id) => (
                  <TokenInput
                    id={id}
                    value={outputText}
                    invalid={!output.ok}
                    onChange={setOutputText}
                    onBlur={() => output.ok && setOutputText(groupTokens(output.value))}
                  />
                )}
              </Field>
            </div>
            <div className="grid gap-3 sm:grid-cols-2 sm:gap-6">
              <div className="flex min-w-0 flex-col gap-1.5">
                <p className="model-hub-model-field-label" id="model-hub-input-modalities">{t('settings.models.gateway.modelEditor.inputModalities')}</p>
                <div className="flex flex-wrap gap-2" role="group" aria-labelledby="model-hub-input-modalities">
                  {BACKEND_MODEL_INPUT_MODALITIES.map((modality) => (
                    <ChipButton key={modality} on={draft.input_modalities.includes(modality)} onClick={() => toggleInput(modality)}>
                      {t(`settings.models.gateway.modelEditor.modality.${modality}`)}
                    </ChipButton>
                  ))}
                </div>
              </div>
              <div className="flex min-w-0 flex-col gap-1.5">
                <p className="model-hub-model-field-label" id="model-hub-output-modalities">{t('settings.models.gateway.modelEditor.outputModalities')}</p>
                <div className="flex flex-wrap gap-2" role="group" aria-labelledby="model-hub-output-modalities">
                  {BACKEND_MODEL_OUTPUT_MODALITIES.map((modality) => (
                    <ChipButton key={modality} on={draft.output_modalities.includes(modality)} onClick={() => toggleOutput(modality)}>
                      {t(`settings.models.gateway.modelEditor.modality.${modality}`)}
                    </ChipButton>
                  ))}
                </div>
              </div>
            </div>
          </section>

          <section className="model-hub-model-section">
            <SectionLabel>{t('settings.models.gateway.modelEditor.section.capabilities')}</SectionLabel>
            <div className="grid gap-3 sm:grid-cols-2 sm:gap-6">
              <CapabilityField
                label={t('settings.models.gateway.modelEditor.supportsTools') as string}
                value={draft.supports_tools}
                onChange={(next) => patch({ supports_tools: next })}
              />
              <CapabilityField
                label={t('settings.models.gateway.modelEditor.supportsReasoning') as string}
                value={draft.supports_reasoning}
                onChange={(next) => patch({ supports_reasoning: next })}
              />
            </div>
            {/* An unstated capability still owns its efforts, so hide the list
                only when the user has actually said this model cannot reason.
                Hidden, not dropped: the draft keeps them for the answer that
                comes back. */}
            {draft.supports_reasoning !== false && (
              <div className="flex flex-col gap-1.5">
                <p className="model-hub-model-field-label" id="model-hub-efforts">{t('settings.models.gateway.modelEditor.reasoningEfforts')}</p>
                <div className="flex flex-wrap gap-2" role="group" aria-labelledby="model-hub-efforts">
                  {efforts.map((effort) => (
                    <ChipButton key={effort} on={draft.reasoning_efforts.includes(effort)} onClick={() => toggleEffort(effort)}>
                      {effort}
                    </ChipButton>
                  ))}
                  {customOpen ? (
                    <span className="model-hub-model-chip model-hub-model-chip--custom">
                      <Input
                        variant="bare"
                        autoFocus
                        value={customEffort}
                        maxLength={BACKEND_MODEL_EFFORT_MAX_LENGTH}
                        aria-label={t('settings.models.gateway.modelEditor.customEffort') as string}
                        placeholder={t('settings.models.gateway.modelEditor.customEffortPlaceholder') as string}
                        onChange={(event) => setCustomEffort(event.target.value)}
                        onKeyDown={(event) => {
                          // The Enter that accepts an IME candidate is not the
                          // Enter that names an effort.
                          if (isComposingKey(event)) return;
                          if (event.key === 'Enter') {
                            event.preventDefault();
                            addCustomEffort();
                          } else if (event.key === 'Escape') {
                            event.preventDefault();
                            event.stopPropagation();
                            setCustomOpen(false);
                            setCustomEffort('');
                          }
                        }}
                        onBlur={addCustomEffort}
                        className="w-[104px] text-[12px]"
                      />
                    </span>
                  ) : (
                    <button type="button" className="model-hub-model-chip model-hub-model-chip--add" onClick={() => setCustomOpen(true)}>
                      <Plus className="size-[13px] shrink-0" aria-hidden="true" />
                      {t('settings.models.gateway.modelEditor.customEffort')}
                    </button>
                  )}
                </div>
                <p className="model-hub-model-note">{t('settings.models.gateway.modelEditor.effortNote')}</p>
              </div>
            )}
          </section>
        </div>

        <DialogFooter className="model-hub-model-editor-foot shrink-0 items-center border-t border-border sm:justify-end">
          <Button type="button" variant="outline" className="model-hub-model-control rounded-md px-5 text-[12.5px] font-semibold" onClick={onCancel}>
            {t('settings.models.gateway.modelEditor.cancel')}
          </Button>
          <Button type="button" variant="brand" className="model-hub-model-control rounded-md px-5 text-[12.5px] font-semibold" onClick={commit}>
            {t(creating ? 'settings.models.gateway.modelEditor.add' : 'settings.models.gateway.modelEditor.apply')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
};

const TokenInput: React.FC<{
  id: string;
  value: string;
  invalid: boolean;
  onChange: (next: string) => void;
  onBlur: () => void;
}> = ({ id, value, invalid, onChange, onBlur }) => {
  const { t } = useTranslation();
  return (
    <div className={cn('model-hub-model-control model-hub-model-token flex min-w-0 items-center gap-2', invalid && 'is-invalid')}>
      <Input
        id={id}
        variant="bare"
        inputMode="numeric"
        value={value}
        aria-invalid={invalid}
        onChange={(event) => onChange(event.target.value)}
        onBlur={onBlur}
        className="min-w-0 flex-1 font-mono text-[12.5px]"
      />
      <span className="model-hub-model-token-suffix shrink-0" aria-hidden="true">{t('settings.models.gateway.modelEditor.tokens')}</span>
    </div>
  );
};

export default BackendModelEditorDialog;
