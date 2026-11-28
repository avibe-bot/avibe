// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest';
import { act, render } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { I18nextProvider } from 'react-i18next';

import i18n from '@/i18n';
import type { WorkbenchMessage, WorkbenchSession } from '@/context/ApiContext';
import { BotsContext, type BotsState } from '@/context/BotsContext';
import { ToastProvider } from '@/context/ToastProvider';
import { MessageRow } from '@/components/workbench/ChatPage';
import { clearLiveSessionTitles, setLiveSessionTitle } from '@/lib/liveSessionTitles';
import type { MentionReference } from '@/lib/mentions';
import { Markdown } from './markdown';

const mockBotsState: BotsState = {
  enabled: true,
  bots: [
    {
      handle: 'product',
      display_name: '产品伙伴',
      avatar: null,
      agent_name: 'product',
      session_id: 'ses_product',
      builtin: false,
      hide_details: true,
    },
  ],
  loaded: true,
  listed: true,
  mainBot: null,
  limit: null,
  atLimit: false,
  statusBySession: {},
  botForSession: (sessionId) => (sessionId === 'ses_product' ? mockBotsState.bots[0] : undefined),
  botForAgent: () => undefined,
  botForHandle: (handle) => (handle === 'product' ? mockBotsState.bots[0] : undefined),
  displayName: (bot) => bot.display_name || bot.handle,
  afterWrite: async () => undefined,
  holdOrder: () => () => undefined,
  setHideDetails: async () => false,
  openProfile: () => undefined,
  profileRequest: null,
  closeProfile: () => undefined,
};

afterEach(() => {
  clearLiveSessionTitles();
});

