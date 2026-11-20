/* @vitest-environment jsdom */
import { describe, expect, it, vi } from 'vitest';
import type { ComposeTarget } from '../context/ComposerBridgeContext';
import { createReceiver, type ReceiverDeps } from './desktopComposeReceiver';

// Ask Vibey's page side. Native (desktop/src-tauri/src/compose.rs) begins a
// share, streams file chunks, commits, then polls status; nothing is sent.
// ui/design: composer/ask-vibey/inserted-zh, composer/ask-vibey/appended-zh

const b64 = (text: string) => btoa(String.fromCharCode(...new TextEncoder().encode(text)));

function vibey(overrides: Partial<ComposeTarget> = {}) {
  const lines: string[] = [];
  const files: File[][] = [];
  const target: ComposeTarget = {
    sessionId: 'ses_main',
    composerReady: true,
    showChat: vi.fn(),
    appendLine: vi.fn((text: string) => { lines.push(text); return true; }),
    addFiles: vi.fn((added: File[]) => { files.push(added); return true; }),
    ...overrides,
  };
  return { target, lines, files };
}

function harness(initial: ComposeTarget | null) {
  let current = initial;
  const toast = vi.fn();
  const openVibey = vi.fn();
  const deps: ReceiverDeps = { target: () => current, openVibey, toast, wait: async () => { await Promise.resolve(); } };
  const receiver = createReceiver(deps);
  const settle = async (id: string) => {
    for (let i = 0; i < 1000 && receiver.status(id) === 'inserting'; i++) await new Promise((resolve) => setTimeout(resolve, 0));
    return receiver.status(id);
  };
  return { receiver, toast, openVibey, settle, set: (next: ComposeTarget | null) => { current = next; } };
}

