// Proposed 2026-11-21 (frames only): 「连接方式」 after the 07:51 decisions (kit/devices-connection-v2.tsx),
// replacing the keep-or-put-back trial frames of settings/devices-connection. The instance verifies a
// tunnel change itself and reports one status line; direct and the relay are plain switches, refused
// with a reason when the change would cut the person off; the relay row exists only on an enrolled
// instance; a run of 3 minutes without a connection sets fixed values back to automatic, on record.
import { frames, type FrameSpec } from '../../frame';
import { ConnectionV2, type ConnectionV2Props } from '../../kit/devices-connection-v2';
import { settingsFixtures, settingsPage } from './fixtures';

const route = '/settings/remote-access';
const copy = { 'settings.sections.remoteAccess': { zh: '设备', en: 'Devices' } };
const fixtures = settingsFixtures({}, { copy });
const t = (zh: string, en: string) => ({ zh, en });

const base: ConnectionV2Props = { viewer: 'remote', direct: 'ready', protocol: 'auto' };
const WIDE = { width: 1440, height: 1300 };
const PHONE = { width: 375, height: 1400 };

const frame = (name: string, title: string, size: { width: number; height: number }, props: ConnectionV2Props, extra: Partial<FrameSpec> = {}): FrameSpec => ({
  name, title, ...size, status: 'proposed', themes: ['light'], route, fixtures,
  render: settingsPage('remote-access', <ConnectionV2 {...props} />),
  ...extra,
});

const designFrames = frames([
  frame('verifying', '传输协议改为 HTTP/2 之后：验证中（不承诺秒数，没有「保留」按钮）', WIDE,
    { ...base, protocol: 'http2', change: { setting: 'protocol', state: 'verifying' } }, { themes: ['light', 'dark'] }),
  frame('applied', '验证通过：已生效', WIDE, { ...base, protocol: 'http2', change: { setting: 'protocol', state: 'applied' } }),
  frame('restored', '没连上：已改回原来的值', WIDE,
    { ...base, change: { setting: 'protocol', state: 'restored', reason: 'verification_failed', from: t('自动', 'Automatic') } }),
  frame('restored-unverifiable', '改之前隧道就不通、无法验证：已改回', WIDE,
    { ...base, change: { setting: 'ipFamily', state: 'restored', reason: 'verification_unavailable', from: t('IPv4', 'IPv4') } }),
  frame('restore-pending', '已改回，隧道还在恢复', WIDE,
    { ...base, change: { setting: 'outbound', state: 'failed', reason: 'saved_restored_runtime_pending', from: t('跟随系统', 'System automatic') } }),
  frame('direct-refused', '关直连被拒：隧道此刻不通、你正靠直连连着，一句话说明', WIDE,
    { ...base, refused: { row: 'direct', reason: 'tunnel_unhealthy' } }),
  frame('relay', '已从本机命令行加入中继试用：有中继这一行；开着时直连只有所有者能用', WIDE,
    { ...base, relay: { enabled: true, connected: true } }),
  frame('relay-refused', '改中继被拒：隧道此刻不通、你正靠直连连着', WIDE,
    { ...base, relay: { enabled: false, connected: false }, refused: { row: 'relay', reason: 'tunnel_unhealthy' } }),
  frame('no-relay', '没有加入试用的实例：完全没有中继这一行（在本机打开，隧道标「可用」）', WIDE,
    { ...base, viewer: 'local' }),
  frame('returned-to-automatic', '固定值连续 3 分钟连不上，系统改回自动之后的那条记录', WIDE,
    { ...base, returnedToAutomatic: { at: t('今天 09:12', 'Today 09:12'), changes: [{ setting: 'protocol', from: t('HTTP/2', 'HTTP/2') }, { setting: 'ipFamily', from: t('IPv6', 'IPv6') }] } }),
  frame('verifying-phone', '验证中 · 375 宽', PHONE, { ...base, protocol: 'http2', change: { setting: 'protocol', state: 'verifying' } }),
  frame('relay-phone', '有中继这一行 · 375 宽', PHONE, { ...base, relay: { enabled: true, connected: false }, refused: { row: 'direct', reason: 'tunnel_unhealthy' } }),
]);

export default designFrames;
