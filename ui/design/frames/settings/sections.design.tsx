// The Settings section list: a phone's Settings root (a desktop keeps it as the rail beside each page).
import { Route, Routes } from 'react-router-dom';

import { SettingsLayout } from '@/components/settings/SettingsLayout';

import { frames } from '../../frame';
import { settingsFixtures } from './fixtures';

const designFrames = frames([
  {
    name: 'phone',
    title: '设置 · 分区列表 · 手机',
    width: 390,
    height: 844,
    status: 'shipped',
    route: '/settings',
    fixtures: settingsFixtures(),
    render: () => (
      <Routes>
        <Route path="/settings" element={<SettingsLayout />} />
      </Routes>
    ),
  },
]);

export default designFrames;
