// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import en from '../../i18n/en.json';
import zh from '../../i18n/zh.json';
import { WeChatConfig } from './WeChatConfig';

const mocks = vi.hoisted(() => ({
  wechatStartLogin: vi.fn(),
  wechatPollLogin: vi.fn(),
}));

vi.mock('../../context/ApiContext', () => ({
  useApi: () => mocks,
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('WeChatConfig restart activation notice', () => {
  it('shows the localized restart notice after login is saved without a restart', async () => {
    mocks.wechatStartLogin.mockResolvedValue({
      qrcode_url: 'https://wechat.example.test/login',
      session_key: 'qr-session',
    });
    mocks.wechatPollLogin.mockResolvedValue({
      status: 'confirmed',
      bot_token: 'wechat-token',
      restart_scheduled: false,
      restart_reason: 'restart_not_scheduled_package_busy',
    });

    render(
      <WeChatConfig
        data={{ wechat: {} }}
        onNext={vi.fn()}
      />,
    );

    expect(await screen.findByText('wechatConfig.restartRequired')).toBeTruthy();
    expect(en.wechatConfig.restartRequired).toBe('Login saved. A restart is needed to activate it.');
    expect(zh.wechatConfig.restartRequired).toBe('登录已保存，需重启后生效。');
  });
});