describe('Agent reply Session & Bot mentions in Markdown', () => {
  it('renders resolved bracketed and bare sessions as clickable chips, Bot mentions as Bot chips, and unresolved ids as exact plain text', () => {
    const references: MentionReference[] = [
      { kind: 'session', session_id: 'sesw34zgzgcvp', title: '登录跳转修复', openable: true },
      { kind: 'session', session_id: 'seslongtitle1', title: '一个非常非常长的后台会话标题用于验证芯片内的省略号截断效果', openable: true },
      { kind: 'bot', handle: 'product', display_name: '产品伙伴' },
      { kind: 'agent', name: 'vibey' },
    ];
    const content =
      '好，已经起了一个后台 @<agent:vibey>（[发版检查](#<sesw34zgzgcvp>)），并同步 #<seslongtitle1> 给 @<bot:product>。'
      + '不存在的 #<sesunknown123>、#sesunknown456 和 @<agent:ghost> 保持原样，代码里的 `#<sesw34zgzgcvp>` 也不动。';

    const { container } = render(
      <I18nextProvider i18n={i18n}>
        <MemoryRouter>
          <BotsContext.Provider value={mockBotsState}>
            <Markdown content={content} references={references} agentMentions />
          </BotsContext.Provider>
        </MemoryRouter>
      </I18nextProvider>,
    );

    const sessionLinks = Array.from(container.querySelectorAll('a[data-session-mention]'));
    expect(sessionLinks).toHaveLength(2);
    expect(sessionLinks[0].getAttribute('href')).toBe('/chat/sesw34zgzgcvp');
    expect(sessionLinks[0].textContent).toBe('#登录跳转修复');
    expect(sessionLinks[0].className).toContain('leading-none');
    expect(sessionLinks[1].getAttribute('href')).toBe('/chat/seslongtitle1');
    expect(sessionLinks[1].textContent).toBe('#一个非常非常长的后台会话标题用于验证芯片内的省略号截断效果');
    expect(sessionLinks[1].querySelector('span')?.className).toContain('truncate');

    // Unresolved bracketed id must stay visible verbatim (never dropped as raw HTML).
    expect(container.textContent).toContain('#<sesunknown123>');
    expect(container.textContent).toContain('#sesunknown456');
    expect(container.querySelector('[data-session-mention="sesunknown123"]')).toBeNull();
    expect(container.querySelector('[data-session-mention="sesunknown456"]')).toBeNull();

    // Code span stays literal.
    expect(container.querySelector('code')?.textContent).toBe('#<sesw34zgzgcvp>');

    // Bot and Agent mentions render as chips; unresolved @<agent:ghost> stays literal.
    expect(container.textContent).toContain('@产品伙伴');
    expect(container.textContent).toContain('@vibey');
    expect(container.textContent).toContain('@<agent:ghost>');
  });

  it('shows localized untitled label for an existing untitled session and updates live on title change', () => {
    const references: MentionReference[] = [
      { kind: 'session', session_id: 'sesuntitled01', title: null, openable: true },
    ];
    const { container } = render(
      <I18nextProvider i18n={i18n}>
        <MemoryRouter>
          <Markdown content="Opened #<sesuntitled01>." references={references} agentMentions />
        </MemoryRouter>
      </I18nextProvider>,
    );

    const link = container.querySelector('a[data-session-mention="sesuntitled01"]');
    expect(link).not.toBeNull();
    // Never renders the bare id `sesuntitled01`.
    expect(link?.textContent).not.toContain('sesuntitled01');

    act(() => {
      setLiveSessionTitle('sesuntitled01', 'Live Renamed Session');
    });
    expect(link?.textContent).toBe('#Live Renamed Session');
  });

  it('does not let a Session title containing $<SECRET_NAME> or <https://...> inject cards or nested links', () => {
    const references: MentionReference[] = [
      { kind: 'session', session_id: 'sessecret001', title: 'Rotate $<API_KEY> <https://evil.test>', openable: true },
    ];
    const { container } = render(
      <I18nextProvider i18n={i18n}>
        <MemoryRouter>
          <Markdown content="See #<sessecret001>." references={references} agentMentions secretRequests readOnly />
        </MemoryRouter>
      </I18nextProvider>,
    );

    const link = container.querySelector('a[data-session-mention="sessecret001"]');
    expect(link).not.toBeNull();
    expect(link?.textContent).toBe('#Rotate $<API_KEY> <https://evil.test>');
    expect(container.querySelector('a[href="https://evil.test"]')).toBeNull();
    // Must not render any SecretRequestCard button/input for API_KEY.
    expect(container.querySelector('[data-secret-name]')).toBeNull();
    expect(container.querySelectorAll('button')).toHaveLength(0);
  });

  it('keeps Bot Session display name on null SSE title updates and restores server title on clearLiveSessionTitles', () => {
    const references: MentionReference[] = [
      { kind: 'session', session_id: 'ses_product', title: '产品伙伴', openable: true },
      { kind: 'session', session_id: 'sesw34zgzgcvp', title: 'Server Title', openable: true },
    ];
    const { container } = render(
      <I18nextProvider i18n={i18n}>
        <MemoryRouter>
          <Markdown content="Talk to #<ses_product> about #<sesw34zgzgcvp>." references={references} agentMentions />
        </MemoryRouter>
      </I18nextProvider>,
    );

    const botSessionLink = container.querySelector('a[data-session-mention="ses_product"]');
    const regularLink = container.querySelector('a[data-session-mention="sesw34zgzgcvp"]');
    expect(botSessionLink?.textContent).toBe('#产品伙伴');
    expect(regularLink?.textContent).toBe('#Server Title');

    act(() => {
      setLiveSessionTitle('ses_product', null);
      setLiveSessionTitle('sesw34zgzgcvp', 'Stale SSE Title');
    });
    expect(botSessionLink?.textContent).toBe('#产品伙伴');
    expect(regularLink?.textContent).toBe('#Stale SSE Title');

    // Clearing the live overlay (e.g. on SSE stream gap) falls back to the fresh prop title.
    act(() => {
      clearLiveSessionTitles();
    });
    expect(regularLink?.textContent).toBe('#Server Title');
  });

  it('renders nested 4-space bullet mentions as chips and preserves bracketed markers inside outer link labels', () => {
    const references: MentionReference[] = [
      { kind: 'session', session_id: 'sesw34zgzgcvp', title: 'Auth Fix', openable: true },
    ];
    const content =
      '- parent\n'
      + '    - child sees #<sesw34zgzgcvp> and #<sesmissing99>\n\n'
      + 'Read [notes for #<sesw34zgzgcvp>](https://example.com).';
    const { container } = render(
      <I18nextProvider i18n={i18n}>
        <MemoryRouter>
          <Markdown content={content} references={references} agentMentions />
        </MemoryRouter>
      </I18nextProvider>,
    );

    const chip = container.querySelector('a[data-session-mention="sesw34zgzgcvp"]');
    expect(chip?.textContent).toBe('#Auth Fix');
    expect(container.textContent).toContain('#<sesmissing99>');
    const outerLink = container.querySelector('a[href="https://example.com"]');
    expect(outerLink?.textContent).toBe('notes for #<sesw34zgzgcvp>');
  });

  it('renders a non-interactive span when interactive={false}', () => {
    const references: MentionReference[] = [
      { kind: 'session', session_id: 'sesw34zgzgcvp', title: 'Auth Fix', openable: true },
    ];
    const { container } = render(
      <I18nextProvider i18n={i18n}>
        <MemoryRouter>
          <Markdown content="See #<sesw34zgzgcvp>." references={references} agentMentions interactive={false} />
        </MemoryRouter>
      </I18nextProvider>,
    );

    expect(container.querySelector('a[data-session-mention]')).toBeNull();
    expect(container.querySelector('span[data-session-mention="sesw34zgzgcvp"]')?.textContent).toBe('#Auth Fix');
  });

  it('renders Harness bubble header, collapsed preview, and expanded body with Session & Bot chips and without SecretRequestCard', () => {
    const mockSession = {
      id: 'sesmain00001',
      scope_id: 'avibe::project::p1',
      project_id: 'p1',
      title: 'Main Chat',
      agent_id: 'agt-1',
      agent_name: 'claude',
      agent_backend: 'claude_code',
      agent_variant: null,
      model: null,
      reasoning_effort: null,
      status: 'active',
      pinned: false,
      agent_status: 'idle',
      workdir: null,
      native_session_id: null,
      created_at: '2026-11-29T00:00:00Z',
      updated_at: '2026-11-29T00:00:00Z',
      last_active_at: '2026-11-29T00:00:00Z',
      metadata: {},
    } as WorkbenchSession;

    const harnessMsg = {
      id: 'msg_h1',
      scope_id: 'avibe::project::p1',
      session_id: 'sesmain00001',
      platform: 'avibe',
      author: 'harness',
      type: 'harness',
      source: 'harness',
      author_id: 'run_1',
      author_name: 'agent_run',
      native_message_id: 'agent_run:run_1',
      parent_native_message_id: null,
      source_session_id: 'ses_product',
      source_session_title: null,
      source_session_agent_name: 'product',
      text:
        '[Reply truncated; use `vibe data query` to read #<sesw34zgzgcvp>\'s transcript in full.] '
        + 'See #sesw34zgzgcvp, @<bot:product>, #<sesmissing99>, and $<API_KEY>.',
      content: {
        references: [
          { kind: 'session', session_id: 'sesw34zgzgcvp', title: '登录跳转修复', openable: true },
          { kind: 'bot', handle: 'product', display_name: '产品伙伴' },
        ],
      },
      metadata: {},
      created_at: '2026-11-29T00:00:00Z',
      updated_at: '2026-11-29T00:00:00Z',
      delivered_at: '2026-11-29T00:00:00Z',
      read_at: '2026-11-29T00:00:00Z',
    } as unknown as WorkbenchMessage;

    // Collapsed: header uses SessionMentionChip with Bot display name (no bare id suffix)
    // and collapsed preview resolves mentions to plain-text titles.
    const { container: collapsedContainer } = render(
      <I18nextProvider i18n={i18n}>
        <ToastProvider>
          <MemoryRouter>
            <BotsContext.Provider value={mockBotsState}>
              <MessageRow message={harnessMsg} session={mockSession} harnessOpen={false} />
            </BotsContext.Provider>
          </MemoryRouter>
        </ToastProvider>
      </I18nextProvider>,
    );
    const headerChip = collapsedContainer.querySelector('a[data-session-mention="ses_product"]');
    expect(headerChip?.getAttribute('href')).toBe('/chat/ses_product');
    expect(headerChip?.textContent).toBe('#产品伙伴');
    const toggleBtn = collapsedContainer.querySelector('button[aria-expanded="false"]');
    expect(toggleBtn?.textContent).toContain('登录跳转修复');
    expect(toggleBtn?.textContent).toContain('@产品伙伴');
    expect(toggleBtn?.textContent).not.toContain('#<sesw34zgzgcvp>');

    // Expanded: body renders clickable Session chips (for both bracketed receipt pointer
    // and bare #ses...), Bot chip, verbatim unresolved id, and never a SecretRequestCard.
    const { container: expandedContainer } = render(
      <I18nextProvider i18n={i18n}>
        <ToastProvider>
          <MemoryRouter>
            <BotsContext.Provider value={mockBotsState}>
              <MessageRow message={harnessMsg} session={mockSession} harnessOpen />
            </BotsContext.Provider>
          </MemoryRouter>
        </ToastProvider>
      </I18nextProvider>,
    );
    const bodySessionChips = expandedContainer.querySelectorAll('a[data-session-mention="sesw34zgzgcvp"]');
    expect(bodySessionChips).toHaveLength(2);
    expect(bodySessionChips[0].getAttribute('href')).toBe('/chat/sesw34zgzgcvp');
    expect(bodySessionChips[0].textContent).toBe('#登录跳转修复');
    expect(expandedContainer.textContent).toContain('@产品伙伴');
    expect(expandedContainer.textContent).toContain('#<sesmissing99>');
    expect(expandedContainer.querySelector('[data-secret-name]')).toBeNull();
  });
});
