// Settings · Models in plain words. Provider cards name the kind only (the API format is in the
// provider's details), and a model no provider lists reads 「原样转发」 rather than 「透传」.
import { SettingsModelsPage } from '@/components/settings/models/SettingsModelsPage';

import { frames, type FrameSpec } from '../../frame';
import { settingsFixtures, settingsPage } from '../settings/fixtures';
import { demoAgents, demoSources, hubApi } from './fixtures';

const page = settingsPage('models', <SettingsModelsPage />);
const route = '/settings/models';
const themes: FrameSpec['themes'] = ['light'];

const designFrames = frames([
  { name: 'overview', title: '模型网关 · 卡片不写 API 格式；没列出的模型「原样转发」', width: 1440, height: 2200, themes, status: 'shipped', route, fixtures: settingsFixtures(hubApi({ sources: demoSources, agents: demoAgents })), render: page },
]);

export default designFrames;
