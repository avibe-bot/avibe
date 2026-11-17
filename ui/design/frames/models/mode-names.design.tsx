// The own-login mode reads 「自己登录」 in Chinese, matching the English "Own login": the provider
// group label, the card's kind badge and its 「正在使用」 line. 「模型网关管理」 stays.
import { SettingsModelsPage } from '@/components/settings/models/SettingsModelsPage';

import { frames, type FrameSpec } from '../../frame';
import { settingsFixtures, settingsPage } from '../settings/fixtures';
import { demoAgents, demoSources, hubApi } from './fixtures';

const page = settingsPage('models', <SettingsModelsPage />);
const route = '/settings/models';
const themes: FrameSpec['themes'] = ['light'];
const base = settingsFixtures(hubApi({ sources: demoSources, agents: demoAgents }));

const designFrames = frames([
  { name: 'overview', title: '模式名称 · 自己登录 / 模型网关管理', width: 1440, height: 1100, themes, status: 'shipped', route, fixtures: base, render: page },
]);

export default designFrames;
