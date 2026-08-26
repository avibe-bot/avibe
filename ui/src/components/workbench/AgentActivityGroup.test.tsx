/** @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

import { ActivityCard } from './AgentActivityGroup';

afterEach(() => {
  cleanup();
});

describe('ActivityCard display shortcut', () => {
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
