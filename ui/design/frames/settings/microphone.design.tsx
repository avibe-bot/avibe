import { Route, Routes } from 'react-router-dom';

import { SettingsLayout } from '@/components/settings/SettingsLayout';

import { frames } from '../../frame';
import { MicrophoneSettingsPreview } from './microphone.preview';
import { settingsFixtures } from './fixtures';
import type { MicrophoneSettingsState } from '@/components/settings/MicrophoneSettingsPage';

const copy = { 'settings.sections.microphone': { zh: '麦克风', en: 'Microphone' } };

const page = () => (
  <Routes>
    <Route path="/settings" element={<SettingsLayout />}>
      <Route path="microphone" element={<MicrophoneSettingsPreview />} />
    </Route>
  </Routes>
);

const states: MicrophoneSettingsState[] = [
  'default',
  'selected',
  'picker',
  'testing',
  'quiet',
  'prompt',
  'requesting',
  'denied',
  'missing',
  'disconnected',
];
const labels: Record<MicrophoneSettingsState, string> = {
  default: '系统默认',
  selected: '指定设备',
  picker: '选择设备',
  testing: '正在收声',
  quiet: '暂时安静',
  prompt: '尚未授权',
  requesting: '等待授权',
  denied: '权限已拒绝',
  missing: '设备缺失',
  disconnected: '测试中断开',
};

const designFrames = frames([
  {
    name: 'ios-menu',
    title: '麦克风 · iOS · 设置入口',
    width: 390,
    height: 844,
    status: 'shipped',
    route: '/settings',
    fixtures: settingsFixtures({}, { copy, microphone: { platform: 'ios', state: 'default' } }),
    render: page,
  },
  ...states.map((state) => ({
    name: `desktop-${state}`,
    title: `麦克风 · Desktop · ${labels[state]}`,
    width: 1280,
    height: 800,
    status: 'shipped' as const,
    route: '/settings/microphone',
    fixtures: settingsFixtures({}, { desktopShell: true, copy, microphone: { platform: 'desktop', state } }),
    render: page,
  })),
  ...([1280, 720] as const).map((width) => ({
    name: `desktop-long-device-${width}`,
    title: 'Microphone · Desktop · Long device name',
    width,
    height: 800,
    status: 'shipped' as const,
    route: '/settings/microphone',
    fixtures: settingsFixtures({}, {
      desktopShell: true,
      microphone: {
        platform: 'desktop',
        state: 'selected',
        devices: [{ deviceId: 'usb', label: 'Studio USB microphone — Conference room input / 会议室外接麦克风 (High Definition Audio Device)' }],
      },
    }),
    render: page,
  })),
  ...(['default', 'testing', 'quiet', 'prompt', 'denied'] as const).map((state) => ({
    name: `ios-${state}`,
    title: `麦克风 · iOS · ${labels[state]}`,
    width: 390,
    height: 844,
    status: 'shipped' as const,
    route: '/settings/microphone',
    fixtures: settingsFixtures({}, { copy, microphone: { platform: 'ios', state } }),
    render: page,
  })),
]);

export default designFrames;
