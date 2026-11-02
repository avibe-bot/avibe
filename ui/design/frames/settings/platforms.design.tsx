// Settings · Platform connections: the enabled-platform grid and one card per connected platform,
// plus a platform's credential form as its card opens it (the shipped embed, drawn on its own).
import { PlatformConfigEmbed } from '@/components/settings/PlatformConfigEmbed';
import { SettingsPlatformsPage } from '@/components/settings/SettingsPlatformsPage';

import { frames } from '../../frame';
import { settingsConfig, settingsFixtures, settingsPage } from './fixtures';

const page = settingsPage('platforms', <SettingsPlatformsPage />);
const route = '/settings/platforms';

// The app manifest the Slack form offers to create the app from.
const manifest = JSON.stringify({ display_information: { name: 'Avibe' }, features: { bot_user: { display_name: 'Avibe' } }, settings: { socket_mode_enabled: true } });
const slackFormFixtures = settingsFixtures({ '/api/slack/manifest': { ok: true, manifest: JSON.stringify(JSON.parse(manifest), null, 2), manifest_compact: manifest } });

const designFrames = frames([
  {
    name: 'default',
    title: '设置 · 平台接入 · 已连 3 个平台',
    width: 1440,
    height: 900,
    status: 'shipped',
    route,
    fixtures: settingsFixtures(),
    render: page,
  },
  {
    name: 'phone',
    title: '设置 · 平台接入 · 手机',
    width: 390,
    height: 844,
    status: 'shipped',
    route,
    fixtures: settingsFixtures(),
    render: page,
  },
  {
    name: 'slack-form',
    title: '设置 · 平台接入 · Slack 凭证表单（卡片展开后）',
    width: 880,
    height: 720,
    status: 'shipped',
    fixtures: slackFormFixtures,
    render: () => (
      <div className="h-full overflow-hidden bg-background px-5 py-4">
        <PlatformConfigEmbed platform="slack" config={settingsConfig} onApply={async () => {}} onCancel={() => {}} />
      </div>
    ),
  },
]);

export default designFrames;
