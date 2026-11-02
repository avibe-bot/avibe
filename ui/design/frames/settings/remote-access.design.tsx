// Settings · Remote access: pair this machine with Avibe Cloud and reach it from anywhere.
import { RemoteAccessPage } from '@/components/RemoteAccessPage';

import { frames } from '../../frame';
import { settingsConfig, settingsFixtures, settingsPage } from './fixtures';

const page = settingsPage('remote-access', <RemoteAccessPage />);
const route = '/settings/remote-access';

const unpaired = settingsFixtures({ '/api/remote-access/status': { ok: true, enabled: false, paired: false, running: false } });

const pairedConfig = {
  ...settingsConfig,
  remote_access: {
    ...settingsConfig.remote_access,
    vibe_cloud: { ...settingsConfig.remote_access.vibe_cloud, enabled: true, paired: true, public_url: 'https://demo-mac.app.avibe.bot' },
  },
};
const paired = settingsFixtures({
  '/api/config': pairedConfig,
  '/api/remote-access/status': {
    ok: true,
    enabled: true,
    paired: true,
    running: true,
    public_url: 'https://demo-mac.app.avibe.bot',
    pid_state: 'running',
    transport_protocol: 'auto',
    settings: { transport_protocol: 'auto', auto_recovery: true, optimization_profile: 'balanced', edge_ip_version: '4', edge_bind_address: '' },
  },
  '/api/remote-access/network-interfaces': { ok: true, interfaces: [{ id: 'en0', name: 'Wi-Fi', address: '192.168.1.23', ip_version: '4' }] },
});

const designFrames = frames([
  { name: 'unpaired', title: '设置 · 远程访问 · 未配对', width: 1440, height: 900, status: 'shipped', route, fixtures: unpaired, render: page },
  { name: 'running', title: '设置 · 远程访问 · 已配对、运行中', width: 1440, height: 900, status: 'shipped', route, fixtures: paired, render: page },
  { name: 'phone', title: '设置 · 远程访问 · 手机', width: 390, height: 844, status: 'shipped', route, fixtures: unpaired, render: page },
]);

export default designFrames;
