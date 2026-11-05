// Settings · Diagnostics: the health check (Doctor) with its passed, warning, and failed checks, and
// the offline history line (the local message store) below it, drawn as its view: the store it reads
// is IndexedDB, which a frame has no fixture for.
import { LocalStoreDiagnosticsView } from '@/components/settings/LocalStoreDiagnostics';
import { SettingsDiagnosticsPage } from '@/components/settings/SettingsDiagnosticsPage';

import { frames } from '../../frame';
import { settingsFixtures, settingsPage } from './fixtures';

const page = settingsPage('diagnostics', <SettingsDiagnosticsPage />);
const route = '/settings/diagnostics';

const check = (status: 'pass' | 'warn' | 'fail', message: string, action?: string) => ({ status, message, action });

const withIssues = {
  ok: false,
  summary: { pass: 9, warn: 2, fail: 1 },
  groups: [
    {
      name: '运行环境',
      items: [
        check('pass', 'Python 3.12.12'),
        check('pass', '配置文件可读：~/.avibe/config/config.json'),
        check('pass', '服务进程运行中（PID 48213）'),
        check('warn', '日志目录占用 1.8 GB', '在「服务」页查看日志文件位置后清理旧日志'),
      ],
    },
    {
      name: 'Agent 后端',
      items: [
        check('pass', 'Claude Code 2.4.18 已就绪'),
        check('pass', 'OpenCode 1.6.2 已就绪'),
        check('fail', 'Codex CLI 未安装', '到 设置 → 后端 安装 Codex'),
      ],
    },
    {
      name: '通讯平台',
      items: [
        check('pass', 'Slack · acme-vibe 已连接'),
        check('pass', 'Discord · AI Hands 已连接'),
        check('warn', 'Telegram 机器人未关闭隐私模式，群组里只能收到 @ 消息', '在 BotFather 中执行 /setprivacy'),
        check('pass', 'Avibe Workbench 可用'),
        check('pass', '依赖 askill 0.1.13 已就绪'),
      ],
    },
  ],
};

const healthy = {
  ok: true,
  summary: { pass: 12, warn: 0, fail: 0 },
  groups: withIssues.groups.map((group) => ({ ...group, items: group.items.map((item) => ({ ...item, status: 'pass', action: undefined })) })),
};

const designFrames = frames([
  {
    name: 'issues',
    title: '设置 · 诊断 · 有警告和失败',
    width: 1440,
    height: 1240,
    status: 'shipped',
    route,
    fixtures: settingsFixtures({ '/api/doctor': withIssues }),
    render: page,
  },
  {
    name: 'healthy',
    title: '设置 · 诊断 · 全部通过',
    width: 1440,
    height: 1240,
    status: 'shipped',
    route,
    fixtures: settingsFixtures({ '/api/doctor': healthy }),
    render: page,
  },
  {
    name: 'phone',
    title: '设置 · 诊断 · 手机',
    width: 390,
    height: 844,
    status: 'shipped',
    route,
    fixtures: settingsFixtures({ '/api/doctor': withIssues }),
    render: page,
  },
  {
    name: 'offline-history',
    title: '设置 · 诊断 · 离线历史 · 已安装的 App',
    width: 720,
    height: 260,
    status: 'shipped',
    route,
    fixtures: settingsFixtures({ '/api/doctor': healthy }),
    render: () => (
      <div className="p-6">
        <LocalStoreDiagnosticsView persistence="granted" bytes={48_300_000} />
      </div>
    ),
  },
  {
    name: 'offline-history-tab',
    title: '设置 · 诊断 · 离线历史 · 浏览器标签页',
    width: 720,
    height: 260,
    status: 'shipped',
    route,
    fixtures: settingsFixtures({ '/api/doctor': healthy }),
    render: () => (
      <div className="p-6">
        <LocalStoreDiagnosticsView persistence="not-requested" bytes={3_200_000} />
      </div>
    ),
  },
]);

export default designFrames;
