/** @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createInstance } from 'i18next';
import { I18nextProvider, initReactI18next } from 'react-i18next';

import type { WorkbenchMessage } from '../../context/ApiContext';
import zh from '../../i18n/zh.json';
import { FailureRetry } from './FailureRetry';

const i18n = createInstance();
void i18n.use(initReactI18next).init({
  lng: 'zh', resources: { zh: { translation: zh } }, interpolation: { escapeValue: false },
});
const notice = {
  id: 'notice', session_id: 'session', type: 'notify', author: 'agent', source: 'agent',
  text: '模型连接中断', content: {},
  metadata: { event: 'backend_failure', failure_id: 'failed', turn_id: 'turn' },
} as WorkbenchMessage;

afterEach(cleanup);

function mount(props: Partial<Parameters<typeof FailureRetry>[0]> = {}) {
  const onRetry = vi.fn().mockResolvedValue(true);
  render(
    <I18nextProvider i18n={i18n}>
      <FailureRetry message={notice} onRetry={onRetry} {...props} />
    </I18nextProvider>,
  );
  return onRetry;
}

describe('failed-turn retry action', () => {
  it('locks a click burst and reports only an admitted retry', async () => {
    let finish!: (value: boolean) => void;
    const pending = new Promise<boolean>((resolve) => { finish = resolve; });
    const onRetry = vi.fn().mockReturnValue(pending);
    mount({ onRetry });
    const button = screen.getByRole('button', { name: '重试' });
    fireEvent.click(button);
    fireEvent.click(button);
    expect(onRetry).toHaveBeenCalledExactlyOnceWith('notice');
    expect((button as HTMLButtonElement).disabled).toBe(true);
    await act(async () => { finish(true); });
    expect(screen.getByRole('button', { name: '已请求重试' })).toBeTruthy();
  });

  it('unlocks after an unsuccessful submission', async () => {
    const onRetry = vi.fn().mockResolvedValueOnce(false).mockResolvedValueOnce(true);
    mount({ onRetry });
    await act(async () => { fireEvent.click(screen.getByRole('button')); });
    expect((screen.getByRole('button') as HTMLButtonElement).disabled).toBe(false);
    await act(async () => { fireEvent.click(screen.getByRole('button')); });
    expect(onRetry).toHaveBeenCalledTimes(2);
  });

  it.each(['queued', 'claimed', 'accepted'])('restores %s feedback on reload', (state) => {
    mount({ message: { ...notice, content: { failure_retry: { delivery_id: 'delivery', state } } } });
    expect((screen.getByRole('button', { name: '已请求重试' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it.each([
    { readOnly: true },
    { message: { ...notice, metadata: { ...notice.metadata, event: 'progress' } } },
    { message: { ...notice, metadata: { ...notice.metadata, detached: true } } },
    { message: { ...notice, metadata: { event: 'backend_failure' } } },
    { message: { ...notice, author: 'user' } as WorkbenchMessage },
  ])('does not offer an ineligible action', (props) => {
    mount(props);
    expect(screen.queryByRole('button')).toBeNull();
  });
});
