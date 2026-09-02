// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { I18nextProvider } from 'react-i18next';
import { afterEach, describe, expect, it, vi } from 'vitest';

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

    const id = screen.getByLabelText('Backend model ID');
    await user.type(id, 'taken');
    expect(screen.getByText('This list already has a model with this ID.')).toBeTruthy();

    await user.clear(id);
    await user.type(id, 'kept');
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

    const id = screen.getByLabelText('Backend model ID');
    expect(id.getAttribute('maxlength')).toBe('256');
    // The browser stops at the cap, so reaching the error needs a value the
    // field never lets a keystroke produce.
    fireEvent.change(id, { target: { value: 'x'.repeat(257) } });
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

    const id = screen.getByLabelText('Backend model ID') as HTMLInputElement;
    // Shown in full, not clipped: the cap belongs to what can be typed.
    expect(id.value).toBe(legacy.id);
    expect(id.getAttribute('maxlength')).toBeNull();
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
    const id = screen.getByLabelText('Backend model ID') as HTMLInputElement;
    expect(id.readOnly).toBe(true);
    await user.type(id, 'suffix');
    expect(id.value).toBe('anthropic/claude-opus-4');

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

    await user.type(screen.getByLabelText('Backend model ID'), 'm');
    await user.type(screen.getByLabelText('Context window'), '20k');
    await user.click(screen.getByRole('button', { name: 'Add model' }));

    expect(screen.getByLabelText('Context window').getAttribute('aria-invalid')).toBe('true');
    expect(onCommit).not.toHaveBeenCalled();
  });

  it('fills every field from a single models.dev match and leaves them editable', async () => {
    const user = userEvent.setup();
    const search = vi.spyOn(modelsApi, 'searchModelsDev').mockResolvedValue([match()]);
    const { onCommit } = renderEditor();

    await user.type(screen.getByLabelText('Backend model ID'), 'anthropic/claude-sonnet-4-5-20250929');
    await user.click(screen.getByRole('button', { name: 'Fill from models.dev' }));

    await screen.findByText('models.dev · anthropic/claude-sonnet-4-5');
    expect(search).toHaveBeenCalledWith('anthropic/claude-sonnet-4-5-20250929');
    expect((screen.getByLabelText('Display name') as HTMLInputElement).value).toBe('Claude Sonnet 4.5');
    expect((screen.getByLabelText('Context window') as HTMLInputElement).value).toBe('200,000');
    expect(modalities('Input').getByRole('checkbox', { name: 'Image' }).getAttribute('aria-checked')).toBe('true');
    expect(answered('Reasoning')).toBe('Yes');

    // Every filled field stays the user's: the fill is a starting point.
    await user.click(modalities('Input').getByRole('checkbox', { name: 'Image' }));
    await user.clear(screen.getByLabelText('Maximum output'));
    await user.type(screen.getByLabelText('Maximum output'), '8000');
    await user.click(screen.getByRole('checkbox', { name: 'low' }));
    await user.click(screen.getByRole('button', { name: 'Add model' }));

    expect(onCommit).toHaveBeenCalledWith(expect.objectContaining({
      // A fill never renames the row the user typed.
      id: 'anthropic/claude-sonnet-4-5-20250929',
      models_dev_id: 'anthropic/claude-sonnet-4-5',
      display_name: 'Claude Sonnet 4.5',
      origin: 'models_dev',
      input_modalities: ['text'],
      max_output_tokens: 8000,
      reasoning_efforts: ['high'],
    }));
  });

  it('offers a selectable list when models.dev returns several matches', async () => {
    const user = userEvent.setup();
    vi.spyOn(modelsApi, 'searchModelsDev').mockResolvedValue([
      match(),
      match({ provider_id: 'bedrock', provider_name: 'Amazon Bedrock', models_dev_id: 'bedrock/claude-sonnet-4-5', display_name: 'Sonnet 4.5 (Bedrock)', context_window: 180000 }),
    ]);
    renderEditor();

    await user.type(screen.getByLabelText('Backend model ID'), 'claude-sonnet-4-5');
    await user.click(screen.getByRole('button', { name: 'Fill from models.dev' }));

    const options = await screen.findAllByRole('option');
    expect(options).toHaveLength(2);
    expect((screen.getByLabelText('Context window') as HTMLInputElement).value).toBe('');

    await user.click(options[1]);

    expect(screen.queryByRole('option')).toBeNull();
    expect((screen.getByLabelText('Context window') as HTMLInputElement).value).toBe('180,000');
    expect(screen.getByText('models.dev · bedrock/claude-sonnet-4-5')).toBeTruthy();
  });

  it('reports an empty or unreachable models.dev without touching the draft', async () => {
    const user = userEvent.setup();
    const search = vi.spyOn(modelsApi, 'searchModelsDev')
      .mockResolvedValueOnce([])
      .mockRejectedValueOnce(new Error('offline'));
    renderEditor();

    await user.type(screen.getByLabelText('Backend model ID'), 'private/model');
    await user.click(screen.getByRole('button', { name: 'Fill from models.dev' }));
    expect(await screen.findByText('models.dev has no model matching this ID.')).toBeTruthy();

    await user.click(screen.getByRole('button', { name: 'Fill from models.dev' }));
    expect(await screen.findByText('models.dev could not be reached.')).toBeTruthy();
    expect(search).toHaveBeenCalledTimes(2);
    expect((screen.getByLabelText('Backend model ID') as HTMLInputElement).value).toBe('private/model');
  });

  it('names an upstream models.dev outage as a cause of its own', async () => {
    // The server separates 「the catalog is down」 from every other fill failure,
    // and it is the one where retrying is the wrong advice: the fields it would
    // have filled are all typeable by hand.
    const user = userEvent.setup();
    vi.spyOn(modelsApi, 'searchModelsDev').mockRejectedValue(
      new ApiCallError('upstream_unavailable', 'modelHub.errors.models_dev_unavailable', true),
    );
    renderEditor();

    await user.type(screen.getByLabelText('Backend model ID'), 'openai/gpt-5');
    await user.click(screen.getByRole('button', { name: 'Fill from models.dev' }));
    expect(
      await screen.findByText('The models.dev catalog is unavailable right now. You can fill these fields in by hand.'),
    ).toBeTruthy();
    expect(screen.queryByText('models.dev could not be reached.')).toBeNull();
    expect(screen.queryByText('modelHub.errors.models_dev_unavailable')).toBeNull();
  });

  it('drops a models.dev answer once the ID that asked for it is gone', async () => {
    // The answer describes the id that was typed when 填充 was pressed. Landing
    // it on the id that replaced it would file one model's context window,
    // modalities and efforts under another model's name.
    const user = userEvent.setup();
    let settle: (matches: ModelsDevMatch[]) => void = () => {};
    vi.spyOn(modelsApi, 'searchModelsDev').mockImplementation(
      () => new Promise<ModelsDevMatch[]>((resolve) => { settle = resolve; }),
    );
    const { onCommit } = renderEditor();

    const id = screen.getByLabelText('Backend model ID');
    await user.type(id, 'anthropic/claude-sonnet-4-5');
    await user.click(screen.getByRole('button', { name: 'Fill from models.dev' }));
    await user.clear(id);
    await user.type(id, 'openai/gpt-5');

    await act(async () => { settle([match()]); });

    expect(screen.queryByText('models.dev · anthropic/claude-sonnet-4-5')).toBeNull();
    expect(screen.queryByRole('option')).toBeNull();
    expect((screen.getByLabelText('Display name') as HTMLInputElement).value).toBe('');
    expect((screen.getByLabelText('Context window') as HTMLInputElement).value).toBe('');
    // The retired request also releases the button, so the new id can be filled.
    expect((screen.getByRole('button', { name: 'Fill from models.dev' }) as HTMLButtonElement).disabled).toBe(false);

    await user.click(screen.getByRole('button', { name: 'Add model' }));
    expect(onCommit).toHaveBeenCalledWith(expect.objectContaining({
      id: 'openai/gpt-5',
      origin: 'manual',
      models_dev_id: null,
      display_name: null,
      context_window: null,
      reasoning_efforts: [],
    }));
  });

  it('retires filled metadata once the ID it describes is replaced', async () => {
    // The settled answer is the same hazard as the one in flight: a fill fetched
    // for one id must not end up saved under another.
    const user = userEvent.setup();
    vi.spyOn(modelsApi, 'searchModelsDev').mockResolvedValue([match()]);
    const { onCommit } = renderEditor();

    const id = screen.getByLabelText('Backend model ID');
    await user.type(id, 'anthropic/claude-sonnet-4-5');
    await user.click(screen.getByRole('button', { name: 'Fill from models.dev' }));
    await screen.findByText('models.dev · anthropic/claude-sonnet-4-5');

    await user.clear(id);
    await user.type(id, 'openai/gpt-5');

    expect(screen.queryByText('models.dev · anthropic/claude-sonnet-4-5')).toBeNull();
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

    await user.type(screen.getByLabelText('Backend model ID'), 'internal/hous');
    await user.type(screen.getByLabelText('Display name'), 'House model');
    await user.type(screen.getByLabelText('Context window'), '32000');
    await user.click(modalities('Input').getByRole('checkbox', { name: 'Image' }));
    // A row that owes models.dev nothing has nothing to retire: fixing a typo in
    // the id is not a decision to retype everything under it.
    await user.type(screen.getByLabelText('Backend model ID'), 'e');
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

    await user.type(screen.getByLabelText('Backend model ID'), 'm');
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
    vi.spyOn(modelsApi, 'searchModelsDev')
      .mockResolvedValue([match({ supports_tools: null, supports_reasoning: null, reasoning_efforts: [] })]);
    const { onCommit } = renderEditor();

    await user.type(screen.getByLabelText('Backend model ID'), 'anthropic/claude-sonnet-4-5');
    await user.click(screen.getByRole('button', { name: 'Fill from models.dev' }));
    await screen.findByText('models.dev · anthropic/claude-sonnet-4-5');

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

    await user.type(screen.getByLabelText('Backend model ID'), 'abandoned');
    // The header close and the footer button are the same decision, so both
    // discard the draft rather than one of them saving it.
    const cancels = screen.getAllByRole('button', { name: 'Cancel' });
    expect(cancels).toHaveLength(2);
    await user.click(cancels[0]);
    await user.click(cancels[1]);

    expect(onCancel).toHaveBeenCalledTimes(2);
    expect(onCommit).not.toHaveBeenCalled();
  });
});
