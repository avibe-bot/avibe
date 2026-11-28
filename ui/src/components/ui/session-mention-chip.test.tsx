// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest';
import { act, render } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { I18nextProvider } from 'react-i18next';

import i18n from '@/i18n';
import { BotsContext, type BotsState } from '@/context/BotsContext';
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
  botForSession: () => undefined,
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
    ];
    const content =
      '好，已经起了一个后台 Agent（#sesw34zgzgcvp），并同步 #<seslongtitle1> 给 @<bot:product>。'
      + '不存在的 #<sesunknown123> 和 #sesunknown456 保持原样，代码里的 `#<sesw34zgzgcvp>` 也不动。';

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

    // Bot mention renders as BotMentionChip with its display name.
    expect(container.textContent).toContain('@产品伙伴');
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
});
