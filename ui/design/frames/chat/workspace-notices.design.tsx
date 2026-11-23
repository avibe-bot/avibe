// The 工作区通知 thread: failure notices of background work that had no Session to report to. Each names
// the Session it came from (`content.failure_source`): 「打开会话」 opens it, and 「重试」 retries its failed
// Turn there with the chat page's own retry. A failure that can no longer be retried offers only the open
// button; a retry already started shows as started; a notice without a Session shows neither.
import { Route, Routes } from 'react-router-dom';

import { ChatPage } from '@/components/workbench/ChatPage';

import { frames } from '../../frame';
import { DesktopScreen } from '../../shell/DesktopScreen';
import { chatFixtures, chatPhone, message, withChatProviders } from './fixtures';

const WORKSPACE = 'ses-workspace-notices';
const notice = (text: string, minutesAgo: number, source: { session_id: string; retry: boolean } | null, started = false) =>
  message('agent', 'notify', text, minutesAgo, {
    source: 'agent',
    read_at: null,
    metadata: { event: 'backend_failure', backend: 'vibey', failure_id: 'turn:trn_design' },
    content: {
      ...(source ? { failure_source: source } : {}),
      ...(started ? { failure_retry: { delivery_id: 'dlv_started', state: 'accepted' } } : {}),
    },
  });

const messages = [
  notice('[Avibe Harness] 后台任务「仓库每周依赖检查」执行失败。\n错误：Watch 已停止，结果没有送达。', 95, null),
  notice('[Avibe Harness] 后台任务「自托管 Runner 暴露调查」执行失败。\n错误：本次请求未完成，自动恢复已结束。你可以重试或换一个模型。', 52, { session_id: 'ses_runner', retry: false }, true),
  notice('[Avibe Harness] 后台任务「账号删除后端调查」执行失败。\n错误：本次请求未完成，自动恢复已结束。你可以重试或换一个模型。', 31, { session_id: 'ses_account', retry: false }),
  notice('[Avibe Harness] 后台任务「每日热点汇总」执行失败。\n错误：供应商响应已中断。上游最后一次报错：Upstream HTTP/2 stream failed。', 4, { session_id: 'ses_daily', retry: true }),
];

const fixtures = chatFixtures({
  session: { id: WORKSPACE, title: '工作区通知', visibility: 'system', project_id: null, scope_id: null },
  messages,
  api: {
    // 「重试」 retries in the Session the notice names, as the chat page's own retry does.
    'POST /api/sessions/ses_daily/messages': { id: 'dlv_design', state: 'accepted', delivery_state: 'accepted' },
  },
});

const desktop = () => withChatProviders(
  <DesktopScreen page={<Routes><Route path="/chat/:sessionId" element={<ChatPage />} /></Routes>} />,
);
const route = `/chat/${WORKSPACE}`;
const retry = ['role=button[name="{t:common.retry}"]'];

const designFrames = frames([
  {
    name: 'desktop',
    title: '工作区通知 · 每条报错下方：打开会话、重试（不能重试的只有打开会话，已重试的显示已请求）',
    width: 1440,
    height: 900,
    status: 'proposed',
    route,
    fixtures,
    render: desktop,
  },
  {
    name: 'desktop-retried',
    title: '工作区通知 · 点「重试」之后：显示「已请求重试」，不能再点',
    width: 1440,
    height: 900,
    status: 'proposed',
    route,
    fixtures,
    click: retry,
    render: desktop,
  },
  {
    name: 'phone',
    title: '工作区通知 · 手机',
    width: 390,
    height: 844,
    status: 'proposed',
    route,
    fixtures,
    render: chatPhone,
  },
  {
    name: 'phone-retried',
    title: '工作区通知 · 手机 · 点「重试」之后',
    width: 390,
    height: 844,
    status: 'proposed',
    route,
    fixtures,
    click: retry,
    render: chatPhone,
  },
]);

export default designFrames;
