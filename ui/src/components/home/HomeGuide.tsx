import { useTranslation } from 'react-i18next';
import { useLocation, useNavigate } from 'react-router-dom';

import { useBots } from '../../context/BotsContext';
import { useInstanceAuthorization } from '../../context/InstanceAuthorizationContext';
import { isDesktopShell } from '../../lib/desktopShell';
import { formatDesktopPetShortcut } from '../../lib/desktopPetShortcuts';
import { localDate, type SetupItem } from '../../lib/homePreferences';
import { isApplePlatform } from '../../lib/platform';
import { settingsOverlayContinuationState } from '../../lib/settingsOverlay';
import { useDesktopPetSettings } from '../../lib/useDesktopPetSettings';
import { useIsDesktop } from '../../lib/useIsDesktop';
import { MODEL_HUB_SETTINGS_PATH } from '../settings/models/modelHubRoutes';
import { GettingStarted } from './GettingStarted';
import { useTips, type Tip } from './homeCopy';
import { TipOfTheDay } from './TipOfTheDay';
import { dayNumber, TIP_IDS, tipsForDay, type TipId } from './tips';
import { useHomePreferences } from './useHomePreferences';
import { useSetupGaps } from './useSetupGaps';

/** Where each Getting started item is fixed, when that is a page (notifications turn on in place). */
const FIX_PATH: Partial<Record<SetupItem, string>> = {
  model: MODEL_HUB_SETTINGS_PATH,
  computer_use: '/settings/computer-use',
  chat_apps: '/settings/platforms',
  phone: '/settings/remote-access',
};

/**
 * The home's second and third blocks (ui/design: home/home/*): Getting started while something is
 * missing, then the tip of the day. Both remember the account's answers (`useHomePreferences`):
 * "Later" hides Getting started until tomorrow, "Not needed" drops an item for good, × hides the
 * tip until tomorrow, and Settings · General turns tips off. Neither shows before those answers are
 * read, so nothing put off today flashes up first. The tip waits while the main Bot has no model:
 * until it can work, teaching more is noise.
 *
 * The tip is chosen by day from the tips this reader can act on, the capabilities not known to be
 * used first. Used is read from facts the home already has: a custom Bot exists, a chat app is
 * connected. Whether Show Pages, Tasks, the Vault, Skills, the desktop pet, or voice have been used
 * is not observed here, so those rotate by date.
 */
export function HomeGuide({
  owner,
  noModel,
  name,
  onAsk,
}: {
  /** The Owner (with Bots); a member sees what is theirs to fix and to try. */
  owner: boolean;
  noModel: boolean;
  /** The main Bot's name, which the copy speaks of. */
  name: string;
  /** Put an ask in the composer (never sends). */
  onAsk: (ask: string) => void;
}) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const location = useLocation();
  const desktop = useIsDesktop();
  const { capabilities } = useInstanceAuthorization();
  const { bots, openProfile } = useBots();
  const { preferences, loaded, update } = useHomePreferences();
  const gaps = useSetupGaps({ owner, noModel });
  const pet = useDesktopPetSettings();
  const petShortcut = pet.state?.voice_shortcut ? formatDesktopPetShortcut(pet.state.voice_shortcut, t, isApplePlatform()) : null;
  const tipCopy = useTips({ name, petShortcut });
  const now = new Date();
  const today = localDate(now);

  // Settings opens over the home and comes back to it; any other page is an ordinary visit.
  const open = (to: string) =>
    navigate(to, to.startsWith('/settings') ? { state: settingsOverlayContinuationState(location) } : undefined);
  const pending = gaps.pending.filter((item) => !preferences.setup_not_needed.includes(item));
  const showSetup = loaded && pending.length > 0 && preferences.setup_later_on !== today;

  const available = TIP_IDS.filter((id): boolean => {
    switch (id) {
      case 'bots':
      case 'chatApps':
        return owner;
      case 'showPages':
        return capabilities.can_use_show_pages;
      case 'tasks':
        return true;
      case 'vault':
        return capabilities.can_use_vault_secrets;
      case 'skills':
        return capabilities.can_use_skills;
      case 'desktopPet':
        return owner && isDesktopShell();
      case 'voice':
        return owner && !desktop;
    }
  });
  const used = new Set<TipId>();
  if (bots.some((bot) => !bot.builtin)) used.add('bots');
  if (gaps.chatAppsConnected) used.add('chatApps');
  const { order, start } = tipsForDay(available, used, dayNumber(now));
  const showTip = loaded && !noModel && preferences.tips_enabled && preferences.tip_hidden_on !== today && order.length > 0;

  const tryTip = (tip: Tip) => {
    const action = tip.tryIt;
    if (!action) return;
    if (action.kind === 'ask') onAsk(action.ask);
    else if (action.kind === 'addBot') openProfile({ kind: 'create' });
    else open(action.to);
  };

  return (
    <>
      {showSetup && (
        <GettingStarted
          pending={pending}
          name={name}
          computerUseHost={gaps.computerUseHost}
          onAction={(item) => {
            const path = FIX_PATH[item];
            if (path) open(path);
            else void gaps.enableNotifications();
          }}
          onNotNeeded={(item) => update({ setup_not_needed: [item] })}
          onLater={() => update({ setup_later_on: today })}
        />
      )}
      {showTip && (
        <TipOfTheDay
          tips={order.map((id) => tipCopy[id])}
          start={start}
          onTry={tryTip}
          onHide={() => update({ tip_hidden_on: today })}
        />
      )}
    </>
  );
}
