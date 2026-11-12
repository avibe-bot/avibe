// @vitest-environment jsdom
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { I18nextProvider } from 'react-i18next';
import { afterEach, describe, expect, it, vi } from 'vitest';

import i18n from '@/i18n';
import { ToastContext } from '@/context/ToastContext';
import { AddModelsDialog } from './AddModelsDialog';
import { ApiCallError, modelsApi } from './modelsApi';
import type { BackendModelCandidates } from './types';

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

const supply = { backend: 'claude', mode: 'hub', menu_kind: 'fixed', cli_present: true, catalog_models: [] };
const offering = (sourceId: string, name: string): BackendModelCandidates => ({
  builtin: [], in_list: [],
  providers: [{ id: 'opus-x', display_name: null, reasoning_efforts: [], origin: 'provider', suppliers: [{ source_id: sourceId, source_name: name, model_id: 'opus-x' }] }],
});

describe('AddModelsDialog', () => {
  it('asks again with today\'s suppliers when the ones it showed have changed', async () => {
    const user = userEvent.setup();
    vi.spyOn(modelsApi, 'getAgentSources').mockResolvedValue(supply as never);
    vi.spyOn(modelsApi, 'searchModelsDev').mockResolvedValue([]);
    const candidates = vi.spyOn(modelsApi, 'getAgentModelCandidates')
      .mockResolvedValueOnce(offering('src_a', 'Relay A'))
      .mockResolvedValue(offering('src_b', 'Relay B'));
    const write = vi.spyOn(modelsApi, 'putAgentModels')
      .mockRejectedValueOnce(new ApiCallError('candidate_suppliers_changed', 'modelHub.errors.candidate_suppliers_changed', true))
      .mockResolvedValue(supply as never);
    const onAdded = vi.fn();
    render(
      <I18nextProvider i18n={i18n}>
        <ToastContext.Provider value={{ showToast: vi.fn() }}>
          <AddModelsDialog open backend="claude" onClose={vi.fn()} onAdded={onAdded} />
        </ToastContext.Provider>
      </I18nextProvider>,
    );

    await user.click(await screen.findByRole('checkbox', { name: /opus-x/ }));
    await user.click(screen.getByRole('button', { name: 'Add 1 model' }));

    // Refused: the candidates are read again and the same model comes back picked,
    // now showing the supplier the server would seed.
    await waitFor(() => expect(candidates).toHaveBeenCalledTimes(2));
    const row = await screen.findByRole('checkbox', { name: /opus-x/ });
    await waitFor(() => expect(row.getAttribute('aria-checked')).toBe('true'));
    expect(within(row).getByText('Relay B')).toBeTruthy();
    expect(onAdded).not.toHaveBeenCalled();

    await user.click(screen.getByRole('button', { name: 'Add 1 model' }));
    await waitFor(() => expect(onAdded).toHaveBeenCalledWith(['opus-x'], supply));
    expect(write.mock.calls[1][1].expected_suppliers).toEqual({ 'opus-x': [{ source_id: 'src_b', model_id: 'opus-x' }] });
  });

  it('keeps the ticked rows across the custom-model editor: saved with it, still ticked after Cancel', async () => {
    const user = userEvent.setup();
    vi.spyOn(modelsApi, 'getAgentSources').mockResolvedValue(supply as never);
    vi.spyOn(modelsApi, 'searchModelsDev').mockResolvedValue([]);
    vi.spyOn(modelsApi, 'getAgentModelCandidates').mockResolvedValue(offering('src_a', 'Relay A'));
    const write = vi.spyOn(modelsApi, 'putAgentModels').mockResolvedValue(supply as never);
    const onAdded = vi.fn();
    render(
      <I18nextProvider i18n={i18n}>
        <ToastContext.Provider value={{ showToast: vi.fn() }}>
          <AddModelsDialog open backend="claude" onClose={vi.fn()} onAdded={onAdded} />
        </ToastContext.Provider>
      </I18nextProvider>,
    );

    await user.click(await screen.findByRole('checkbox', { name: /opus-x/ }));
    await user.click(screen.getByRole('button', { name: 'Add custom model…' }));
    const editor = () => screen.getByRole('dialog', { name: 'Add model' });
    await user.click(within(editor()).getByRole('button', { name: 'Cancel' }));
    expect(screen.getByRole('checkbox', { name: /opus-x/ }).getAttribute('aria-checked')).toBe('true');

    await user.click(screen.getByRole('button', { name: 'Add custom model…' }));
    await user.type(within(editor()).getByLabelText('Model'), 'my-finetune');
    await user.click(within(editor()).getByRole('button', { name: 'Add model' }));

    await waitFor(() => expect(onAdded).toHaveBeenCalled());
    expect(write.mock.calls[0][1].models.map((row: { id: string }) => row.id)).toEqual(['opus-x', 'my-finetune']);
  });
});
