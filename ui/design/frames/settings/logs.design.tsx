// Settings · Diagnostics · Logs: the service log tail with level and source filters.
import { SettingsLogsPage } from '@/components/settings/SettingsLogsPage';

import { frames } from '../../frame';
import { settingsFixtures, settingsPage } from './fixtures';

const page = settingsPage('diagnostics/logs', <SettingsLogsPage />);
const route = '/settings/diagnostics/logs';

const entry = (time: string, level: string, logger: string, message: string, source = 'service') => ({
  timestamp: `2026-11-01 ${time}`,
  level,
  logger,
  message,
  source,
});

// Oldest first, as the server returns them; the panel shows the newest on top.
const logs = [
  entry('15:18:42,214', 'INFO', 'modules.im.slack', '[slack.py:412] - received #ai-bots from @alex (mention required)'),
  entry('15:18:42,218', 'DEBUG', 'core.message_dispatcher', '[message_dispatcher.py:233] - routing to agent=vibey cwd=~/dev/avibe-bot-backend'),
  entry('15:18:43,871', 'INFO', 'modules.agents.vibey', '[runtime.py:618] - turn started session=ses_zMG4 model=claude-opus-5-5'),
  entry('15:18:46,302', 'DEBUG', 'modules.im.slack', '[slack.py:780] - posting reaction :eyes: as ack'),
  entry('15:18:48,015', 'WARNING', 'modules.im.discord', '[discord.py:1204] - rate-limit hit on guild=ai-hands · backing off 4s'),
  entry('15:18:52,118', 'INFO', 'modules.agents.vibey', '[runtime.py:702] - turn finished session=ses_zMG4 elapsed=8.2s tokens_in=18342 tokens_out=412'),
  entry('15:18:52,221', 'INFO', 'modules.im.slack', '[slack.py:833] - posted reply (4 lines, 412 chars) · removed reaction'),
  entry('15:19:01,847', 'ERROR', 'core.controller', '[controller.py:1880] - codex backend unavailable: CLI not found on PATH'),
  entry('15:19:02,003', 'INFO', 'vibe.ui_server', '[ui_server.py:402] - GET /api/inbox 200 12ms', 'ui_stdout'),
];

const sources = [
  { key: 'all', filename: '', path: '', exists: true, total: 18420 },
  { key: 'service', filename: 'vibe_remote.log', path: '~/.avibe/logs/vibe_remote.log', exists: true, total: 18211 },
  { key: 'service_stderr', filename: 'service_stderr.log', path: '~/.avibe/logs/service_stderr.log', exists: true, total: 3 },
  { key: 'ui_stdout', filename: 'ui_stdout.log', path: '~/.avibe/logs/ui_stdout.log', exists: true, total: 206 },
];

const designFrames = frames([
  {
    name: 'default',
    title: '设置 · 日志',
    width: 1440,
    height: 1000,
    status: 'shipped',
    route,
    fixtures: settingsFixtures({ '/api/logs': { logs, total: 18420, sources, source: 'all' } }),
    render: page,
  },
  {
    name: 'empty',
    title: '设置 · 日志 · 还没有日志',
    width: 1440,
    height: 900,
    status: 'shipped',
    route,
    fixtures: settingsFixtures({ '/api/logs': { logs: [], total: 0, sources: sources.map((s) => ({ ...s, total: 0 })), source: 'all' } }),
    render: page,
  },
  {
    name: 'phone',
    title: '设置 · 日志 · 手机',
    width: 390,
    height: 844,
    status: 'shipped',
    route,
    fixtures: settingsFixtures({ '/api/logs': { logs, total: 18420, sources, source: 'all' } }),
    render: page,
  },
]);

export default designFrames;
