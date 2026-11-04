import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router-dom';
import { ChevronRight, MessagesSquare, Settings2 } from 'lucide-react';
import clsx from 'clsx';

import { useBots } from '../../context/BotsContext';
import { useInstanceAuthorization } from '../../context/InstanceAuthorizationContext';
import { useNotificationPreferences } from '../../context/NotificationPreferencesContext';
import { NOTIFICATIONS_SETTINGS_PATH } from '../../lib/notificationPreferences';
import { BotAvatar } from '../ui/bot-avatar';
import { Switch } from '../ui/switch';
import { WebPushControl } from './WebPushControl';

function CategoryRow({
  icon,
  title,
  hint,
  checked,
  disabled,
  onChange,
}: {
  icon: React.ReactNode;
  title: string;
  hint: string;
  checked: boolean;
  disabled: boolean;
  onChange: (next: boolean) => void;
}) {
  return (
    <div className={clsx('flex items-center gap-2.5 px-1 py-1.5', disabled && 'opacity-50')}>
      {icon}
      <div className="min-w-0 flex-1 leading-tight">
        <div className="text-[12.5px] font-semibold text-foreground">{title}</div>
        <div className="mt-0.5 text-[10.5px] text-muted">{hint}</div>
      </div>
      <Switch size="sm" checked={checked} disabled={disabled} onCheckedChange={onChange} label={title} />
    </div>
  );
}

/**
 * The notification switches in one compact panel (docs/plans/workbench-refinements.md
 * §1): the master switch, the two categories when the caller has Bots, this
 * device's push state, and the way to the full Settings page. The Inbox page's
 * Notifications button and the Inbox popover's bell both open it.
 */
export function NotificationMenu({ onNavigate }: { onNavigate?: () => void }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { preferences, update } = useNotificationPreferences();
  const { enabled: hasBots, bots, displayName } = useBots();
  const { capabilities } = useInstanceAuthorization();
  const off = !preferences.enabled;

  return (
    <div className="flex w-full flex-col gap-1">
      <div className="flex items-center gap-2 px-1 pb-1">
        <div className="min-w-0 flex-1">
          <div className="text-[13px] font-bold text-foreground">{t('workbench.notifications.title')}</div>
          <div className="text-[10.5px] leading-snug text-muted">{t('workbench.notifications.description')}</div>
        </div>
        <Switch
          checked={preferences.enabled}
          onCheckedChange={(enabled) => void update({ enabled })}
          label={t('workbench.notifications.title')}
        />
      </div>
      {hasBots && (
        <>
          <div className="mx-1 h-px bg-border" />
          <CategoryRow
            icon={(
              <span className="flex shrink-0 -space-x-1.5">
                {bots.slice(0, 3).map((bot) => (
                  <BotAvatar key={bot.handle} avatar={bot.avatar} name={displayName(bot)} size={18} inList className="ring-2 ring-surface-2" />
                ))}
              </span>
            )}
            title={t('workbench.notifications.bots')}
            hint={t('workbench.notifications.botsHint')}
            checked={preferences.bots}
            disabled={off}
            onChange={(bots) => void update({ bots })}
          />
          <CategoryRow
            icon={(
              <span className="grid size-[22px] shrink-0 place-items-center rounded-md bg-foreground/[0.06] text-muted">
                <MessagesSquare className="size-3.5" />
              </span>
            )}
            title={t('workbench.notifications.sessions')}
            hint={t('workbench.notifications.sessionsHint')}
            checked={preferences.sessions}
            disabled={off}
            onChange={(sessions) => void update({ sessions })}
          />
          {!off && !preferences.sessions && (
            <div className="mx-1 rounded-md bg-foreground/[0.04] px-2 py-1.5 text-[10.5px] leading-relaxed text-muted">
              {t('workbench.notifications.sessionsOff')}
            </div>
          )}
        </>
      )}
      {capabilities.can_read_instance && (
        <>
          <div className="mx-1 my-1 h-px bg-border" />
          <div className="flex flex-col gap-1.5 px-1">
            <span className="text-[11px] text-muted">{t('workbench.notifications.device')}</span>
            <WebPushControl />
          </div>
        </>
      )}
      <button
        type="button"
        onClick={() => {
          onNavigate?.();
          navigate(NOTIFICATIONS_SETTINGS_PATH);
        }}
        className="mt-1 flex items-center gap-1 rounded-md px-1 py-1 text-left text-[11px] font-medium text-cyan-ink hover:underline"
      >
        <Settings2 className="size-3" />
        {t('workbench.notifications.moreSettings')}
        <ChevronRight className="size-3" />
      </button>
    </div>
  );
}
