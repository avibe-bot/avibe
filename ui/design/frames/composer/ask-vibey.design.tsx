// "Ask Vibey" (zh 「问 Vibey」) from macOS Services puts the selection into the main Bot's composer on the instance
// the main window shows. Nothing is sent and no draft card opens: text is quoted the way "ask in a new
// Session" quotes a chat selection (quoteText), a link stays bare so it renders as a link once sent, and
// a file is staged as an ordinary composer attachment. Each share goes on its own line after whatever is
// already there; attachments already staged stay. Every part drawn here is shipped, including what Ask Vibey puts in the
// composer (owner request and approval, 2026-11-20).
import type { ReactNode } from 'react';

import type { WorkbenchMessage } from '@/context/ApiContext';
import { quoteText } from '@/lib/quoteText';
import type { SessionDraftAttachment } from '@/lib/sessionDraftAttachmentsCache';

import { frames } from '../../frame';
import { DesktopScreen } from '../../shell/DesktopScreen';
import { botConversationApi, demoProjects, demoSessions, inboxRow, mainBot, message, type WorkbenchFixtures } from '../../shell/fixtures';
import { agentsApi } from '../../shell/teamFixtures';

const route = '/chat/ses_main';
const size = { width: 1440, height: 900 };

type Content = {
  transcript: WorkbenchMessage[];
  selection: string;
  link: string;
  files: SessionDraftAttachment[];
  typed: string;
  kept: SessionDraftAttachment;
};

const file = (token: string, name: string, mime: string, bytes: number): SessionDraftAttachment => ({
  token,
  name,
  mime,
  size: bytes,
  kind: 'file',
  url: `/api/media/${token}`,
});

const zh: Content = {
  transcript: [
    message('ses_main', 'z1', 'user', '下周要和供应商谈续约，帮我盯一下合同里的风险点。', 40),
    message('ses_main', 'z2', 'agent', '好的。你把合同或相关段落发我，我按付款、违约和自动续约三块逐条看。', 39),
  ],
  selection: '乙方应在每季度结束后 15 个工作日内提交服务报告；逾期超过 30 日的，甲方有权单方解除合同且无需承担违约责任。',
  link: 'https://example.com/contracts/2026/renewal-terms',
  files: [file('tok_contract', '供应商续约合同-2026.pdf', 'application/pdf', 1_284_000), file('tok_quote', '报价单-第三版.xlsx', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', 86_400)],
  typed: '这条对我们是不是太严了？顺便看下附件里的付款节点。',
  kept: file('tok_notes', '谈判要点.md', 'text/markdown', 3_200),
};

const en: Content = {
  transcript: [
    message('ses_main', 'e1', 'user', "We're renewing with a vendor next week. Help me keep an eye on risky clauses.", 40),
    message('ses_main', 'e2', 'agent', "Sure. Send me the contract or the parts you're unsure about, and I'll go through payment, default and auto-renewal one by one.", 39),
  ],
  selection: 'The Supplier shall deliver a service report within 15 business days after each quarter; if a report is more than 30 days late, the Customer may terminate this Agreement without liability.',
  link: 'https://example.com/contracts/2026/renewal-terms',
  files: [file('tok_contract', 'vendor-renewal-2026.pdf', 'application/pdf', 1_284_000), file('tok_quote', 'quote-v3.xlsx', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', 86_400)],
  typed: 'Is this clause too strict for us? Also check the payment milestones in the attachment.',
  kept: file('tok_notes', 'negotiation-notes.md', 'text/markdown', 3_200),
};

const attachmentsKey = 'avibe.session-draft-attachments.v1.ses_main';

/** The main Bot's conversation with `draft` and `staged` in its composer, as the composer reloads them. */
function vibey(content: Content, draft: string, staged: SessionDraftAttachment[]): WorkbenchFixtures {
  const api = botConversationApi(mainBot, [mainBot], content.transcript);
  const bootstrap = api['/api/sessions/ses_main/bootstrap'] as Record<string, unknown>;
  return {
    projects: demoProjects,
    sessionsByProject: demoSessions,
    bots: [mainBot],
    inboxSessions: [inboxRow('ses_main', null, '', content.transcript.at(-1)!.text, 39)],
    api: {
      ...agentsApi,
      ...api,
      '/api/sessions/ses_main/bootstrap': { ...bootstrap, draft: { text: draft, updated_at: null } },
      '/api/asr/status': { available: false },
    },
    storage: { [attachmentsKey]: JSON.stringify({ version: 1, generation: 1, attachments: staged }) },
  };
}

/** Three shares in a row into an empty composer: a selected passage, a link, then two files. */
const inserted = (content: Content) => vibey(content, `${quoteText(content.selection)}\n${content.link}`, content.files);

/** A share while something is already typed and staged: it goes on a new line; the attachment stays. */
const appended = (content: Content) => vibey(content, `${content.typed}\n${quoteText(content.selection)}`, [content.kept]);

const screen = (): ReactNode => <DesktopScreen />;

const designFrames = frames([
  {
    name: 'inserted-zh',
    title: '问 Vibey · 连续发来一段文字、一个链接、两个文件，放进输入框未发送 · 中文',
    ...size,
    status: 'shipped',
    route,
    fixtures: inserted(zh),
    render: screen,
  },
  {
    name: 'inserted-en',
    title: '问 Vibey · 连续发来一段文字、一个链接、两个文件，放进输入框未发送 · 英文',
    ...size,
    status: 'shipped',
    route,
    fixtures: inserted(en),
    render: screen,
  },
  {
    name: 'appended-zh',
    title: '问 Vibey · 输入框已有未发送文字和附件时追加到新一行 · 中文',
    ...size,
    status: 'shipped',
    route,
    fixtures: appended(zh),
    render: screen,
  },
  {
    name: 'appended-en',
    title: '问 Vibey · 输入框已有未发送文字和附件时追加到新一行 · 英文',
    ...size,
    status: 'shipped',
    route,
    fixtures: appended(en),
    render: screen,
  },
]);

export default designFrames;
