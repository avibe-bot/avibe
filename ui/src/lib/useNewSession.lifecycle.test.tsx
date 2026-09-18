/* @vitest-environment jsdom */
import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import type { ComposerAttachment } from '../components/workbench/Composer';
const mocks = vi.hoisted(() => ({
  create: vi.fn(), upload: vi.fn(), fetch: vi.fn(), events: undefined as undefined | (() => void),
  projects: [
    { id: '中文项目', capabilities: { can_chat: true }, created_at: '2026-09-19T00:00:00Z' },
    { id: '其他项目', capabilities: { can_chat: true }, created_at: '2026-09-18T00:00:00Z' },
  ],
}));
const api = {
  listVibeAgents: async () => ({ agents: [{ id: 'a', name: 'codex', backend: 'codex', enabled: true }], default_agent_name: 'codex' }),
  connectWorkbenchEvents: ({ onAuthorizationChanged }: { onAuthorizationChanged: () => void }) => {
    mocks.events = onAuthorizationChanged;
    return () => {};
  },
};
vi.mock('../context/ApiContext', () => ({ useApi: () => api }));
vi.mock('../context/WorkbenchProjectsContext', () => ({ useWorkbenchProjectsTree: () => ({ projects: mocks.projects, projectsError: null, createSessionForProject: mocks.create }) }));
vi.mock('./apiFetch', () => ({ apiFetch: mocks.fetch }));
vi.mock('./workbenchUpload', async (original) => ({ ...await original<typeof import('./workbenchUpload')>(), uploadWorkbenchAttachment: mocks.upload }));
import { useNewSession } from './useNewSession';
const attachment: ComposerAttachment = { localId: 'file-id', file: new File(['内容'], '中文.txt'), token: '', name: '中文.txt', mime: 'text/plain', size: 6, kind: 'file', url: '', status: 'staged' };
const mount = () => renderHook(() => useNewSession({ loadErrorText: 'load failed', createFailedText: 'send failed' }));
beforeEach(() => {
  mocks.create.mockReset().mockImplementation(async () => ({ id: `session-${mocks.create.mock.calls.length}` }));
  mocks.upload.mockReset().mockResolvedValue({ token: 'token', name: '中文.txt', mime: 'text/plain', size: 6, kind: 'file', url: '/api/media/token' });
  mocks.fetch.mockReset().mockResolvedValue(new Response('{}', { status: 201 }));
});
afterEach(cleanup);
it('creates and submits once from a same-tick double send, only after explicit input', async () => {
  const { result } = mount();
  await waitFor(() => expect(result.current.loaded).toBe(true));
  expect(mocks.create).not.toHaveBeenCalled();
  await act(async () => { await Promise.all([result.current.send('', [attachment]), result.current.send('', [attachment])]); });
  expect(mocks.create).toHaveBeenCalledTimes(1);
  expect(mocks.fetch).toHaveBeenCalledTimes(1);
  expect(mocks.upload).toHaveBeenCalledWith('session-1', attachment.file, attachment.localId);
});
it('preserves successful uploads for retry but creates a fresh media scope when the selection changes', async () => {
  const { result } = mount();
  await waitFor(() => expect(result.current.loaded).toBe(true));
  mocks.fetch.mockResolvedValueOnce(new Response('{"state":"retired"}', { status: 502 }));
  await act(async () => { expect(await result.current.send('初稿', [attachment])).toBeNull(); });
  act(() => result.current.setSelected('其他项目'));
  await act(async () => { await result.current.send('初稿', [attachment]); });
  expect(mocks.create).toHaveBeenNthCalledWith(2, '其他项目', {});
  expect(mocks.upload).toHaveBeenNthCalledWith(2, 'session-2', attachment.file, attachment.localId);
});
it('captures the project and Agent at Send rather than reading later live selections', async () => {
  let release!: (session: { id: string }) => void;
  mocks.create.mockReturnValue(new Promise((resolve) => { release = resolve; }));
  const { result } = mount();
  await waitFor(() => expect(result.current.loaded).toBe(true));
  let submission!: ReturnType<typeof result.current.send>;
  act(() => { submission = result.current.send('中文', [attachment]); });
  act(() => result.current.setSelected('其他项目'));
  await act(async () => { release({ id: 'captured-session' }); await submission; });
  expect(mocks.create).toHaveBeenCalledWith('中文项目', {});
  expect(mocks.upload).toHaveBeenCalledWith('captured-session', attachment.file, attachment.localId);
  expect(mocks.fetch.mock.calls[0][0]).toBe('/api/sessions/captured-session/messages');
});
it('authorization changes terminate a pending submission before upload or dispatch', async () => {
  let release!: (session: { id: string }) => void;
  mocks.create.mockReturnValue(new Promise((resolve) => { release = resolve; }));
  const { result } = mount();
  await waitFor(() => expect(result.current.loaded).toBe(true));
  let submission!: ReturnType<typeof result.current.send>;
  act(() => { submission = result.current.send('中文', [attachment]); });
  await act(async () => { mocks.events?.(); });
  await act(async () => { release({ id: 'captured-session' }); expect(await submission).toBeNull(); });
  expect(mocks.upload).not.toHaveBeenCalled();
  expect(mocks.fetch).not.toHaveBeenCalled();
});
it.each(['network', 'pending'])('uncertain %s admission cannot be resubmitted', async (kind) => {
  const { result } = mount();
  await waitFor(() => expect(result.current.loaded).toBe(true));
  if (kind === 'network') mocks.fetch.mockRejectedValue(new Error('lost acknowledgment'));
  else mocks.fetch.mockResolvedValue(new Response('{"dispatch_error":"dispatch_pending"}', { status: 504 }));
  await act(async () => { expect(await result.current.send('中文', [attachment])).toBeNull(); });
  expect(result.current.uncertainSessionId).toBe('session-1');
  await act(async () => { expect(await result.current.send('中文', [attachment])).toBeNull(); });
  expect(mocks.fetch).toHaveBeenCalledTimes(1);
});

