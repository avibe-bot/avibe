import type { TranslationKey } from '@/i18n/types';
// One editor for every backend catalog row, add and edit alike.
//
// It writes nothing. The row it commits goes back into the catalog dialog's
// draft, so the whole list still settles through one `PUT .../models` with one
// baseline — an editor that saved on its own would make every row its own
// mutation and leave no way to cancel a list still being arranged.
//
// The model catalog (models.dev) is a metadata source here, not an authority. The
// ID is the user's: what their provider calls the model. Whatever registered
// model that ID answers to — the same ID, or the closest one the server stands
// behind — fills the parameters of a blank form and is named on the line above
// them; once parameters are there, a different answer is only offered. 「更换」
// searches the catalog and applies another model's parameters; neither path
// touches the ID or the display name the user typed.
// ui/design: models/model-menu/custom-model-closest, custom-model-unmatched,
// custom-model-offer, custom-model-change
import * as React from 'react';
import { Check, LoaderCircle, Plus, Search } from 'lucide-react';
import { Trans, useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { SegmentedRadio } from '@/components/ui/segmented';
import { isComposingKey } from '@/lib/imeComposition';
import { NO_REASONING_EFFORT } from '@/lib/effortOptions';
import { cn } from '@/lib/utils';
import { getBackendUiMeta } from '@/lib/agentBackends';
import { applyModelsDevParameters, blankBackendModel, clearModelsDevParameters, draftWithId, modelsDevBaseId } from './backendCatalog';
import { Field } from './dialogFields';
import { ModelHubInfoHint } from './ModelHubInfoHint';
import { formatTokensCompact } from './format';
import { apiFailure, modelsApi } from './modelsApi';
import { modelsDevFillFailureKey } from './serverCopy';
import {
  BACKEND_MODEL_EFFORT_MAX_LENGTH,
  BACKEND_MODEL_INPUT_MODALITIES,
  BACKEND_MODEL_OUTPUT_MODALITIES,
  NATIVE_PROTOCOLS,
  type AgentBackend,
  type BackendModel,
  type BackendModelInputModality,
  type BackendModelOutputModality,
  type ModelsDevMatch,
  type NativeProtocol,
} from './types';

/** Long enough that a typed word is one read rather than one per keystroke,
 *  short enough that the answer is there when the typing stops. */
const LOOKUP_DEBOUNCE_MS = 250;

/** Common limits, offered under an empty box: the value and how it reads. Round
 *  thousands throughout, so 「64K」 is never more than a model sold as 64K allows. */
const CONTEXT_PRESETS = [[128_000, '128K'], [200_000, '200K'], [400_000, '400K'], [1_000_000, '1M']] as const;
const OUTPUT_PRESETS = [[8_000, '8K'], [32_000, '32K'], [64_000, '64K'], [128_000, '128K']] as const;

/** What the catalog said about one ID: asked, answered with a model, or answered with none. */
type IdAnswer = { id: string; match: ModelsDevMatch | null };
/** The catalog search under 「更换」. */
type SourceSearch = { query: string } & ({ matches: ModelsDevMatch[] } | { failed: TranslationKey });

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
  /** A row written here before and not yet saved, reopened in add mode as it was
   *  left — a write refused for another reason does not cost what was typed. */
  draftSeed?: BackendModel;
  /** Ids the draft catalog already holds; a second row may not claim one. */
  takenIds: ReadonlySet<string>;
  /** Effort values this backend's other rows already use. Suggestions, never a
   *  vocabulary — every effort is sent verbatim and any string is equally valid. */
  effortSuggestions: readonly string[];
  /** A committed answer is being written; the editor holds still until it settles. */
  busy?: boolean;
  onCancel: () => void;
  onCommit: (model: BackendModel) => void;
}> = ({ open, backend, model, seedId, draftSeed, takenIds, effortSuggestions, busy = false, onCancel, onCommit }) => {
  const { t } = useTranslation();
  const creating = model === null;
  const [draft, setDraft] = React.useState<BackendModel>(() => model ?? blankBackendModel());
  /** Held as text, like the token fields: the schema's name is a non-empty
   *  string or null, and only `commit` gets to decide which an empty box is. */
  const [nameText, setNameText] = React.useState('');
  const [contextText, setContextText] = React.useState('');
  const [outputText, setOutputText] = React.useState('');
  const [customOpen, setCustomOpen] = React.useState(false);
  const [customEffort, setCustomEffort] = React.useState('');
  const [submitted, setSubmitted] = React.useState(false);
  /** The catalog's answer about the ID as it stands; null until it has one. */
  const [idAnswer, setIdAnswer] = React.useState<IdAnswer | null>(null);
  /** Whether a parameter has been set by hand since the form opened, and which
   *  registered model the parameters stand on. A blank form takes the catalog's
   *  answer by itself; a form somebody has worked on is only offered it. Read
   *  when an answer lands, so they are refs: nothing is drawn from them. */
  const touched = React.useRef(false);
  const standsOn = React.useRef<string | null>(null);
  const [sourceOpen, setSourceOpen] = React.useState(false);
  const [sourceQuery, setSourceQuery] = React.useState('');
  /** The last catalog search answered; it is the answer only while its query is the one typed. */
  const [sourceSearch, setSourceSearch] = React.useState<SourceSearch | null>(null);
  const sourceToggleRef = React.useRef<HTMLButtonElement>(null);

  const seed = React.useCallback((next: BackendModel, worked: boolean) => {
    setDraft(next);
    setNameText(next.display_name ?? '');
    setContextText(groupTokens(next.context_window));
    setOutputText(groupTokens(next.max_output_tokens));
    setCustomOpen(false);
    setCustomEffort('');
    setSubmitted(false);
    setIdAnswer(null);
    touched.current = worked;
    standsOn.current = next.models_dev_id ?? null;
    setSourceOpen(false);
    setSourceQuery('');
    setSourceSearch(null);
  }, []);

  React.useEffect(() => {
    if (!open) return;
    // A seeded add opens on the ID the picker carried over: the user typed it
    // there, and asking for it again would be the surface forgetting. The ID it
    // lands as goes through the one chokepoint like every other produced ID.
    const next = model ?? draftSeed ?? draftWithId(blankBackendModel(), seedId?.trim() ?? '', backend);
    // A draft reopened after a refused write is the user's work already: the
    // catalog may offer, never refill.
    seed(next, model === null && draftSeed !== undefined);
  }, [backend, draftSeed, model, open, seed, seedId]);

  /** Take a registered model's parameters and leave the ID and the name alone:
   *  the row stays the model the user named, described by the one chosen. A row
   *  being added records that it was described from the catalog; a saved row
   *  keeps how it was first created, and the API it is answered on. */
  const applyParameters = React.useCallback((match: ModelsDevMatch) => {
    standsOn.current = match.models_dev_id;
    setDraft((current) => applyModelsDevParameters(current, match, creating ? 'models_dev' : current.origin, creating ? backend : null));
    setContextText(groupTokens(match.context_window));
    setOutputText(groupTokens(match.max_output_tokens));
  }, [backend, creating]);

  /**
   * What the catalog knows about the ID as it stands.
   *
   * Debounced, because the ID is typed and the answer is a network hop; aborted
   * on the way out, so an answer about an ID the user has already replaced never
   * lands. An unreachable catalog is no answer at all: the form stays the
   * user's, and 「更换」 says why when it is opened.
   */
  const askedId = open ? draft.id.trim() : '';
  React.useEffect(() => {
    if (askedId === '') return;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      void modelsApi.searchModelsDev(askedId, controller.signal).then((found) => {
        if (controller.signal.aborted) return;
        const bare = modelsDevBaseId(askedId);
        const match = found.find((candidate) => candidate.model_id === askedId || candidate.model_id === bare) ?? null;
        setIdAnswer({ id: askedId, match });
        // A blank form takes the answer by itself: the model being added is the
        // one named, and nothing on screen is anyone's work yet. Only the
        // parameters: the display name is the user's to give.
        if (match && creating && !touched.current && standsOn.current === null) applyParameters(match);
      }, () => {});
    }, LOOKUP_DEBOUNCE_MS);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [applyParameters, askedId, creating]);

  const sourceId = draft.models_dev_id ? modelsDevBaseId(draft.models_dev_id) : null;
  const answered = idAnswer !== null && idAnswer.id === draft.id.trim() ? idAnswer : null;
  const answeredId = answered?.match ? modelsDevBaseId(answered.match.models_dev_id) : null;


  // 「更换」: a search of the catalog, answered like the ID was.
  const searchedFor = sourceOpen ? sourceQuery.trim() : '';
  React.useEffect(() => {
    if (searchedFor === '') return;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      void modelsApi.searchModelsDev(searchedFor, controller.signal).then(
        (matches) => { if (!controller.signal.aborted) setSourceSearch({ query: searchedFor, matches }); },
        (error) => { if (!controller.signal.aborted) setSourceSearch({ query: searchedFor, failed: modelsDevFillFailureKey(apiFailure(error)?.detail) }); },
      );
    }, LOOKUP_DEBOUNCE_MS);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [searchedFor]);
  const searchAnswer = searchedFor !== '' && sourceSearch?.query === searchedFor ? sourceSearch : null;
  const searching = searchedFor !== '' && searchAnswer === null;
  const searchMatches = searchAnswer && 'matches' in searchAnswer ? searchAnswer.matches : null;

  const context = parseTokens(contextText);
  const output = parseTokens(outputText);
  const trimmedId = draft.id.trim();
  // A saved row that already has its limits is not asked to reconsider them each
  // time it is opened: the catalog speaks up about a row being added, an ID that
  // was just changed, or limits nobody has filled in.
  const wantsAnswer = creating || trimmedId !== model.id || (context.value === null && output.value === null);
  /** The parameters the ID would bring, when they are not the ones on screen. */
  // Which model the parameters stand on is the whole catalog key: two providers'
  // entries for one model name are different answers, with their own limits.
  const offered = wantsAnswer && answered?.match && answered.match.models_dev_id !== draft.models_dev_id ? answered.match : null;
  /** How the offer names its model: the registered name, or the whole key when
   *  that name is the one the parameters already stand on. */
  const offeredName = offered && answeredId === sourceId ? offered.models_dev_id : answeredId;
  /** 「The closest」 is the catalog's own judgement about this ID, said only
   *  while it stands: not for a model the user chose, nor for an ID it does not know. */
  const closest = answered?.match?.match_kind === 'closest' && answered.match.models_dev_id === draft.models_dev_id;
  const unknownToCatalog = wantsAnswer && sourceId === null && answered !== null && answered.match === null;
  /**
   * The row as it would be stored.
   *
   * Add mode resolves the id through the one chokepoint here, so the rules below
   * and the row `commit` sends judge and write the same value — the one the
   * backend receives. Checking the raw box instead would measure a length and a
   * collision against an id that does not exist yet: canonicalization trims
   * surrounding whitespace, and a row already holding `foo` collides with a
   * freshly typed ` foo `.
   *
   * An edit mints nothing. The id came from the server and is held read-only, so
   * resolving it again would rename a saved row whose id predates the rule.
   */
  const resolved = creating ? draftWithId(draft, trimmedId, backend) : draft;
  // Existing IDs are read-only; only a newly entered identity needs these checks.
  const idError = !creating
    ? null
    : trimmedId === ''
      ? 'required'
      : takenIds.has(resolved.id)
        ? 'duplicate'
        : null;
  const valid = idError === null && context.ok && output.ok;

  const patch = (next: Partial<BackendModel>) => setDraft((current) => ({ ...current, ...next }));
  /** A parameter set by hand: from here on the catalog offers, it does not fill. */
  const patchParameters = (next: Partial<BackendModel>) => {
    touched.current = true;
    patch(next);
  };

  const toggleInput = (value: BackendModelInputModality) => patchParameters({
    input_modalities: draft.input_modalities.includes(value)
      ? draft.input_modalities.filter((entry) => entry !== value)
      : [...draft.input_modalities, value],
  });
  const toggleOutput = (value: BackendModelOutputModality) => patchParameters({
    output_modalities: draft.output_modalities.includes(value)
      ? draft.output_modalities.filter((entry) => entry !== value)
      : [...draft.output_modalities, value],
  });
  const toggleEffort = (value: string) => patchParameters({
    reasoning_efforts: draft.reasoning_efforts.includes(value)
      ? draft.reasoning_efforts.filter((entry) => entry !== value)
      : [...draft.reasoning_efforts, value],
  });

  /** 「更换」 opens on the model the parameters stand on, so the list starts
   *  where the user is. */
  const toggleSource = () => {
    setSourceQuery(sourceOpen ? '' : sourceId ?? '');
    setSourceOpen(!sourceOpen);
  };
  const closeSource = () => {
    setSourceOpen(false);
    setSourceQuery('');
    sourceToggleRef.current?.focus();
  };
  const chooseSource = (match: ModelsDevMatch) => {
    applyParameters(match);
    closeSource();
  };
  /** 「不套用，自己填」: the parameters go back to empty under the same ID and
   *  name, and the catalog stops filling them. */
  const clearParameters = () => {
    setDraft((current) => {
      const cleared = clearModelsDevParameters(current);
      return creating ? cleared : { ...cleared, origin: current.origin, ...(current.native_protocol ? { native_protocol: current.native_protocol } : {}) };
    });
    setContextText('');
    setOutputText('');
    touched.current = true;
    standsOn.current = null;
    closeSource();
  };

  // Escape closes the innermost open thing first: the catalog search, or
  // the custom effort being typed, then the editor. It is decided here because
  // the dialog hears Escape on the document's capture phase, before a field's own
  // key handler could stop it — so a field cannot keep the key for itself.
  const onEscapeKeyDown = (event: KeyboardEvent) => {
    // The Escape that cancels an IME composition is the input method's, too.
    if (busy || isComposingKey(event)) {
      event.preventDefault();
      return;
    }
    const target = event.target instanceof Element ? event.target : null;
    if (sourceOpen && target?.closest('[data-model-source]')) {
      event.preventDefault();
      closeSource();
    } else if (customOpen && target?.closest('[data-custom-effort]')) {
      event.preventDefault();
      setCustomOpen(false);
      setCustomEffort('');
    }
  };

  const addCustomEffort = () => {
    const value = customEffort.trim();
    if (value === '' || value.length > BACKEND_MODEL_EFFORT_MAX_LENGTH) return;
    if (!draft.reasoning_efforts.includes(value)) patchParameters({ reasoning_efforts: [...draft.reasoning_efforts, value] });
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

  // A suggestion is not a capability declaration: Off stays unchecked until
  // the operator adds it to this exact model, just like any custom effort.
  const efforts = [...new Set([NO_REASONING_EFFORT, ...effortSuggestions, ...draft.reasoning_efforts])];
  const backendName = getBackendUiMeta(backend).label;
  const protocolLabel = t('settings.models.gateway.modelEditor.nativeProtocol.label') as string;
  const idHint = submitted && idError ? t(`settings.models.gateway.modelEditor.id.${idError}`) as string : null;

  return (
    <Dialog open={open} onOpenChange={(next) => { if (!next && !busy) onCancel(); }}>
      <DialogContent
        onEscapeKeyDown={onEscapeKeyDown}
        keepOnOutsideClick
        mobileSheetHeight="tall"
        closeLabel={t('common.close') as string}
        className="model-hub-model-editor flex h-[min(660px,calc(100dvh-32px))] w-[min(720px,calc(100vw-32px))] max-w-[720px] flex-col gap-0 overflow-hidden rounded-[14px] p-0 max-md:w-full max-md:max-w-none max-md:rounded-t-2xl max-md:p-0 max-md:pt-2"
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
                <Input
                  id={id}
                  value={draft.id}
                  readOnly={!creating}
                  aria-invalid={Boolean(idHint)}
                  spellCheck={false}
                  autoComplete="off"
                  onChange={(event) => patch({ id: event.target.value })}
                  placeholder={t('settings.models.gateway.modelEditor.id.placeholder') as string}
                  className="model-hub-model-control w-full font-mono text-[12.5px] read-only:text-muted"
                />
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
            {/* Where the parameters came from, and the two ways to change that:
                take what the ID itself answers to, or look another model up. */}
            <div className="model-hub-model-source" data-model-source>
              <p className="model-hub-model-source-line">
                {sourceId !== null && (
                  <span className="min-w-0">
                    <Trans
                      i18nKey={closest ? 'settings.models.gateway.modelEditor.source.fromClosest' : 'settings.models.gateway.modelEditor.source.from'}
                      values={{ model: sourceId }}
                      components={{ id: <span className="model-hub-model-source-id" /> }}
                    />
                  </span>
                )}
                {unknownToCatalog && <span role="status">{t('settings.models.gateway.modelEditor.source.none')}</span>}
                {offered && (
                  <button type="button" className="model-hub-model-source-offer" onClick={() => applyParameters(offered)}>
                    <Trans
                      i18nKey="settings.models.gateway.modelEditor.source.updateTo"
                      values={{ model: offeredName }}
                      components={{ id: <span className="font-mono" /> }}
                    />
                  </button>
                )}
                {!offered && (sourceId !== null || unknownToCatalog) && <span aria-hidden="true" className="shrink-0 before:content-['·']" />}
                <button
                  ref={sourceToggleRef}
                  type="button"
                  aria-expanded={sourceOpen}
                  className="model-hub-model-source-action"
                  onClick={toggleSource}
                >
                  {t(sourceId !== null ? 'settings.models.gateway.modelEditor.source.change' : 'settings.models.gateway.modelEditor.source.pick')}
                </button>
              </p>
              {sourceOpen && (
                <div className="model-hub-model-source-panel">
                  <div className="model-hub-model-control model-hub-model-source-search flex min-w-0 items-center gap-2">
                    <Search className="size-4 shrink-0 text-muted" aria-hidden="true" />
                    <Input
                      variant="bare"
                      autoFocus
                      value={sourceQuery}
                      onChange={(event) => setSourceQuery(event.target.value)}
                      placeholder={t('settings.models.gateway.modelEditor.source.search') as string}
                      aria-label={t('settings.models.gateway.modelEditor.source.search') as string}
                      spellCheck={false}
                      autoComplete="off"
                      className="min-w-0 flex-1 text-[12.5px]"
                    />
                    {searching && <LoaderCircle className="size-4 shrink-0 animate-spin text-muted" aria-label={t('settings.models.gateway.modelEditor.lookupLoading') as string} />}
                  </div>
                  {searchAnswer && 'failed' in searchAnswer && <p className="model-hub-model-match-state" role="status">{t(searchAnswer.failed)}</p>}
                  {searchMatches !== null && searchMatches.length === 0 && (
                    <p className="model-hub-model-match-state" role="status">{t('settings.models.gateway.modelEditor.source.empty')}</p>
                  )}
                  {searchMatches !== null && searchMatches.length > 0 && (
                    <div role="group" aria-label={t('settings.models.gateway.modelEditor.matches') as string} className="model-hub-model-source-list flex min-w-0 flex-col">
                      {searchMatches.map((match) => {
                        const size = formatTokensCompact(match.context_window);
                        const registered = modelsDevBaseId(match.models_dev_id);
                        const current = match.models_dev_id === draft.models_dev_id;
                        return (
                          <button
                            key={match.models_dev_id}
                            type="button"
                            aria-current={current || undefined}
                            onClick={() => chooseSource(match)}
                            className={cn('model-hub-model-match model-hub-model-source-row text-left', current && 'is-selected')}
                          >
                            <span className="model-hub-model-match-name truncate">{match.display_name ?? registered}</span>
                            {/* Name on its own line on a phone, the rest under it: neither is cut to fit the other. */}
                            <span className="model-hub-model-source-row-detail">
                              <span className="model-hub-model-match-id truncate font-mono">{registered}</span>
                              <span className="model-hub-model-match-meta truncate">{match.provider_name}</span>
                              <span className="model-hub-model-source-row-end">
                                {current && <Check className="size-3.5 shrink-0" aria-hidden="true" />}
                                {size && <span className="model-hub-model-match-meta">{size}</span>}
                              </span>
                            </span>
                          </button>
                        );
                      })}
                    </div>
                  )}
                  <div className="model-hub-model-source-foot">
                    <span>{t('settings.models.gateway.modelEditor.source.applyNote')}</span>
                    {sourceId !== null && (
                      <button type="button" className="model-hub-model-source-action shrink-0" onClick={clearParameters}>
                        {t('settings.models.gateway.modelEditor.source.clear')}
                      </button>
                    )}
                  </div>
                </div>
              )}
            </div>
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
                    onChange={(next) => { touched.current = true; setContextText(next); }}
                    onBlur={() => context.ok && setContextText(groupTokens(context.value))}
                    presets={CONTEXT_PRESETS}
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
                    onChange={(next) => { touched.current = true; setOutputText(next); }}
                    onBlur={() => output.ok && setOutputText(groupTokens(output.value))}
                    presets={OUTPUT_PRESETS}
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
                onChange={(next) => patchParameters({ supports_tools: next })}
              />
              <CapabilityField
                label={t('settings.models.gateway.modelEditor.supportsReasoning') as string}
                value={draft.supports_reasoning}
                onChange={(next) => patchParameters({ supports_reasoning: next })}
              />
            </div>
            {/* An unstated capability still owns its efforts, so hide the list
                only when the user has actually said this model cannot reason.
                Hidden, not dropped: the draft keeps them for the answer that
                comes back. */}
            {draft.supports_reasoning !== false && (
              <div className="flex flex-col gap-1.5">
                <div className="flex items-center gap-1.5">
                  <p className="model-hub-model-field-label" id="model-hub-efforts">{t('settings.models.gateway.modelEditor.reasoningEfforts')}</p>
                  <ModelHubInfoHint
                    label={t('settings.models.gateway.modelEditor.effortInfoLabel') as string}
                    content={t('settings.models.gateway.modelEditor.effortNote')}
                  />
                </div>
                <div className="flex flex-wrap gap-2" role="group" aria-labelledby="model-hub-efforts">
                  {efforts.map((effort) => (
                    <ChipButton key={effort} on={draft.reasoning_efforts.includes(effort)} onClick={() => toggleEffort(effort)}>
                      {effort === NO_REASONING_EFFORT ? t('chat.picker.effortOptions.none') : effort}
                    </ChipButton>
                  ))}
                  {customOpen ? (
                    <span className="model-hub-model-chip model-hub-model-chip--custom" data-custom-effort>
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
              </div>
            )}
          </section>

          {/* The protocol, and the only surface that names it.
            *
            * It is a fact about the provider overlay OpenCode is given — which
            * of the two APIs Avibe answers this model on — not a capability of
            * the model, so it gets its own block rather than a place under the
            * group above. Unlabelled, and last: the field label is the one
            * string this mechanism is allowed to add anywhere, which is also
            * why no list, picker, row, or chip shows it.
            *
            * The value is read from `resolved` rather than from a default of
            * this component's own, so the box shows what the row would be
            * stored with. Non-null because C8 makes it required on an OpenCode
            * row: the server sends one with every saved row, and `draftWithId`
            * gives every produced row its own — a missing value here would be
            * that chokepoint broken, not a state to render. */}
          {backend === 'opencode' && (
            <section className="model-hub-model-section">
              <div className="grid gap-3 sm:grid-cols-2 sm:gap-6">
                <div className="flex min-w-0 flex-col gap-1.5">
                  <p className="model-hub-model-field-label">{protocolLabel}</p>
                  <SegmentedRadio
                    value={resolved.native_protocol!}
                    onChange={(next: NativeProtocol) => patchParameters({ native_protocol: next })}
                    options={NATIVE_PROTOCOLS.map((id) => ({
                      id,
                      label: t(`settings.models.gateway.modelEditor.nativeProtocol.${id}`) as string,
                    }))}
                    ariaLabel={protocolLabel}
                  />
                </div>
              </div>
            </section>
          )}
        </div>

        <DialogFooter className="model-hub-model-editor-foot shrink-0 items-center border-t border-border sm:justify-end">
          <Button type="button" variant="outline" className="model-hub-model-control rounded-md px-5 text-[12.5px] font-semibold" disabled={busy} onClick={onCancel}>
            {t('settings.models.gateway.modelEditor.cancel')}
          </Button>
          <Button type="button" variant="brand" className="model-hub-model-control rounded-md px-5 text-[12.5px] font-semibold" disabled={busy} onClick={commit}>
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
  /** Common values, offered only while the box is empty. */
  presets: readonly (readonly [number, string])[];
}> = ({ id, value, invalid, onChange, onBlur, presets }) => {
  const { t } = useTranslation();
  return (
    <>
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
    {value.trim() === '' && (
      <div className="model-hub-model-presets" role="group" aria-label={t('settings.models.gateway.modelEditor.presets') as string}>
        {presets.map(([tokens, label]) => (
          <button key={tokens} type="button" className="model-hub-model-preset" onClick={() => onChange(groupTokens(tokens))}>
            {label}
          </button>
        ))}
      </div>
    )}
    </>
  );
};

export default BackendModelEditorDialog;
