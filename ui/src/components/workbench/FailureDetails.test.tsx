/** @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createInstance } from 'i18next';
import { I18nextProvider, initReactI18next } from 'react-i18next';

import type { WorkbenchMessage } from '../../context/ApiContext';
import { InstanceAuthorizationContext } from '../../context/InstanceAuthorizationContext';
import { ToastProvider } from '../../context/ToastProvider';
import { DENIED_INSTANCE_CAPABILITIES } from '../../lib/sessionInfo';
import zh from '../../i18n/zh.json';
import { ApiCallError, modelsApi } from '../settings/models/modelsApi';
import type { TurnProvenance } from '../settings/models/types';
import { FailureDetails } from './FailureDetails';
import { resetFailureDetailsCache } from './failureDetailsReads';

const i18n = createInstance();
void i18n.use(initReactI18next).init({
  lng: 'zh', resources: { zh: { translation: zh } }, interpolation: { escapeValue: false },
});
const notice = {
  id: 'notice', session_id: 'session', type: 'notify', author: 'agent', source: 'agent',
  text: '模型连接中断', content: {},
  metadata: { event: 'backend_failure', failure_id: 'failed', turn_id: 'turn-1' },
} as WorkbenchMessage;

const record = {
  contract_version: 1,
  turn_id: 'turn-1',
  ts: '2026-09-23T08:00:00Z',
  agent: 'opencode',
  requested_model_id: 'grok-4.6',
  outcome: 'failed_terminal',
  failed_attempts: [
    { source_id: 'src_a', configured_model_id: 'grok-4.6', channel: 'hub', reason: 'rate_limited', http_status: 429 },
  ],
  served: null,
  canceled_attempt: null,
  terminal_error: {
    source_id: 'src_b', configured_model_id: 'grok-4.6-beta', channel: 'hub',
    reason: 'invalid_parameter', stream_started: false, http_status: 400, upstream_error_code: 'model_not_found',
  },
  model_supply_state: null,
  blockers: [{ source_id: 'src_c', model_id: 'grok-4.6', reason: 'source_missing' }],
} as unknown as TurnProvenance;

beforeEach(() => {
  vi.spyOn(modelsApi, 'listSources').mockResolvedValue([{ id: 'src_a', display_name: 'xAI 官方' }] as never);
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); resetFailureDetailsCache(); });

function mount(message: WorkbenchMessage | WorkbenchMessage[] = notice, canManage = true) {
  const authorization = {
    remote: true, instanceKind: null, instanceRole: null,
    capabilities: { ...DENIED_INSTANCE_CAPABILITIES, can_chat: true, can_manage_instance: canManage },
  };
  const messages = Array.isArray(message) ? message : [message];
  render(
    <I18nextProvider i18n={i18n}><ToastProvider>
      <InstanceAuthorizationContext.Provider value={authorization}>
        {messages.map((item) => <FailureDetails key={item.id} message={item} />)}
      </InstanceAuthorizationContext.Provider>
    </ToastProvider></I18nextProvider>,
  );
}

describe('failed-turn upstream details', () => {
  it('stays hidden for a notice that is not a linked backend failure', () => {
    const read = vi.spyOn(modelsApi, 'getTurnProvenance');
    mount({ ...notice, metadata: { event: 'backend_failure' } } as WorkbenchMessage);
    expect(screen.queryByRole('button', { name: '查看详情' })).toBeNull();
    expect(read).not.toHaveBeenCalled();
  });

  it('does not read Model Hub provenance for a chat-only role', () => {
    const read = vi.spyOn(modelsApi, 'getTurnProvenance');
    mount(notice, false);
    expect(screen.queryByRole('button', { name: '查看详情' })).toBeNull();
    expect(read).not.toHaveBeenCalled();
  });

  it('shares one Sources read across the notices of a transcript', async () => {
    vi.spyOn(modelsApi, 'getTurnProvenance').mockResolvedValue(record);
    const sources = vi.mocked(modelsApi.listSources);
    mount([notice, { ...notice, id: 'second', metadata: { ...notice.metadata, turn_id: 'turn-2' } } as WorkbenchMessage]);
    expect(await screen.findAllByRole('button', { name: '查看详情' })).toHaveLength(2);
    expect(sources).toHaveBeenCalledTimes(1);
  });

  it('expands to the upstream status, error code and reason for every attempt', async () => {
    const read = vi.spyOn(modelsApi, 'getTurnProvenance').mockResolvedValue(record);
    mount();
    fireEvent.click(await screen.findByRole('button', { name: '查看详情' }));
    expect(read).toHaveBeenCalledExactlyOnceWith('turn-1');
    expect(screen.getByText('上游 API 拒绝了 grok-4.6 的请求。')).toBeTruthy();
    expect(screen.getByText('xAI 官方 · grok-4.6')).toBeTruthy();
    expect(screen.getByText('429')).toBeTruthy();
    expect(screen.getByText('触发限流')).toBeTruthy();
    // A source the Sources list no longer names still shows its stable id.
    expect(screen.getByText('src_b · grok-4.6-beta')).toBeTruthy();
    expect(screen.getByText('400')).toBeTruthy();
    expect(screen.getByText('model_not_found')).toBeTruthy();
    expect(screen.getByText('请求参数被拒绝')).toBeTruthy();
    expect(screen.getByText(/供应商已不存在/)).toBeTruthy();
    expect(screen.getByText(/由上游供应商的 API 返回/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '收起详情' }));
    expect(screen.queryByText('429')).toBeNull();
  });

  it.each([
    new ApiCallError('provenance_unavailable', 'models.provenance.direct_mode'),
    new ApiCallError('provenance_unavailable', 'models.provenance.attribution_ambiguous'),
    new ApiCallError('turn_not_found', 'not found'),
    new Error('offline'),
  ])('offers no details when the record cannot be shown (%s)', async (error) => {
    const read = vi.spyOn(modelsApi, 'getTurnProvenance').mockRejectedValue(error);
    mount();
    await act(async () => { await Promise.resolve(); });
    expect(read).toHaveBeenCalledOnce();
    expect(screen.queryByRole('button', { name: '查看详情' })).toBeNull();
  });

  it('reads again once a live notice\'s Turn has been settled', async () => {
    vi.useFakeTimers();
    try {
      const read = vi.spyOn(modelsApi, 'getTurnProvenance')
        .mockRejectedValueOnce(new ApiCallError('turn_not_found', 'not found'))
        .mockResolvedValue(record);
      mount();
      await act(async () => { await Promise.resolve(); });
      expect(screen.queryByRole('button', { name: '查看详情' })).toBeNull();
      await act(async () => { await vi.advanceTimersByTimeAsync(1_000); });
      expect(read).toHaveBeenCalledTimes(2);
      expect(screen.getByRole('button', { name: '查看详情' })).toBeTruthy();
    } finally {
      vi.useRealTimers();
    }
  });

  it('names Avibe-side terminal failures and localizes every blocker reason', async () => {
    vi.spyOn(modelsApi, 'getTurnProvenance').mockResolvedValue({
      ...record,
      failed_attempts: [],
      terminal_error: { ...record.terminal_error, reason: 'engine_down', http_status: null, upstream_error_code: null },
      blockers: [{ source_id: 'src_a', model_id: 'grok-4.6', reason: 'cooldown' }],
    } as unknown as TurnProvenance);
    mount();
    fireEvent.click(await screen.findByRole('button', { name: '查看详情' }));
    expect(screen.getByText('grok-4.6 的请求失败了，原因见下方。')).toBeTruthy();
    expect(screen.queryByText(/上游 API 拒绝/)).toBeNull();
    expect(screen.queryByText(/由上游供应商的 API 返回/)).toBeNull();
    expect(screen.getByText(/冷却中/)).toBeTruthy();
  });
});
