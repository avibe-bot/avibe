/** @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

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

  it('from another device without the computer\'s name says the computer running Avibe', () => {
    render(<UnbindConfirm host={host} confirm={{ ...base, remote: true, computer: undefined }} />);
    const text = screen.getByRole('dialog').textContent ?? '';
    expect(text).toContain('运行 Avibe 的那台电脑上已同步的健康数据会清掉');
    expect(text).toContain('运行 Avibe 的那台电脑上的工作区和对话都留着。');
    expect(text).toContain('要再开启，得在运行 Avibe 的那台电脑上按「开启远程访问」。');
    expect(text).toContain('这期间可以在运行 Avibe 的那台电脑或别的电脑上领回。');
    expect(text).not.toContain('这台电脑');
  });

  it('once pressed, cannot be cancelled or closed', () => {
    const onCancel = vi.fn();
    render(<UnbindConfirm host={host} confirm={{ ...base, remote: false, busy: true }} onCancel={onCancel} />);
    expect((screen.getByRole('button', { name: '取消' }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' });
    expect(onCancel).not.toHaveBeenCalled();
    expect(screen.getByRole('dialog')).toBeTruthy();
  });
});

describe('RebindDialogs · binding as a new computer, once pressed', () => {
  it('cannot be cancelled or closed, the delete-first one and the local-only one alike', async () => {
    const { RebindDialogs } = await import('./RebindViews');
    const state = { state: 'asNewOffered', reason: 'predecessor_unrecoverable' } as const;
    for (const dialog of [
      { kind: 'asNewCloud', healthConnected: true, holdDays: 30 } as const,
      { kind: 'asNew', healthConnected: true, afterRefusal: true } as const,
    ]) {
      const onRebindClose = vi.fn();
      const { unmount } = render(<RebindDialogs dialog={dialog} state={state} host={host} busy actions={{ onRebindClose }} />);
      expect((screen.getByRole('button', { name: '取消' }) as HTMLButtonElement).disabled).toBe(true);
      fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' });
      expect(onRebindClose).not.toHaveBeenCalled();
      unmount();
    }
  });
});

describe('ConnectionPageView · 「解除绑定」 after it', () => {
  it('is not offered again once refused as not eligible, and puts the tunnel settings away while it needs repair', async () => {
    const { ConnectionPageView } = await import('./ConnectionPageView');
    const props = {
      load: 'ready' as const,
      connection: { settings: { transport_protocol: 'auto' as const, auto_recovery: true, optimization_profile: 'balanced' as const, edge_ip_version: 'auto' as const, edge_bind_address: '' }, interfaces: [], direct: 'ready' as const },
      tunnel: { state: 'up' as const, ms: 120 }, remote: false, host,
    };
    const { unmount } = render(<ConnectionPageView {...props} unbind={{ state: 'notChanged' }} />);
    expect(screen.queryByRole('button', { name: '解除绑定' })).toBeNull();
    expect(screen.getByText(said('没有解除绑定，什么都没变。'))).toBeTruthy();
    unmount();

    render(<ConnectionPageView {...props} unbind={{ state: 'needsRepair' }} />);
    expect(screen.queryByText('Cloudflare 隧道设置')).toBeNull();
    expect(screen.queryByRole('button', { name: /优化线路/ })).toBeNull();
    expect(screen.queryByRole('button', { name: '解除绑定' })).toBeNull();
  });
});
