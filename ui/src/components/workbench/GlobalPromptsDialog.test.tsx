/* @vitest-environment jsdom */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { GlobalPromptsDialog } from './GlobalPromptsDialog';

const mocks = vi.hoisted(() => ({
  getGlobalPrompts: vi.fn(),
  saveGlobalPrompts: vi.fn(),
}));
vi.mock('../../context/ApiContext', () => ({ useApi: () => mocks }));
vi.mock('../../context/ToastContext', () => ({ useToast: () => ({ showToast: vi.fn() }) }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
vi.mock('../ui/markdown-editor', () => ({ MarkdownEditor: () => null }));

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('global prompts', () => {
  const file = (backend: string) => ({
    backend, content: 'Instructions', path: `/fixture/${backend}/AGENTS.md`,
    filename: 'AGENTS.md', exists: true, read_error: false,
  });

  // The server owns which backends have a global file: Vibey's in the Avibe home, and each
  // native CLI's. The dialog lists exactly those, in catalog order with Vibey first, and the
  // sync payload covers every listed file and nothing else: a hidden target is still a write.
  it('offers and synchronizes the files the server lists, in catalog order', async () => {
    const files = ['claude', 'opencode', 'codex', 'vibey'].map(file);
    mocks.getGlobalPrompts.mockResolvedValue({ backends: files });
    mocks.saveGlobalPrompts.mockResolvedValue({ backends: files });
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    render(<GlobalPromptsDialog open onClose={vi.fn()} />);
    await screen.findByRole('tab', { name: 'Vibey' });
    expect(screen.getAllByRole('tab').map((tab) => tab.textContent)).toEqual(['Vibey', 'Claude Code', 'Codex', 'OpenCode']);
    expect(screen.getByRole('tab', { name: 'Vibey' }).getAttribute('aria-selected')).toBe('true');
    fireEvent.click(screen.getByRole('button', { name: 'globalPrompts.sync' }));
    await waitFor(() => expect(mocks.saveGlobalPrompts).toHaveBeenCalledWith({
      content: 'Instructions', backends: ['vibey', 'claude', 'codex', 'opencode'],
    }));
  });

  it('never invents a file the server does not list', async () => {
    const files = ['claude', 'codex', 'opencode'].map(file);
    mocks.getGlobalPrompts.mockResolvedValue({ backends: [...files, file('cursor')] });
    mocks.saveGlobalPrompts.mockResolvedValue({ backends: files });
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    render(<GlobalPromptsDialog open onClose={vi.fn()} />);
    await screen.findByRole('tab', { name: 'Claude Code' });
    expect(screen.getAllByRole('tab').map((tab) => tab.textContent)).toEqual(['Claude Code', 'Codex', 'OpenCode']);
    fireEvent.click(screen.getByRole('button', { name: 'globalPrompts.sync' }));
    await waitFor(() => expect(mocks.saveGlobalPrompts).toHaveBeenCalledWith({
      content: 'Instructions', backends: ['claude', 'codex', 'opencode'],
    }));
  });
});
