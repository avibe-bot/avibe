// The Inbox page: every conversation with new Agent replies, Bots first, then project Sessions.
import { InboxPage } from '@/components/workbench/InboxPage';

import { frames } from '../../frame';
import { inboxRow } from '../../shell/fixtures';
import { consolePage, phoneShell, workbenchFixtures } from '../workbench/fixtures';

const page = <InboxPage onOpenSearch={() => {}} />;

const rows = [
  inboxRow('ses_cso', null, '首席安全官', 'avault 依赖里发现 2 个高危漏洞，**升级到 0.7.0** 可以修复，要我开 PR 吗？', 6, 2),
  inboxRow('ses_demo_cache', 'avibe', '修复中途断缓存', '定位到清理旧工具结果时前缀断开，PR #3141 已开，等你 review。', 12, 3),
  inboxRow('ses_pm', null, '产品经理', '需求池按优先级排好了，前三项需要你拍板。', 25, 1),
  inboxRow('ses_demo_pr', 'avibe', 'PR #3073 交付', 'CI 失败：`unit-tests (2/8)`，是 flaky 的 SQLite 锁超时，已经重跑。', 40, 2),
  { ...inboxRow('ses_demo_audit', 'chainbot', '合约审计', '三个函数的权限检查已补齐，测试全绿。', 95), replied: true },
  inboxRow('ses_demo_docs', 'avibe-docs', '中英文档校对', '发现 3 处中英文不对应，已列出 diff 等你拍板是否直接改。', 60 * 26),
];
const unread = { ses_cso: 2, ses_demo_cache: 3, ses_pm: 1, ses_demo_pr: 2 };
const populated = workbenchFixtures({}, { inboxSessions: rows, unreadBySession: unread });
const allTab = { 'vibe-remote:inbox-filter': JSON.stringify({ tab: 'all', leftAt: 0 }) };

// The first page has not arrived and nothing is cached (ui/design: system/skeleton/*). Beside each, the
// same page once it did, at the same size.
const loading = workbenchFixtures({}, { inboxLoading: true });
// The first read failed and nothing is cached: no skeleton that never ends, no false "all caught up".
const failed = workbenchFixtures({}, { inboxFailed: true });

const designFrames = frames([
  {
    name: 'loading',
    title: '收件箱 · 骨架 · 首次加载',
    width: 1192,
    height: 1100,
    status: 'shipped',
    route: '/inbox',
    fixtures: loading,
    render: consolePage(page),
  },
  {
    name: 'phone-loading',
    title: '收件箱 · 骨架 · 首次加载 · 手机',
    width: 390,
    height: 844,
    status: 'shipped',
    route: '/inbox',
    fixtures: loading,
    render: phoneShell('/inbox', page),
  },
  {
    name: 'load-failed',
    title: '收件箱 · 首次加载失败（可重试）',
    width: 1192,
    height: 700,
    status: 'shipped',
    route: '/inbox',
    fixtures: failed,
    render: consolePage(page),
  },
  {
    name: 'phone-load-failed',
    title: '收件箱 · 首次加载失败 · 手机',
    width: 390,
    height: 844,
    status: 'shipped',
    route: '/inbox',
    fixtures: failed,
    render: phoneShell('/inbox', page),
  },
  {
    name: 'unread',
    title: '收件箱 · 未读（伙伴在前）',
    width: 1192,
    height: 1100,
    status: 'shipped',
    route: '/inbox',
    fixtures: populated,
    render: consolePage(page),
  },
  {
    name: 'all',
    title: '收件箱 · 全部',
    width: 1192,
    height: 1240,
    status: 'shipped',
    route: '/inbox',
    fixtures: { ...populated, storage: allTab },
    render: consolePage(page),
  },
  {
    name: 'caught-up',
    title: '收件箱 · 全部看完了',
    width: 1192,
    height: 560,
    status: 'shipped',
    route: '/inbox',
    fixtures: workbenchFixtures({}, { inboxSessions: rows.map((row) => ({ ...row, unread: false, unread_count: 0 })) }),
    render: consolePage(page),
  },
  {
    name: 'notifications',
    title: '收件箱 · 通知开关菜单',
    width: 1192,
    height: 700,
    status: 'shipped',
    route: '/inbox',
    fixtures: populated,
    click: ['button[aria-haspopup="dialog"] >> nth=0'],
    render: consolePage(page),
  },
  {
    name: 'phone',
    title: '收件箱 · 手机',
    width: 390,
    height: 844,
    status: 'shipped',
    route: '/inbox',
    fixtures: populated,
    render: phoneShell('/inbox', page),
  },
]);

export default designFrames;
