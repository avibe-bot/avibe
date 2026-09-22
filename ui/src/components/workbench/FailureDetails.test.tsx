/** @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createInstance } from 'i18next';
import { I18nextProvider, initReactI18next } from 'react-i18next';

import type { WorkbenchMessage } from '../../context/ApiContext';
import { ToastProvider } from '../../context/ToastProvider';
import zh from '../../i18n/zh.json';
import { ApiCallError, modelsApi } from '../settings/models/modelsApi';
import type { TurnProvenance } from '../settings/models/types';
import { FailureDetails } from './FailureDetails';

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
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

function mount(message: WorkbenchMessage = notice) {
  render(<I18nextProvider i18n={i18n}><ToastProvider><FailureDetails message={message} /></ToastProvider></I18nextProvider>);
}

describe('failed-turn upstream details', () => {
  it('stays hidden for a notice that is not a linked backend failure', () => {
    mount({ ...notice, metadata: { event: 'backend_failure' } } as WorkbenchMessage);
    expect(screen.queryByRole('button', { name: '查看详情' })).toBeNull();
  });

  it('expands to the upstream status, error code and reason for every attempt', async () => {
    const read = vi.spyOn(modelsApi, 'getTurnProvenance').mockResolvedValue(record);
    mount();
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '查看详情' })); });
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

  it('explains a turn that never went through the gateway instead of failing', async () => {
    vi.spyOn(modelsApi, 'getTurnProvenance').mockRejectedValue(
      new ApiCallError('provenance_unavailable', 'models.provenance.direct_mode'),
    );
    mount();
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '查看详情' })); });
    expect(screen.getByText('这一轮没有经过模型网关，所以没有上游记录。')).toBeTruthy();
  });

  it('offers a retry when the record cannot be read', async () => {
    const read = vi.spyOn(modelsApi, 'getTurnProvenance')
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce(record);
    mount();
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '查看详情' })); });
    expect(screen.getByRole('alert').textContent).toContain('详情读取失败');
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '重试' })); });
    expect(read).toHaveBeenCalledTimes(2);
    expect(screen.getByText('model_not_found')).toBeTruthy();
  });
});
