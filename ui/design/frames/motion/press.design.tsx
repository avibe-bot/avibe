// Press feedback on a phone (docs/plans/web-ui-motion.md M8), as shipped: each frame holds a finger down on
// one shipped control. A control (a tab, a chip, a button) sinks to `--motion-scale-press-control` and a
// row or card to `--motion-scale-press`, each with its colour step; under reduced motion only the colour
// step remains. Pressing is `:active`, so the screenshot holds the pointer down (`press`).
import { InboxPage } from '@/components/workbench/InboxPage';
import { ProjectsPage } from '@/components/workbench/ProjectsPage';

import { frames } from '../../frame';
import { inboxRow } from '../../shell/fixtures';
import { phoneProjects, phoneSessions } from '../mobile/fixtures';
import { phoneShell, workbenchFixtures } from '../workbench/fixtures';

const inbox = workbenchFixtures({}, {
  inboxSessions: [
    inboxRow('ses_demo_cache', 'avibe', '修复中途断缓存', '定位到清理旧工具结果时前缀断开，PR #3141 已开，等你 review。', 12, 3),
    inboxRow('ses_demo_pr', 'avibe', 'PR #3073 交付', 'CI 失败：`unit-tests (2/8)`，是 flaky 的 SQLite 锁超时，已经重跑。', 40, 2),
  ],
  unreadBySession: { ses_demo_cache: 3, ses_demo_pr: 2 },
});
const projects = workbenchFixtures({}, {
  projects: phoneProjects,
  sessionsByProject: phoneSessions,
  expandedProjects: ['proj_avibe', 'proj_chainbot'],
  unreadBySession: { ses_demo_cache: 3, ses_demo_pr: 2 },
});

const phone = { width: 390, height: 844, status: 'shipped' as const };
const inboxPage = { ...phone, route: '/inbox', fixtures: inbox, render: phoneShell('/inbox', <InboxPage onOpenSearch={() => {}} />) };
const projectsPage = { ...phone, route: '/projects', fixtures: projects, render: phoneShell('/projects', <ProjectsPage />) };

/** The first card's Open chip, and a Session's row in the Projects tree. */
const CHIP = '[data-inbox-session-id="ses_demo_cache"] button';
const ROW = 'button:has-text("合约审计")';

const designFrames = frames([
  { ...inboxPage, name: 'phone-chip', title: '按下 · 卡片里的按钮 · 手机', press: CHIP },
  { ...inboxPage, name: 'phone-tab', title: '按下 · 标签栏 · 手机', press: 'nav a[href="/projects"]' },
  { ...projectsPage, name: 'phone-row', title: '按下 · 列表行 · 手机', press: ROW },
  { ...projectsPage, name: 'phone-row-reduced', title: '按下 · 列表行 · 减少动态效果 · 手机', press: ROW, reducedMotion: true },
]);

export default designFrames;
