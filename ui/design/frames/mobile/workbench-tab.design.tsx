// The Workbench tab on a phone (owner decisions, 2026-11-02), inside the real shell:
// the Bots strip on top and the projects below, as in the desktop sidebar; every
// project row has a ＋ to the right of its ⋯ that starts a Session there. The center
// of the bar is the main Bot for the Owner and the new-session ＋ for a member.
import { ProjectsPage } from '@/components/workbench/ProjectsPage';
import { OWNER_INSTANCE_CAPABILITIES } from '@/lib/sessionInfo';

import { frames } from '../../frame';
import type { WorkbenchFixtures } from '../../shell/fixtures';
import { aloneFixtures, fullFixtures, manyFixtures, teamFixtures } from '../../shell/teamFixtures';
import { phoneShell, workbenchFixtures } from '../workbench/fixtures';

const page = phoneShell('/projects', <ProjectsPage />);
/** The team's Bots and projects, with the reads the shell makes on every route. */
const inShell = (fixtures: WorkbenchFixtures) => workbenchFixtures(fixtures.api as Record<string, unknown>, fixtures);
const alone = inShell(aloneFixtures);
const team = inShell(teamFixtures);
const full = inShell(fullFixtures);
const many = inShell(manyFixtures);

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
]);

export default designFrames;
