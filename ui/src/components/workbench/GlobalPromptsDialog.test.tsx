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

describe('native global prompts', () => {
  // Growing the Agent catalog must not invent a native prompt file for Avibe.
  // The sync payload is as important as the tabs: a hidden target is still a write.
  it('offers and synchronizes only native backend files', async () => {
    const files = ['claude', 'opencode', 'codex', 'avibe'].map((backend) => ({
      backend, content: 'Instructions', path: `/fixture/${backend}/AGENTS.md`,
      filename: 'AGENTS.md', exists: true, read_error: false,
    }));
    mocks.getGlobalPrompts.mockResolvedValue({ backends: files });
    mocks.saveGlobalPrompts.mockResolvedValue({ backends: files });
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    render(<GlobalPromptsDialog open onClose={vi.fn()} />);
    await screen.findByRole('tab', { name: 'Claude Code' });
    expect(screen.getAllByRole('tab').map((tab) => tab.textContent)).toEqual(['Claude Code', 'OpenCode', 'Codex']);
    expect(screen.queryByRole('tab', { name: 'Avibe Agent' })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'globalPrompts.sync' }));
    await waitFor(() => expect(mocks.saveGlobalPrompts).toHaveBeenCalledWith({
      content: 'Instructions', backends: ['claude', 'opencode', 'codex'],
    }));
  });
});
