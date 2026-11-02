// Variant A: the raised center of the mobile tab bar is the main Bot. One tap opens
// its conversation; a long press opens every Bot (BotSwitchSheet). The side tabs
// are the shipped ones; "new session" moves to the header (NewSessionHeaderAction).
import clsx from 'clsx';

import { MobileNavLink, type ShellNavItem } from '@/components/MobileChrome';
import { useBots } from '@/context/BotsContext';
import { useWorkbenchInbox } from '@/context/WorkbenchInboxContext';

import { BotFace } from './BotFace';
import { useDraft } from './draft';

export function BotCenterTabBar({ items, pressed = false }: { items: ShellNavItem[]; pressed?: boolean }) {
  const draft = useDraft();
  const { mainBot, displayName, statusBySession } = useBots();
  const { unreadBySession } = useWorkbenchInbox({ feed: false });
  const half = Math.ceil(items.length / 2);
  const name = mainBot ? displayName(mainBot) : '';
  const running = mainBot ? statusBySession[mainBot.session_id] === 'running' : false;
  return (
    <nav className={clsx('fixed inset-x-0 bottom-0 border-t border-border bg-surface/96 px-2 pt-2 pb-[calc(0.5rem+env(safe-area-inset-bottom))] backdrop-blur md:hidden', pressed ? 'z-[60]' : 'z-40')}>
      <div className="flex items-end justify-between gap-1">
        {items.slice(0, half).map((item) => <MobileNavLink key={item.label} item={item} />)}
        {mainBot && (
          <button
            type="button"
            aria-label={draft(`和${name}对话`, `Talk to ${name}`)}
            className="flex min-w-0 flex-1 flex-col items-center justify-end gap-1 px-1 pb-2 text-[10px]"
          >
            <span
              className={clsx(
                // Lifted out of the row, so the bar keeps its height and the avatar rises above its edge.
                '-mt-5 rounded-full bg-surface p-[3px] ring-2 transition',
                running ? 'shadow-glow-lg-mint ring-mint' : 'shadow-glow-md-mint ring-mint/45',
                pressed && 'scale-95 ring-mint',
              )}
            >
              <BotFace
                bot={mainBot}
                name={name}
                size={50}
                unread={unreadBySession[mainBot.session_id] ?? 0}
                running={running}
              />
            </span>
            <span className={clsx('max-w-full truncate font-semibold', running ? 'text-mint-ink' : 'text-foreground')}>
              {name}
            </span>
          </button>
        )}
        {items.slice(half).map((item) => <MobileNavLink key={item.label} item={item} />)}
      </div>
    </nav>
  );
}

/** A first-run hint above the center: what a tap and a long press do. */
export function BotCenterHint() {
  const draft = useDraft();
  const { mainBot, displayName } = useBots();
  const name = mainBot ? displayName(mainBot) : '';
  return (
    <div className="pointer-events-none fixed inset-x-0 bottom-[calc(6.5rem+env(safe-area-inset-bottom))] z-50 flex justify-center px-6 md:hidden">
      <div className="relative max-w-[320px] rounded-2xl border border-mint/35 bg-surface-2 px-3.5 py-2.5 text-center text-[12.5px] leading-snug text-foreground shadow-glow-md-mint">
        <span className="font-semibold">{draft(`${name}在这里`, `${name} is here`)}</span>
        <span className="text-muted">
          {draft('：点一下说话，长按看全部伙伴', ': tap to talk, hold for all your Bots')}
        </span>
        <span className="absolute -bottom-1.5 left-1/2 size-3 -translate-x-1/2 rotate-45 border-b border-r border-mint/35 bg-surface-2" />
      </div>
    </div>
  );
}
