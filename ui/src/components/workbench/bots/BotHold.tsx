import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode } from 'react';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';

import { useApi } from '../../../context/ApiContext';
import { useBotOrderHold, useBots } from '../../../context/BotsContext';
import { botSessionPath, type BotAvatar as BotAvatarSpec } from '@/lib/bots';
import { useIsDesktop } from '@/lib/useIsDesktop';
import { loadVoiceContext } from '@/lib/voiceContext';
import { MouseHint, type HoldPointer } from '../../voice/ComposerHold';
import { MainBotVoiceToast, type HoldView } from '../../voice/MainBotVoice';
import { MOUSE_HOLD_GESTURE, useHoldToTalk } from '../../voice/useHoldToTalk';
import { VoiceCancelTarget, VoiceCard, VoiceInterruptedActions } from '../../voice/VoiceCard';
import { BotHoldContext, type BotAvatarHold, type BotHoldValue } from './BotHoldContext';

// Hold a Bot's avatar to talk to it (docs/plans/bots.md D5): the composer's hold-to-talk on the
// Bots strip, desktop and phone, the main Bot included. One hold serves every avatar, so the
// strip asks once whether voice is available and two avatars can never record at once. The
// gesture is `useHoldToTalk`'s, with the composer's numbers: a mouse holds 350 ms with the ring
// and cancels by leaving the avatar, a finger holds 200 ms and slides up to cancel, Esc cancels,
// there is no time limit, and a take cut off keeps its words. A tap still opens the
// conversation and a right-click still opens the menu. After sending, the reader stays where
// they are; the toast's View opens the conversation.

// The page keeps its own long press: callouts and selection would cover the face being held.
// Scrolling stays the page's until the hold opens the microphone (`BotStrip` stops it then), so
// a scroll of the strip never starts a hold.
const AVATAR_HOLD_STYLE: CSSProperties = { WebkitTouchCallout: 'none', WebkitUserSelect: 'none', userSelect: 'none' };

type Target = { handle: string; pointer: HoldPointer };

/** One hold for every Bot avatar, its card, and its toast. Mounted once over the shell. */
export function BotHoldProvider({ children }: { children: ReactNode }) {
  const api = useApi();
  const navigate = useNavigate();
  const { enabled, botForHandle, displayName } = useBots();
  const [target, setTarget] = useState<Target | null>(null);
  const targetRef = useRef<Target | null>(null);
  const tapRef = useRef<(() => void) | null>(null);
  const hold = useHoldToTalk({
    send: async (text, metadata) => {
      const bot = botForHandle(targetRef.current?.handle);
      if (!bot) return false;
      await api.sendSessionMessage(bot.session_id, { text, metadata });
      return true;
    },
    onTap: () => tapRef.current?.(),
    // The held Bot's own conversation: its last reply and what the user last said to it.
    context: () => {
      const bot = botForHandle(targetRef.current?.handle);
      return bot ? loadVoiceContext(api.listSessionMessages, bot.session_id) : undefined;
    },
    gesture: (pointerType) => (pointerType === 'mouse' ? MOUSE_HOLD_GESTURE : {}),
    // The system taking the press (blur, background, a lost pointer) keeps what was heard, as the
    // composer's hold does: the card offers to send, retry, or discard it.
    onSystemInterrupt: 'interrupt',
  });
  // A Bot does not move under the avatar being held (D13), nor while its take waits on the reader.
  useBotOrderHold(hold.phase !== 'idle');

  const { bind: gesture, phase } = hold;
  const bind = useCallback((bot: { handle: string }): BotAvatarHold => ({
    onPointerDown: (event, onTap) => {
      // The press a hold may take decides who the take goes to; a press while a take is pending
      // (or with another button) leaves it as it is.
      if (phase === 'idle' && event.button === 0) {
        const next: Target = { handle: bot.handle, pointer: event.pointerType === 'mouse' ? 'mouse' : 'touch' };
        targetRef.current = next;
        tapRef.current = onTap;
        setTarget(next);
      }
      gesture.onPointerDown(event);
    },
    onPointerMove: gesture.onPointerMove,
    onPointerUp: gesture.onPointerUp,
    onPointerCancel: gesture.onPointerCancel,
    onLostPointerCapture: gesture.onLostPointerCapture,
    onClick: gesture.onClick,
    onContextMenu: gesture.onContextMenu,
    style: AVATAR_HOLD_STYLE,
  }), [gesture, phase]);

  const value = useMemo<BotHoldValue>(() => ({
    held: enabled && target && phase !== 'idle'
      ? { handle: target.handle, phase, progress: hold.progress, pointer: target.pointer }
      : null,
    bind: enabled ? bind : undefined,
  }), [enabled, target, phase, hold.progress, bind]);

  const heldBot = target ? botForHandle(target.handle) : undefined;
  const face = heldBot ? { name: displayName(heldBot), avatar: heldBot.avatar } : null;
  // A take whose Bot is gone (removed while its take waited) has nowhere to go: it is discarded, so
  // the one hold every avatar shares is free again rather than waiting on a card nobody can see.
  const { discard } = hold;
  const orphaned = Boolean(target) && !heldBot && phase !== 'idle';
  useEffect(() => {
    if (orphaned) discard();
  }, [orphaned, discard]);
  return (
    <BotHoldContext.Provider value={value}>
      {children}
      {face && target && (
        <>
          <BotHoldLayer hold={hold} bot={face} handle={target.handle} pointer={target.pointer} />
          <MainBotVoiceToast
            hold={hold}
            bot={face}
            placement="shell"
            onView={() => {
              if (heldBot) navigate(botSessionPath(heldBot));
            }}
          />
        </>
      )}
    </BotHoldContext.Provider>
  );
}

