import { describe, expect, it } from 'vitest';

import i18n from '../../i18n';
import { activityText } from './rules';
import type { DeviceActivity } from './types';

// A device no longer in the list is a phrase, not a name: in Chinese its sentences have no space
// around it, while a name keeps the ones that set it off. Every sentence that names a device has one.

const at = '2026-11-25T01:00:00.000Z';
const entry = (device: string | null, event: DeviceActivity['event']): DeviceActivity => ({ id: 'a', at, device, event });

describe('activityText · a device no longer listed', () => {
  it('says its sentences without a space in Chinese', async () => {
    const t = await i18n.changeLanguage('zh');
    expect(activityText(t, 'zh', entry(null, { kind: 'signedOut', actor: 'Max 的 MacBook Pro' })))
      .toBe('一台已不在列表里的设备已被退出 · 由 Max 的 MacBook Pro 操作');
    expect(activityText(t, 'zh', entry(null, { kind: 'signedOut', actor: null }))).toBe('一台已不在列表里的设备已被退出');
    expect(activityText(t, 'zh', entry(null, { kind: 'signedIn' }))).toBe('一台已不在列表里的设备登录');
    expect(activityText(t, 'zh', entry(null, { kind: 'alarmCreated', label: '06:30 起床', confirmed: true })))
      .toBe('创建闹钟「06:30 起床」，已在一台已不在列表里的设备上确认');
    expect(activityText(t, 'zh', entry('iPhone 15 Pro', { kind: 'reminderCreated', label: null, confirmed: true })))
      .toBe('创建了一个提醒，已在 iPhone 15 Pro 上确认');
    // A name keeps its space.
    expect(activityText(t, 'zh', entry('iPhone 15 Pro', { kind: 'signedOut', actor: null }))).toBe('iPhone 15 Pro 已被退出');
  });

  it('says the same in English', async () => {
    const t = await i18n.changeLanguage('en');
    expect(activityText(t, 'en', entry(null, { kind: 'signedOut', actor: 'Max’s MacBook Pro' })))
      .toBe('A device no longer in the list was signed out · from Max’s MacBook Pro');
    expect(activityText(t, 'en', entry(null, { kind: 'signedIn' }))).toBe('A device no longer in the list signed in');
    expect(activityText(t, 'en', entry(null, { kind: 'reminderCreated', label: null, confirmed: true })))
      .toMatch(/, confirmed on a device no longer in the list$/);
  });
});
