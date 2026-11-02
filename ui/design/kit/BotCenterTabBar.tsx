// The raised center of the mobile tab bar is the main Bot (owner direction,
// 2026-11-02). Tap: open its conversation. Press and hold: speak to it; the
// recording overlay (MainBotVoice) takes over above the bar. There is no
// new-session sheet here any more; new sessions start from a project row.
import { Loader2, Mic } from 'lucide-react';
import clsx from 'clsx';

import { MobileNavLink, type ShellNavItem } from '@/components/MobileChrome';
import { useBots } from '@/context/BotsContext';
import { useWorkbenchInbox } from '@/context/WorkbenchInboxContext';

import { BotFace } from './BotFace';
import { useDraft } from './draft';

/** Where a press on the center is: resting, pressed (not yet a hold), recording,
 *  recording with the finger in the cancel zone, or released and settling. */
export type CenterPhase = 'idle' | 'pressed' | 'recording' | 'armed' | 'settling';

export function BotCenterTabBar({ items, phase = 'idle' }: { items: ShellNavItem[]; phase?: CenterPhase }) {
  const draft = useDraft();
  const { mainBot, displayName, statusBySession } = useBots();
  const { unreadBySession } = useWorkbenchInbox({ feed: false });
  const half = Math.ceil(items.length / 2);
  const name = mainBot ? displayName(mainBot) : '';
  const running = mainBot ? statusBySession[mainBot.session_id] === 'running' : false;
  const voice = phase === 'recording' || phase === 'armed' || phase === 'settling';
  return (
    <nav className={clsx('fixed inset-x-0 bottom-0 border-t border-border bg-surface/96 px-2 pt-2 pb-2 backdrop-blur md:hidden', voice ? 'z-50' : 'z-40')}>
      <div className="flex items-end justify-between gap-1">
        {items.slice(0, half).map((item) => <MobileNavLink key={item.label} item={item} />)}
        {mainBot && (
          <button
            type="button"
            aria-label={draft(`${name}：点一下打开对话，按住说话`, `${name}: tap to open, hold to speak`)}
            className="flex min-w-0 flex-1 flex-col items-center justify-end gap-1 px-1 pb-2 text-2xs"
          >
            <span
              className={clsx(
                // Lifted out of the row, so the bar keeps its height and the avatar rises above its edge.
                '-mt-5 relative rounded-full bg-surface p-0.5 ring-2 transition',
                phase === 'idle' && (running ? 'shadow-glow-lg-mint ring-mint' : 'shadow-glow-md-mint ring-mint/45'),
                phase === 'pressed' && 'scale-95 shadow-glow-md-mint ring-mint',
                phase === 'recording' && 'scale-110 shadow-glow-xl-mint ring-4 ring-mint',
                phase === 'armed' && 'scale-105 ring-4 ring-destructive/70 opacity-70',
                phase === 'settling' && 'shadow-glow-lg-mint ring-mint',
              )}
            >
              {phase === 'recording' && <span className="absolute -inset-2 animate-ping rounded-full border-2 border-mint/50 motion-reduce:animate-none" aria-hidden="true" />}
              <BotFace
                bot={mainBot}
                name={name}
                size={50}
                unread={voice ? 0 : unreadBySession[mainBot.session_id] ?? 0}
                running={phase === 'idle' && running}
              />
              {(phase === 'recording' || phase === 'armed') && (
                <span className={clsx('absolute -bottom-1 -right-1 grid size-5 place-items-center rounded-full text-primary-foreground', phase === 'armed' ? 'bg-destructive' : 'bg-mint')}>
                  <Mic className="size-3" />
                </span>
              )}
              {phase === 'settling' && (
                <span className="absolute -bottom-1 -right-1 grid size-5 place-items-center rounded-full bg-surface text-mint-ink">
                  <Loader2 className="size-3.5 animate-spin" />
                </span>
              )}
            </span>
            <span className={clsx('max-w-full truncate font-semibold', running || voice ? 'text-mint-ink' : 'text-foreground')}>
              {name}
            </span>
          </button>
        )}
        {items.slice(half).map((item) => <MobileNavLink key={item.label} item={item} />)}
      </div>
      <div className="h-(--safe-area-bottom)" />
    </nav>
  );
}
