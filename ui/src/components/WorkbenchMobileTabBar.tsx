import React from 'react';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { Plus } from 'lucide-react';

import { useApi } from '../context/ApiContext';
import { useBots } from '../context/BotsContext';
import { useWorkbenchInbox } from '../context/WorkbenchInboxContext';
import { MAIN_BOT_SESSION_ID, botSessionPath, type BotAvatar as BotAvatarSpec } from '../lib/bots';
import { loadVoiceContext } from '../lib/voiceContext';
import { MobileTabBar, type BotCenterPhase, type ShellNavItem } from './MobileChrome';
import { MainBotHoldOverlay, MainBotVoiceToast } from './voice/MainBotVoice';
import { useHoldToTalk, type HoldPhase } from './voice/useHoldToTalk';

// The Workbench's phone tab bar and what sits in its middle. For the Owner it is
// the main Bot: a tap opens its conversation (ui/design: mobile/workbench-tab/team),
// and pressing and holding speaks to it (ui/design: mobile/main-bot-voice/*).
// Everyone else keeps the new-session ＋ (ui/design: mobile/workbench-tab/member).
// The main Bot's Session id is fixed, so the center is there from the first paint,
// with the main Bot's default name and mascot until the Bot list answers.
export const WorkbenchMobileTabBar: React.FC<{
  items: ShellNavItem[];
  /** Opens the new-session sheet; absent when this reader cannot start one. */
  onNewSession?: () => void;
}> = ({ items, onNewSession }) => {
  const { t } = useTranslation();
  const { enabled } = useBots();
  if (enabled) return <MainBotTabBar items={items} />;
  return (
    <MobileTabBar
      items={items}
      center={onNewSession ? { onClick: onNewSession, label: t('appShell.newSession'), icon: Plus } : undefined}
    />
  );
};

const CENTER_PHASE: Record<HoldPhase, BotCenterPhase> = {
  idle: 'idle',
  pressing: 'pressed',
  listening: 'recording',
  recording: 'recording',
  armed: 'armed',
  settling: 'settling',
  interrupted: 'idle',
};

/** The Owner's bar: the main Bot in the middle, tap to open, hold to speak. After a take the
 *  reader stays where they are; the toast's View opens the conversation. */
const MainBotTabBar: React.FC<{ items: ShellNavItem[] }> = ({ items }) => {
  const { t } = useTranslation();
  const api = useApi();
  const navigate = useNavigate();
  const { mainBot, displayName, statusBySession } = useBots();
  const { unreadBySession } = useWorkbenchInbox({ feed: false });
  const name = mainBot ? displayName(mainBot) : t('bots.mainDefaultName');
  const avatar: BotAvatarSpec | null = mainBot ? mainBot.avatar : { kind: 'mascot' };
  const open = () => navigate(botSessionPath({ session_id: MAIN_BOT_SESSION_ID }));
  const hold = useHoldToTalk({
    send: async (text, metadata) => {
      await api.sendSessionMessage(MAIN_BOT_SESSION_ID, { text, metadata });
      return true;
    },
    onTap: open,
    context: () => loadVoiceContext(api.listSessionMessages, MAIN_BOT_SESSION_ID),
    // Kept on the device until sent; if it cannot be, it waits in the main Bot's composer.
    keep: () => ({ target: MAIN_BOT_SESSION_ID }),
  });
  return (
    <>
      <MobileTabBar
        items={items}
        bot={{
          avatar,
          name,
          label: t('appShell.openMainBot', { name }),
          unread: unreadBySession[MAIN_BOT_SESSION_ID] ?? 0,
          running: statusBySession[MAIN_BOT_SESSION_ID] === 'running',
          onClick: open,
          // Pressed from the gesture's ring delay (100 ms), as every hold surface is: a tap does not flash.
          phase: hold.phase === 'pressing' && hold.progress <= 0 ? 'idle' : CENTER_PHASE[hold.phase],
          hold: hold.bind,
        }}
      />
      <MainBotHoldOverlay hold={hold} bot={{ name, avatar }} />
      <MainBotVoiceToast hold={hold} bot={{ name, avatar }} onView={open} />
    </>
  );
};
