// Message search as `/api/search/messages` answers it: matches grouped by Session.
import type { MessageSearchMatch, MessageSearchResult, MessageSearchSession } from '@/context/ApiContext';

import { DESIGN_NOW } from '../../shell/clock';

const at = (minutesAgo: number) => new Date(DESIGN_NOW - minutesAgo * 60_000).toISOString();

const match = (id: string, author: string, type: string, minutesAgo: number, prefix: string, hit: string, suffix: string): MessageSearchMatch => ({
  id,
  author,
  source: null,
  type,
  created_at: at(minutesAgo),
  snippet: { prefix, match: hit, suffix },
});

const group = (sessionId: string, title: string, projectName: string | null, matches: MessageSearchMatch[]): MessageSearchSession => ({
  session_id: sessionId,
  title,
  project_id: projectName ? `proj_${projectName}` : null,
  project_name: projectName,
  archived: false,
  matches,
});

export const ratelimitResults: MessageSearchResult = {
  total: 5,
  session_count: 3,
  sessions: [
    group('ses_demo_rc', '重构会话 RC', 'avibe', [
      match('msg_1', 'agent', 'result', 2, '修复了 Slack 发送时的 ', 'ratelimit', ' 重试，退避到 30s'),
      match('msg_2', 'user', 'user', 5, '', 'ratelimit', ' 又触发了吗？先看下日志，确认下退避是否生效'),
      match('msg_3', 'agent', 'result', 60 * 24, '把 ', 'ratelimit', ' 的退避策略写进了 README 和 FAQ'),
    ]),
    group('ses_deploy', '部署排查', 'claude-sg', [
      match('msg_4', 'agent', 'result', 60 * 72, '上游返回 429，命中了 ', 'ratelimit', '，已加入队列缓冲'),
    ]),
    group('ses_docs', '故障排查文档', 'avibe-docs', [
      match('msg_5', 'user', 'user', 60 * 30, '记得把 ', 'ratelimit', ' 的处理也补到故障排查那页'),
    ]),
  ],
};

export const searchApi = (query: string, results: MessageSearchResult) => ({
  [`/api/search/messages?q=${encodeURIComponent(query)}`]: results,
  '/api/search/messages': { total: 0, session_count: 0, sessions: [] },
});
