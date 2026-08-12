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
  instanceRole: 'owner',
  capabilities: OWNER_INSTANCE_CAPABILITIES,
};

const remoteOwner: InstanceAuthorizationValue = {
  remote: true,
  instanceRole: 'owner',
  capabilities: {
    ...OWNER_INSTANCE_CAPABILITIES,
    can_use_system: false,
  },
};

const activeOrgMember: InstanceAuthorizationValue = {
  ...remoteOwner,
  instanceRole: 'viewer',
  hasTemporaryUnrestrictedOrgAccess: true,
};

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

  it('shows the protected messaging controls to a trusted-local owner', async () => {
    renderPage(localOwner);

    expect(await screen.findByText('dashboard.errorRetryLimit')).toBeTruthy();
    expect(screen.getByText('dashboard.opencodeActiveTurnTimeout')).toBeTruthy();
    expect(screen.getByText('dashboard.showPagesPrompt')).toBeTruthy();
  });

  it('shows every runtime messaging control to an active Organization member', async () => {
    renderPage(activeOrgMember);

    expect(await screen.findByText('dashboard.errorRetryLimit')).toBeTruthy();
    expect(screen.getByText('dashboard.opencodeActiveTurnTimeout')).toBeTruthy();
    expect(screen.getByText('dashboard.showPagesPrompt')).toBeTruthy();
  });
});
