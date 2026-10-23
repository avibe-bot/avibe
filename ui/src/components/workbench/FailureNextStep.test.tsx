/** @vitest-environment jsdom */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createInstance } from 'i18next';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { MemoryRouter, Route, Routes } from 'react-router-dom';

import type { WorkbenchMessage } from '../../context/ApiContext';
import { InstanceAuthorizationContext } from '../../context/InstanceAuthorizationContext';
import { DENIED_INSTANCE_CAPABILITIES } from '../../lib/sessionInfo';
import en from '../../i18n/en.json';
import { FailureNextStep } from './FailureNextStep';

const i18n = createInstance();
void i18n.use(initReactI18next).init({
  lng: 'en', resources: { en: { translation: en } }, interpolation: { escapeValue: false },
});
// The body is the server's plain text; the step rides as data beside it.
const refusal = {
  id: 'notice', session_id: 'session', type: 'notify', author: 'agent', source: 'agent',
  text: '❌ Vibey has no model source yet. Add an API key or a subscription in Models.', content: {},
  metadata: { event: 'backend_failure', failure_id: 'failed', turn_id: 'turn-1', failure_next_step: 'models' },
} as WorkbenchMessage;

type StepProps = Omit<Parameters<typeof FailureNextStep>[0], 'message'>;

function mount(message: WorkbenchMessage, canManage: boolean, props: StepProps = {}) {
  const authorization = {
    remote: true, instanceKind: null, instanceRole: null,
    capabilities: { ...DENIED_INSTANCE_CAPABILITIES, can_chat: true, can_manage_instance: canManage },
  };
  return render(
    <I18nextProvider i18n={i18n}>
      <InstanceAuthorizationContext.Provider value={authorization as never}>
        <MemoryRouter initialEntries={['/chat/session']}>
          <Routes>
            <Route path="/chat/session" element={<FailureNextStep message={message} {...props} />} />
            <Route path="/settings/models" element={<p>Models page</p>} />
          </Routes>
        </MemoryRouter>
      </InstanceAuthorizationContext.Provider>
    </I18nextProvider>,
  );
}

afterEach(cleanup);

describe('FailureNextStep', () => {
  it('opens Models for a caller who manages the instance', () => {
    mount(refusal, true);
    fireEvent.click(screen.getByRole('button', { name: 'Open Models' }));
    expect(screen.getByText('Models page')).toBeTruthy();
  });

  it('tells anyone else who can fix it, with no link', () => {
    mount(refusal, false);
    expect(screen.getByText('Ask an Avibe admin to fix this in Models.')).toBeTruthy();
    expect(screen.queryByRole('button')).toBeNull();
  });

  it('draws nothing for a notice without the step', () => {
    const { container } = mount({ ...refusal, metadata: { ...refusal.metadata, failure_next_step: undefined } }, true);
    expect(container.textContent).toBe('');
  });

  // The Agent the session runs on was disabled: the notice offers to move to Vibey.
  const agentOff = {
    ...refusal,
    text: '❌ Codex is turned off, so this session can\'t continue on it.',
    metadata: { ...refusal.metadata, failure_next_step: 'continue_on_vibey' },
  } as WorkbenchMessage;

  it('continues on Vibey through the notice-bound retry', async () => {
    const onRetry = vi.fn().mockResolvedValue(true);
    mount(agentOff, false, { onRetry });
    fireEvent.click(screen.getByRole('button', { name: 'Continue with Vibey' }));
    expect(onRetry).toHaveBeenCalledWith('notice', { continueOnBackend: 'vibey' });
    await waitFor(() => expect(screen.getByRole('button', { name: 'Switched to Vibey' })).toHaveProperty('disabled', true));
  });

  it('offers nothing on a read-only transcript, and settles from the durable retry state', () => {
    mount(agentOff, true, { readOnly: true, onRetry: vi.fn() });
    expect(screen.queryByRole('button')).toBeNull();
    cleanup();
    mount({ ...agentOff, content: { failure_retry: { state: 'accepted' } } } as WorkbenchMessage, true, { onRetry: vi.fn() });
    expect(screen.getByRole('button', { name: 'Switched to Vibey' })).toHaveProperty('disabled', true);
  });
});
