// @vitest-environment jsdom
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { I18nextProvider } from 'react-i18next';
import { afterEach, describe, expect, it, vi } from 'vitest';
import i18n from '@/i18n';
import { modelsApi } from './modelsApi';
import { RouteRecordedTurn } from './RouteRecordedTurn';
import { PERSISTED_TURN_CONTRACT_VERSIONS } from './types';
import type { TurnProvenance } from './types';

const record: TurnProvenance = {
  contract_version: 10, turn_id: 'turn-recorded', ts: '2026-09-05T15:00:00Z', agent: 'codex', requested_model_id: 'requested-model',
  outcome: 'failed_terminal', failed_attempts: [], served: null, canceled_attempt: null, model_supply_state: null, blockers: [],
  terminal_error: { source_id: 'src_historical', configured_model_id: 'historical-model', channel: 'hub', reason: 'invalid_parameter', stream_started: false, http_status: 404, upstream_error_code: 'model_not_found' },
};
const view = (modelId = 'requested-model') => <I18nextProvider i18n={i18n}><RouteRecordedTurn backend="codex" modelId={modelId} /></I18nextProvider>;
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe('recorded turn error', () => {
  it('renders exact historical identity and opens that same structured record', async () => {
    const user = userEvent.setup();
    vi.spyOn(modelsApi, 'getAgentProvenance').mockResolvedValue(record);
    render(view());
    expect(await screen.findByText(/Latest recorded turn.*Upstream returned model not found/)).toBeTruthy();
    expect(screen.getByText('src_historical · historical-model')).toBeTruthy();
    expect(document.querySelector('time')?.dateTime).toBe(record.ts);
    await user.click(screen.getByRole('button', { name: 'View error details' }));
    expect(screen.getByRole('dialog').querySelector('pre')?.textContent).toBe(JSON.stringify(record, null, 2));
  });
  it.each(PERSISTED_TURN_CONTRACT_VERSIONS)('reads v%s terminal errors without rewriting records or inventing model-not-found evidence', async (version) => {
    const user = userEvent.setup();
    const historical: TurnProvenance = { ...record, contract_version: version, terminal_error: { source_id: 'src_old', configured_model_id: 'old-model', channel: 'hub', reason: 'invalid_parameter', stream_started: false } };
    vi.spyOn(modelsApi, 'getAgentProvenance').mockResolvedValue(historical);
    render(view());
    expect(await screen.findByText(/Latest recorded turn.*Request parameters rejected/)).toBeTruthy();
    expect(screen.queryByText(/model not found/)).toBeNull();
    await user.click(screen.getByRole('button', { name: 'View error details' }));
    expect(screen.getByRole('dialog').querySelector('pre')?.textContent).toBe(JSON.stringify(historical, null, 2));
  });
  it.each(['served', 'canceled', null] as const)('does not show an old error when latest outcome is %s', async (outcome) => {
    const read = vi.spyOn(modelsApi, 'getAgentProvenance').mockResolvedValue(outcome ? { ...record, outcome, terminal_error: null } : null);
    render(view());
    await act(async () => { await read.mock.results[0].value; });
    expect(document.querySelector('.model-hub-recorded-turn')).toBeNull();
  });
  it('retries a failed read and ignores a previous model read that resolves late', async () => {
    const user = userEvent.setup();
    let resolve!: (value: TurnProvenance) => void;
    const read = vi.spyOn(modelsApi, 'getAgentProvenance')
      .mockRejectedValueOnce(new Error('offline'))
      .mockReturnValueOnce(new Promise((done) => { resolve = done; }))
      .mockResolvedValueOnce(null);
    const page = render(view());
    await user.click(await screen.findByRole('button', { name: 'Retry' }));
    page.rerender(view('other-model'));
    await waitFor(() => expect(read).toHaveBeenCalledWith('codex', 'other-model'));
    await act(async () => resolve(record));
    expect(screen.queryByText(/Latest recorded turn/)).toBeNull();
  });
});
