/** @vitest-environment jsdom */
import { act, cleanup, renderHook } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { useLiveActivityObservation } from './useLiveActivityObservation';

const observation = { sessionId: 'ses_a', agentName: '首席安全官', sessionTitle: '依赖审计 🔒', running: true };
function shell(capabilities: Record<string, number>) {
  const call = vi.fn(async (_method: string, _params?: Record<string, unknown>) => ({ ok: true }));
  vi.stubGlobal('avibeNative', { contract: 1, platform: 'ios', shellVersion: 'test', capabilities, call, on: () => () => {} });
  return call;
}
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it.each([{}, { liveActivities: 1 }, { liveActivities: 2 }, { lockScreenProgress: 0 }])('does not observe with capabilities %j', (capabilities) => {
  const call = shell(capabilities);
  const view = renderHook(() => useLiveActivityObservation(observation));
  view.unmount();
  expect(call).not.toHaveBeenCalled();
});

it('observes changed fields once, preserves Unicode and clears on departure', () => {
  const call = shell({ lockScreenProgress: 1 });
  const view = renderHook((props) => useLiveActivityObservation(props), { initialProps: observation });
  expect(call).toHaveBeenCalledExactlyOnceWith('liveActivity.observe', observation);
  view.rerender({ ...observation });
  expect(call).toHaveBeenCalledTimes(1);
  for (const change of [{ running: false }, { sessionTitle: '另一个标题' }, { agentName: 'Vibey' }, { sessionId: 'ses_b' }]) {
    const next = { ...observation, ...change };
    view.rerender(next);
    expect(call).toHaveBeenLastCalledWith('liveActivity.observe', next);
    const count = call.mock.calls.length;
    view.rerender({ ...next });
    expect(call).toHaveBeenCalledTimes(count);
  }
  view.unmount();
  expect(call).toHaveBeenLastCalledWith('liveActivity.observe', { sessionId: null });
  expect(call).toHaveBeenCalledTimes(6);
});

it('deduplicates a cleared observation and accepts a Bot without a title', async () => {
  const call = shell({ lockScreenProgress: 1 });
  const view = renderHook((props: Parameters<typeof useLiveActivityObservation>[0]) => useLiveActivityObservation(props), {
    initialProps: { ...observation, sessionTitle: null },
  });
  view.rerender({ sessionId: null });
  view.rerender({ sessionId: null });
  view.unmount();
  await act(async () => {});
  expect(call.mock.calls.map((args) => args[1])).toEqual([{ ...observation, sessionTitle: null }, { sessionId: null }]);
});
