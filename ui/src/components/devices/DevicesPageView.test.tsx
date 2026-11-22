/** @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import i18n from '../../i18n';
import { DevicesPageView } from './DevicesPageView';
import {
  TESTFLIGHT_INVITE_URL, capabilityControl, capabilityOffNeedsConfirm, signOutNeedsConfirm, signOutOthersNeedsConfirm,
  turnOffNeedsConfirm,
} from './rules';
import type { Device, Reach } from './types';

// ui/design: settings/devices/*
const account = 'max@example.test';
const host = 'max-app.avibe.bot';
const on: Reach = { state: 'on', host, account, tunnel: 'good', ms: 120 };

const macbook: Device = {
  id: 'host', group: 'mine', kind: 'host', name: 'MacBook', client: { kind: 'runtime', version: '3.2.8', platform: 'macOS' }, online: {},
  capabilities: [{ kind: 'computerUse', on: true }, { kind: 'notifications', on: true }],
};
const iphone: Device = {
  id: 'iphone', group: 'mine', kind: 'iphone', name: 'iPhone', client: { kind: 'app' }, online: { path: 'punched', ms: 38 },
  capabilities: [{ kind: 'notifications', on: true }, { kind: 'health', on: true, data: ['steps'] }],
};
const chrome: Device = { id: 'chrome', group: 'mine', kind: 'browser', name: 'Chrome', client: { kind: 'browser' }, capabilities: [] };
const sara: Device = { ...chrome, id: 'sara', group: 'others', name: 'Sara Chrome', person: 'sara@example.test' };
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
    // Health switched off keeps what it synced: signing out still deletes it.
    expect(signOutNeedsConfirm({ ...iphone, capabilities: [{ kind: 'health', on: false, data: ['steps'], syncedAt: new Date().toISOString() }] })).toBe(true);
    // Turning remote access off at the computer itself locks nobody out; from another device it does,
    // and so it asks while this device is not known.
    expect(turnOffNeedsConfirm([self(macbook), iphone])).toBe(false);
    expect(turnOffNeedsConfirm([macbook, self(iphone)])).toBe(true);
    expect(turnOffNeedsConfirm([macbook, iphone])).toBe(true);
    // Health off deletes what it synced; signing out every other device always asks.
    expect(capabilityOffNeedsConfirm({ kind: 'health', on: true, data: ['steps'] })).toBe(true);
    expect(capabilityOffNeedsConfirm({ kind: 'notifications', on: true })).toBe(false);
    expect(signOutOthersNeedsConfirm()).toBe(true);
  });

  it('shows a member only their own devices, with no instance controls', () => {
    render(<DevicesPageView reach={on} viewer="member" ownerEmail={account} devices={[self(chrome)]}
      activity={[{ id: 'a', at: new Date().toISOString(), event: { kind: 'directOn' } }]} />);
    expect(screen.getByText(`你登录了这个 Avibe 的设备。这个 Avibe 由 ${account} 管理。`)).toBeTruthy();
    expect(screen.queryByRole('switch', { name: '远程访问' })).toBeNull();
    expect(screen.queryByText('其他人的设备')).toBeNull();
    expect(screen.queryByText('最近活动')).toBeNull();
    // Adding a device of their own is theirs too.
    expect(screen.getByRole('button', { name: '添加设备' })).toBeTruthy();
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
    expect(within(dialog).queryByRole('switch', { name: '电脑操作' })).toBeNull();
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

  it('shows someone else\'s device functions without letting the owner change them', () => {
    const saraPhone: Device = { ...iphone, id: 'sara-phone', group: 'others', person: 'sara@example.test' };
    render(<DevicesPageView reach={on} devices={[self(macbook), saraPhone]} detail="sara-phone" />);
    const dialog = screen.getByRole('dialog');
    expect(within(dialog).queryAllByRole('switch')).toHaveLength(0);
    expect(within(dialog).getAllByText('已开启')).toHaveLength(2);
  });

  it('offers Health only as a way to turn it off, and lists it as off once it is', () => {
    expect(capabilityControl(iphone, iphone.capabilities[1])).toBe('switch');
    const healthOff: Device = { ...iphone, capabilities: [{ kind: 'health', on: false, data: ['steps'], syncedAt: new Date().toISOString() }] };
    expect(capabilityControl(healthOff, healthOff.capabilities[0])).toBe('state');
    const onCapabilityChange = vi.fn();
    const { rerender } = render(<DevicesPageView reach={on} devices={[self(macbook), iphone]} detail="iphone" onCapabilityChange={onCapabilityChange} />);
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('switch', { name: '健康' }));
    expect(onCapabilityChange).toHaveBeenCalledWith('iphone', 'health', false);
    rerender(<DevicesPageView reach={on} devices={[self(macbook), iphone]} detail="iphone" confirm="health-off" />);
    const dialog = screen.getByRole('dialog');
    expect(within(dialog).getByText('关闭健康？')).toBeTruthy();
    expect(dialog.textContent).toContain('已同步的健康数据会删除。要再用，在 iPhone 上重新连接健康。');
    // Named apart from the dialog's own 关闭, so a screen reader tells them apart.
    expect(within(dialog).getByRole('button', { name: '关闭健康' })).toBeTruthy();
    expect(within(dialog).getAllByRole('button', { name: '关闭' })).toHaveLength(1);
    rerender(<DevicesPageView reach={on} devices={[self(macbook), healthOff]} detail="iphone" />);
    expect(within(screen.getByRole('dialog')).queryByRole('switch', { name: '健康' })).toBeNull();
  });

  it('names no one it does not know when turning off, and counts everyone locked out', () => {
    const { rerender } = render(<DevicesPageView reach={on} devices={[{ ...macbook, name: '' }, iphone, sara]} confirm="turn-off" />);
    let text = screen.getByRole('dialog').textContent ?? '';
    expect(text).not.toContain('你正在用');
    // This device is one of the two remote ones, so it is not counted among the others.
    expect(text).toContain('关闭后这台设备和其他 1 台都打不开，只能回到运行 Avibe 的电脑上重新开启。域名会为你保留。');
    rerender(<DevicesPageView reach={on} devices={[macbook, self(iphone), sara, { ...chrome, id: 'push', group: 'unattributed' }]} confirm="turn-off" />);
    text = screen.getByRole('dialog').textContent ?? '';
    expect(text).toContain('你正在用 iPhone 远程打开它。关闭后这台设备和其他 1 台都打不开，只能回到 MacBook 上重新开启。');
  });

  it('never counts an unattributed push row as one of mine', () => {
    const push: Device = { ...chrome, id: 'push', name: 'Unattributed', group: 'unattributed' };
    render(<DevicesPageView reach={on} devices={[self(macbook), iphone, push]} />);
    expect(screen.queryByText('Unattributed')).toBeNull();
    // Only one other device of mine is signed in: no 「退出其他所有设备」.
    expect(screen.queryByRole('button', { name: '退出其他所有设备' })).toBeNull();
  });

  it('shows the five latest entries on the page and the whole list newest first, or says there is none', () => {
    const now = Date.now();
    const entries = Array.from({ length: 7 }, (_, index) => ({
      id: `e${index}`, at: new Date(now - (7 - index) * 60_000).toISOString(), event: { kind: 'signedIn', device: `D${index}` },
    } as const));
    const onBack = vi.fn();
    const { rerender } = render(<DevicesPageView reach={on} devices={[self(macbook), iphone]} activity={entries} />);
    expect(screen.getAllByText(/^D\d 登录$/).map((each) => each.textContent)).toEqual(['D6 登录', 'D5 登录', 'D4 登录', 'D3 登录', 'D2 登录']);
    rerender(<DevicesPageView reach={on} devices={[self(macbook), iphone]} activity={entries} view="activity" onBack={onBack} />);
    expect(screen.getAllByText(/^D\d 登录$/)).toHaveLength(7);
    expect(screen.getAllByText(/^D\d 登录$/)[0].textContent).toBe('D6 登录');
    fireEvent.click(screen.getByRole('button', { name: '返回设备' }));
    expect(onBack).toHaveBeenCalledTimes(1);
    rerender(<DevicesPageView reach={on} devices={[self(macbook)]} activity={[]} view="activity" />);
    expect(screen.getByText('还没有活动。')).toBeTruthy();
  });

  it('says when there are no devices, and still offers to add one', () => {
    render(<DevicesPageView reach={on} viewer="member" ownerEmail={account} viewerAccount="sara@example.test" devices={[]} />);
    expect(screen.getByText('还没有设备。')).toBeTruthy();
    expect(screen.getByText('打开 App，用 sara@example.test 登录。')).toBeTruthy();
  });

  it('gives the code\'s address as a link to copy, for a phone that cannot scan its own screen', () => {
    const onCopyLink = vi.fn();
    render(<DevicesPageView reach={on} devices={[self(macbook)]} onCopyLink={onCopyLink} />);
    fireEvent.click(screen.getByRole('button', { name: '复制链接' }));
    expect(onCopyLink).toHaveBeenCalledWith(TESTFLIGHT_INVITE_URL);
  });

  it('says a sync time in English with the sentence\'s own template', async () => {
    await i18n.changeLanguage('en');
    const old = new Date(Date.now() - 20 * 86_400_000);
    old.setHours(8, 12, 0, 0);
    const synced: Device = { ...iphone, capabilities: [{ kind: 'health', on: true, data: ['steps', 'sleep'], syncedAt: old.toISOString() }] };
    render(<DevicesPageView reach={on} devices={[self(macbook), synced]} detail="iphone" />);
    const date = new Intl.DateTimeFormat('en', { month: 'short', day: 'numeric' }).format(old);
    expect(screen.getByText(`Steps and sleep · synced ${date} at 08:12`)).toBeTruthy();
  });

  it('leaves out a sync time it cannot parse, rather than failing the page', () => {
    const odd: Device = { ...iphone, capabilities: [{ kind: 'health', on: true, data: ['steps'], syncedAt: 'soon' }] };
    render(<DevicesPageView reach={on} devices={[self(macbook), odd]} detail="iphone" />);
    expect(within(screen.getByRole('dialog')).getByText('步数')).toBeTruthy();
  });
});
