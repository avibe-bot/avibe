/** @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import i18n from '../../i18n';
import { DevicesPageView } from './DevicesPageView';
import { TESTFLIGHT_INVITE_URL, signOutNeedsConfirm, turnOffNeedsConfirm } from './rules';
import type { Device, Reach } from './types';

// ui/design: settings/devices/*
const account = 'max@example.test';
const host = 'max-app.avibe.bot';
const on: Reach = { state: 'on', host, account, tunnel: 'good', ms: 120 };

const macbook: Device = {
  id: 'host', kind: 'host', name: 'MacBook', client: { kind: 'runtime', version: '3.2.8', platform: 'macOS' }, online: {},
  capabilities: [{ kind: 'computerUse', on: true }, { kind: 'notifications', on: true }],
};
const iphone: Device = {
  id: 'iphone', kind: 'iphone', name: 'iPhone', client: { kind: 'app' }, online: { path: 'punched', ms: 38 },
  capabilities: [{ kind: 'notifications', on: true }, { kind: 'health', on: true, data: ['steps'] }],
};
const chrome: Device = { id: 'chrome', kind: 'browser', name: 'Chrome', client: { kind: 'browser' }, capabilities: [] };
const sara: Device = { ...chrome, id: 'sara', name: 'Sara Chrome', person: 'sara@example.test' };
const self = (device: Device): Device => ({ ...device, self: true });

beforeEach(async () => {
  await i18n.changeLanguage('zh');
});
afterEach(cleanup);

describe('DevicesPageView', () => {
  it('asks first only where something is lost', () => {
    expect(signOutNeedsConfirm(iphone)).toBe(true);
    expect(signOutNeedsConfirm(chrome)).toBe(false);
    expect(signOutNeedsConfirm({ ...iphone, capabilities: [{ kind: 'health', on: false, data: [] }] })).toBe(false);
    // Turning remote access off at the computer itself locks nobody out; from another device it does.
    expect(turnOffNeedsConfirm([self(macbook), iphone])).toBe(false);
    expect(turnOffNeedsConfirm([macbook, self(iphone)])).toBe(true);
  });

  it('shows a member only their own devices, with no instance controls', () => {
    render(<DevicesPageView reach={on} viewer="member" ownerEmail={account} devices={[self(chrome), sara]}
      activity={[{ id: 'a', at: new Date().toISOString(), event: { kind: 'directOn' } }]} />);
    expect(screen.getByText(`你登录了这个 Avibe 的设备。这个 Avibe 由 ${account} 管理。`)).toBeTruthy();
    expect(screen.queryByRole('switch', { name: '远程访问' })).toBeNull();
    expect(screen.queryByText('其他人的设备')).toBeNull();
    expect(screen.queryByText('最近活动')).toBeNull();
    expect(screen.queryByRole('button', { name: '添加设备' })).toBeNull();
  });

  it('turns on from a plain browser in one action, and says when avibe.bot cannot be reached', () => {
    const onTurnOn = vi.fn();
    const { rerender } = render(<DevicesPageView reach={{ state: 'off', surface: 'browser' }} devices={[self(macbook)]} onTurnOn={onTurnOn} />);
    fireEvent.click(screen.getByRole('button', { name: '开启远程访问' }));
    expect(onTurnOn).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('alert')).toBeNull();
    rerender(<DevicesPageView reach={{ state: 'off', surface: 'browser', error: 'unreachable' }} devices={[self(macbook)]} />);
    expect(screen.getByRole('alert').textContent).toContain('暂时连不上 avibe.bot');
  });

  it('claims in the Desktop App: a taken name blocks turning on and is never replaced by a suggestion', () => {
    const onNameChange = vi.fn();
    const { rerender } = render(<DevicesPageView
      reach={{ state: 'off', surface: 'desktop', account, name: 'max', nameState: 'taken' }}
      devices={[self(macbook)]} onNameChange={onNameChange} />);
    const name = screen.getByRole('textbox', { name: '名称' }) as HTMLInputElement;
    expect(name.value).toBe('max');
    expect(screen.getByText('已被占用，换一个名称。')).toBeTruthy();
    expect((screen.getByRole('button', { name: '开启远程访问' }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(name, { target: { value: 'maxwell' } });
    expect(onNameChange).toHaveBeenCalledWith('maxwell');

    rerender(<DevicesPageView reach={{ state: 'off', surface: 'desktop', account, name: 'maxwell', nameState: 'free' }} devices={[self(macbook)]} />);
    expect(screen.getByText('域名是 maxwell-app.avibe.bot。')).toBeTruthy();
    expect((screen.getByRole('button', { name: '开启远程访问' }) as HTMLButtonElement).disabled).toBe(false);

    const onSignIn = vi.fn();
    rerender(<DevicesPageView reach={{ state: 'off', surface: 'desktop', account: null, name: '' }} devices={[self(macbook)]} onSignIn={onSignIn} />);
    expect(screen.queryByRole('textbox')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '登录并开启' }));
    expect(onSignIn).toHaveBeenCalledTimes(1);
  });

  it('switches remote access off and on, and holds the switch while it starts', () => {
    const onRemoteAccessChange = vi.fn();
    const { rerender } = render(<DevicesPageView reach={on} devices={[self(macbook), iphone]} onRemoteAccessChange={onRemoteAccessChange} />);
    fireEvent.click(screen.getByRole('switch', { name: '远程访问' }));
    expect(onRemoteAccessChange).toHaveBeenLastCalledWith(false);
    rerender(<DevicesPageView reach={{ state: 'paused', host, account }} devices={[self(macbook), iphone]} onRemoteAccessChange={onRemoteAccessChange} />);
    fireEvent.click(screen.getByRole('switch', { name: '远程访问' }));
    expect(onRemoteAccessChange).toHaveBeenLastCalledWith(true);
    rerender(<DevicesPageView reach={{ state: 'starting', host, account }} devices={[self(macbook)]} />);
    expect((screen.getByRole('switch', { name: '远程访问' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('offers 添加设备 inline while this computer is alone, and as a dialog once others are listed', () => {
    const { rerender } = render(<DevicesPageView reach={on} devices={[self(macbook)]} />);
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(screen.getByTestId('add-device-code').dataset.code).toBe(TESTFLIGHT_INVITE_URL);
    expect(screen.getByText('用 iPhone 相机扫这个码，在 TestFlight 里安装 Avibe OS。')).toBeTruthy();

    rerender(<DevicesPageView reach={on} devices={[self(macbook)]} add="android" />);
    expect(screen.getByTestId('add-device-code').dataset.code).toBe(`https://${host}`);
    expect(screen.getByText('没有安卓 App。')).toBeTruthy();

    rerender(<DevicesPageView reach={on} devices={[self(macbook), iphone]} />);
    expect(screen.queryByTestId('add-device-code')).toBeNull();
    expect(screen.getByRole('button', { name: '添加设备' })).toBeTruthy();
    rerender(<DevicesPageView reach={on} devices={[self(macbook), iphone]} add="computer" />);
    const dialog = screen.getByRole('dialog');
    expect(within(dialog).getByRole('button', { name: '下载页' })).toBeTruthy();
    expect(within(dialog).queryByTestId('add-device-code')).toBeNull();
  });

  it('offers to sign out the others only when more than one other device is signed in', () => {
    const { rerender } = render(<DevicesPageView reach={on} devices={[self(macbook), iphone]} />);
    expect(screen.queryByRole('button', { name: '退出其他所有设备' })).toBeNull();
    rerender(<DevicesPageView reach={on} devices={[self(macbook), iphone, chrome]} />);
    expect(screen.getByRole('button', { name: '退出其他所有设备' })).toBeTruthy();
    rerender(<DevicesPageView reach={on} ownerEmail={account} devices={[self(macbook), iphone, chrome]} confirm="remove-others" />);
    expect(screen.getByRole('dialog').textContent).toContain(`其他 2 台都会退出 ${account}。它们同步的健康数据会被删除。`);
  });

  it('gives the computer that runs Avibe no way out, and every other device one', () => {
    const onSignOutDevice = vi.fn();
    const { rerender } = render(<DevicesPageView reach={on} devices={[self(macbook), iphone, chrome]} detail="host" />);
    expect(within(screen.getByRole('dialog')).queryByRole('button', { name: '退出这台设备' })).toBeNull();
    rerender(<DevicesPageView reach={on} devices={[self(macbook), iphone, chrome]} detail="chrome" onSignOutDevice={onSignOutDevice} />);
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: '退出这台设备' }));
    expect(onSignOutDevice).toHaveBeenCalledWith('chrome');
  });

  it('confirms signing out a device that synced health, and names what is deleted', () => {
    render(<DevicesPageView reach={on} devices={[self(macbook), iphone]} detail="iphone" confirm="remove" />);
    const dialog = screen.getByRole('dialog');
    expect(within(dialog).getByText('退出 iPhone？')).toBeTruthy();
    expect(within(dialog).getByRole('button', { name: '退出并删除健康数据' })).toBeTruthy();
  });

  it('locks Computer Use when it is seen from another device', () => {
    const onCapabilityChange = vi.fn();
    const remoteHost: Device = { ...macbook, capabilities: [{ kind: 'computerUse', on: true, fixed: true }, { kind: 'notifications', on: true }] };
    render(<DevicesPageView reach={on} devices={[remoteHost, self(iphone)]} detail="host" onCapabilityChange={onCapabilityChange} />);
    const dialog = screen.getByRole('dialog');
    expect((within(dialog).getByRole('switch', { name: '电脑操作' }) as HTMLButtonElement).disabled).toBe(true);
    expect(within(dialog).getByText('已就绪。在那台电脑的 Avibe 里开关。')).toBeTruthy();
    fireEvent.click(within(dialog).getByRole('switch', { name: '通知' }));
    expect(onCapabilityChange).toHaveBeenCalledWith('host', 'notifications', false);
  });

  it('confirms turning off from another device with how many others are locked out', () => {
    render(<DevicesPageView reach={on} devices={[macbook, self(iphone), chrome]} confirm="turn-off" />);
    expect(screen.getByRole('dialog').textContent).toContain('你正在用 iPhone 远程打开它。关闭后这台设备和其他 1 台都打不开，只能回到 MacBook 上重新开启。');
  });

  it('says the turn-off clause only when others are locked out too, with the right plural', async () => {
    const { rerender } = render(<DevicesPageView reach={on} devices={[macbook, self(iphone)]} confirm="turn-off" />);
    expect(screen.getByRole('dialog').textContent).toContain('关闭后这台设备就打不开了，只能回到 MacBook 上重新开启。');
    await i18n.changeLanguage('en');
    rerender(<DevicesPageView reach={on} devices={[macbook, self(iphone), chrome]} confirm="turn-off" />);
    expect(screen.getByRole('dialog').textContent).toContain('this device and 1 other cannot open it');
  });

  it('tells a member their device leaves their own account, not the owner\'s', () => {
    render(<DevicesPageView reach={on} viewer="member" ownerEmail={account} viewerAccount="sara@example.test"
      devices={[self(chrome), { ...iphone, id: 'sara-phone' }]} detail="sara-phone" />);
    const dialog = screen.getByRole('dialog');
    expect(dialog.textContent).toContain('这台设备会退出 sara@example.test，不能再打开这个 Avibe');
    expect(dialog.textContent).not.toContain(account);
  });

  it('says a list of health data with the locale\'s own conjunction, and one day in the singular', async () => {
    const item = { id: 'h', at: new Date().toISOString(), event: { kind: 'healthRead', days: 1, data: ['steps', 'sleep'] } } as const;
    const { rerender } = render(<DevicesPageView reach={on} devices={[self(macbook), iphone]} activity={[item]} />);
    expect(screen.getByText('Agent 读取了近 1 天的步数和睡眠')).toBeTruthy();
    await i18n.changeLanguage('en');
    rerender(<DevicesPageView reach={on} devices={[self(macbook), iphone]} activity={[item]} />);
    expect(screen.getByText('An Agent read 1 day of steps and sleep')).toBeTruthy();
  });
});
