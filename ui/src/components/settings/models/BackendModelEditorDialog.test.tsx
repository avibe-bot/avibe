// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { I18nextProvider } from 'react-i18next';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { pressOutside } from '@/lib/testing/pressOutside';
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
  native_protocol: 'anthropic',
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

/** The one surface anywhere that names the protocol. */
const protocol = () => within(screen.getByRole('radiogroup', { name: 'API' }));

const protocolAnswer = (): string | null =>
  protocol().getAllByRole('radio').find((radio) => radio.getAttribute('aria-checked') === 'true')?.textContent ?? null;

const modelField = () => screen.getByLabelText('Model ID') as HTMLInputElement;

const spy = () => vi.spyOn(modelsApi, 'searchModelsDev');
let search: ReturnType<typeof spy>;

beforeEach(() => {
  // The first field is a debounced search now, so tests about other fields do
  // not reach the network or have to say so.
  search = spy().mockResolvedValue([]);
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('BackendModelEditorDialog', () => {
  it('stays open on a click outside, keeping the model being typed', async () => {
    const { onCancel } = renderEditor();
    await userEvent.type(screen.getByLabelText('Model ID'), 'my-finetune');
    await pressOutside();
    expect(onCancel).not.toHaveBeenCalled();
    expect((screen.getByLabelText('Model ID') as HTMLInputElement).value).toBe('my-finetune');
  });

  it.each(['claude', 'codex', 'opencode'] as const)('does not add Off when saving an existing %s model', async (backend) => {
    const user = userEvent.setup();
    const model: BackendModel = {
      ...blankBackendModel(),
      id: '模型/unchanged',
      supports_reasoning: true,
      native_protocol: backend === 'opencode' ? 'openai_responses' : undefined,
      reasoning_efforts: ['high', 'medium', '自定义'],
    };
    const { onCommit } = renderEditor({ backend, model });

    expect(screen.getByRole('checkbox', { name: 'Off' }).getAttribute('aria-checked')).toBe('false');
    await user.click(screen.getByRole('button', { name: 'Save model' }));

    expect(onCommit).toHaveBeenCalledWith(model);
  });

  it.each(['en', 'zh'] as const)('persists Off only after an explicit model choice (%s)', async (language) => {
    const previous = i18n.language;
    await i18n.changeLanguage(language);
    try {
      const user = userEvent.setup();
      const model: BackendModel = {
        ...blankBackendModel(),
        id: '模型/opt-in',
        supports_reasoning: true,
        reasoning_efforts: ['high', '自定义'],
      };
      const { onCommit } = renderEditor({ model });
      const off = screen.getByRole('checkbox', { name: language === 'zh' ? '关闭' : 'Off' });
      await user.click(off);
      expect(off.getAttribute('aria-checked')).toBe('true');
      await user.click(screen.getByRole('button', { name: i18n.t('settings.models.gateway.modelEditor.apply') }));
      expect(onCommit).toHaveBeenCalledWith({ ...model, reasoning_efforts: ['high', '自定义', 'none'] });
    } finally {
      cleanup();
      await i18n.changeLanguage(previous);
    }
  });

  it('closes only the custom effort field on Escape', async () => {
    const { onCancel } = renderEditor({ model: { ...blankBackendModel(), id: 'reasoner', supports_reasoning: true } });
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: 'Custom effort' }));
    const effort = screen.getByLabelText(i18n.t('settings.models.gateway.modelEditor.customEffort') as string);
    await user.type(effort, 'ultra');
    await user.keyboard('{Escape}');
    expect(screen.queryByLabelText(i18n.t('settings.models.gateway.modelEditor.customEffort') as string)).toBeNull();
    expect(onCancel).not.toHaveBeenCalled();
  });

  it('refuses an empty or already-taken model ID', async () => {
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

  it.each(['x'.repeat(257), '模型🧪/e\u0301'.repeat(3000)])('submits a long pasted ID without shortening it (%#)', async (identifier) => {
    const user = userEvent.setup();
    const { onCommit } = renderEditor({ backend: 'opencode' });

    expect(modelField().getAttribute('maxlength')).toBeNull();
    await user.click(modelField());
    await user.paste(`  ${identifier}  `);
    await waitFor(() => expect(search).toHaveBeenCalledWith(
      expect.stringContaining(identifier), expect.any(AbortSignal),
    ));
    await user.click(screen.getByRole('button', { name: 'Add model' }));

    expect(onCommit).toHaveBeenCalledWith(expect.objectContaining({ id: identifier }));
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

    // The full persisted ID remains read-only while its metadata is editable.
    expect(modelField().value).toBe(legacy.id);
    expect(modelField().getAttribute('maxlength')).toBeNull();

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

  it('keeps a saved row under its ID when the ID is left alone, and commits the edited row', async () => {
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
    expect(modelField().readOnly).toBe(false);
    // Nothing was changed, so nothing is said about changing it.
    expect(screen.queryByText(/switch to the new ID/)).toBeNull();
    expect(document.querySelector('.model-hub-model-source-line')?.textContent).toContain('Parameters from claude-opus-4');

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

  // ui/design: models/model-menu/rename-model, rename-model-unrouted
  describe('changing a saved model\'s ID', () => {
    const saved: BackendModel = { ...blankBackendModel(), id: 'relay-opus', origin: 'provider', context_window: 200000, max_output_tokens: 32000 };
    const rename = async (user: ReturnType<typeof userEvent.setup>, to: string) => {
      await user.clear(modelField());
      if (to) await user.type(modelField(), to);
    };

    it.each([
      ['vibey', false],
      ['claude', true],
      ['codex', true],
      ['opencode', true],
    ] as const)('says what the change carries along on %s, and warns of a running conversation only where one can break', async (backend, warns) => {
      const user = userEvent.setup();
      const { onCommit } = renderEditor({ backend, model: { ...saved, ...(backend === 'opencode' ? { native_protocol: 'openai_responses' as const } : {}) }, takenIds: new Set([saved.id, 'taken']) });

      await rename(user, 'relay-opus-2');
      const note = screen.getByText(/Agents and Sessions that use this model switch to the new ID/);
      expect(note.textContent).toContain('An old ID written in a command or script does not change.');
      expect(note.textContent?.includes('A conversation in progress may be interrupted.')).toBe(warns);
      // Every parameter stays; only the ID moves.
      await user.click(screen.getByRole('button', { name: 'Save model' }));
      expect(onCommit).toHaveBeenCalledWith(expect.objectContaining({ id: 'relay-opus-2', origin: 'provider', context_window: 200000, max_output_tokens: 32000 }));
    });

    it('refuses an empty ID or one another row holds, and lets the row keep its own', async () => {
      const user = userEvent.setup();
      const { onCommit } = renderEditor({ model: saved, takenIds: new Set([saved.id, 'taken']) });

      await rename(user, 'taken');
      await user.click(screen.getByRole('button', { name: 'Save model' }));
      expect(screen.getByRole('alert').textContent).toBe('This list already has a model with this ID.');
      // A refused ID is not a rename to describe.
      expect(screen.queryByText(/switch to the new ID/)).toBeNull();
      await rename(user, '');
      expect(screen.getByRole('alert').textContent).toBe('Enter a backend model ID.');
      expect(onCommit).not.toHaveBeenCalled();
      // Typed back to what it was: its own ID is not somebody else's.
      await rename(user, saved.id);
      expect(screen.queryByRole('alert')).toBeNull();
      await user.click(screen.getByRole('button', { name: 'Save model' }));
      expect(onCommit).toHaveBeenCalledWith(expect.objectContaining({ id: saved.id }));
    });

    it('warns that a row following default routing has no route under an ID no provider lists', async () => {
      const user = userEvent.setup();
      const readListedIds = vi.fn().mockResolvedValue(new Set(['listed-by-a-provider']));
      renderEditor({ model: saved, takenIds: new Set([saved.id]), readListedIds });
      // Opening the row asks nothing: the question exists only once the ID changes.
      expect(readListedIds).not.toHaveBeenCalled();

      await rename(user, 'nobody-lists-this');
      expect(await screen.findByText('No provider lists this ID. After saving, set its route separately.')).toBeTruthy();
      await rename(user, 'listed-by-a-provider');
      expect(screen.queryByText(/No provider lists this ID/)).toBeNull();
      expect(screen.getByText(/switch to the new ID/)).toBeTruthy();
    });

    it('does not warn about routing for a row that has a route of its own, which moves with it', async () => {
      const user = userEvent.setup();
      const readListedIds = vi.fn().mockResolvedValue(new Set<string>());
      renderEditor({ model: saved, takenIds: new Set([saved.id]), ownRoute: true, readListedIds });

      await rename(user, 'nobody-lists-this');
      expect(screen.getByText(/switch to the new ID/)).toBeTruthy();
      expect(readListedIds).not.toHaveBeenCalled();
      expect(screen.queryByText(/No provider lists this ID/)).toBeNull();
    });
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

  it('asks for the API only where there is a choice, and asks for it last', () => {
    // The protocol is how Avibe answers OpenCode for this model, not a fact
    // about the model — so it is the one thing on this form that is about the
    // backend, and the only backend that has two of them is the one that is
    // asked. On the others the question has no answer to offer.
    for (const backend of ['claude', 'codex'] as const) {
      renderEditor({ backend });
      expect(screen.queryByRole('radiogroup', { name: 'API' })).toBeNull();
      cleanup();
    }

    renderEditor({ backend: 'opencode' });
    // Last, because it is the only field the user is not expected to touch: the
    // form reads as the model first and the wiring after it.
    const fields = Array.from(document.querySelectorAll('[role="group"],[role="radiogroup"]'));
    expect(fields.at(-1)?.getAttribute('aria-label')).toBe('API');
  });

  it('opens a saved OpenCode row on the API it is stored with', async () => {
    const user = userEvent.setup();
    const saved: BackendModel = {
      ...blankBackendModel(),
      id: 'glm-4.7',
      display_name: 'GLM 4.7',
      native_protocol: 'anthropic',
    };
    const { onCommit } = renderEditor({ backend: 'opencode', model: saved, takenIds: new Set([saved.id]) });

    expect(protocolAnswer()).toBe('Anthropic Messages');
    await user.click(protocol().getByRole('radio', { name: 'OpenAI Responses' }));
    await user.click(screen.getByRole('button', { name: 'Save model' }));

    expect(onCommit).toHaveBeenCalledWith(expect.objectContaining({
      id: 'glm-4.7',
      native_protocol: 'openai_responses',
    }));
  });

  // ── The catalog and the form ───────────────────────────────────────────
  // ui/design: models/model-menu/custom-model-exact, -closest, -unmatched,
  // -offer, -change. The ID is the user's. The catalog fills a blank form with
  // what it knows about the model that ID answers to; once parameters are on
  // screen it only offers; 「Change」 applies another model's parameters. None of
  // them touches the ID or the display name.
  const source = () => within(document.querySelector<HTMLElement>('[data-model-source]')!);
  const sourceLine = () => document.querySelector<HTMLElement>('.model-hub-model-source-line')!.textContent;

  it('fills a blank form from what the catalog knows about the typed ID, and leaves the ID and the name alone', async () => {
    const user = userEvent.setup();
    search.mockResolvedValue([match()]);
    const { onCommit } = renderEditor();

    await user.type(modelField(), match().model_id);
    await waitFor(() => expect((screen.getByLabelText('Context window') as HTMLInputElement).value).toBe('200,000'));

    expect(sourceLine()).toContain(`Parameters from ${match().model_id}`);
    expect(modelField().value).toBe(match().model_id);
    // The name is the user's to give; the list shows the ID without one.
    expect((screen.getByLabelText('Display name') as HTMLInputElement).value).toBe('');
    expect((screen.getByLabelText('Maximum output') as HTMLInputElement).value).toBe('64,000');
    expect(modalities('Input').getByRole('checkbox', { name: 'Image' }).getAttribute('aria-checked')).toBe('true');
    expect(answered('Reasoning')).toBe('Yes');

    // Every filled field stays the user's.
    await user.click(modalities('Input').getByRole('checkbox', { name: 'Image' }));
    await user.clear(screen.getByLabelText('Maximum output'));
    await user.type(screen.getByLabelText('Maximum output'), '8000');
    await user.click(screen.getByRole('checkbox', { name: 'low' }));
    await user.click(screen.getByRole('button', { name: 'Add model' }));

    expect(onCommit).toHaveBeenCalledWith(expect.objectContaining({
      id: match().model_id,
      models_dev_id: match().models_dev_id,
      display_name: null,
      origin: 'models_dev',
      input_modalities: ['text'],
      max_output_tokens: 8000,
      reasoning_efforts: ['high'],
    }));
  });

  it('says so when the parameters come from the closest registered model, not the ID itself', async () => {
    // A provider's name for a variant: the catalog files nothing under it and
    // answers with its base, under the ID that was asked about.
    search.mockResolvedValue([match({ match_kind: 'closest', model_id: 'claude-sonnet-4-5-thinking' })]);
    const { onCommit } = renderEditor({ seedId: 'claude-sonnet-4-5-thinking' });

    await waitFor(() => expect(sourceLine()).toContain('Parameters from the closest match, claude-sonnet-4-5'));
    expect(modelField().value).toBe('claude-sonnet-4-5-thinking');
    await userEvent.setup().click(screen.getByRole('button', { name: 'Add model' }));
    expect(onCommit).toHaveBeenCalledWith(expect.objectContaining({
      id: 'claude-sonnet-4-5-thinking',
      models_dev_id: 'anthropic/claude-sonnet-4-5',
      context_window: 200000,
    }));
  });

  it('says the catalog has no such model, and offers common limits while the boxes are empty', async () => {
    const user = userEvent.setup();
    const { onCommit } = renderEditor({ seedId: 'my-private-model-v2' });

    expect(await source().findByText('The model catalog has no match for this model')).toBeTruthy();
    const limits = screen.getAllByRole('group', { name: 'Common values' });
    expect(limits.map((group) => within(group).getAllByRole('button').map((button) => button.textContent)))
      .toEqual([['128K', '200K', '400K', '1M'], ['8K', '32K', '64K', '128K']]);
    // Round thousands: 「64K」 must not exceed what a model sold as 64K allows.
    await user.click(within(limits[1]).getByRole('button', { name: '64K' }));
    expect((screen.getByLabelText('Maximum output') as HTMLInputElement).value).toBe('64,000');
    await user.clear(screen.getByLabelText('Maximum output'));
    await user.click(within(limits[0]).getByRole('button', { name: '200K' }));
    // A box with a value needs no suggestions.
    expect(screen.getAllByRole('group', { name: 'Common values' })).toHaveLength(1);
    await user.click(screen.getByRole('button', { name: 'Add model' }));
    expect(onCommit).toHaveBeenCalledWith(expect.objectContaining({ id: 'my-private-model-v2', origin: 'manual', context_window: 200000, max_output_tokens: null }));
  });

  it('offers, and does not apply, the parameters an edited ID answers to', async () => {
    const user = userEvent.setup();
    const opus = match({ model_id: 'claude-opus-4', models_dev_id: 'anthropic/claude-opus-4', display_name: 'Claude Opus 4', context_window: 1000000 });
    search.mockImplementation(async (query) => (query === 'claude-opus-4' ? [opus] : query === match().model_id ? [match()] : []));
    const { onCommit } = renderEditor({ seedId: match().model_id });
    await waitFor(() => expect((screen.getByLabelText('Context window') as HTMLInputElement).value).toBe('200,000'));

    await user.clear(modelField());
    await user.type(modelField(), 'claude-opus-4');
    const offer = await source().findByRole('button', { name: 'Update parameters to claude-opus-4' });
    // Nothing moved: the numbers on screen may be the user's own by now.
    expect((screen.getByLabelText('Context window') as HTMLInputElement).value).toBe('200,000');
    expect(sourceLine()).toContain(`Parameters from ${match().model_id}`);

    await user.click(offer);
    expect((screen.getByLabelText('Context window') as HTMLInputElement).value).toBe('1,000,000');
    expect(source().queryByRole('button', { name: /Update parameters/ })).toBeNull();
    await user.click(screen.getByRole('button', { name: 'Add model' }));
    expect(onCommit).toHaveBeenCalledWith(expect.objectContaining({ id: 'claude-opus-4', models_dev_id: 'anthropic/claude-opus-4', context_window: 1000000 }));
  });

  it('treats a draft reopened after a refused write as worked on: offered, not refilled', async () => {
    search.mockResolvedValue([match()]);
    const written: BackendModel = { ...blankBackendModel(), id: match().model_id, context_window: 32000 };
    renderEditor({ draftSeed: written });

    expect(await source().findByRole('button', { name: `Update parameters to ${match().model_id}` })).toBeTruthy();
    expect((screen.getByLabelText('Context window') as HTMLInputElement).value).toBe('32,000');
  });

  it('offers another provider\'s entry for the same model name, by its whole key', async () => {
    const user = userEvent.setup();
    const relay = match({ provider_id: 'relay', provider_name: 'Relay', model_id: `relay/${match().model_id}`, models_dev_id: `relay/${match().model_id}`, context_window: 2000 });
    search.mockImplementation(async (query) => (query === match().model_id ? [match()] : query === relay.model_id ? [relay] : []));
    renderEditor({ seedId: match().model_id });
    await waitFor(() => expect((screen.getByLabelText('Context window') as HTMLInputElement).value).toBe('200,000'));

    await user.clear(modelField());
    await user.type(modelField(), relay.model_id);
    // Same registered name, a different entry with its own limits: still an offer,
    // and named so it cannot be mistaken for the one already applied.
    await user.click(await source().findByRole('button', { name: `Update parameters to ${relay.models_dev_id}` }));
    expect((screen.getByLabelText('Context window') as HTMLInputElement).value).toBe('2,000');
  });

  it('keeps an API the user chose before the catalog answered', async () => {
    const user = userEvent.setup();
    search.mockResolvedValue([match({ model_id: 'glm-4.7' })]);
    renderEditor({ backend: 'opencode' });

    await user.click(protocol().getByRole('radio', { name: 'OpenAI Responses' }));
    await user.type(modelField(), 'glm-4.7');
    // Choosing the API is work on the form: the answer is offered, not poured in over it.
    expect(await source().findByRole('button', { name: 'Update parameters to claude-sonnet-4-5' })).toBeTruthy();
    expect(protocolAnswer()).toBe('OpenAI Responses');
  });

  it('does not fill a form somebody has worked on; it offers instead', async () => {
    const user = userEvent.setup();
    search.mockImplementation(async (query) => (query === match().model_id ? [match()] : []));
    renderEditor();

    await user.type(screen.getByLabelText('Context window'), '32000');
    await user.type(modelField(), match().model_id);
    expect(await source().findByRole('button', { name: `Update parameters to ${match().model_id}` })).toBeTruthy();
    expect((screen.getByLabelText('Context window') as HTMLInputElement).value).toBe('32,000');
  });

  it('applies another model\'s parameters through Change, keeping the ID and the name', async () => {
    const user = userEvent.setup();
    const opus = match({ model_id: 'claude-opus-4', models_dev_id: 'anthropic/claude-opus-4', display_name: 'Claude Opus 4', context_window: 1000000 });
    search.mockImplementation(async (query) => (query === 'opus' ? [opus, match()] : []));
    const { onCommit } = renderEditor({ seedId: 'house-model' });
    await source().findByText('The model catalog has no match for this model');
    await user.type(screen.getByLabelText('Display name'), 'House model');

    await user.click(source().getByRole('button', { name: 'Pick a model to copy parameters from' }));
    await user.type(screen.getByLabelText('Search the model catalog'), 'opus');
    await user.click(await source().findByRole('button', { name: /Claude Opus 4/ }));

    // Applied and closed, focus back on the line it belongs to. The user chose
    // this model; nobody judged it the closest, so the line does not say so.
    expect(screen.queryByLabelText('Search the model catalog')).toBeNull();
    expect(sourceLine()).toContain('Parameters from claude-opus-4');
    expect(sourceLine()).not.toContain('closest');
    expect(document.activeElement).toBe(source().getByRole('button', { name: 'Change' }));
    expect(modelField().value).toBe('house-model');
    await user.click(screen.getByRole('button', { name: 'Add model' }));
    expect(onCommit).toHaveBeenCalledWith(expect.objectContaining({
      id: 'house-model', display_name: 'House model', models_dev_id: 'anthropic/claude-opus-4', context_window: 1000000,
    }));
  });

  it('empties the parameters on request, and stops filling them', async () => {
    const user = userEvent.setup();
    search.mockResolvedValue([match()]);
    const { onCommit } = renderEditor({ seedId: match().model_id });
    await waitFor(() => expect((screen.getByLabelText('Context window') as HTMLInputElement).value).toBe('200,000'));

    await user.click(source().getByRole('button', { name: 'Change' }));
    await user.click(screen.getByRole('button', { name: 'Fill in myself' }));

    expect((screen.getByLabelText('Context window') as HTMLInputElement).value).toBe('');
    expect(answered('Reasoning')).toBe('No');
    // The catalog still knows the model; it is offered again, not poured back in.
    expect(source().getByRole('button', { name: `Update parameters to ${match().model_id}` })).toBeTruthy();
    await user.click(screen.getByRole('button', { name: 'Add model' }));
    expect(onCommit).toHaveBeenCalledWith(expect.objectContaining({ id: match().model_id, origin: 'manual', models_dev_id: null, context_window: null }));
  });

  it('closes only the catalog search on the first Escape, and the editor on the next', async () => {
    const { onCancel } = renderEditor();
    const user = userEvent.setup();
    await user.click(source().getByRole('button', { name: 'Pick a model to copy parameters from' }));
    await user.type(screen.getByLabelText('Search the model catalog'), 'qwen');

    await user.keyboard('{Escape}');
    expect(screen.queryByLabelText('Search the model catalog')).toBeNull();
    expect(onCancel).not.toHaveBeenCalled();

    await user.keyboard('{Escape}');
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it('takes an OpenCode ID as typed, bare', async () => {
    const user = userEvent.setup();
    const { onCommit } = renderEditor({ backend: 'opencode' });

    await user.type(modelField(), 'glm-4.7');
    // An ID nobody published says nothing about how it is spoken to, and the two
    // answers are not equally likely: OpenAI Responses is what a self-hosted or
    // proxied endpoint almost always is.
    expect(protocolAnswer()).toBe('OpenAI Responses');
    await user.click(screen.getByRole('button', { name: 'Add model' }));
    expect(onCommit).toHaveBeenCalledWith(expect.objectContaining({
      id: 'glm-4.7',
      origin: 'manual',
      native_protocol: 'openai_responses',
    }));
  });

  it('takes the API from the model the ID answers to, and commits the one the user changes it to', async () => {
    const user = userEvent.setup();
    search.mockResolvedValue([match({ model_id: 'glm-4.7', display_name: 'GLM 4.7' })]);
    const { onCommit } = renderEditor({ backend: 'opencode' });

    await user.type(modelField(), 'glm-4.7');
    // The catalog knows whose model this is, and the vendor family is what the
    // protocol follows from — so a described model arrives already answered.
    await waitFor(() => expect(protocolAnswer()).toBe('Anthropic Messages'));

    // And the answer is still the user's: an endpoint that serves someone else's
    // model over the other API is exactly the case this field exists for.
    await user.click(protocol().getByRole('radio', { name: 'OpenAI Responses' }));
    await user.click(screen.getByRole('button', { name: 'Add model' }));

    expect(onCommit).toHaveBeenCalledWith(expect.objectContaining({
      id: 'glm-4.7',
      origin: 'models_dev',
      native_protocol: 'openai_responses',
    }));
  });

  it('opens on the ID the picker carried over, and asks about it once', async () => {
    // The user already typed it once, in the picker's own search. Asking again
    // would be the surface forgetting.
    search.mockResolvedValue([match({ model_id: 'sonnet' })]);
    renderEditor({ seedId: 'sonnet' });

    expect(modelField().value).toBe('sonnet');
    await waitFor(() => expect(sourceLine()).toContain('Parameters from'));
    expect(search).toHaveBeenCalledTimes(1);
    expect(search).toHaveBeenCalledWith('sonnet', expect.anything());
  });

  it('answers about the ID the user is on, never the one they left', async () => {
    const user = userEvent.setup();
    let answerFirst!: (matches: ModelsDevMatch[]) => void;
    search.mockImplementation((query) => (query === 'old-id'
      ? new Promise<ModelsDevMatch[]>((resolve) => { answerFirst = resolve; })
      : Promise.resolve([])));
    renderEditor({ seedId: 'old-id' });
    await waitFor(() => expect(search).toHaveBeenCalledWith('old-id', expect.anything()));

    await user.clear(modelField());
    await user.type(modelField(), 'new-id');
    await source().findByText('The model catalog has no match for this model');
    // The late answer is about an ID no longer in the box: it fills nothing.
    await act(async () => { answerFirst([match({ model_id: 'old-id' })]); });
    expect((screen.getByLabelText('Context window') as HTMLInputElement).value).toBe('');
    expect(sourceLine()).not.toContain('Parameters from');
  });

  it('lets the user add their own model while the catalog is slow or unreachable', async () => {
    const user = userEvent.setup();
    search.mockRejectedValue(new ApiCallError('models_dev_unavailable', 'modelHub.errors.models_dev_unavailable'));
    const { onCommit } = renderEditor();

    await user.type(modelField(), 'house-model');
    // No answer is no claim: the line neither names a model nor says none exists.
    expect(sourceLine()).toBe('Pick a model to copy parameters from');
    // Asked directly, it says why in its own words.
    await user.click(source().getByRole('button', { name: 'Pick a model to copy parameters from' }));
    await user.type(screen.getByLabelText('Search the model catalog'), 'house');
    expect(await source().findByText('The model catalog is unavailable')).toBeTruthy();
    await user.click(screen.getByRole('button', { name: 'Add model' }));
    expect(onCommit).toHaveBeenCalledWith(expect.objectContaining({ id: 'house-model', origin: 'manual' }));
  });

  it('keeps every parameter while the ID is edited, and saves them under the new ID', async () => {
    const user = userEvent.setup();
    search.mockImplementation(async (query) => (query === match().model_id ? [match()] : []));
    const { onCommit } = renderEditor({ seedId: match().model_id });
    await waitFor(() => expect((screen.getByLabelText('Context window') as HTMLInputElement).value).toBe('200,000'));

    // A relay's name for the same model: the parameters stay under the ID as it
    // is retyped, and the line goes on naming the model they came from.
    await user.type(modelField(), '-relay');
    await waitFor(() => expect(search).toHaveBeenCalledWith(`${match().model_id}-relay`, expect.anything()));
    expect(sourceLine()).toContain(`Parameters from ${match().model_id}`);
    await user.click(screen.getByRole('button', { name: 'Add model' }));

    expect(onCommit).toHaveBeenCalledWith(expect.objectContaining({
      id: `${match().model_id}-relay`,
      models_dev_id: match().models_dev_id,
      origin: 'models_dev',
      context_window: 200000,
      max_output_tokens: 64000,
      reasoning_efforts: ['low', 'high'],
    }));
  });

  it('carries a capability the catalog does not state through the fill', async () => {
    const user = userEvent.setup();
    search.mockResolvedValue([match({ supports_tools: null, supports_reasoning: null, reasoning_efforts: [] })]);
    const { onCommit } = renderEditor({ seedId: match().model_id });
    await waitFor(() => expect(sourceLine()).toContain('Parameters from'));

    expect(answered('Tool calling')).toBe('Not set');
    expect(answered('Reasoning')).toBe('Not set');
    await user.click(screen.getByRole('button', { name: 'Add model' }));
    expect(onCommit).toHaveBeenCalledWith(expect.objectContaining({
      supports_tools: null,
      supports_reasoning: null,
    }));
  });

  it('leaves a saved row that has its limits alone, whatever the catalog would say', async () => {
    const existing: BackendModel = { ...blankBackendModel(), id: 'claude-sonnet-4-5', origin: 'builtin', context_window: 180000, max_output_tokens: 32000 };
    search.mockResolvedValue([match()]);
    renderEditor({ model: existing, takenIds: new Set([existing.id]) });

    await waitFor(() => expect(search).toHaveBeenCalled());
    await act(async () => {});
    // Opening a row to look at it is not a question about its numbers.
    expect(sourceLine()).toBe('Pick a model to copy parameters from');
    expect((screen.getByLabelText('Context window') as HTMLInputElement).value).toBe('180,000');
  });

  it('offers a saved row what its ID answers to, and changes nothing about how the row was created', async () => {
    const user = userEvent.setup();
    // A row added before the catalog could describe it: no parameters, no source.
    const existing: BackendModel = { ...blankBackendModel(), id: 'claude-sonnet-4-5-thinking', origin: 'provider' };
    search.mockResolvedValue([match({ match_kind: 'closest', model_id: existing.id })]);
    const { onCommit } = renderEditor({ model: existing, takenIds: new Set([existing.id]) });

    const offer = await source().findByRole('button', { name: 'Update parameters to claude-sonnet-4-5' });
    expect((screen.getByLabelText('Context window') as HTMLInputElement).value).toBe('');
    await user.click(offer);
    await user.click(screen.getByRole('button', { name: 'Save model' }));
    expect(onCommit).toHaveBeenCalledWith(expect.objectContaining({
      id: existing.id, origin: 'provider', models_dev_id: 'anthropic/claude-sonnet-4-5', context_window: 200000,
    }));
  });

  it('debounces rapid model ID typing into one catalog read', async () => {
    vi.useFakeTimers();
    try {
      renderEditor();
      const field = modelField();
      fireEvent.change(field, { target: { value: 's' } });
      fireEvent.change(field, { target: { value: 'so' } });
      fireEvent.change(field, { target: { value: 'sonnet' } });

      expect(search).not.toHaveBeenCalled();
      await act(async () => { vi.advanceTimersByTime(249); });
      expect(search).not.toHaveBeenCalled();
      await act(async () => { vi.advanceTimersByTime(1); });

      expect(search).toHaveBeenCalledTimes(1);
      expect(search).toHaveBeenCalledWith('sonnet', expect.any(AbortSignal));
    } finally {
      vi.runOnlyPendingTimers();
      vi.useRealTimers();
    }
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
