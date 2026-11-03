// The Bot profile dialog (docs/plans/bots.md D5; owner decisions 2026-11-03). A custom Bot's
// avatar picker leads with one emoji per common role and opens every emoji from "More": in its
// popover on desktop, as a sheet on a phone. The main Bot always shows the Vibey mascot, so its
// dialog has no avatar controls and only its name changes.
import { BotProfileDialog } from '@/components/workbench/bots/BotProfileDialog';
import type { BotProfileRequest } from '@/context/BotsContext';

import { frames } from '../../frame';
import { bot, type WorkbenchFixtures } from '../../shell/fixtures';
import { agentsApi, team } from '../../shell/teamFixtures';
import { WhenBotsListed } from '../../shell/WhenBotsListed';

const fixtures: WorkbenchFixtures = {
  api: agentsApi,
  bots: team,
  // Emoji picked before from the full set, which it then lists first.
  storage: { 'avibe.bots.emoji-recents.v1': JSON.stringify(['🦊', '🐙', '🧑‍🚀', '🪐', '🐝']) },
};
/** The security lead with an emoji from the full set rather than the leading ones. */
const foxFixtures: WorkbenchFixtures = {
  ...fixtures,
  bots: team.map((each) => (each.handle === 'cso'
    ? bot('cso', '首席安全官', '依赖与权限的安全审计，每周汇报', { kind: 'emoji', emoji: '🦊', hue: 12 }, 1)
    : each)),
};

const dialog = (request: BotProfileRequest) => () => (
  <WhenBotsListed>
    <BotProfileDialog request={request} onClose={() => {}} />
  </WhenBotsListed>
);
const editCso = dialog({ kind: 'edit', handle: 'cso' });
const editMain = dialog({ kind: 'edit', handle: 'main' });

const openPicker = ['role=button[name="{t:bots.dialog.avatar}"]'];
const openAll = [...openPicker, 'role=button[name="{t:bots.avatarPicker.more}"]'];

const designFrames = frames([
  { name: 'avatar-picker', title: '伙伴资料 · 头像：按角色挑的常用表情，最后一格是「更多」', width: 1000, height: 760, status: 'shipped', fixtures, click: openPicker, render: editCso },
  { name: 'all-emoji', title: '伙伴资料 · 头像：全部表情，最近用过的在前，可搜中英文', width: 1000, height: 760, status: 'shipped', fixtures, click: openAll, wait: 600, render: editCso },
  { name: 'picked-elsewhere', title: '伙伴资料 · 头像来自全部表情：「更多」那格显示它', width: 1000, height: 760, status: 'shipped', fixtures: foxFixtures, click: openPicker, render: editCso },
  { name: 'main', title: '伙伴资料 · 主伙伴：始终是吉祥物，只改名字', width: 1000, height: 560, status: 'shipped', fixtures, render: editMain },
  { name: 'avatar-picker-phone', title: '伙伴资料 · 头像 · 手机', width: 390, height: 844, status: 'shipped', fixtures, click: openPicker, render: editCso },
  { name: 'all-emoji-phone', title: '伙伴资料 · 全部表情 · 手机：底部弹层', width: 390, height: 844, status: 'shipped', fixtures, click: openAll, wait: 600, render: editCso },
  { name: 'main-phone', title: '伙伴资料 · 主伙伴 · 手机', width: 390, height: 844, status: 'shipped', fixtures, render: editMain },
]);

export default designFrames;
