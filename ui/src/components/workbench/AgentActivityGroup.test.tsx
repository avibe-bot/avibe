/** @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string) => key === 'chat.agentActivity.dayShort' ? 'd' : key,
  }),
}));

import { ActivityCard, ActivityChip } from './AgentActivityGroup';

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe('shell tool-call display', () => {
  it.each(['live', 'history'])('shows the command body and preserves the original in %s details', (surface) => {
    const command = "/bin/zsh -lc 'npm test'";
    const rows = [{
      id: 'shell-command',
      kind: 'tool_call' as const,
      text: `🔧 \`bash\` \`${JSON.stringify({ command, exit_code: 0, output: 'passed' })}\``,
      created_at: '2026-09-05T02:00:00Z',
    }];
    render(surface === 'live' ? (
      <ActivityCard
        rows={rows}
        startedAtMs={Date.now()}
        expanded
        onToggleExpanded={vi.fn()}
        showToolCalls
        onToggleTools={vi.fn()}
      />
    ) : (
      <ActivityChip
        group={{
          id: 'completed-turn', anchorMessageId: 'reply', anchorPosition: 'before',
          open: false, status: 'done', steps: 1, durationMs: 1000, rows,
        }}
        expanded
        loading={false}
        onToggle={vi.fn()}
        showToolCalls
        onToggleTools={vi.fn()}
      />
    ));

    const tool = screen.getByRole('button', { name: /^bash\s*npm test$/ });
    expect(tool.textContent).toBe('bashnpm test');
    expect(screen.queryByText(command)).toBeNull();

    fireEvent.click(tool);
    expect(screen.getByText(command)).toBeTruthy();
    expect(screen.getByText('passed')).toBeTruthy();
    expect(tool.textContent).toBe('bashnpm test');
  });
});

describe('ActivityCard display shortcut', () => {
  it('shows hour and day units when a live run crosses those boundaries', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-08-26T02:00:00Z'));
    const elapsedMs = (24 + 14) * 3_600_000 + 33 * 60_000 + 11_000;

    render(
      <ActivityCard
        rows={[]}
        startedAtMs={Date.now() - elapsedMs}
        expanded
        onToggleExpanded={vi.fn()}
        showToolCalls
        onToggleTools={vi.fn()}
      />,
    );

    expect(screen.getByText('1d 14:33:11')).toBeTruthy();
  });

  it('renders a compact neutral close control beside the existing header controls', () => {
    const onDisableActivity = vi.fn();
    render(
      <ActivityCard
        rows={[]}
        startedAtMs={Date.now()}
        expanded
        onToggleExpanded={vi.fn()}
        showToolCalls
        onToggleTools={vi.fn()}
        onDisableActivity={onDisableActivity}
      />,
    );

    const tools = screen.getByTitle('chat.agentActivity.hideTools');
    const close = screen.getByRole('button', { name: 'chat.agentActivity.disable' });
    const collapse = screen.getByRole('button', { name: 'chat.agentActivity.collapse' });
    expect(close.className).toContain('size-6');
    for (const sharedClass of ['border-border', 'bg-foreground/[0.04]', 'text-foreground/80']) {
      expect(tools.className).toContain(sharedClass);
      expect(close.className).toContain(sharedClass);
    }
    expect(close.nextElementSibling).toBe(collapse);

    fireEvent.click(close);
    expect(onDisableActivity).toHaveBeenCalledOnce();
  });
});

describe('ActivityChip completed duration', () => {
  it.each([
    ['collapsed', false],
    ['expanded', true],
  ])('uses the long-duration clock while %s', (_state, expanded) => {
    render(
      <ActivityChip
        group={{
          id: 'completed-turn',
          anchorMessageId: 'reply',
          anchorPosition: 'before',
          open: false,
          status: 'done',
          steps: 4,
          durationMs: (24 + 14) * 3_600_000 + 33 * 60_000 + 11_000,
          rows: [],
        }}
        expanded={expanded}
        loading={false}
        onToggle={vi.fn()}
        showToolCalls
        onToggleTools={vi.fn()}
      />,
    );

    expect(screen.getByText(/1d 14:33:11/)).toBeTruthy();
  });
});
