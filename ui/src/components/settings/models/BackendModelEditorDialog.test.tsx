// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { I18nextProvider } from 'react-i18next';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import i18n from '@/i18n';
import { BackendModelEditorDialog } from './BackendModelEditorDialog';
import { blankBackendModel } from './backendCatalog';
import { ApiCallError, modelsApi } from './modelsApi';
import type { BackendModel, ModelsDevMatch } from './types';

const match = (overrides: Partial<ModelsDevMatch> = {}): ModelsDevMatch => ({
  provider_id: 'anthropic',
  provider_name: 'Anthropic',
  model_id: 'claude-sonnet-4-5',
  models_dev_id: 'anthropic/claude-sonnet-4-5',
  display_name: 'Claude Sonnet 4.5',
  context_window: 200000,
  max_output_tokens: 64000,
  input_modalities: ['text', 'image'],
  output_modalities: ['text'],
  supports_tools: true,
  supports_reasoning: true,
  reasoning_efforts: ['low', 'high'],
  ...overrides,
});

const renderEditor = (overrides: Partial<React.ComponentProps<typeof BackendModelEditorDialog>> = {}) => {
  const onCommit = vi.fn();
  const onCancel = vi.fn();
  render(
    <I18nextProvider i18n={i18n}>
      <BackendModelEditorDialog
        open
        backend="claude"
        model={null}
        takenIds={new Set()}
        effortSuggestions={[]}
        standardVendors={new Set()}
        onCancel={onCancel}
        onCommit={onCommit}
        {...overrides}
      />
    </I18nextProvider>,
  );
  return { onCommit, onCancel };
};

/** Input and output share a modality vocabulary, so a chip is only unambiguous
 *  inside its own group. */
const modalities = (direction: 'Input' | 'Output') =>
  within(screen.getByRole('group', { name: `${direction} modalities` }));

/** Both capabilities answer Yes/No/Not set, so an option only names one of them
 *  from inside its own group. */
const capability = (name: 'Tool calling' | 'Reasoning') =>
  within(screen.getByRole('radiogroup', { name }));

const answered = (name: 'Tool calling' | 'Reasoning'): string | null =>
  capability(name).getAllByRole('radio').find((radio) => radio.getAttribute('aria-checked') === 'true')?.textContent
    ?? null;

const modelField = () => screen.getByLabelText('Model') as HTMLInputElement;

const spy = () => vi.spyOn(modelsApi, 'searchModelsDev');
let search: ReturnType<typeof spy>;

