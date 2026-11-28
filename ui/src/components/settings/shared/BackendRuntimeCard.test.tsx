/* @vitest-environment jsdom */

import { render, screen } from '@testing-library/react';
import { Sparkles } from 'lucide-react';
import { describe, expect, it, vi } from 'vitest';

import { BackendRuntimeCard } from './BackendRuntimeCard';
import type { BackendRuntimeState } from './useBackendRuntime';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

vi.mock('../BackendLifecycleChip', () => ({
  BackendLifecycleChip: () => null,
}));

function makeRuntime(cliStatus: BackendRuntimeState['cliStatus']): BackendRuntimeState {
  return {
    loaded: true,
    enabled: false,
    cliPath: 'agy',
    cliStatus,
    detectedRuntime: null,
    detecting: false,
    installing: false,
    installResult: null,
    runtimeDirty: false,
    savingRuntime: false,
    connectionRevision: 0,
    setCliPath: vi.fn(),
    detect: vi.fn(),
    install: vi.fn(),
    toggleEnabled: vi.fn(),
    onSaveRuntime: vi.fn(),
    handleLifecycleChanged: vi.fn(),
  };
}

describe('BackendRuntimeCard Antigravity CLI vs desktop app note', () => {
  it('shows the CLI-vs-app note only in Antigravity not-installed state', () => {
    const { rerender } = render(
      <BackendRuntimeCard
        backend="antigravity"
        label="Antigravity"
        description="desc"
        Icon={Sparkles}
        iconTileClassName="bg-cyan-soft"
        iconClassName="text-cyan-ink"
        runtime={makeRuntime('missing')}
      />,
    );
    expect(screen.getByText('agentDetection.antigravityCliNote')).toBeTruthy();

    rerender(
      <BackendRuntimeCard
        backend="antigravity"
        label="Antigravity"
        description="desc"
        Icon={Sparkles}
        iconTileClassName="bg-cyan-soft"
        iconClassName="text-cyan-ink"
        runtime={makeRuntime('broken')}
      />,
    );
    expect(screen.queryByText('agentDetection.antigravityCliNote')).toBeNull();

    rerender(
      <BackendRuntimeCard
        backend="claude"
        label="Claude Code"
        description="desc"
        Icon={Sparkles}
        iconTileClassName="bg-cyan-soft"
        iconClassName="text-cyan-ink"
        runtime={makeRuntime('missing')}
      />,
    );
    expect(screen.queryByText('agentDetection.antigravityCliNote')).toBeNull();
  });
});
