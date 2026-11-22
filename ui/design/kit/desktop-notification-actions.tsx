// D2 (Desktop): act on a request from the macOS notification, and add an Agent's follow-up to the Mac's
// Reminders. Proposed pieces only; the macOS notifications are an illustration (macOS draws them), drawn
// with product tokens to fix the words, the buttons, and their order.
import type { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { ChevronDown } from 'lucide-react';

import { SettingsPageShell } from '@/components/settings/SettingsPageShell';
import { SettingsPanel, SettingsRow, ToggleSwitch } from '@/components/settings/SettingsPrimitives';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import logo from '@/assets/logo.png';

type Lang = 'zh' | 'en';
const useLang = (): Lang => (useTranslation().i18n.language?.startsWith('zh') ? 'zh' : 'en');
const noop = () => {};
const pick = (lang: Lang) => (zh: string, en: string) => (lang === 'zh' ? zh : en);

/* ------------------------------------------------------------------ */
/* macOS notification (illustration)                                   */
/* ------------------------------------------------------------------ */

export type MacNotificationProps = {
  title: string;
  body: string;
  time?: string;
  /** Action titles in macOS order. Shown in the open Options menu. */
  actions?: string[];
  /** The Options menu is open (the state a pointer over the banner reaches). */
  open?: boolean;
  /** The text field a Reply action opens, with what has been typed. */
  reply?: { text: string };
  /** The second line macOS shows under the title. */
  subtitle?: string;
};

/** One macOS banner: app icon, title, body, the time, and its Options menu or Reply field. */
export function MacNotification({ title, subtitle, body, time, actions = [], open = false, reply }: MacNotificationProps) {
  const t = pick(useLang());
  return (
    <div className="relative w-96">
      <div className="flex gap-3 rounded-2xl border border-edge bg-popover p-3 text-popover-foreground shadow-elevation-3">
        <img src={logo} alt="" className="size-9 shrink-0 rounded-md" />
        <div className="flex min-w-0 flex-1 flex-col gap-0.5">
          <div className="flex items-baseline justify-between gap-2">
            <span className="truncate text-sm font-semibold text-foreground">{title}</span>
            {time && <span className="shrink-0 text-xs text-muted">{time}</span>}
          </div>
          {subtitle && <span className="truncate text-xs font-medium text-foreground">{subtitle}</span>}
          {reply ? (
            <div className="mt-1.5 flex items-center gap-2">
              <div className="flex h-7 min-w-0 flex-1 items-center rounded-md border border-field-edge bg-field px-2 text-xs text-foreground">
                <span className="truncate">{reply.text}</span>
                <span aria-hidden className="ml-px h-3.5 w-px bg-foreground" />
              </div>
              <Button size="sm" className="h-7 px-3 text-xs">{t('发送', 'Send')}</Button>
            </div>
          ) : (
            <p className="line-clamp-3 text-xs leading-snug text-foreground/90">{body}</p>
          )}
        </div>
        {actions.length > 0 && !reply && (
          <div className="flex shrink-0 items-start">
            <span className="inline-flex h-6 items-center gap-0.5 rounded-md border border-edge bg-surface-2 px-2 text-xs font-medium text-foreground">
              {t('选项', 'Options')}
              <ChevronDown aria-hidden className="size-3" />
            </span>
          </div>
        )}
      </div>
      {open && actions.length > 0 && (
        <div className="absolute right-3 top-full mt-1 z-10 w-52 overflow-hidden rounded-lg border border-edge bg-popover py-1 shadow-elevation-3">
          {actions.map((action, index) => (
            <div
              key={action}
              className={`px-3 py-1.5 text-sm ${index === 0 ? 'bg-primary text-primary-foreground' : 'text-foreground'}`}
            >
              {action}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/** The corner of a Mac screen where banners appear, labelled as an illustration. */
export function MacScreenCorner({ children, note }: { children: ReactNode; note?: string }) {
  const t = pick(useLang());
  return (
    <div className="flex h-full flex-col bg-background text-foreground">
      <div className="flex h-7 shrink-0 items-center justify-end gap-4 border-b border-edge bg-surface px-4 text-xs text-muted">
        <span>{t('11月20日 周五 10:24', 'Fri Nov 20 10:24')}</span>
      </div>
      <div className="relative flex-1 bg-surface-2">
        <div className="absolute left-5 top-4 flex max-w-sm flex-col gap-1.5">
          <Badge variant="secondary">{t('示意 · 系统通知由 macOS 绘制', 'Illustration · macOS draws the notification')}</Badge>
          {note && <p className="text-xs leading-relaxed text-muted">{note}</p>}
        </div>
        <div className="absolute right-4 top-4 flex flex-col items-end gap-3">{children}</div>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* The illustrated notifications, by case                              */
/* ------------------------------------------------------------------ */

/** Draft copy in the frame's language. */
const useCopy = () => {
  const zh = useTranslation().i18n.language?.startsWith('zh') ?? true;
  return (zhText: string, enText: string) => (zh ? zhText : enText);
};

/** The Agent's question as macOS shows it: Options open, or the reply field. */
export function QuestionNotification({ open = false, reply = false }: { open?: boolean; reply?: boolean }) {
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

/** The Reminders confirmation as macOS shows it. */
export function ReminderNotification() {
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

/** The follow-up notifications for an action that did not take effect. */
export function OutcomeNotifications() {
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

/* ------------------------------------------------------------------ */
/* Settings › Notifications on the Desktop app                         */
/* ------------------------------------------------------------------ */

export type DesktopDeviceState = 'ready' | 'needs-permission' | 'reminders-denied' | 'unsupported';

/** "This device" on the Desktop app: system notifications and Reminders, each with its real status. */
export function DesktopDevicePanel({ state }: { state: DesktopDeviceState }) {
  const t = pick(useLang());
  const notificationsAllowed = state !== 'needs-permission';
  const reminders: { badge: ReactNode; description: string; control?: ReactNode } = {
    ready: {
      badge: <Badge variant="success">{t('已允许', 'Allowed')}</Badge>,
      description: t(
        'Agent 提出的跟进事项，你在通知里点「添加到提醒事项」后，加到这台 Mac 的「提醒事项」。Avibe 只修改或删除自己添加的条目；用 iCloud 同步时 iPhone 上也会看到同一条。',
        'When an Agent suggests a follow-up and you choose "Add to Reminders" in the notification, it goes into this Mac\'s Reminders. Avibe only changes or removes items it added. With iCloud, the same item shows on your iPhone.',
      ),
    },
    'needs-permission': {
      badge: <Badge variant="secondary">{t('还没有询问', 'Not asked yet')}</Badge>,
      description: t(
        '第一次添加提醒时 macOS 会询问一次。也可以现在就允许。确认添加要用到通知，所以先允许通知。',
        'macOS asks the first time a reminder is added, or you can allow it now. Confirming uses a notification, so allow notifications first.',
      ),
      control: <Button variant="outline" size="sm" disabled>{t('允许访问', 'Allow access')}</Button>,
    },
    'reminders-denied': {
      badge: <Badge variant="warning">{t('未允许', 'Not allowed')}</Badge>,
      description: t(
        'macOS 没有允许 Avibe 访问「提醒事项」，Agent 的跟进不会加到这台 Mac。到系统设置 › 隐私与安全性 › 提醒事项里打开 Avibe。',
        "macOS hasn't allowed Avibe to use Reminders, so Agent follow-ups won't be added on this Mac. Turn Avibe on in System Settings › Privacy & Security › Reminders.",
      ),
      control: <Button variant="outline" size="sm">{t('打开系统设置', 'Open System Settings')}</Button>,
    },
    unsupported: {
      badge: <Badge variant="secondary">{t('需要 macOS 14', 'Needs macOS 14')}</Badge>,
      description: t(
        '由 Avibe 添加提醒事项需要 macOS 14 或更新版本，这台 Mac 的系统不支持。Agent 的跟进会交给支持的 iPhone，或者只留在会话里。',
        "Adding reminders from Avibe needs macOS 14 or later, which this Mac doesn't have. Agent follow-ups go to an iPhone that supports them, or stay in the conversation.",
      ),
    },
  }[state];

  return (
    <SettingsPanel title={t('本设备', 'This device')}>
      <SettingsRow
        title={t('桌面 App 系统通知', 'Desktop app notifications')}
        tone={notificationsAllowed ? 'default' : 'warning'}
        description={notificationsAllowed
          ? t(
            'Vault 审批、后台任务完成，以及 Agent 等你回复的消息。等你回复的消息可以直接在通知里选或回复。只对这台电脑生效，和菜单栏里的开关是同一个。',
            'Vault approvals, finished background runs, and Agent messages waiting for your answer, which you can pick or reply to right in the notification. This computer only; same switch as the menu bar.',
          )
          : t(
            'macOS 没有允许 Avibe 发通知，所以这台电脑收不到通知，也不能在通知里回复。',
            "macOS hasn't allowed Avibe to send notifications, so this computer gets no alerts and you can't reply from them.",
          )}
        control={notificationsAllowed
          ? <ToggleSwitch enabled onClick={noop} label={t('桌面 App 系统通知', 'Desktop app notifications')} />
          : <Button variant="outline" size="sm">{t('打开系统设置', 'Open System Settings')}</Button>}
      />
      <SettingsRow
        title={(
          <span className="inline-flex items-center gap-2">
            {t('提醒事项', 'Reminders')}
            {reminders.badge}
          </span>
        )}
        description={reminders.description}
        control={reminders.control}
      />
    </SettingsPanel>
  );
}

/** Settings › Notifications as the Desktop app would show it: the account's switches as shipped, then this device. */
export function DesktopNotificationsSettings({ state }: { state: DesktopDeviceState }) {
  const { t } = useTranslation();
  return (
    <SettingsPageShell
      title={t('settings.sections.notifications')}
      subtitle={t('settings.notifications.subtitle')}
      activeTab="notifications"
    >
      <SettingsPanel>
        <SettingsRow
          title={t('workbench.notifications.title')}
          description={t('settings.notifications.masterHint')}
          control={<ToggleSwitch enabled onClick={noop} label={t('workbench.notifications.title')} />}
        />
        <SettingsRow
          title={t('workbench.notifications.bots')}
          description={t('settings.notifications.botsHint')}
          control={<ToggleSwitch enabled onClick={noop} label={t('workbench.notifications.bots')} />}
        />
        <SettingsRow
          title={t('workbench.notifications.sessions')}
          description={t('settings.notifications.sessionsHint')}
          control={<ToggleSwitch enabled onClick={noop} label={t('workbench.notifications.sessions')} />}
        />
      </SettingsPanel>
      <DesktopDevicePanel state={state} />
    </SettingsPageShell>
  );
}
