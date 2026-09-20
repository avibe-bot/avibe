/* @vitest-environment jsdom */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';

import { SettingsMessagingPage } from './SettingsMessagingPage';
import {
  InstanceAuthorizationContext,
  type InstanceAuthorizationValue,
} from '../../context/InstanceAuthorizationContext';
import { OWNER_INSTANCE_CAPABILITIES } from '../../lib/sessionInfo';
import {
  configMutationsToPayload,
  type ConfigMutation,
} from '../../lib/configMutations';

const api = vi.hoisted(() => ({
  getConfig: vi.fn(),
  mutateConfig: vi.fn(),
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
  agent_progress_style: 'off',
  audio_asr: { enabled: true, echo_transcript: true, enabled_configured: true },
  remote_access: { vibe_cloud: { paired: true } },
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
  remote: true,
  instanceKind: null,
  instanceRole: 'editor',
  capabilities: {
    ...OWNER_INSTANCE_CAPABILITIES,
    can_manage_instance: false,
    can_use_system: false,
    can_manage_projects: false,
    can_manage_agents: false,
  },
};

const remoteViewer: InstanceAuthorizationValue = {
  remote: true,
  instanceKind: null,
  instanceRole: 'viewer',
  capabilities: {
    ...OWNER_INSTANCE_CAPABILITIES,
    can_chat: false,
    can_manage_instance: false,
    can_use_system: false,
    can_manage_projects: false,
    can_manage_agents: false,
    can_use_agents: false,
    can_use_skills: false,
    can_use_vault_secrets: false,
    can_use_show_pages: false,
    can_use_terminal_files: false,
    can_use_terminal: false,
    can_use_files: false,
  },
};

function asrToggle() {
  const title = screen.getByText('dashboard.audioTranscription');
  const row = title.closest('.flex.flex-col.gap-3') ?? title.parentElement?.parentElement;
  return row?.querySelector('[role="switch"]') as HTMLButtonElement | null;
}

// Identifies a control by its SettingsRow label so coverage survives re-renders.
function controlLabel(control: Element): string {
  const row = control.closest('div.flex.flex-col.gap-3');
  return row?.querySelector('div.flex.min-w-0')?.textContent ?? '';
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

function mutationPayload(callIndex = 0) {
  const mutations = api.mutateConfig.mock.calls[callIndex]?.[0] as
    | readonly ConfigMutation[]
    | undefined;
  return mutations ? configMutationsToPayload(mutations) : undefined;
}

describe('SettingsMessagingPage locality gating', () => {
  beforeEach(() => {
    api.getConfig.mockResolvedValue(baseConfig);
    api.mutateConfig.mockResolvedValue(baseConfig);
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
    expect(screen.queryByText('dashboard.showPagesPrompt')).toBeNull();
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

  it('shows a paired Viewer the live ASR state without making it clickable', async () => {
    renderPage(remoteViewer);

    await screen.findByText('dashboard.audioTranscription');
    const toggle = asrToggle();
    expect(toggle?.getAttribute('aria-checked')).toBe('true');
    expect(toggle?.disabled).toBe(true);
  });

  it('sends only the changed ASR field when an Editor toggles transcription', async () => {
    const user = userEvent.setup();
    renderPage(remoteEditor);

    await screen.findByText('dashboard.audioTranscription');
    const toggle = asrToggle();
    expect(toggle).toBeTruthy();
    await user.click(toggle!);

    expect(mutationPayload()).toEqual({
      audio_asr: { enabled: false, enabled_configured: true },
    });
  });

  it('sends only the changed acknowledgement field when an Editor changes ack mode', async () => {
    const user = userEvent.setup();
    renderPage(remoteEditor);

    const select = await screen.findByDisplayValue('dashboard.ackTyping');
    await user.selectOptions(select, 'message');

    expect(mutationPayload()).toEqual({ ack_mode: 'message' });
  });

  it('hides Slack preview controls from Editors when they cannot manage the instance', async () => {
    api.getConfig.mockResolvedValue({
      ...baseConfig,
      platforms: { enabled: ['slack'] },
      platform_catalog: [{ id: 'slack', capabilities: { supports_reaction_indicator: true, supports_typing_indicator: true } }],
    });
    renderPage(remoteEditor);

    await screen.findByText('dashboard.ackMode');
    expect(screen.queryByText('dashboard.slackLinkPreviews')).toBeNull();
  });

  it('keeps the Slack preview control for a remote Owner and sends its own field patch', async () => {
    // A remote Owner also saves field-specifically, so gating this control on
    // "can use the local system" would have hidden it from a role allowed to
    // write ``slack.*``. It is gated on instance management and carries a patch.
    const user = userEvent.setup();
    renderPage(remoteOwner);

    await screen.findByText('dashboard.slackLinkPreviews');
    const row = screen.getByText('dashboard.slackLinkPreviews').closest('div.flex.flex-col.gap-3');
    await user.click(row?.querySelector('[role="switch"]') as HTMLButtonElement);

    expect(mutationPayload()).toEqual({ slack: { disable_link_unfurl: true } });
  });

  it.each([
    ['Editor', remoteEditor],
    ['local Owner', localOwner],
  ])('never posts an empty mutation from any control offered to a %s', async (_label, context) => {
    // The property, not today's control list: a field-specific save carries only
    // the control's own patch, so any control rendered without one posts ``{}``
    // — a save that reports success and loses the change on reload. Sweeping
    // whatever is rendered catches the next control added without a patch; an
    // enumeration of the current controls never would.
    const user = userEvent.setup();
    renderPage(context);
    await screen.findByText('dashboard.ackMode');

    const switches = () => screen.queryAllByRole('switch') as HTMLButtonElement[];
    const selects = () => screen.queryAllByRole('combobox') as HTMLSelectElement[];
    const exercised = new Set<string>();
    const unsaved: string[] = [];
    // Checked per interaction against the call the interaction itself produced:
    // a control without a field patch either posts ``{}`` or trips the guard in
    // ``persist`` and posts nothing, and both look identical at the end of a
    // sweep once a later control's successful save has cleared the state.
    const recordInteraction = async (label: string, act: () => Promise<void>) => {
      const before = api.mutateConfig.mock.calls.length;
      exercised.add(label);
      await act();
      const mutations = api.mutateConfig.mock.calls[before]?.[0] as
        | readonly ConfigMutation[]
        | undefined;
      if (!mutations || mutations.length === 0) unsaved.push(label);
    };

    // Two rounds, re-querying every step: toggling a parent reveals a child
    // control (show_tool_calls) and toggling ASR off disables its echo child, so
    // one pass can neither see nor reach the whole surface.
    for (let round = 0; round < 2; round += 1) {
      for (let index = 0; index < switches().length; index += 1) {
        const control = switches()[index];
        if (control.disabled) continue;
        await recordInteraction(controlLabel(control), () => user.click(control));
      }
      for (let index = 0; index < selects().length; index += 1) {
        const select = selects()[index];
        const option = Array.from(select.options).find(
          (each) => !each.disabled && each.value !== select.value,
        );
        if (!option) continue;
        await recordInteraction(controlLabel(select), () =>
          user.selectOptions(select, option.value),
        );
      }
    }

    const offered = [...switches(), ...selects()].filter(
      (control) => !(control as HTMLButtonElement).disabled,
    );
    expect(offered.length).toBeGreaterThan(0);
    expect(offered.map(controlLabel).filter((label) => !exercised.has(label))).toEqual([]);
    expect(unsaved).toEqual([]);
  });
});
