/* @vitest-environment jsdom */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

import { SettingsMessagingPage } from './SettingsMessagingPage';
import {
  InstanceAuthorizationContext,
  type InstanceAuthorizationValue,
} from '../../context/InstanceAuthorizationContext';
import { OWNER_INSTANCE_CAPABILITIES } from '../../lib/sessionInfo';

const api = vi.hoisted(() => ({
  getConfig: vi.fn(),
  saveConfig: vi.fn(),
}));

const translate = vi.hoisted(() => (key: string) => key);

vi.mock('../../context/ApiContext', async (loadOriginal) => {
  const original = await loadOriginal<typeof import('../../context/ApiContext')>();
  return { ...original, useApi: () => api };
});

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: translate }),
}));

vi.mock('./SettingsPageShell', () => ({
  SettingsPageShell: ({ children }: { children: React.ReactNode }) => children,
}));

const baseConfig = {
  platforms: { enabled: ['slack'] },
  ack_mode: 'typing',
  show_duration: true,
  include_time_info: true,
  include_user_info: false,
  reply_enhancements: true,
  show_pages_prompt: true,
  agent_progress_style: 'off',
  audio_asr: { enabled: true, echo_transcript: true, enabled_configured: true },
  remote_access: { vibe_cloud: { enabled: true, instance_id: 'inst_123' } },
  ui: { chat_message_font_size: 14, show_agent_activity: false, show_tool_calls: true },
  slack: { disable_link_unfurl: false },
  agents: {
    opencode: {
      error_retry_limit: 2,
      active_turn_timeout_seconds: 5400,
    },
  },
};

const localOwner: InstanceAuthorizationValue = {
  remote: false,
  instanceKind: null,
  instanceRole: 'owner',
  capabilities: OWNER_INSTANCE_CAPABILITIES,
};

const remoteOwner: InstanceAuthorizationValue = {
  remote: true,
  instanceKind: null,
  instanceRole: 'owner',
  capabilities: {
    ...OWNER_INSTANCE_CAPABILITIES,
    can_use_system: false,
  },
};

const remoteEditor: InstanceAuthorizationValue = {
  ...remoteOwner,
  instanceRole: 'editor',
};

function asrToggle() {
  const title = screen.getByText('dashboard.audioTranscription');
  const row = title.closest('.flex.flex-col.gap-3') ?? title.parentElement?.parentElement;
  return row?.querySelector('[role="switch"]') as HTMLButtonElement | null;
}

function renderPage(context: InstanceAuthorizationValue) {
  return render(
    <InstanceAuthorizationContext.Provider value={context}>
      <MemoryRouter>
        <SettingsMessagingPage />
      </MemoryRouter>
    </InstanceAuthorizationContext.Provider>,
  );
}

describe('SettingsMessagingPage locality gating', () => {
  beforeEach(() => {
    api.getConfig.mockResolvedValue(baseConfig);
    api.saveConfig.mockResolvedValue(baseConfig);
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it('keeps remote-safe messaging preferences visible to a remote owner', async () => {
    renderPage(remoteOwner);

    expect(await screen.findByText('dashboard.ackMode')).toBeTruthy();
    expect(screen.getByText('dashboard.showDuration')).toBeTruthy();
    expect(screen.getByText('dashboard.replyEnhancements')).toBeTruthy();
    expect(screen.queryByText('dashboard.errorRetryLimit')).toBeNull();
    expect(screen.queryByText('dashboard.opencodeActiveTurnTimeout')).toBeNull();
    expect(screen.queryByText('dashboard.showPagesPrompt')).toBeNull();
  });

  it('shows the protected messaging controls to a local owner', async () => {
    renderPage(localOwner);

    expect(await screen.findByText('dashboard.errorRetryLimit')).toBeTruthy();
    expect(screen.getByText('dashboard.opencodeActiveTurnTimeout')).toBeTruthy();
    expect(screen.getByText('dashboard.showPagesPrompt')).toBeTruthy();
  });

  it('keeps owner-only runtime messaging controls hidden from an Editor', async () => {
    renderPage(remoteEditor);

    await screen.findByText('dashboard.ackMode');
    expect(screen.queryByText('dashboard.errorRetryLimit')).toBeNull();
    expect(screen.queryByText('dashboard.opencodeActiveTurnTimeout')).toBeNull();
    expect(screen.queryByText('dashboard.showPagesPrompt')).toBeNull();
  });

  it('shows a paired Editor the live ASR preference instead of a forced-off toggle', async () => {
    renderPage(remoteEditor);

    await screen.findByText('dashboard.audioTranscription');
    const toggle = asrToggle();
    expect(toggle).toBeTruthy();
    expect(toggle?.getAttribute('aria-checked')).toBe('true');
    expect(toggle?.disabled).toBe(false);
    expect(screen.queryByText('dashboard.audioTranscriptionRequiresVibeCloud')).toBeNull();
  });

  it('keeps a paired Editor able to turn ASR off', async () => {
    api.getConfig.mockResolvedValue({
      ...baseConfig,
      audio_asr: { enabled: false, echo_transcript: true, enabled_configured: true },
    });
    renderPage(remoteEditor);

    await screen.findByText('dashboard.audioTranscription');
    const toggle = asrToggle();
    expect(toggle?.getAttribute('aria-checked')).toBe('false');
    expect(toggle?.disabled).toBe(false);
  });
});
