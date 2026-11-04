// The Workbench tab on a phone (owner decisions, 2026-11-02), inside the real shell:
// the Bots strip on top and the projects below, as in the desktop sidebar; every
// project row has a ＋ to the right of its ⋯ that starts a Session there. The center
// of the bar is the main Bot for the Owner and the new-session ＋ for a member.
// Holding any avatar in the strip speaks to that Bot (owner decision, 2026-11-04): the
// card opens under the strip, the cancel target waits right above the finger, and only
// the row of avatars stays lit; after sending the reader stays here and the Bot moves up.
import type { ReactNode } from 'react';

import { ProjectsPage } from '@/components/workbench/ProjectsPage';
import { OWNER_INSTANCE_CAPABILITIES } from '@/lib/sessionInfo';

import { frames } from '../../frame';
import type { WorkbenchFixtures } from '../../shell/fixtures';
import { HeldBot } from '../../shell/HeldBot';
import { aloneFixtures, fullFixtures, manyFixtures, spoken, team as teamBots, teamFixtures } from '../../shell/teamFixtures';
import { phoneShell, workbenchFixtures } from '../workbench/fixtures';

const page = phoneShell('/projects', <ProjectsPage />);
/** The team's Bots and projects, with the reads the shell makes on every route. */
const inShell = (fixtures: WorkbenchFixtures) => workbenchFixtures(fixtures.api as Record<string, unknown>, fixtures);
const alone = inShell(aloneFixtures);
const team = inShell(teamFixtures);
const full = inShell(fullFixtures);
const many = inShell(manyFixtures);

/** The team with Cloud voice on, so the strip's avatars can be held. */
const speaking = inShell({ ...teamFixtures, api: { ...teamFixtures.api, '/api/asr/status': { available: true }, '/api/cloud/token': { ok: false } } });
/** After a take to the PM: the PM is the Bot the user last wrote to, right after the main Bot (bots.md D13). */
const [mainBot, cso, pm, research] = teamBots;
const afterSpeaking = { ...speaking, bots: [mainBot, pm, cso, research] };
const recording = { phase: 'recording', text: spoken.text, stash: spoken.stash, seconds: 6 } as const;
const held = (hold: Parameters<typeof HeldBot>[0]['hold']) => () => (
  <HeldBot handle="pm" pointer="touch" hold={hold}>
    {page()}
  </HeldBot>
);
const holdFrame = (name: string, title: string, render: () => ReactNode, fixtures = speaking) => (
  { name, title, width: 390, height: 844, status: 'shipped' as const, fixtures, route: '/projects', render }
);

/** A member: no Bots, so no strip, and the bar keeps the new-session ＋. */
const member = inShell({
  ...teamFixtures,
  capabilities: { ...OWNER_INSTANCE_CAPABILITIES, is_instance_owner: false, can_manage_instance: false },
  bots: [],
  botStatus: {},
  unreadBySession: { ses_demo_cache: 3 },
  inboxSessions: [],
});

const designFrames = frames([
  { name: 'alone', title: '工作台 · 只有歪比', width: 390, height: 844, status: 'shipped', fixtures: alone, route: '/projects', render: page },
  { name: 'team', title: '工作台 · 4 个伙伴：未读、产品经理运行中；每个项目行 ⋯ 右边是 ＋ 新会话', width: 390, height: 844, status: 'shipped', fixtures: team, route: '/projects', render: page },
  { name: 'at-limit', title: '工作台 · 刚好 5 位伙伴：没有「添加」', width: 390, height: 844, status: 'shipped', fixtures: full, route: '/projects', render: page },
  { name: 'many', title: '工作台 · 限额前留下的 7 位伙伴：都保留、横向滑动，没有「添加」', width: 390, height: 844, status: 'shipped', fixtures: many, route: '/projects', render: page },
  { name: 'team-small', title: '工作台 · 小屏 375×667', width: 375, height: 667, status: 'shipped', fixtures: team, route: '/projects', render: page },
  { name: 'member', title: '工作台 · 没有伙伴的成员：没有伙伴条，底栏中间仍是新会话', width: 390, height: 844, status: 'shipped', fixtures: member, route: '/projects', render: page },
  holdFrame('hold-recording', '按住伙伴说话 · 录音中：卡片在伙伴条下面，取消在手指正上方', held(recording)),
  holdFrame('hold-armed', '按住伙伴说话 · 上滑进取消区：松开取消', held({ ...recording, phase: 'armed', seconds: 7 })),
  holdFrame('hold-sent', '按住伙伴说话 · 已发给产品经理：留在工作台，它排到歪比后面', held({ outcome: 'sent', text: spoken.final }), afterSpeaking),
]);

export default designFrames;
