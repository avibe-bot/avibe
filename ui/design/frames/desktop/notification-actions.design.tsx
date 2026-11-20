// D2 (Desktop): answer an Agent from the macOS notification, confirm a follow-up into the Mac's Reminders,
// and the honest result when an action does not go through. The notifications are illustrations (macOS
// draws them); Settings and the chat are the shipped layout and chat page with the proposed pieces.
import { useTranslation } from 'react-i18next';

import { frames } from '../../frame';
import {
  DesktopNotificationsSettings,
  MacNotification,
  MacScreenCorner,
  OutcomeNotice,
  type DesktopDeviceState,
} from '../../kit/desktop-notification-actions';
import { agentSays, chatDesktop, chatFixtures, chatRoute, userSays } from '../chat/fixtures';
import { settingsFixtures, settingsPage } from '../settings/fixtures';

const useCopy = () => {
  const zh = useTranslation().i18n.language?.startsWith('zh') ?? true;
  return (zhText: string, enText: string) => (zh ? zhText : enText);
};

function QuestionNotification({ open = false, reply = false }: { open?: boolean; reply?: boolean }) {
  const t = useCopy();
  return (
    <MacScreenCorner
      note={t(
        'Agent 的消息带快捷回复、Avibe 主窗口不在前台时发出。前三个快捷回复是按钮，「回复…」打开输入框；回复作为一条普通消息发进这个会话。',
        "Sent when an Agent's message has quick replies and the Avibe window isn't in front. The first three quick replies are buttons; Reply… opens a text field and sends an ordinary message to this conversation.",
      )}
    >
      <MacNotification
        title={t('avibe-app · Dock 修复交付', 'avibe-app · Dock fix delivery')}
        subtitle={t('等你回复', 'Waiting for your answer')}
        body={t(
          'PR #4031 的 Codex 评审已通过，CI 全绿。要我现在合并吗？',
          'Codex review on PR #4031 passed and CI is green. Merge it now?',
        )}
        time={t('刚刚', 'now')}
        open={open}
        reply={reply ? { text: t('合并前先把 changelog 补上', 'Add the changelog entry before merging') } : undefined}
        actions={[
          t('✅ 合并', '✅ Merge'),
          t('👀 我先看看', '👀 Let me look first'),
          t('⏸ 先不合', '⏸ Not yet'),
          t('回复…', 'Reply…'),
        ]}
      />
    </MacScreenCorner>
  );
}

function ReminderNotification() {
  const t = useCopy();
  return (
    <MacScreenCorner
      note={t(
        '这台 Mac 有人在用、并且允许了通知时，由它接下这条提醒请求。确认后写进「提醒事项」的默认列表；「不用了」或过期都不会添加。',
        'This Mac takes the request only while someone is using it and notifications are allowed. Confirming writes it to the default Reminders list; Not now or expiry adds nothing.',
      )}
    >
      <MacNotification
        title="Vibey"
        subtitle={t('添加到提醒事项？', 'Add to Reminders?')}
        body={t(
          '跟进合同签署 · 11月27日 周五 15:00（上海时间）',
          'Follow up on the contract signing · Fri, Nov 27, 3:00 PM (Shanghai)',
        )}
        time={t('刚刚', 'now')}
        open
        actions={[t('添加到提醒事项', 'Add to Reminders'), t('不用了', 'Not now')]}
      />
    </MacScreenCorner>
  );
}

function OutcomeNotifications() {
  const t = useCopy();
  const source = t('avibe-app · Dock 修复交付', 'avibe-app · Dock fix delivery');
  return (
    <MacScreenCorner
      note={t(
        '动作没有生效时才再发一条，说明原因；点按打开对应的会话或设置。成功时不另发通知，原通知直接消失。',
        "A second notification appears only when an action didn't take effect, with the reason; clicking it opens the conversation or Settings. On success the notification simply goes away.",
      )}
    >
      <MacNotification
        title={source}
        subtitle={t('没有发送', 'Not sent')}
        body={t('已经在其他设备上回复了「✅ 合并」。', 'Already answered "✅ Merge" on another device.')}
        time={t('刚刚', 'now')}
      />
      <MacNotification
        title={source}
        subtitle={t('没有发送', 'Not sent')}
        body={t('会话已经有了新进展。打开看看再回复。', 'The conversation has moved on. Open it to see the latest before replying.')}
        time={t('2 分钟前', '2 min ago')}
      />
      <MacNotification
        title={source}
        subtitle={t('没有送达', 'Not delivered')}
        body={t('连不上这个实例。打开 Avibe 重新回复。', "Couldn't reach this instance. Open Avibe to answer again.")}
        time={t('5 分钟前', '5 min ago')}
      />
      <MacNotification
        title="Vibey"
        subtitle={t('没有添加提醒', 'Reminder not added')}
        body={t('Avibe 没有「提醒事项」的访问权限。点按打开设置。', "Avibe can't access Reminders. Click to open Settings.")}
        time={t('8 分钟前', '8 min ago')}
      />
    </MacScreenCorner>
  );
}

