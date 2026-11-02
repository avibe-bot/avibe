// What a transcript row can be, rendered by the shipped ChatPage: agent media (inline images, file cards,
// an outside image that only loads on a click), a user's attachment, and the rows the agent did not write
// in reply to you: Harness triggers (a scheduled task, a Watch, another Session's callback, a folded run of
// Watch results), a muted progress note, and a failure notice with Retry.
import chartUrl from '../../assets/weekly-messages-chart.svg';
import regionUrl from '../../assets/annotation-region.svg';
import { frames } from '../../frame';
import { agentSays, chatDesktop, chatFixtures, chatPhone, chatRoute, failureApi, failureNotice, message, userSays } from './fixtures';

// ----- Media -----

const media = [
  userSays('这两张卡片的间距不一样，顺手修一下。', 70, {
    content: {
      attachments: [{ url: '/api/media/tok_region', name: 'screenshot.png', mime: 'image/png', kind: 'image', width: 640, height: 300 }],
    },
  }),
  agentSays('已改成统一的 16px 间距，两张卡片现在对齐了。', 66),
  userSays('画一张近 8 周 Web 和 IM 的消息量对比图，周报和运行日志也打包给我。', 30),
  agentSays(
    '图做好了，Web 对话从 W40 起一直在涨，W44 已经比 IM 多四成：\n\n![weekly-messages.png](/api/media/tok_chart?w=960&h=540)\n\n周报和日志在这里：\n\n[weekly-report.pdf](/api/media/tok_report)\n\n[run-logs.zip](/api/media/tok_logs)',
    24,
  ),
  userSays('官网那张架构图也贴一下？', 10),
  agentSays('外链图片不会自动加载，点开才会请求对方的服务器：\n\n![architecture.png](https://docs.example.com/img/architecture.png)', 8),
];

const mediaApi = {
  '/api/media/tok_chart': chartUrl,
  '/api/media/tok_region': regionUrl,
  '/api/media/tok_report/meta': { name: 'weekly-report.pdf', ext: 'pdf', size: 2_457_600, content_type: 'application/pdf' },
  '/api/media/tok_logs/meta': { name: 'run-logs.zip', ext: 'zip', size: 880_640, content_type: 'application/zip' },
};

// ----- Rows the agent did not write in reply to you -----

const harness = (kind: string, id: string, text: string, minutesAgo: number, extra = {}) =>
  message('harness', 'harness', text, minutesAgo, { author_name: kind, author_id: id, native_message_id: `${kind}:${id}:${minutesAgo}`, ...extra });

const system = [
  userSays('每天早上 9 点给我一份晨报：昨天合并的 PR、CI 失败、Inbox 里没回的会话。', 60 * 24 + 30),
  agentSays('好，已建定时任务「每日晨报」，明早 9 点第一次运行。', 60 * 24 + 28),
  harness('scheduled', 'task_daily_digest', '每日晨报：汇总昨天合并的 PR、CI 失败和 Inbox 里没回的会话，写成一页。', 7 * 60),
  agentSays('**晨报 · 11 月 1 日**\n\n- 合并 6 个 PR，其中 #3137、#3139 是设计帧\n- CI 失败 1 次：`unit-tests (2/8)`，SQLite 锁超时，重跑已过\n- Inbox 里 2 个会话等你回复', 7 * 60 - 2),
  harness('watch', 'watch_pr3141', 'Watch「PR #3141 评审」：Codex 给出 1 条评审意见。', 3 * 60),
  harness('watch', 'watch_pr3141', 'Watch「PR #3141 评审」：CI 全部通过。', 2 * 60),
  agentSays('评审意见只有一处命名问题，已经改好推上去了。', 100),
  harness('agent_run', 'run_7f2c', '子任务完成：Show Pages 的手机布局已经改好，截图在 PR 描述里。', 90, {
    source_session_id: 'sesk2v9d0rq3n',
    source_session_title: 'Show Pages 手机布局',
    source_session_agent_name: 'claude',
  }),
  message('agent', 'interim', '先看一下 #3141 的评审意见，再决定要不要改。', 88),
  failureNotice('turn_8c1d', 86),
];

const claude = { title: '周报与图表', agent_name: 'claude', agent_backend: 'claude', agent_variant: 'claude', model: 'claude-opus-5-5' };

const designFrames = frames([
  {
    name: 'media',
    title: '会话 · 回复里的图片与文件',
    width: 1192,
    height: 1340,
    status: 'shipped',
    route: chatRoute(),
    fixtures: chatFixtures({ session: claude, messages: media, api: mediaApi }),
    render: chatDesktop,
  },
  {
    name: 'harness-and-notices',
    title: '会话 · 定时任务、Watch、回调与失败通知',
    width: 1192,
    height: 1240,
    status: 'shipped',
    route: chatRoute(),
    fixtures: chatFixtures({ session: { title: '每日晨报' }, messages: system, api: failureApi('turn_8c1d') }),
    render: chatDesktop,
  },
  {
    name: 'harness-expanded',
    title: '会话 · 展开定时任务的提示词、Watch 结果和失败详情',
    width: 1192,
    height: 1700,
    status: 'shipped',
    route: chatRoute(),
    fixtures: chatFixtures({ session: { title: '每日晨报' }, messages: system, api: failureApi('turn_8c1d') }),
    click: [
      'role=button[name=/每日晨报：汇总/]',
      'button:has-text("{t:chat.harnessGroup.expand}")',
      'role=button[name="{t:chat.failureDetails.show}"]',
    ],
    render: chatDesktop,
  },
  {
    name: 'phone-media',
    title: '会话 · 回复里的图片与文件 · 手机',
    width: 390,
    height: 844,
    status: 'shipped',
    route: chatRoute(),
    fixtures: chatFixtures({ session: claude, messages: media.slice(2, 4), api: mediaApi }),
    render: chatPhone,
  },
]);

export default designFrames;
