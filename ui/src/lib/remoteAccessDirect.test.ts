import { describe, expect, it } from 'vitest';

import i18n from '../i18n';
import { deviceUsingText, type DeviceUsing } from './remoteAccessDirect';

// ui/design: settings/remote-access/direct-app-*
describe('deviceUsingText', () => {
  const text = async (lang: 'zh' | 'en', using: DeviceUsing) => {
    await i18n.changeLanguage(lang);
    return deviceUsingText(i18n.t, using);
  };

  it("writes Product's words, with the family and the measured round trip", async () => {
    expect(await text('zh', { kind: 'tunnel', relayUp: false })).toBe('本设备正在使用：Cloudflare 隧道');
    expect(await text('zh', { kind: 'tunnel', relayUp: true })).toBe('本设备正在使用：Cloudflare 隧道（中继可用，但更慢）');
    expect(await text('zh', { kind: 'relay', rttMs: null })).toBe('本设备正在使用：中继');
    expect(await text('zh', { kind: 'relay', rttMs: 140 })).toBe('本设备正在使用：中继 · 140 ms');
    expect(await text('zh', { kind: 'punched', family: 'ipv6', rttMs: 85 })).toBe('本设备正在使用：打洞直连 · IPv6 · 85 ms');
    expect(await text('zh', { kind: 'lan', family: 'ipv4', rttMs: null })).toBe('本设备正在使用：局域网直连 · IPv4');
    expect(await text('zh', { kind: 'direct', family: 'ipv6', rttMs: null })).toBe('本设备正在使用：直连 · IPv6');
  });

  it('follows the same pattern in English', async () => {
    expect(await text('en', { kind: 'tunnel', relayUp: true }))
      .toBe('This device is using: Cloudflare tunnel (relay available, but slower)');
    expect(await text('en', { kind: 'punched', family: 'ipv4', rttMs: 85 }))
      .toBe('This device is using: Hole-punched direct · IPv4 · 85 ms');
    expect(await text('en', { kind: 'lan', family: 'ipv6', rttMs: 2 })).toBe('This device is using: LAN direct · IPv6 · 2 ms');
    await i18n.changeLanguage('zh');
  });
});