function Notices() {
  const t = useCopy();
  return (
    <div className="flex h-full flex-col gap-3 bg-background px-4 py-4">
      <OutcomeNotice kind="handled-elsewhere" choice={t('✅ 合并', '✅ Merge')} />
      <OutcomeNotice kind="stale" />
      <OutcomeNotice kind="not-delivered" />
      <OutcomeNotice kind="expired" />
      <OutcomeNotice kind="reminders-denied" />
    </div>
  );
}

const settings = (state: DesktopDeviceState) => settingsPage('notifications', <DesktopNotificationsSettings state={state} />);
const settingsRoute = '/settings/notifications';
const settingsData = settingsFixtures({}, { desktopShell: true });

const handled = (lang: 'zh' | 'en') => chatFixtures({
  session: { title: lang === 'zh' ? 'Dock 修复交付' : 'Dock fix delivery', agent_name: 'claude', agent_backend: 'claude', agent_variant: 'claude', model: 'claude-opus-5-5' },
  messages: [
    userSays(lang === 'zh' ? '#4031 评审过了告诉我。' : 'Tell me when #4031 passes review.', 40),
    agentSays(
      lang === 'zh' ? 'PR #4031 的 Codex 评审已通过，CI 全绿。要我现在合并吗？' : 'Codex review on PR #4031 passed and CI is green. Merge it now?',
      12,
      {
        content: {
          quick_replies: lang === 'zh' ? ['✅ 合并', '👀 我先看看', '⏸ 先不合'] : ['✅ Merge', '👀 Let me look first', '⏸ Not yet'],
          quick_reply_chosen: lang === 'zh' ? '✅ 合并' : '✅ Merge',
        },
      },
    ),
    userSays(lang === 'zh' ? '✅ 合并' : '✅ Merge', 9),
    agentSays(lang === 'zh' ? '已合并 #4031，master 上的构建在跑。' : 'Merged #4031; the master build is running.', 8),
  ],
});

const inChat = (lang: 'zh' | 'en') => () => (
  <div className="relative h-full">
    {chatDesktop()}
    <div className="pointer-events-none absolute inset-x-0 top-[64px] px-6">
      <OutcomeNotice kind="handled-elsewhere" choice={lang === 'zh' ? '✅ 合并' : '✅ Merge'} />
    </div>
  </div>
);

const designFrames = frames([
  { name: 'question-options', title: '通知 · 等你回复 · 选项展开（示意）', width: 960, height: 420, status: 'proposed', render: () => <QuestionNotification open /> },
  { name: 'question-reply', title: '通知 · 等你回复 · 在通知里回复（示意）', width: 960, height: 420, status: 'proposed', render: () => <QuestionNotification reply /> },
  { name: 'reminder-confirm', title: '通知 · 添加到提醒事项 · 确认（示意）', width: 960, height: 420, status: 'proposed', render: () => <ReminderNotification /> },
  { name: 'outcomes', title: '通知 · 动作没有生效时的结果（示意）', width: 960, height: 560, status: 'proposed', render: () => <OutcomeNotifications /> },
  { name: 'settings-ready', title: '设置 · 通知 · 本设备 · 都已允许', width: 1440, height: 900, status: 'proposed', route: settingsRoute, fixtures: settingsData, render: settings('ready') },
  { name: 'settings-needs-permission', title: '设置 · 通知 · 本设备 · 系统未允许通知', width: 1440, height: 900, status: 'proposed', route: settingsRoute, fixtures: settingsData, render: settings('needs-permission') },
  { name: 'settings-reminders-denied', title: '设置 · 通知 · 本设备 · 提醒事项被拒绝', width: 1440, height: 900, status: 'proposed', route: settingsRoute, fixtures: settingsData, render: settings('reminders-denied') },
  { name: 'settings-unsupported', title: '设置 · 通知 · 本设备 · 系统版本不支持提醒事项', width: 1440, height: 900, status: 'proposed', route: settingsRoute, fixtures: settingsData, render: settings('unsupported') },
  { name: 'notices', title: '会话 · 从通知打开时的结果提示', width: 1100, height: 260, status: 'proposed', render: () => <Notices /> },
  { name: 'in-chat-handled', title: '会话 · 已在另一端处理（中文内容）', width: 1192, height: 640, status: 'proposed', route: chatRoute(), fixtures: handled('zh'), render: inChat('zh') },
  { name: 'in-chat-handled-en', title: '会话 · 已在另一端处理（英文内容）', width: 1192, height: 640, status: 'proposed', route: chatRoute(), fixtures: handled('en'), render: inChat('en') },
]);

export default designFrames;
