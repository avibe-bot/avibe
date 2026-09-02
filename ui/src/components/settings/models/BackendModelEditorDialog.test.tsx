// @vitest-environment jsdom
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
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
    expect(onCommit.mock.calls[0][0]).toMatchObject({ id: 'kept', origin: 'manual', models_dev_id: null });
  });

  it('caps the ID at the contract length', () => {
    renderEditor();

    expect(screen.getByLabelText('Backend model ID').getAttribute('maxlength')).toBe('256');
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
    expect((screen.getByLabelText('Context window') as HTMLInputElement).value).toBe('200,000');
    expect(modalities('Input').getByRole('checkbox', { name: 'Image' }).getAttribute('aria-checked')).toBe('true');
    expect(screen.getByRole('switch', { name: 'Reasoning' }).getAttribute('aria-checked')).toBe('true');

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

  it('sends a custom reasoning effort verbatim and drops the list when reasoning is off', async () => {
    const user = userEvent.setup();
    const { onCommit } = renderEditor({ effortSuggestions: ['low'] });

    await user.type(screen.getByLabelText('Backend model ID'), 'm');
    await user.click(screen.getByRole('switch', { name: 'Reasoning' }));
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
    await user.click(screen.getByRole('switch', { name: 'Reasoning' }));
    await user.click(screen.getByRole('button', { name: 'Add model' }));
    await waitFor(() => expect(onCommit).toHaveBeenCalledWith(expect.objectContaining({
      supports_reasoning: false,
      reasoning_efforts: [],
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

    expect(screen.getAllByText('not set')).toHaveLength(2);
    expect(screen.getByRole('switch', { name: 'Tool calling' }).getAttribute('aria-checked')).toBe('false');
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

    await user.click(screen.getByRole('switch', { name: 'Tool calling' }));
    expect(screen.getAllByText('not set')).toHaveLength(1);
    // Saying "no reasoning" is the one answer that retires the efforts.
    await user.click(screen.getByRole('switch', { name: 'Reasoning' }));
    await user.click(screen.getByRole('switch', { name: 'Reasoning' }));
    expect(screen.queryByRole('checkbox', { name: 'high' })).toBeNull();
    await user.click(screen.getByRole('button', { name: 'Save model' }));

    expect(onCommit).toHaveBeenCalledWith(expect.objectContaining({
      supports_tools: true,
      supports_reasoning: false,
      reasoning_efforts: [],
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

    expect(screen.getAllByText('not set')).toHaveLength(2);
    await user.click(screen.getByRole('button', { name: 'Add model' }));
    expect(onCommit).toHaveBeenCalledWith(expect.objectContaining({
      supports_tools: null,
      supports_reasoning: null,
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