it('a fresh sheet open starts a new submission lifetime after an uncertain send', async () => {
  const { result, rerender } = renderHook(({ active }) => useNewSession({ active, loadErrorText: 'load failed', createFailedText: 'send failed' }), { initialProps: { active: true } });
  await waitFor(() => expect(result.current.loaded).toBe(true));
  mocks.fetch.mockRejectedValueOnce(new Error('lost acknowledgment'));
  await act(async () => { await result.current.send('第一条'); });
  expect(result.current.uncertainSessionId).toBe('session-1');
  rerender({ active: false });
  rerender({ active: true });
  await waitFor(() => expect(result.current.loaded).toBe(true));
  expect(result.current.uncertainSessionId).toBeNull();
  await act(async () => { expect(await result.current.send('新的对话')).toEqual({ sessionId: 'session-2' }); });
  expect(mocks.create).toHaveBeenCalledTimes(2);
});

it.each(['create', 'message'])('a response from the closed sheet %s cannot alter a new sheet lifetime', async (stage) => {
  let release!: (value: unknown) => void;
  const held = new Promise((resolve) => { release = resolve; });
  if (stage === 'create') mocks.create.mockReturnValueOnce(held);
  else mocks.fetch.mockReturnValueOnce(held);
  const { result, rerender } = renderHook(({ active }) => useNewSession({ active, loadErrorText: 'load failed', createFailedText: 'send failed' }), { initialProps: { active: true } });
  await waitFor(() => expect(result.current.loaded).toBe(true));
  let prior!: ReturnType<typeof result.current.send>;
  act(() => { prior = result.current.send('旧对话'); });
  await waitFor(() => expect(stage === 'create' ? mocks.create : mocks.fetch).toHaveBeenCalledTimes(1));
  rerender({ active: false });
  rerender({ active: true });
  await waitFor(() => expect(result.current.loaded).toBe(true));
  await act(async () => {
    release(stage === 'create' ? { id: 'old-session' } : new Response('{"dispatch_error":"dispatch_pending"}', { status: 504 }));
    expect(await prior).toBeNull();
  });
  expect(result.current.uncertainSessionId).toBeNull();
  await act(async () => { expect(await result.current.send('新对话')).not.toBeNull(); });
  expect(mocks.create).toHaveBeenCalledTimes(2);
});

it.each([403, 404, 409])('terminal message HTTP %s discards only the invalid scope, preserving the original file for retry', async (status) => {
  const { result } = mount();
  await waitFor(() => expect(result.current.loaded).toBe(true));
  mocks.fetch.mockResolvedValueOnce(new Response('{"error":"session unavailable"}', { status }));
  await act(async () => { expect(await result.current.send('重试', [attachment])).toBeNull(); });
  await act(async () => { expect(await result.current.send('重试', [attachment])).toEqual({ sessionId: 'session-2' }); });
  expect(mocks.upload).toHaveBeenNthCalledWith(2, 'session-2', attachment.file, attachment.localId);
});

it('a missing upload session gets a new scope on explicit retry', async () => {
  const { WorkbenchUploadError } = await import('./workbenchUpload');
  mocks.upload.mockRejectedValueOnce(new WorkbenchUploadError('session_not_found', 'missing', 404));
  const { result } = mount();
  await waitFor(() => expect(result.current.loaded).toBe(true));
  await act(async () => { expect(await result.current.send('', [attachment])).toBeNull(); });
  expect(mocks.fetch).not.toHaveBeenCalled();
  await act(async () => { expect(await result.current.send('', [attachment])).toEqual({ sessionId: 'session-2' }); });
  expect(mocks.upload).toHaveBeenNthCalledWith(2, 'session-2', attachment.file, attachment.localId);
});