/** The held avatar and its strip's row of avatars as they stand now: a strip's avatar carries
 *  `data-bot-hold-anchor`, its row `data-bot-strip-row`. */
function findAnchor(handle: string): { avatar: DOMRect; strip: DOMRect } | null {
  // A handle is lowercase ASCII letters, digits, and hyphens (bots.md D2): safe in the selector as is.
  const avatar = [...document.querySelectorAll<HTMLElement>(`[data-bot-hold-anchor="${handle}"]`)]
    .find((element) => element.getClientRects().length > 0);
  const strip = avatar?.closest('[data-bot-strip-row]');
  return avatar && strip ? { avatar: avatar.getBoundingClientRect(), strip: strip.getBoundingClientRect() } : null;
}

/** A ref that places its element from the held avatar each time it commits: the card and the
 *  cancel target follow the avatar they belong to without a second render. */
const placedBy = (
  handle: string,
  place: (style: CSSStyleDeclaration, anchor: { avatar: DOMRect; strip: DOMRect }, node: HTMLElement) => void,
) =>
  (node: HTMLElement | null) => {
    if (!node) return;
    const anchor = findAnchor(handle);
    if (anchor) place(node.style, anchor, node);
  };

/** How close the cancel target may come to the screen's edges. */
const SCREEN_MARGIN = 8;

/** The take's card while it runs, settles, or waits after being cut off, and how to cancel (owner
 *  decision, 2026-11-04: next to the held avatar on both). A desktop: the card beside the avatar,
 *  its top at the avatar's, the hint under it. A phone: the card under the strip, the cancel target
 *  right above the finger. The page stays as it is but for the card's own halo. */
export function BotHoldLayer({
  hold,
  bot,
  handle,
  pointer,
}: {
  hold: HoldView;
  bot: { name: string; avatar: BotAvatarSpec | null };
  handle: string;
  pointer: HoldPointer;
}) {
  const { t } = useTranslation();
  const desktop = useIsDesktop();
  const { phase } = hold;
  const quiet = phase === 'interrupted' && hold.interruptReason === 'failed' && !hold.text.trim();
  if (phase === 'idle' || phase === 'pressing' || quiet) return null;
  const live = phase === 'listening' || phase === 'recording' || phase === 'armed';
  const card = (
    <VoiceCard
      to={{ name: bot.name, avatar: bot.avatar }}
      phase={phase}
      text={hold.text}
      stash={hold.stash}
      seconds={hold.seconds}
      reason={hold.interruptReason ?? 'dropped'}
      level={hold.level}
      actions={
        <VoiceInterruptedActions
          canRetry={hold.canRetry}
          canSend={Boolean(hold.text.trim())}
          onDiscard={hold.discard}
          onRetry={hold.retry}
          onSend={hold.sendInterrupted}
        />
      }
    />
  );
  const cancel = live && (pointer === 'mouse'
    ? <MouseHint armed={phase === 'armed'} box="avatar" />
    : <VoiceCancelTarget armed={phase === 'armed'} />);
  const label = t('voice.to', { name: bot.name });

  if (desktop) {
    // Beside the avatar, its top at the avatar's; the hint under the card.
    return (
      <div
        ref={placedBy(handle, (style, { avatar }) => {
          style.top = `${Math.max(16, avatar.top - 8)}px`;
        })}
        role="dialog"
        aria-label={label}
        className="fixed top-24 left-[calc(var(--app-sidebar-w)+1rem)] z-50 flex w-full max-w-md flex-col items-start gap-3"
      >
        {card}
        {cancel}
      </div>
    );
  }

  // A phone: the card under the strip, and the target the finger slides up to right above it.
  return (
    <>
      {cancel && (
        <div
          ref={placedBy(handle, (style, { avatar }, node) => {
            // Above the finger, but whole on the screen: the main Bot sits at the strip's far
            // left, and a strip near the top leaves little room under the status bar. Measured
            // once per take, so the target does not move under the finger when it arms (its hint
            // changes width).
            node.dataset.width ??= String(node.offsetWidth);
            const half = Number(node.dataset.width) / 2;
            const center = avatar.left + avatar.width / 2;
            style.left = `${Math.min(Math.max(center, half + SCREEN_MARGIN), window.innerWidth - half - SCREEN_MARGIN)}px`;
            style.top = `max(${avatar.top - 8}px, calc(env(safe-area-inset-top, 0px) + ${node.offsetHeight + SCREEN_MARGIN}px))`;
          })}
          className="fixed z-50 -translate-x-1/2 -translate-y-full"
        >
          {cancel}
        </div>
      )}
      <div
        ref={placedBy(handle, (style, { strip }) => {
          style.top = `${strip.bottom + 16}px`;
        })}
        role="dialog"
        aria-label={label}
        className="fixed inset-x-0 top-40 z-50 flex flex-col items-center px-5"
      >
        {card}
      </div>
    </>
  );
}
