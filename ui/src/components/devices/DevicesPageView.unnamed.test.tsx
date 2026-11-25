/** @vitest-environment jsdom */

import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import i18n from '../../i18n';
import { DevicesPageView } from './DevicesPageView';
import type { Device, Reach } from './types';

// A row with no name of its own: it is called an unrecognized client, and the second line says when
// it signed in, so two of them can be told apart. A named row is unchanged.

const on: Reach = { state: 'on', host: 'max-app.avibe.bot', tunnel: 'up', ms: 120 };
const macbook: Device = { id: 'host', group: 'mine', kind: 'host', name: 'MacBook', client: { kind: 'runtime' }, self: true, functions: [] };
const now = new Date(2026, 10, 25, 12, 0);
const at = (day: number, hour: number) => new Date(2026, 10, day, hour, 5).toISOString();
const unnamed = (id: string, kind: 'browser' | 'other', signedInAt: string): Device => ({
  id, group: 'mine', kind: kind === 'browser' ? 'browser' : 'other', name: null, client: { kind }, functions: [], signedInAt,
});
const devices: Device[] = [
  macbook,
  unnamed('a', 'browser', at(25, 9)),
  unnamed('b', 'other', at(24, 22)),
  unnamed('c', 'browser', at(19, 8)),
  { id: 'd', group: 'mine', kind: 'browser', name: 'Chrome · macOS', client: { kind: 'browser' }, functions: [], signedInAt: at(25, 9) },
  { id: 'e', group: 'mine', kind: 'desktop', name: null, client: { kind: 'desktop' }, functions: [], signedInAt: at(25, 9) },
  { id: 'f', group: 'mine', kind: 'other', name: 'WebKit · macOS', client: { kind: 'other' }, functions: [], signedInAt: at(25, 9) },
];

beforeEach(() => { vi.useFakeTimers({ toFake: ['Date'] }); vi.setSystemTime(now); });
afterEach(() => { cleanup(); vi.useRealTimers(); });

describe('DevicesPageView · rows with no name', () => {
  it('calls them unrecognized clients and says when each signed in (zh)', async () => {
    await i18n.changeLanguage('zh');
    render(<DevicesPageView reach={on} devices={devices} />);
    expect(screen.getAllByText('未识别的客户端')).toHaveLength(3);
    expect(screen.getByText('今天 09:05 登录')).toBeTruthy();
    expect(screen.getByText('昨天 22:05 登录')).toBeTruthy();
    expect(screen.getByText(/^11月19日 登录$/)).toBeTruthy();
    // A named row and a known kind keep their own words, with no sign-in line.
    expect(screen.getByText('Chrome · macOS')).toBeTruthy();
    expect(screen.getByText('Avibe Desktop')).toBeTruthy();
    expect(screen.getAllByText(/登录$/)).toHaveLength(3);
    // A named row of an unknown kind does not add 「设备」 under its name.
    expect(screen.getByText('WebKit · macOS')).toBeTruthy();
    expect(screen.getAllByText('设备').every((each) => each.tagName === 'H1')).toBe(true);
  });

  it('says the same in English', async () => {
    await i18n.changeLanguage('en');
    render(<DevicesPageView reach={on} devices={devices} />);
    expect(screen.getAllByText('Unrecognized client')).toHaveLength(3);
    expect(screen.getByText('Signed in today at 09:05')).toBeTruthy();
    expect(screen.getByText('Signed in yesterday at 22:05')).toBeTruthy();
    expect(screen.getByText('Signed in Nov 19')).toBeTruthy();
  });
});
