/** @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import i18n from '../../i18n';
import { ConnectionPageView, type ConnectionPageViewProps } from './ConnectionPageView';

// ui/design: settings/devices-connection/*
const base: ConnectionPageViewProps = {
  viewer: 'browser',
  direct: 'ready',
  relay: true,
  tunnel: {
    ms: 120,
    active: 'quic',
    settings: { transport_protocol: 'auto', auto_recovery: true, optimization_profile: 'balanced', edge_ip_version: '4', edge_bind_address: '' },
    interfaces: [],
  },
};
const host = 'max-app.avibe.bot';

beforeEach(async () => {
  await i18n.changeLanguage('zh');
});
afterEach(cleanup);

describe('ConnectionPageView', () => {
  it('gives the App two choices for its own path, and one line when it went back by itself', () => {
    const onPreferenceChange = vi.fn();
    const { rerender } = render(<ConnectionPageView {...base} viewer="app"
      path={{ kind: 'punched', family: 'ipv6', rttMs: 38 }}
      preference={{ mode: 'automatic', reverted: null }} onPreferenceChange={onPreferenceChange} />);
    const group = screen.getByRole('radiogroup', { name: '这台设备' });
    expect(within(group).getAllByRole('radio').map((each) => each.textContent)).toEqual([
      '自动哪条快走哪条，断了自动换。',
      '仅 Cloudflare 隧道不尝试直连和中继。',
    ]);
    expect(screen.getByText('本设备正在使用：打洞直连 · IPv6 · 38 ms')).toBeTruthy();
    expect(screen.queryByRole('status')).toBeNull();
    fireEvent.click(within(group).getByRole('radio', { name: /仅 Cloudflare 隧道/ }));
    expect(onPreferenceChange).toHaveBeenCalledWith('tunnel');

    rerender(<ConnectionPageView {...base} viewer="app" path={null}
      preference={{ mode: 'automatic', reverted: { at: new Date().toISOString(), reason: 'unreachable' } }} />);
    expect(screen.getByRole('status').textContent).toMatch(/^「仅 Cloudflare 隧道」在 .+ 连不上，App 已自己改回「自动」。$/);
    // A browser has no choice of its own and no domain controls in the App.
    expect(screen.queryByText('更改域名')).toBeNull();
  });

  it('says a remote change is on trial for 30 seconds, and a local one applies at once', () => {
    const { rerender } = render(<ConnectionPageView {...base} />);
    expect(screen.queryByRole('radiogroup', { name: '这台设备' })).toBeNull();
    expect(screen.getByText('从远处更改会先试用：这台设备 30 秒内没有连回来确认，就自动改回去。')).toBeTruthy();
    expect(screen.getByText('自动 · 当前 QUIC')).toBeTruthy();
    rerender(<ConnectionPageView {...base} viewer="local" tunnel={{ ...base.tunnel, settings: { ...base.tunnel.settings, transport_protocol: 'http2' } }} />);
    expect(screen.getByText('在这台电脑上更改，立即生效。')).toBeTruthy();
    expect(screen.getByText('固定后，网络变了不会自己换协议；连续 3 分钟连不上会改回自动。')).toBeTruthy();
  });

  it('asks to keep a change on trial, naming what goes back', () => {
    const onKeepTrial = vi.fn();
    const onPutBack = vi.fn();
    render(<ConnectionPageView {...base} trial={{ change: { setting: 'protocol', to: 'http2', from: 'auto' }, seconds: 24 }}
      onKeepTrial={onKeepTrial} onPutBack={onPutBack} />);
    const dialog = screen.getByRole('dialog');
    expect(dialog.textContent).toContain('传输协议已改为 HTTP/2，这台设备连回来了。24 秒内不保留，会自动改回「自动」。');
    fireEvent.click(within(dialog).getByRole('button', { name: '保留（24）' }));
    expect(onKeepTrial).toHaveBeenCalledTimes(1);
    fireEvent.click(within(dialog).getByRole('button', { name: '改回去' }));
    expect(onPutBack).toHaveBeenCalledTimes(1);
  });

  it('keeps the relay one prop: a switch, or its state only; and no relay while direct is off', () => {
    const { rerender } = render(<ConnectionPageView {...base} />);
    expect(screen.getByRole('switch', { name: '中继' })).toBeTruthy();
    rerender(<ConnectionPageView {...base} relayEditable={false} />);
    expect(screen.queryByRole('switch', { name: '中继' })).toBeNull();
    rerender(<ConnectionPageView {...base} direct="off" relay={false} />);
    expect((screen.getByRole('switch', { name: '中继' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('explains every path direct affects while direct is not allowed, under a badge of its own', async () => {
    const { rerender } = render(<ConnectionPageView {...base} direct="denied" />);
    expect(screen.getAllByText('直连还没有对这个 Avibe 开放。')).toHaveLength(3);
    await i18n.changeLanguage('en');
    rerender(<ConnectionPageView {...base} direct="denied" />);
    expect(screen.getAllByText('Not allowed')).toHaveLength(3);
  });

  it('renames in two steps: the consequences with the waiting period passed in, then a held confirmation', () => {
    const { rerender } = render(<ConnectionPageView {...base} viewer="local" host={host} rename={{ step: 'form', name: 'maxwell', voidDays: 7, health: true }} />);
    let dialog = screen.getByRole('dialog');
    expect(dialog.textContent).toContain('max-app.avibe.bot 立即作废，7 天后别人可以领取，不会还给你。');
    expect(dialog.textContent).toContain('已同步的健康数据会删除，要在 iPhone 上重新连接健康。');
    expect((within(dialog).getByRole('button', { name: '继续' }) as HTMLButtonElement).disabled).toBe(false);

    rerender(<ConnectionPageView {...base} viewer="local" host={host} rename={{ step: 'form', name: 'alex', nameState: 'taken', voidDays: 7, health: false }} />);
    dialog = screen.getByRole('dialog');
    expect(dialog.textContent).not.toContain('健康数据');
    expect(within(dialog).getByText('已被占用，换一个名称。')).toBeTruthy();
    expect((within(dialog).getByRole('button', { name: '继续' }) as HTMLButtonElement).disabled).toBe(true);

    rerender(<ConnectionPageView {...base} viewer="local" host={host} rename={{ step: 'confirm', name: 'maxwell', countdown: 3 }} />);
    dialog = screen.getByRole('dialog');
    expect(within(dialog).getByText('改成 maxwell-app.avibe.bot？')).toBeTruthy();
    expect((within(dialog).getByRole('button', { name: '更改域名（3）' }) as HTMLButtonElement).disabled).toBe(true);
    const onRenameConfirm = vi.fn();
    rerender(<ConnectionPageView {...base} viewer="local" host={host} rename={{ step: 'confirm', name: 'maxwell', countdown: 0 }} onRenameConfirm={onRenameConfirm} />);
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: '更改域名' }));
    expect(onRenameConfirm).toHaveBeenCalledTimes(1);
  });
});
