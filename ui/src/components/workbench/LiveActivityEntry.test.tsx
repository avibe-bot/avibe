/** @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ToastContext } from '../../context/ToastContext';
import en from '../../i18n/en.json';
import { LiveActivityEntry } from './LiveActivityEntry';

vi.mock('react-i18next', () => ({ useTranslation: () => ({
  t: (key: string) => en.liveActivity[key.replace('liveActivity.', '') as keyof typeof en.liveActivity],
}) }));
const toast = vi.fn();
let status = { enabled: true, tracking: false };
let startResult: { ok: boolean; code?: string } = { ok: true };
let call: ReturnType<typeof vi.fn>;
const props = { sessionId: 'ses_security', agentName: '首席安全官', sessionTitle: '依赖审计 · avault 🔒', running: true };
const entry = (overrides = {}) => <ToastContext.Provider value={{ showToast: toast }}><LiveActivityEntry {...props} {...overrides} /></ToastContext.Provider>;

beforeEach(() => {
  status = { enabled: true, tracking: false };
  startResult = { ok: true };
  call = vi.fn(async (method: string) => {
    if (method === 'liveActivity.status') return { ...status };
    if (method === 'liveActivity.start') { if (startResult.ok) status.tracking = true; return startResult; }
    if (method === 'liveActivity.stop') status.tracking = false;
    return { ok: true };
  });
  Object.defineProperty(window, 'avibeNative', { configurable: true, value: {
    contract: 1, platform: 'ios', shellVersion: 'test', capabilities: { liveActivities: 1 }, call, on: () => () => {},
  } });
});
afterEach(() => { cleanup(); Reflect.deleteProperty(window, 'avibeNative'); vi.clearAllMocks(); });

describe('the in-conversation Live Activity entry', () => {
  it('sends the displayed non-ASCII title only at start, then stops explicitly', async () => {
    const view = render(entry());
    fireEvent.click(await screen.findByText(en.liveActivity.offer));
    await screen.findByText(en.liveActivity.tracking);
    expect(call).toHaveBeenCalledWith('liveActivity.start', { sessionId: props.sessionId, agentName: props.agentName, sessionTitle: props.sessionTitle });
    view.rerender(entry({ sessionTitle: 'Renamed' }));
    expect(call.mock.calls.filter(([method]) => method === 'liveActivity.start')).toHaveLength(1);
    fireEvent.click(screen.getByText(en.liveActivity.stop));
    await screen.findByText(en.liveActivity.offer);
    expect(call).toHaveBeenCalledWith('liveActivity.stop', { sessionId: props.sessionId });
  });

  it('does not render or call the bridge without a capability or a running Turn', async () => {
    const view = render(entry({ running: false }));
    expect(view.container.textContent).toBe('');
    Reflect.deleteProperty(window, 'avibeNative');
    view.rerender(entry());
    expect(view.container.textContent).toBe('');
    expect(call).not.toHaveBeenCalled();
  });

  it('opens Settings while disabled and refreshes on foreground return', async () => {
    status.enabled = false;
    render(entry());
    fireEvent.click(await screen.findByText(en.liveActivity.settings));
    await waitFor(() => expect(call).toHaveBeenCalledWith('liveActivity.openSettings', {}));
    expect(call.mock.calls.some(([method]) => method === 'liveActivity.start')).toBe(false);
    status.enabled = true;
    await act(async () => { document.dispatchEvent(new Event('visibilitychange')); });
    await screen.findByText(en.liveActivity.offer);
  });

  it.each(['no_active_turn', 'push_unavailable', 'failed'])('shows the approved %s refusal without claiming tracking', async (code) => {
    startResult = { ok: false, code };
    render(entry());
    fireEvent.click(await screen.findByText(en.liveActivity.offer));
    await waitFor(() => expect(toast).toHaveBeenCalledWith(en.liveActivity[code as keyof typeof en.liveActivity], code === 'failed' ? 'error' : 'warning'));
    expect(screen.queryByText(en.liveActivity.tracking)).toBeNull();
  });

  it('turns a disabled refusal into the Settings state without a toast', async () => {
    startResult = { ok: false, code: 'disabled' };
    render(entry({ sessionTitle: null }));
    fireEvent.click(await screen.findByText(en.liveActivity.offer));
    await screen.findByText(en.liveActivity.systemOff);
    expect(toast).not.toHaveBeenCalled();
    expect(call).toHaveBeenCalledWith('liveActivity.start', { sessionId: props.sessionId, agentName: props.agentName, sessionTitle: null });
  });
});
