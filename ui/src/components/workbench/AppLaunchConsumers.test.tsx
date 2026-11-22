/** @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { StrictMode, useState } from 'react';
import { MemoryRouter } from 'react-router-dom';
import { EditorApp } from './EditorApp';
import { TerminalTabs } from './TerminalTabs';
const api = vi.hoisted(() => ({ getAuthSession: vi.fn(async () => ({ remote: false })) }));
vi.mock('../../context/ApiContext', () => ({ useApi: () => api }));
vi.mock('../../lib/apiFetch', () => ({ apiFetch: vi.fn(async () => ({ ok: true })) }));
vi.mock('../../context/WindowManagerContext', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../context/WindowManagerContext')>()), useWindowManager: () => ({ setTitle: vi.fn() }), useWindowCloseGuard: vi.fn(), useWindowState: vi.fn() }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string, opts?: { n?: number }) => opts?.n ? `${key} ${opts.n}` : key }) }));
vi.mock('./FileTree', () => ({ FileTree: ({ rootPath }: { rootPath: string }) => <output data-testid="editor-root">{rootPath}</output> }));
vi.mock('./FilePicker', () => ({ FilePicker: () => null }));
vi.mock('./EditorSearchView', () => ({ EditorSearchView: () => null }));
vi.mock('./EditorFontSizePopover', () => ({ EditorFontSizePopover: () => null }));
vi.mock('./FileEditorPane', () => ({ FileEditorPane: ({ path, onDirtyChange }: { path: string | null; onDirtyChange: (dirty: boolean) => void }) => {
  const [text, setText] = useState('');
  return <textarea aria-label={path ?? 'untitled-buffer'} value={text} onChange={(event) => { setText(event.target.value); onDirtyChange(true); }} />;
} }));
vi.mock('./TerminalView', () => ({ TerminalView: ({ cwd, sessionId }: { cwd?: string; sessionId: string }) => <output data-testid="terminal" data-session={sessionId}>{cwd ?? 'default'}</output> }));
afterEach(() => { cleanup(); localStorage.clear(); vi.clearAllMocks(); });
describe('W1 reused App bodies consume launch state without discarding work', () => {
  it('adds repeated Editor files and new-file launches while preserving an unsaved buffer', async () => {
    const { rerender } = render(<MemoryRouter><EditorApp params={{ newFileDir: '/fixture', launchRequestId: 'one' }} /></MemoryRouter>);
    const draft = await screen.findByRole('textbox', { name: 'untitled-buffer' });
    fireEvent.change(draft, { target: { value: 'keep my draft' } });
    rerender(<MemoryRouter><EditorApp params={{ path: '/fixture/two.txt', filename: 'two.txt', rootDir: '/second-project', mtime: 1, launchRequestId: 'two' }} /></MemoryRouter>);
    expect(await screen.findByRole('textbox', { name: '/fixture/two.txt' })).toBeTruthy();
    expect(screen.getByTestId('editor-root').textContent).toBe('/second-project');
    expect(draft.isConnected).toBe(true);
    expect(document.body.contains(draft)).toBe(true);
    expect((draft as HTMLTextAreaElement).value).toBe('keep my draft');
    rerender(<MemoryRouter><EditorApp params={{ newFileDir: '/fixture', launchRequestId: 'three' }} /></MemoryRouter>);
    expect(await screen.findAllByRole('textbox', { name: 'untitled-buffer' })).toHaveLength(2);
    rerender(<MemoryRouter><EditorApp params={{ newFileDir: '/fixture', launchRequestId: 'three' }} /></MemoryRouter>);
    expect(screen.getAllByRole('textbox', { name: 'untitled-buffer' })).toHaveLength(2);
    expect(draft.isConnected).toBe(true);
    fireEvent.change(draft, { target: { value: 'still editable' } });
    expect((draft as HTMLTextAreaElement).value).toBe('still editable');
  });
  it('delivers repeated cwd to the real Terminal tab owner and retains prior sessions', async () => {
    const { rerender } = render(<StrictMode><TerminalTabs windowed windowId="native-one" params={{ cwd: '/one', launchRequestId: 'one' }} /></StrictMode>);
    const first = await screen.findByTestId('terminal');
    const firstId = first.getAttribute('data-session');
    await act(async () => rerender(<StrictMode><TerminalTabs windowed windowId="native-one" params={{ cwd: '/two', launchRequestId: 'two' }} /></StrictMode>));
    expect(screen.getAllByTestId('terminal')).toHaveLength(2);
    expect(screen.getByText('/two').getAttribute('data-session')).not.toBe(firstId);
    expect(first.getAttribute('data-session')).toBe(firstId);
    await act(async () => rerender(<StrictMode><TerminalTabs windowed windowId="native-one" params={{ cwd: '/two', launchRequestId: 'two' }} /></StrictMode>));
    expect(screen.getAllByTestId('terminal')).toHaveLength(2);
  });
});