describe('Ask Vibey receiver', () => {
  it('quotes text and keeps a link bare, each appended on its own line in arrival order', async () => {
    const { target, lines } = vibey();
    const { receiver, settle, openVibey } = harness(target);
    expect(receiver.begin({ id: 'a', kind: 'text', text: '  乙方应在 15 个工作日内提交报告。 ', files: [] })).toBe('ok');
    expect(receiver.begin({ id: 'b', kind: 'url', text: 'https://example.com/terms', files: [] })).toBe('ok');
    expect(receiver.begin({ id: 'c', kind: 'text', text: 'The Supplier shall deliver.', files: [] })).toBe('ok');
    expect(receiver.status('a')).toBe('receiving');
    for (const id of ['a', 'b', 'c']) expect(receiver.commit(id)).toBe('ok');
    expect(await settle('c')).toBe('done');
    expect(lines).toEqual(['「乙方应在 15 个工作日内提交报告。」', 'https://example.com/terms', '"The Supplier shall deliver."']);
    expect(openVibey).toHaveBeenCalledTimes(3);
    expect(target.addFiles).not.toHaveBeenCalled();
  });

  it('rebuilds files from chunks and stages them as composer attachments; refused files use existing copy', async () => {
    const { target, files } = vibey();
    const { receiver, settle, toast } = harness(target);
    const contract = '合同正文 v3';
    const size = new TextEncoder().encode(contract).length;
    expect(receiver.begin({ id: 'f', kind: 'files', files: [
      { name: '合同.pdf', type: 'application/pdf', size },
      { name: 'movie.mov', refused: 'too_large' },
      { name: 'empty.txt', refused: 'empty' },
    ] })).toBe('ok');
    const bytes = new TextEncoder().encode(contract);
    expect(receiver.chunk('f', 0, btoa(String.fromCharCode(...bytes.slice(0, 5))))).toBe('ok');
    expect(receiver.commit('f')).toBe('invalid');
    expect(receiver.chunk('f', 0, btoa(String.fromCharCode(...bytes.slice(5))))).toBe('ok');
    expect(receiver.chunk('f', 0, b64('x'))).toBe('invalid');
    expect(receiver.chunk('f', 1, b64('x'))).toBe('invalid');
    expect(receiver.commit('f')).toBe('ok');
    expect(await settle('f')).toBe('done');
    expect(files).toHaveLength(1);
    const [file] = files[0];
    expect([file.name, file.type, file.size]).toEqual(['合同.pdf', 'application/pdf', size]);
    expect(new TextDecoder().decode(new Uint8Array(await file.arrayBuffer()))).toBe(contract);
    expect(target.appendLine).not.toHaveBeenCalled();
    expect(toast.mock.calls.map(([key]) => key)).toEqual(['chat.compose.attachmentTooLarge', 'chat.compose.attachmentEmpty']);
  });

  it('opens Vibey from another conversation and inserts once its composer is there', async () => {
    const other = { ...vibey().target, sessionId: 'ses_other' };
    const { target, lines } = vibey();
    const { receiver, settle, openVibey, set } = harness(other);
    // The route changes a few ticks after the request, as navigation does.
    openVibey.mockImplementation(() => { void Promise.resolve().then(() => Promise.resolve()).then(() => set(target)); });
    receiver.begin({ id: 'a', kind: 'text', text: 'hello', files: [] });
    receiver.commit('a');
    expect(await settle('a')).toBe('done');
    expect(other.appendLine).not.toHaveBeenCalled();
    expect(lines).toEqual(['"hello"']);
  });

  it('switches Vibey from its Show Page back to the conversation first', async () => {
    const ready = vibey();
    const covered = vibey({ composerReady: false, appendLine: vi.fn(() => false) });
    const { receiver, settle, set } = harness(covered.target);
    (covered.target.showChat as ReturnType<typeof vi.fn>).mockImplementation(() => set(ready.target));
    receiver.begin({ id: 'a', kind: 'url', text: 'https://example.com', files: [] });
    receiver.commit('a');
    expect(await settle('a')).toBe('done');
    expect(covered.target.showChat).toHaveBeenCalledOnce();
    expect(ready.lines).toEqual(['https://example.com']);
  });

  it('waits for a composer that is still mounting instead of dropping the share', async () => {
    let calls = 0;
    const mounting = vibey({ appendLine: vi.fn(() => ++calls > 3) });
    const { receiver, settle } = harness(mounting.target);
    receiver.begin({ id: 'a', kind: 'text', text: 'x', files: [] });
    receiver.commit('a');
    expect(await settle('a')).toBe('done');
    expect(calls).toBe(4);
  });

  it('reports unavailable when Vibey’s conversation never opens on this instance', async () => {
    const { receiver, settle } = harness(null);
    receiver.begin({ id: 'a', kind: 'text', text: 'x', files: [] });
    receiver.commit('a');
    expect(await settle('a')).toBe('unavailable');
  });

  it('refuses malformed shares and unknown ids', () => {
    const { receiver } = harness(vibey().target);
    expect(receiver.begin({ id: 'a', kind: 'text', text: '', files: [] })).toBe('invalid');
    expect(receiver.begin({ id: 'a', kind: 'files', files: [] })).toBe('invalid');
    expect(receiver.begin({ id: 'a', kind: 'files', files: [{ name: 'a', type: '', size: 104857601 }] })).toBe('invalid');
    expect(receiver.begin({ id: 'a', kind: 'url', text: 'x', files: [{ name: 'a', type: '', size: 1 }] })).toBe('invalid');
    expect(receiver.begin({ id: 'a', kind: 'shell', files: [] })).toBe('invalid');
    expect(receiver.commit('nope')).toBe('invalid');
    expect(receiver.status('nope')).toBe('unknown');
    expect(receiver.chunk('nope', 0, b64('x'))).toBe('invalid');
  });

  it('reports an unreadable selection with the existing attachment copy', async () => {
    const { target } = vibey();
    const { receiver, settle, toast } = harness(target);
    receiver.begin({ id: 'u', kind: 'unreadable', files: [] });
    receiver.commit('u');
    expect(await settle('u')).toBe('done');
    expect(toast).toHaveBeenCalledWith('chat.compose.attachmentFailed');
  });

  it('answers a repeated begin after commit with where the share is, never inserting it twice', async () => {
    const { target, lines } = vibey();
    const { receiver, settle } = harness(target);
    receiver.begin({ id: 'a', kind: 'text', text: 'once', files: [] });
    receiver.commit('a');
    expect(await settle('a')).toBe('done');
    expect(receiver.begin({ id: 'a', kind: 'text', text: 'once', files: [] })).toBe('done');
    expect(receiver.commit('a')).toBe('ok');
    expect(lines).toEqual(['"once"']);
  });

  it('carries a document identity of its own, different for every installed receiver', () => {
    const one = harness(vibey().target).receiver;
    const two = harness(vibey().target).receiver;
    expect(one.document).toMatch(/^[0-9a-f-]{36}$/);
    expect(one.document).not.toBe(two.document);
  });

  // While the composer refuses files (voice capture owns the draft, a send in
  // flight), the share waits; it is never reported done with nothing staged.
  it('waits for the composer to take files and reports unavailable if it never does', async () => {
    const share = (id: string) => ({ id, kind: 'files', files: [{ name: 'a.txt', type: 'text/plain', size: 1 }] });
    let refusals = 3;
    const busy = vibey({ addFiles: vi.fn(() => --refusals < 0) });
    const late = harness(busy.target);
    late.receiver.begin(share('a'));
    late.receiver.chunk('a', 0, b64('x'));
    late.receiver.commit('a');
    expect(await late.settle('a')).toBe('done');
    expect((busy.target.addFiles as ReturnType<typeof vi.fn>).mock.calls).toHaveLength(4);

    const locked = vibey({ addFiles: vi.fn(() => false) });
    const never = harness(locked.target);
    never.receiver.begin(share('b'));
    never.receiver.chunk('b', 0, b64('x'));
    never.receiver.commit('b');
    expect(await never.settle('b')).toBe('unavailable');
  });
});
