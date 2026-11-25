/** @vitest-environment jsdom */

import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import i18n from '../../i18n';
import { said } from './testText';
import { UnbindConfirm } from './UnbindViews';

// ui/design: settings/devices-unbind/confirm*
const host = 'max-app.avibe.bot';
const base = { healthConnected: true, releaseAt: '2026-12-25T00:00:00.000Z', computer: 'Max 的 MacBook Pro' };

beforeEach(async () => { await i18n.changeLanguage('zh'); });
afterEach(cleanup);

describe('UnbindConfirm', () => {
  it('at the computer says 「这台电脑」, and the way back is 「开启远程访问」', () => {
    render(<UnbindConfirm host={host} confirm={{ ...base, remote: false }} />);
    const text = screen.getByRole('dialog').textContent ?? '';
    expect(text).toContain('这台电脑上已同步的健康数据会清掉');
    expect(text).toContain('这台电脑上的工作区和对话都留着。');
    expect(text).toContain('以后要再用，按「开启远程访问」。');
    expect(text).not.toContain('Max 的 MacBook Pro');
    expect(screen.getByText(said('max-app.avibe.bot 在你的账号下保留到 12月25日，这期间可以在这台或别的电脑上领回。'))).toBeTruthy();
  });

  it('from another device names the computer, never 「这台电脑」 or 「那台电脑」', () => {
    render(<UnbindConfirm host={host} confirm={{ ...base, remote: true }} />);
    const text = screen.getByRole('dialog').textContent ?? '';
    expect(text).toContain('Max 的 MacBook Pro 上已同步的健康数据会清掉');
    expect(text).toContain('Max 的 MacBook Pro 上的工作区和对话都留着。');
    expect(text).toContain('你现在用的这台设备会马上断开。要再开启，得在 Max 的 MacBook Pro 上按「开启远程访问」。');
    expect(text).toContain('这期间可以在 Max 的 MacBook Pro 或别的电脑上领回。');
    expect(text).not.toContain('这台电脑');
    expect(text).not.toContain('这台或');
    expect(text).not.toContain('那台电脑');
  });

  it('says nothing about Health where none was synced, and never invents a day', () => {
    render(<UnbindConfirm host={host} confirm={{ ...base, remote: false, healthConnected: false, releaseAt: undefined, holdDays: 30 }} />);
    const text = screen.getByRole('dialog').textContent ?? '';
    expect(text).not.toContain('健康');
    expect(text).toContain('max-app.avibe.bot 在你的账号下保留 30 天');
  });
});
