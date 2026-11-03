import React from 'react';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { Plus } from 'lucide-react';

import { useBots } from '../context/BotsContext';
import { useWorkbenchInbox } from '../context/WorkbenchInboxContext';
import { MAIN_BOT_SESSION_ID, botSessionPath } from '../lib/bots';
import { MobileTabBar, type ShellNavItem } from './MobileChrome';

// The Workbench's phone tab bar and what sits in its middle. For the Owner it is
// the main Bot: a tap opens its conversation (ui/design: mobile/workbench-tab/team).
// Everyone else keeps the new-session ＋ (ui/design: mobile/workbench-tab/member).
// The main Bot's Session id is fixed, so the center is there from the first paint,
// with the main Bot's default name and mascot until the Bot list answers.
export const WorkbenchMobileTabBar: React.FC<{
  items: ShellNavItem[];
  /** Opens the new-session sheet; absent when this reader cannot start one. */
  onNewSession?: () => void;
}> = ({ items, onNewSession }) => {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { enabled, mainBot, displayName, statusBySession } = useBots();
  const { unreadBySession } = useWorkbenchInbox({ feed: false });
  if (enabled) {
    const name = mainBot ? displayName(mainBot) : t('bots.mainDefaultName');
    return (
      <MobileTabBar
        items={items}
        bot={{
          avatar: mainBot ? mainBot.avatar : { kind: 'mascot' },
          name,
          label: t('appShell.openMainBot', { name }),
          unread: unreadBySession[MAIN_BOT_SESSION_ID] ?? 0,
          running: statusBySession[MAIN_BOT_SESSION_ID] === 'running',
          onClick: () => navigate(botSessionPath({ session_id: MAIN_BOT_SESSION_ID })),
        }}
      />
    );
  }
  return (
    <MobileTabBar
      items={items}
      center={onNewSession ? { onClick: onNewSession, label: t('appShell.newSession'), icon: Plus } : undefined}
    />
  );
};
