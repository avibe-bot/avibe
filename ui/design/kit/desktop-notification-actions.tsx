// D2 (Desktop): act on a request from the macOS notification, and add an Agent's follow-up to the Mac's
// Reminders. Proposed pieces only; the macOS notifications are an illustration (macOS draws them), drawn
// with product tokens to fix the words, the buttons, and their order.
import type { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { CheckCheck, ChevronDown, Clock, CloudOff, MessageSquareReply, ShieldOff } from 'lucide-react';

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
    <div className="relative w-[372px]">
      <div className="flex gap-3 rounded-2xl border border-edge bg-popover p-3 text-popover-foreground shadow-elevation-3">
        <img src={logo} alt="" className="size-9 shrink-0 rounded-[9px]" />
        <div className="flex min-w-0 flex-1 flex-col gap-0.5">
          <div className="flex items-baseline justify-between gap-2">
            <span className="truncate text-[13px] font-semibold text-foreground">{title}</span>
            {time && <span className="shrink-0 text-[11px] text-muted">{time}</span>}
          </div>
          {subtitle && <span className="truncate text-[12px] font-medium text-foreground">{subtitle}</span>}
          {reply ? (
            <div className="mt-1.5 flex items-center gap-2">
              <div className="flex h-7 min-w-0 flex-1 items-center rounded-md border border-field-edge bg-field px-2 text-[12px] text-foreground">
                <span className="truncate">{reply.text}</span>
                <span aria-hidden className="ml-px h-3.5 w-px bg-foreground" />
              </div>
              <Button size="sm" className="h-7 px-3 text-[12px]">{t('发送', 'Send')}</Button>
            </div>
          ) : (
            <p className="line-clamp-3 text-[12px] leading-[1.45] text-foreground/90">{body}</p>
          )}
        </div>
        {actions.length > 0 && !reply && (
          <div className="flex shrink-0 items-start">
            <span className="inline-flex h-6 items-center gap-0.5 rounded-md border border-edge bg-surface-2 px-2 text-[11px] font-medium text-foreground">
              {t('选项', 'Options')}
              <ChevronDown aria-hidden className="size-3" />
            </span>
          </div>
        )}
      </div>
      {open && actions.length > 0 && (
        <div className="absolute right-3 top-[calc(100%+4px)] z-10 w-[200px] overflow-hidden rounded-lg border border-edge bg-popover py-1 shadow-elevation-3">
          {actions.map((action, index) => (
            <div
              key={action}
              className={`px-3 py-1.5 text-[12.5px] ${index === 0 ? 'bg-primary text-primary-foreground' : 'text-foreground'}`}
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
      <div className="flex h-7 shrink-0 items-center justify-end gap-4 border-b border-edge bg-surface px-4 text-[11px] text-muted">
        <span>{t('11月20日 周五 10:24', 'Fri Nov 20 10:24')}</span>
      </div>
      <div className="relative flex-1 bg-surface-2">
        <div className="absolute left-5 top-4 flex max-w-[380px] flex-col gap-1.5">
          <Badge variant="secondary">{t('示意 · 系统通知由 macOS 绘制', 'Illustration · macOS draws the notification')}</Badge>
          {note && <p className="text-[11.5px] leading-relaxed text-muted">{note}</p>}
        </div>
        <div className="absolute right-4 top-4 flex flex-col items-end gap-3">{children}</div>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* In-app outcome notice                                               */
/* ------------------------------------------------------------------ */

export type OutcomeKind = 'handled-elsewhere' | 'stale' | 'expired' | 'not-delivered' | 'reminders-denied';

const outcomeIcon: Record<OutcomeKind, typeof CheckCheck> = {
  'handled-elsewhere': CheckCheck,
  stale: MessageSquareReply,
  expired: Clock,
  'not-delivered': CloudOff,
  'reminders-denied': ShieldOff,
};

/** Shown above the transcript when the chat opens from a notification whose action did not go through. */
export function OutcomeNotice({ kind, choice }: { kind: OutcomeKind; choice?: string }) {
  const t = pick(useLang());
  const Icon = outcomeIcon[kind];
  const text: Record<OutcomeKind, string> = {
    'handled-elsewhere': t(`已经在其他设备上回复了「${choice ?? ''}」，通知里的选择没有再发送。`, `Already answered "${choice ?? ''}" on another device. Your choice in the notification wasn't sent.`),
    stale: t('这条消息之后会话已经有了新进展，通知里的回复没有发送。看完最新内容再回复。', "The conversation moved on after this message, so the reply from the notification wasn't sent. Read the latest, then reply."),
    expired: t('这条提醒事项请求已经过期，没有添加。需要的话请 Agent 再提一次。', "This reminder request expired and nothing was added. Ask the Agent again if you still need it."),
    'not-delivered': t('当时连不上这个实例，通知里的回复没有送达。现在可以在这里重新回复。', "This instance couldn't be reached, so the reply from the notification wasn't delivered. You can answer here now."),
    'reminders-denied': t('Avibe 没有「提醒事项」的访问权限，这条提醒没有添加。', "Avibe can't access Reminders, so this reminder wasn't added."),
  };
  const action: Partial<Record<OutcomeKind, string>> = {
    'reminders-denied': t('打开系统设置', 'Open System Settings'),
  };
  return (
    <div
      role="status"
      className="mx-auto flex w-full max-w-[1080px] items-center gap-2 rounded-md border border-edge bg-surface-2 px-3 py-2 text-[12px] text-muted shadow-elevation-0"
    >
      <Icon aria-hidden className="size-3.5 shrink-0" />
      <span className="min-w-0 flex-1">{text[kind]}</span>
      {action[kind] && (
        <Button variant="ghost" size="sm" className="h-6 px-2 text-[12px]">{action[kind]}</Button>
      )}
    </div>
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
