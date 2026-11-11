// Settings · Models: three proposals for the per-backend mode words once "gateway" is no
// longer an English noun (owner review item 6, 2026-11-12). Each frame is the shipped
// overview with only the mode vocabulary replaced through the `copy` fixture; the owner
// picks one and it moves to `src/i18n`.
import { SettingsModelsPage } from '@/components/settings/models/SettingsModelsPage';

import { frames } from '../../frame';
import { settingsFixtures, settingsPage } from '../settings/fixtures';
import { demoAgents, hubApi } from './fixtures';

const page = settingsPage('models', <SettingsModelsPage />);
const route = '/settings/models';

type Copy = Record<string, { zh: string; en: string }>;

/** The mode vocabulary each option sets: the backend's mode word, its menu and switch
 *  actions, and the words for a hop that uses the CLI's own login. */
const vocabulary = (words: {
  hub: [string, string]; direct: [string, string]; native: [string, string];
  toHub: [string, string]; toDirect: [string, string]; hubSupply: [string, string];
}): Copy => {
  const pair = ([zh, en]: [string, string]) => ({ zh, en });
  const k = 'settings.models.';
  return {
    [`${k}gateway.group.mode.gateway`]: pair(words.hub),
    [`${k}gateway.group.mode.direct`]: pair(words.direct),
    [`${k}mode.hub`]: pair(words.hub),
    [`${k}mode.direct`]: pair(words.direct),
    [`${k}modelStatus.direct`]: pair(words.direct),
    [`${k}direct.pill.direct`]: pair(words.direct),
    [`${k}gateway.modeMenu.gatewayCurrent`]: { zh: `${words.hub[0]}（当前）`, en: `${words.hub[1]} (current)` },
    [`${k}supplyMode.hub.title`]: pair(words.hub),
    [`${k}supplyMode.direct.title`]: pair(words.direct),
    [`${k}supplyMode.currentHub`]: { zh: `当前：${words.hub[0]}`, en: `Current: ${words.hub[1]}` },
    [`${k}supplyMode.currentDirect`]: { zh: `当前：${words.direct[0]}`, en: `Current: ${words.direct[1]}` },
    [`${k}gateway.switchToGateway`]: pair(words.toHub),
    [`${k}direct.action.switchToGateway`]: pair(words.toHub),
    [`${k}agents.connectHub`]: pair(words.toHub),
    [`${k}gateway.switchToDirect`]: pair(words.toDirect),
    [`${k}legend.viaGateway`]: pair(words.hubSupply),
    [`${k}legend.native`]: pair(words.native),
    [`${k}source.nativeSupply`]: pair(words.native),
    [`${k}upstream.group.native`]: { zh: `${words.native[0]} · 本机`, en: `${words.native[1]} · on this machine` },
    [`${k}sources.group.native`]: { zh: `${words.native[0]} · 本机`, en: `${words.native[1]} · on this machine` },
    [`${k}upstream.state.supplyingNative`]: { zh: `{{backend}} 正在使用（${words.native[0]}）`, en: `Supplying {{backend}} (${words.native[1].toLowerCase()})` },
    [`${k}upstream.kind.nativeCredential`]: { zh: `${words.native[0]} · 本机登录`, en: `${words.native[1]} · local credential` },
  };
};

const option = (name: string, title: string, copy: Copy) => ({
  name,
  title,
  width: 1440,
  height: 1720,
  status: 'proposed' as const,
  themes: ['light' as const],
  route,
  fixtures: { ...settingsFixtures(hubApi({ agents: demoAgents })), copy },
  render: page,
});

const designFrames = frames([
  // A: the hub's own name is the mode word; the CLI's own login is "own login" in both places.
  option('a-model-hub', '模式用词 A · Model Hub / Direct，本机登录叫 Own login', vocabulary({
    hub: ['模型网关', 'Model Hub'], direct: ['直连', 'Direct'], native: ['自带登录', 'Own login'],
    toHub: ['切换到模型网关', 'Switch to Model Hub'], toDirect: ['切到直连', 'Switch to direct'],
    hubSupply: ['模型网关供给', 'Model Hub supply'],
  })),
  // B: say who holds the credentials, the way Chinese already does (模型网关管理 / Agent 管理).
  option('b-managed', '模式用词 B · Managed / Own login，与中文「模型网关管理 / Agent 管理」一致', vocabulary({
    hub: ['模型网关管理', 'Managed'], direct: ['Agent 管理', 'Own login'], native: ['Agent 管理', 'Own login'],
    toHub: ['交给模型网关管理', 'Let Model Hub manage'], toDirect: ['改为 Agent 管理', 'Use own login'],
    hubSupply: ['模型网关管理', 'Managed by Model Hub'],
  })),
  // C: two short chip words, and Direct mode and the native hop share one word.
  option('c-hub-native', '模式用词 C · Hub / Native，直连与本机登录同用 Native', vocabulary({
    hub: ['网关', 'Hub'], direct: ['原生', 'Native'], native: ['原生', 'Native'],
    toHub: ['切换到网关', 'Switch to Hub'], toDirect: ['切到原生', 'Switch to native'],
    hubSupply: ['网关供给', 'Hub supply'],
  })),
]);

export default designFrames;
