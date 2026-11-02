// Settings · Platform connections: the enabled-platform grid and one card per connected platform,
// plus each platform's credential form as its card opens it (the shipped embed, drawn on its own).
import { PlatformConfigEmbed } from '@/components/settings/PlatformConfigEmbed';
import { SettingsPlatformsPage } from '@/components/settings/SettingsPlatformsPage';

import { frames } from '../../frame';
import { settingsConfig, settingsFixtures, settingsPage } from './fixtures';

const page = settingsPage('platforms', <SettingsPlatformsPage />);
const route = '/settings/platforms';

// The app manifest the Slack form offers to create the app from.
const manifest = JSON.stringify({ display_information: { name: 'Avibe' }, features: { bot_user: { display_name: 'Avibe' } }, settings: { socket_mode_enabled: true } });
const slackFormFixtures = settingsFixtures({ '/api/slack/manifest': { ok: true, manifest: JSON.stringify(JSON.parse(manifest), null, 2), manifest_compact: manifest } });

/** A platform's credential form as its card opens it: Discord and Telegram already connected, Lark and
 *  WeChat being added for the first time. */
const FORM_TITLES = {
  discord: '设置 · 平台接入 · Discord 凭证表单（已连接）',
  telegram: '设置 · 平台接入 · Telegram 凭证表单（已连接）',
  lark: '设置 · 平台接入 · 飞书 / Lark 凭证表单（首次添加）',
  wechat: '设置 · 平台接入 · 微信扫码登录（首次添加）',
};
const FORM_HEIGHTS = { discord: 930, telegram: 1360, lark: 820, wechat: 480 };
const platformForm = (platform: string) => () => (
  <div className="h-full overflow-hidden bg-background px-5 py-4">
    <PlatformConfigEmbed platform={platform} config={settingsConfig} onApply={async () => {}} onCancel={() => {}} />
  </div>
);

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
  ...(['discord', 'telegram', 'lark', 'wechat'] as const).map((platform) => ({
    name: `${platform}-form`,
    title: FORM_TITLES[platform],
    width: 880,
    height: FORM_HEIGHTS[platform],
    status: 'shipped' as const,
    // Lark stops any temporary event connection a previous visit left open.
    fixtures: settingsFixtures({ '/api/lark/temp_ws/stop': { ok: true } }),
    render: platformForm(platform),
  })),
]);

export default designFrames;