beforeEach(() => {
  // The first field is a search now, so every keystroke in it asks models.dev.
  // Stubbed by default so a test that is about something else neither reaches
  // the network nor has to say so.
  search = spy().mockResolvedValue([]);
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('BackendModelEditorDialog', () => {
  it('refuses an empty, overlong, or already-taken model ID', async () => {
    const user = userEvent.setup();
    const { onCommit } = renderEditor({ takenIds: new Set(['taken']) });

    await user.click(screen.getByRole('button', { name: 'Add model' }));
    expect(screen.getByText('Enter a backend model ID.')).toBeTruthy();
    expect(onCommit).not.toHaveBeenCalled();

    await user.type(modelField(), 'taken');
    expect(screen.getByText('This list already has a model with this ID.')).toBeTruthy();

    await user.clear(modelField());
    await user.type(modelField(), 'kept');
    await user.click(screen.getByRole('button', { name: 'Add model' }));
    expect(onCommit).toHaveBeenCalledTimes(1);
    expect(onCommit.mock.calls[0][0]).toMatchObject({
      id: 'kept',
      origin: 'manual',
      models_dev_id: null,
      display_name: null,
    });
  });

  it('caps the ID at the contract length while it can still be typed', async () => {
    const user = userEvent.setup();
    const { onCommit } = renderEditor();

    expect(modelField().getAttribute('maxlength')).toBe('256');
    // The browser stops at the cap, so reaching the error needs a value the
    // field never lets a keystroke produce.
    fireEvent.change(modelField(), { target: { value: 'x'.repeat(257) } });
    await user.click(screen.getByRole('button', { name: 'Add model' }));

    expect(onCommit).not.toHaveBeenCalled();
    expect(screen.getByText('A model ID may be at most 256 characters.')).toBeTruthy();
  });

  it('edits a persisted ID that predates the length cap', async () => {
    // The id is read-only here and the backend still accepts the metadata, so a
    // ceiling on a field nobody can shorten only locks the row out of every
    // edit it is allowed to make.
    const user = userEvent.setup();
    const legacy: BackendModel = {
      ...blankBackendModel(),
      id: `internal/${'x'.repeat(300)}`,
      origin: 'builtin',
      context_window: 100000,
    };
    const { onCommit } = renderEditor({ model: legacy, takenIds: new Set([legacy.id]) });

    // Shown in full, not clipped: the cap belongs to what can be typed.
    expect(modelField().value).toBe(legacy.id);
    expect(modelField().getAttribute('maxlength')).toBeNull();
    expect(screen.queryByText('A model ID may be at most 256 characters.')).toBeNull();

    await user.type(screen.getByLabelText('Display name'), 'Legacy house model');
    await user.clear(screen.getByLabelText('Maximum output'));
    await user.type(screen.getByLabelText('Maximum output'), '8192');
    await user.click(screen.getByRole('button', { name: 'Save model' }));

    expect(onCommit).toHaveBeenCalledWith(expect.objectContaining({
      id: legacy.id,
      display_name: 'Legacy house model',
      max_output_tokens: 8192,
      context_window: 100000,
    }));
  });

  it('keeps the ID read-only in edit mode and commits the edited row', async () => {
    const user = userEvent.setup();
    const existing: BackendModel = {
      ...blankBackendModel(),
      id: 'anthropic/claude-opus-4',
      display_name: 'Claude Opus 4',
      origin: 'models_dev',
      models_dev_id: 'anthropic/claude-opus-4',
      context_window: 200000,
      max_output_tokens: 32000,
    };
    const { onCommit } = renderEditor({ model: existing, takenIds: new Set([existing.id]) });

    expect(screen.getByRole('heading', { name: 'Edit model' })).toBeTruthy();
    expect(modelField().readOnly).toBe(true);
    await user.type(modelField(), 'suffix');
    expect(modelField().value).toBe('anthropic/claude-opus-4');
    // A saved row's id is not a search: the one metadata source this dialog has
    // belongs to the field the user can still answer.
    expect(screen.queryByRole('listbox')).toBeNull();
    expect(search).not.toHaveBeenCalled();

    // The grouped form is what the field shows, so the grouped form is what it
    // must accept back.
    expect((screen.getByLabelText('Context window') as HTMLInputElement).value).toBe('200,000');
    const output = screen.getByLabelText('Maximum output');
    await user.clear(output);
    await user.type(output, '128,000');
    await user.click(screen.getByRole('button', { name: 'Save model' }));

    expect(onCommit).toHaveBeenCalledWith(expect.objectContaining({
      id: 'anthropic/claude-opus-4',
      max_output_tokens: 128000,
      context_window: 200000,
    }));
  });

  it('refuses a token count that is not a whole number', async () => {
    const user = userEvent.setup();
    const { onCommit } = renderEditor();

    await user.type(modelField(), 'm');
    await user.type(screen.getByLabelText('Context window'), '20k');
    await user.click(screen.getByRole('button', { name: 'Add model' }));

    expect(screen.getByLabelText('Context window').getAttribute('aria-invalid')).toBe('true');
    expect(onCommit).not.toHaveBeenCalled();
  });

  it('fills every field a suggestion knows, the ID included, and leaves each editable', async () => {
    const user = userEvent.setup();
    search.mockResolvedValue([match()]);
    const { onCommit } = renderEditor();

    await user.type(modelField(), 'sonnet');
    await user.click(await screen.findByRole('option', { name: /Claude Sonnet 4\.5/ }));

    // Choosing a suggestion is choosing a model: the row becomes the model that
    // was picked, under the id its provider publishes — not the catalog key, and
    // not the half-typed query that found it.
    expect(modelField().value).toBe(match().model_id);
    expect(screen.queryByRole('listbox')).toBeNull();
    expect((screen.getByLabelText('Display name') as HTMLInputElement).value).toBe('Claude Sonnet 4.5');
    expect((screen.getByLabelText('Context window') as HTMLInputElement).value).toBe('200,000');
    expect((screen.getByLabelText('Maximum output') as HTMLInputElement).value).toBe('64,000');
    expect(modalities('Input').getByRole('checkbox', { name: 'Image' }).getAttribute('aria-checked')).toBe('true');
    expect(answered('Reasoning')).toBe('Yes');

    // Every filled field stays the user's: what they wanted may be the model
    // next to the one they found.
    await user.click(modalities('Input').getByRole('checkbox', { name: 'Image' }));
    await user.clear(screen.getByLabelText('Maximum output'));
    await user.type(screen.getByLabelText('Maximum output'), '8000');
    await user.click(screen.getByRole('checkbox', { name: 'low' }));
    await user.click(screen.getByRole('button', { name: 'Add model' }));

    expect(onCommit).toHaveBeenCalledWith(expect.objectContaining({
      id: match().model_id,
      models_dev_id: match().models_dev_id,
      display_name: 'Claude Sonnet 4.5',
      origin: 'models_dev',
      input_modalities: ['text'],
      max_output_tokens: 8000,
      reasoning_efforts: ['high'],
    }));
  });

  it('offers each match once and keeps the keyboard on the same rows as the pointer', async () => {
    const user = userEvent.setup();
    search.mockResolvedValue([
      match(),
      match({
        provider_id: 'bedrock',
        provider_name: 'Amazon Bedrock',
        model_id: 'anthropic.claude-sonnet-4-5-v1',
        models_dev_id: 'bedrock/claude-sonnet-4-5',
        display_name: 'Sonnet 4.5 (Bedrock)',
        context_window: 180000,
      }),
    ]);
    renderEditor();

    await user.type(modelField(), 'sonnet');
    await waitFor(() => expect(screen.getAllByRole('option')).toHaveLength(3));
    // Every match, plus the escape — one row per answer and one row for the
    // query itself, so the list can never dead-end.
    const rows = screen.getAllByRole('option');
    expect(rows[2].textContent).toContain('Use "sonnet" as the model ID');
    // Nothing is filled by merely being offered.
    expect((screen.getByLabelText('Context window') as HTMLInputElement).value).toBe('');

    // The field keeps the caret and the list keeps the focus ring: the active
    // row is what Enter takes, whichever way it was reached.
    await user.keyboard('{ArrowDown}{Enter}');

    expect(modelField().value).toBe('anthropic.claude-sonnet-4-5-v1');
    expect((screen.getByLabelText('Context window') as HTMLInputElement).value).toBe('180,000');
    expect(screen.queryByRole('listbox')).toBeNull();
  });

  it('takes the query as the ID and, on OpenCode, as one OpenCode accepts', async () => {
    const user = userEvent.setup();
    // The one id rule this dialog owns: OpenCode addresses `provider/model`, so
    // the escape supplies the provider a query does not name and offers the id
    // the backend will accept rather than the one it would reject after saving.
    const { onCommit } = renderEditor({ backend: 'opencode', standardVendors: new Set(['zai']) });

    await user.type(modelField(), 'glm-4.7');
    expect(await screen.findByRole('option', { name: 'Use "custom/glm-4.7" as the model ID' })).toBeTruthy();

    await user.clear(modelField());
    await user.type(modelField(), 'zai/glm-4.7');
    // A query that already names a provider is taken as typed.
    expect(await screen.findByRole('option', { name: 'Use "zai/glm-4.7" as the model ID' })).toBeTruthy();

    await user.clear(modelField());
    await user.type(modelField(), 'acme/glm-4.7');
    // Including a provider the standard vendor list does not know. The server
    // admits any provider segment its grammar accepts, so offering
    // `custom/acme/glm-4.7` would save a different public model id than the one
    // that was typed — and save it silently, because the server accepts that too.
    await user.click(await screen.findByRole('option', { name: 'Use "acme/glm-4.7" as the model ID' }));

    expect(modelField().value).toBe('acme/glm-4.7');
    await user.click(screen.getByRole('button', { name: 'Add model' }));
    expect(onCommit).toHaveBeenCalledWith(expect.objectContaining({ id: 'acme/glm-4.7', origin: 'manual' }));
  });

  it('takes the query verbatim on a backend whose IDs have no provider segment', async () => {
    const user = userEvent.setup();
    const { onCommit } = renderEditor({ backend: 'claude' });

    await user.type(modelField(), 'claude-house-1');
    await user.click(await screen.findByRole('option', { name: 'Use "claude-house-1" as the model ID' }));

    expect(modelField().value).toBe('claude-house-1');
    await user.click(screen.getByRole('button', { name: 'Add model' }));
    expect(onCommit).toHaveBeenCalledWith(expect.objectContaining({ id: 'claude-house-1' }));
  });

  it('opens on the query the picker carried over', async () => {
    // The user already typed it once, in the picker's own search. Asking again
    // would be the surface forgetting.
    search.mockResolvedValue([match()]);
    renderEditor({ seedId: 'sonnet' });

    expect(modelField().value).toBe('sonnet');
    expect(await screen.findByRole('option', { name: /Claude Sonnet 4\.5/ })).toBeTruthy();
    expect(search).toHaveBeenCalledWith('sonnet', expect.anything());
  });

  it('answers the query the user is on, never the one they left', async () => {
    // The answer describes the query that asked for it. Landing a late one would
    // file one model's context window, modalities and efforts under another
    // model's name.
    const user = userEvent.setup();
    const pending = new Map<string, (matches: ModelsDevMatch[]) => void>();
    search.mockImplementation((query) => new Promise((resolve) => { pending.set(query, resolve); }));
    renderEditor();

    await user.type(modelField(), 'sonnet');
    await waitFor(() => expect(pending.has('sonnet')).toBe(true));
    await user.type(modelField(), '-4-5');
    await waitFor(() => expect(pending.has('sonnet-4-5')).toBe(true));

    await act(async () => { pending.get('sonnet')?.([match()]); });
    expect(screen.queryByRole('option', { name: /Claude Sonnet 4\.5/ })).toBeNull();

    await act(async () => { pending.get('sonnet-4-5')?.([match({ display_name: 'Sonnet, current' })]); });
    expect(await screen.findByRole('option', { name: /Sonnet, current/ })).toBeTruthy();
  });

  it('lets the user name their own model while models.dev is slow', async () => {
    const user = userEvent.setup();
    search.mockImplementation(() => new Promise<ModelsDevMatch[]>(() => {}));
    const { onCommit } = renderEditor();

    await user.type(modelField(), 'private/model');
    expect(await screen.findByText('Searching models.dev…')).toBeTruthy();
    // The escape is a row in every open state: a catalog that is slow to answer
    // is not a reason the user may not name their own model.
    await user.click(screen.getByRole('option', { name: 'Use "private/model" as the model ID' }));
    await user.click(screen.getByRole('button', { name: 'Add model' }));

    expect(onCommit).toHaveBeenCalledWith(expect.objectContaining({
      id: 'private/model',
      origin: 'manual',
      models_dev_id: null,
    }));
  });

  it('names an unreachable models.dev in its own words and still offers the query', async () => {
    const user = userEvent.setup();
    search
      .mockRejectedValueOnce(new Error('offline'))
      .mockRejectedValueOnce(new ApiCallError('upstream_unavailable', 'modelHub.errors.models_dev_unavailable', true));
    renderEditor();

    await user.type(modelField(), 'private/model');
    expect(await screen.findByText('models.dev could not be reached')).toBeTruthy();
    expect(screen.getByRole('option', { name: 'Use "private/model" as the model ID' })).toBeTruthy();

    // The server separates 「the catalog itself is down」 from every other
    // failure, and its key is never what the user reads.
    await user.type(modelField(), '-2');
    expect(await screen.findByText('models.dev unavailable')).toBeTruthy();
    expect(screen.queryByText('modelHub.errors.models_dev_unavailable')).toBeNull();
    expect(modelField().value).toBe('private/model-2');
  });

  it('retires filled metadata once the ID it describes is replaced', async () => {
    // A fill describes the model it was chosen for. Keeping its facts under a
    // different id would save one model's metadata under another model's name.
    const user = userEvent.setup();
    search.mockResolvedValue([match()]);
    const { onCommit } = renderEditor();

    await user.type(modelField(), 'sonnet');
    await user.click(await screen.findByRole('option', { name: /Claude Sonnet 4\.5/ }));
    expect((screen.getByLabelText('Context window') as HTMLInputElement).value).toBe('200,000');

    await user.clear(modelField());
    await user.type(modelField(), 'openai/gpt-5');

    expect((screen.getByLabelText('Display name') as HTMLInputElement).value).toBe('');
    expect((screen.getByLabelText('Context window') as HTMLInputElement).value).toBe('');
    expect((screen.getByLabelText('Maximum output') as HTMLInputElement).value).toBe('');
    expect(modalities('Input').getByRole('checkbox', { name: 'Image' }).getAttribute('aria-checked')).toBe('false');
    // Back to the blank row's own answers, not the filled model's.
    expect(answered('Reasoning')).toBe('No');
    expect(screen.queryByRole('checkbox', { name: 'high' })).toBeNull();

    await user.click(screen.getByRole('button', { name: 'Add model' }));
    // Nothing of the old model survives — not the provenance, not one field.
    expect(onCommit).toHaveBeenCalledWith({ ...blankBackendModel(), id: 'openai/gpt-5' });
  });

  it('keeps hand-typed metadata while the user corrects the ID', async () => {
    const user = userEvent.setup();
    const { onCommit } = renderEditor();

    await user.type(modelField(), 'internal/hous');
    await user.type(screen.getByLabelText('Display name'), 'House model');
    await user.type(screen.getByLabelText('Context window'), '32000');
    await user.click(modalities('Input').getByRole('checkbox', { name: 'Image' }));
    // A row that owes models.dev nothing has nothing to retire: fixing a typo in
    // the id is not a decision to retype everything under it.
    await user.type(modelField(), 'e');
    await user.click(screen.getByRole('button', { name: 'Add model' }));

    expect(onCommit).toHaveBeenCalledWith(expect.objectContaining({
      id: 'internal/house',
      origin: 'manual',
      display_name: 'House model',
      context_window: 32000,
      input_modalities: ['text', 'image'],
    }));
  });

  it('keeps what the user typed over a fill while retiring the rest of it', async () => {
    // The half the two cases above do not reach: a row that is part fill and
    // part the user's own typing. Which half a field belongs to is decided
    // against the fill itself — `retireModelsDevMatch` owns that rule and states
    // it over every field — so this asserts the wiring the helper cannot see,
    // the three boxes the editor holds as text until it commits.
    const user = userEvent.setup();
    search.mockResolvedValue([match()]);
    const { onCommit } = renderEditor();

    await user.type(modelField(), 'sonnet');
    await user.click(await screen.findByRole('option', { name: /Claude Sonnet 4\.5/ }));
    await user.clear(screen.getByLabelText('Display name'));
    await user.type(screen.getByLabelText('Display name'), 'House relay');
    await user.clear(screen.getByLabelText('Maximum output'));
    await user.type(screen.getByLabelText('Maximum output'), '8000');

    await user.clear(modelField());
    await user.type(modelField(), 'openai/gpt-5');

    // Theirs stands. Correcting an id is not a decision to retype the form.
    expect((screen.getByLabelText('Display name') as HTMLInputElement).value).toBe('House relay');
    // Grouped, because leaving the box is what formats it — their 8000, shown
    // the way every token count is.
    expect((screen.getByLabelText('Maximum output') as HTMLInputElement).value).toBe('8,000');
    // The fill's own answers go, because they describe a model this row no
    // longer names.
    expect((screen.getByLabelText('Context window') as HTMLInputElement).value).toBe('');
    expect(answered('Reasoning')).toBe('No');

    await user.click(screen.getByRole('button', { name: 'Add model' }));
    expect(onCommit).toHaveBeenCalledWith({
      ...blankBackendModel(),
      id: 'openai/gpt-5',
      display_name: 'House relay',
      max_output_tokens: 8000,
    });
  });

  it('closes the suggestion list once the user is working somewhere else', async () => {
    // It is an overlay over the fields below it, so an open list the user has
    // left is covering the controls they moved on to.
    const user = userEvent.setup();
    search.mockResolvedValue([match()]);
    renderEditor();

    await user.type(modelField(), 'sonnet');
    expect(await screen.findByRole('option', { name: /Claude Sonnet 4\.5/ })).toBeTruthy();

    await user.click(screen.getByLabelText('Display name'));

    expect(screen.queryByRole('listbox')).toBeNull();
    // The id the user typed is theirs either way: leaving the list is not
    // abandoning the field.
    expect(modelField().value).toBe('sonnet');
  });

  it('keeps a display name optional, trimmed, and null when the box is empty', async () => {
    const user = userEvent.setup();
    const unnamed: BackendModel = { ...blankBackendModel(), id: 'gpt-5-codex', context_window: 400000 };
    const { onCommit } = renderEditor({ model: unnamed, takenIds: new Set([unnamed.id]) });

    const name = screen.getByLabelText('Display name') as HTMLInputElement;
    expect(name.value).toBe('');

    // A row that arrived without a name still has none after a save it never
    // touched: an empty box is 「no name」, not an empty string the schema refuses.
    await user.click(screen.getByRole('button', { name: 'Save model' }));
    expect(onCommit).toHaveBeenCalledWith(expect.objectContaining({ id: 'gpt-5-codex', display_name: null }));

    onCommit.mockClear();
    await user.type(name, '  GPT-5 Codex  ');
    await user.click(screen.getByRole('button', { name: 'Save model' }));
    expect(onCommit).toHaveBeenCalledWith(expect.objectContaining({ display_name: 'GPT-5 Codex' }));

    onCommit.mockClear();
    await user.clear(name);
    await user.click(screen.getByRole('button', { name: 'Save model' }));
    expect(onCommit).toHaveBeenCalledWith(expect.objectContaining({ display_name: null }));
  });

  it('sends a custom reasoning effort verbatim and keeps the list when reasoning is off', async () => {
    const user = userEvent.setup();
    const { onCommit } = renderEditor({ effortSuggestions: ['low'] });

    await user.type(modelField(), 'm');
    await user.click(capability('Reasoning').getByRole('radio', { name: 'Yes' }));
    await user.click(screen.getByRole('checkbox', { name: 'low' }));
    await user.click(screen.getByRole('button', { name: 'Custom effort' }));
    await user.type(screen.getByLabelText('Custom effort'), 'XHIGH-2{Enter}');

    expect(screen.getByRole('checkbox', { name: 'XHIGH-2' }).getAttribute('aria-checked')).toBe('true');
    await user.click(screen.getByRole('button', { name: 'Add model' }));
    expect(onCommit).toHaveBeenCalledWith(expect.objectContaining({
      supports_reasoning: true,
      reasoning_efforts: ['low', 'XHIGH-2'],
    }));

    onCommit.mockClear();
    // 「No reasoning」 hides the efforts; it does not spend them. Dropping them
    // here would make the answer destructive — take it back and there would be
    // nothing left to restore.
    await user.click(capability('Reasoning').getByRole('radio', { name: 'No' }));
    expect(screen.queryByRole('checkbox', { name: 'XHIGH-2' })).toBeNull();
    await user.click(screen.getByRole('button', { name: 'Add model' }));
    await waitFor(() => expect(onCommit).toHaveBeenCalledWith(expect.objectContaining({
      supports_reasoning: false,
      reasoning_efforts: ['low', 'XHIGH-2'],
    })));
  });

  it('keeps an unstated capability unstated through an unrelated edit', async () => {
    const user = userEvent.setup();
    // How every shipped builtin row arrives: the server states no capability, and
    // null is not false — it means the backend projection omits the flag.
    const shipped: BackendModel = {
      ...blankBackendModel(),
      id: 'gpt-5',
      origin: 'builtin',
      supports_tools: null,
      supports_reasoning: null,
      reasoning_efforts: ['minimal', 'high'],
    };
    const { onCommit } = renderEditor({ model: shipped, takenIds: new Set([shipped.id]) });

    expect(answered('Tool calling')).toBe('Not set');
    expect(answered('Reasoning')).toBe('Not set');
    // An unstated capability still owns its efforts, so they stay visible.
    expect(screen.getByRole('checkbox', { name: 'minimal' }).getAttribute('aria-checked')).toBe('true');

    await user.clear(screen.getByLabelText('Maximum output'));
    await user.type(screen.getByLabelText('Maximum output'), '4096');
    await user.click(screen.getByRole('button', { name: 'Save model' }));

    expect(onCommit).toHaveBeenCalledWith(expect.objectContaining({
      max_output_tokens: 4096,
      supports_tools: null,
      supports_reasoning: null,
      reasoning_efforts: ['minimal', 'high'],
    }));
  });

  it('states a capability once the user answers it', async () => {
    const user = userEvent.setup();
    const shipped: BackendModel = {
      ...blankBackendModel(),
      id: 'gpt-5',
      supports_tools: null,
      supports_reasoning: null,
      reasoning_efforts: ['high'],
    };
    const { onCommit } = renderEditor({ model: shipped, takenIds: new Set([shipped.id]) });

    await user.click(capability('Tool calling').getByRole('radio', { name: 'Yes' }));
    expect(answered('Tool calling')).toBe('Yes');
    expect(answered('Reasoning')).toBe('Not set');
    // Saying "no reasoning" takes the efforts off the surface and leaves them on
    // the row: what a `false` model projects is the backend's question.
    await user.click(capability('Reasoning').getByRole('radio', { name: 'No' }));
    expect(screen.queryByRole('checkbox', { name: 'high' })).toBeNull();
    await user.click(screen.getByRole('button', { name: 'Save model' }));

    expect(onCommit).toHaveBeenCalledWith(expect.objectContaining({
      supports_tools: true,
      supports_reasoning: false,
      reasoning_efforts: ['high'],
    }));
  });

  it('carries a capability models.dev does not state through the fill', async () => {
    const user = userEvent.setup();
    search.mockResolvedValue([match({ supports_tools: null, supports_reasoning: null, reasoning_efforts: [] })]);
    const { onCommit } = renderEditor();

    await user.type(modelField(), 'sonnet');
    await user.click(await screen.findByRole('option', { name: /Claude Sonnet 4\.5/ }));

    expect(answered('Tool calling')).toBe('Not set');
    expect(answered('Reasoning')).toBe('Not set');
    await user.click(screen.getByRole('button', { name: 'Add model' }));
    expect(onCommit).toHaveBeenCalledWith(expect.objectContaining({
      supports_tools: null,
      supports_reasoning: null,
    }));
  });

  it('takes a stated capability back to not set', async () => {
    const user = userEvent.setup();
    // A capability is three-valued, and the third value is not a starting state
    // the user can only spend: a row saved as 「no reasoning」 by mistake has to
    // be able to stop claiming anything at all.
    const stated: BackendModel = {
      ...blankBackendModel(),
      id: 'gpt-5',
      supports_tools: true,
      supports_reasoning: false,
      reasoning_efforts: ['high'],
    };
    const { onCommit } = renderEditor({ model: stated, takenIds: new Set([stated.id]) });

    expect(answered('Tool calling')).toBe('Yes');
    expect(answered('Reasoning')).toBe('No');
    // 「No reasoning」 is the one answer that hides the efforts, so withdrawing it
    // brings them back — the row kept them the whole time.
    expect(screen.queryByRole('checkbox', { name: 'high' })).toBeNull();

    await user.click(capability('Tool calling').getByRole('radio', { name: 'Not set' }));
    await user.click(capability('Reasoning').getByRole('radio', { name: 'Not set' }));
    expect(screen.getByRole('checkbox', { name: 'high' }).getAttribute('aria-checked')).toBe('true');

    await user.click(screen.getByRole('button', { name: 'Save model' }));
    expect(onCommit).toHaveBeenCalledWith(expect.objectContaining({
      supports_tools: null,
      supports_reasoning: null,
      reasoning_efforts: ['high'],
    }));
  });

  it('cancels without committing anything', async () => {
    const user = userEvent.setup();
    const { onCancel, onCommit } = renderEditor();

    await user.type(modelField(), 'abandoned');
    // The header close and the footer button are the same decision, so both
    // discard the draft rather than one of them saving it. They are still two
    // controls, so they answer to two names: each `getByRole` below is singular
    // and throws if the name it asks for reaches more than one of them, which
    // is what sharing 「Cancel」 did — a screen reader announcing it twice named
    // neither, and a by-name locator could not address either.
    const exits = [
      screen.getByRole('button', { name: 'Close' }),
      screen.getByRole('button', { name: 'Cancel' }),
    ];
    expect(new Set(exits).size).toBe(exits.length);
    for (const exit of exits) await user.click(exit);

    expect(onCancel).toHaveBeenCalledTimes(exits.length);
    expect(onCommit).not.toHaveBeenCalled();
  });
});
