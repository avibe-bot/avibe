// Settings · Shortcuts: the keyboard shortcuts of chat and Show Pages.
import { SettingsShortcutsPage } from '@/components/settings/SettingsShortcutsPage';

import { frames } from '../../frame';
import { settingsFixtures, settingsPage } from './fixtures';

const page = settingsPage('shortcuts', <SettingsShortcutsPage />);
const route = '/settings/shortcuts';

const designFrames = frames([
  { name: 'default', title: '设置 · 快捷键', width: 1440, height: 900, status: 'shipped', route, fixtures: settingsFixtures(), render: page },
  { name: 'phone', title: '设置 · 快捷键 · 手机', width: 390, height: 844, status: 'shipped', route, fixtures: settingsFixtures(), render: page },
]);

export default designFrames;
