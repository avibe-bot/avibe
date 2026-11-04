import { createContext, useContext, type CSSProperties } from 'react';

import type { Bot } from '@/lib/bots';
import type { HoldPointer } from '../../voice/ComposerHold';
import type { HoldPhase } from '../../voice/useHoldToTalk';

// The hold every Bot avatar shares (`BotHoldProvider`): which avatar is held, how far, and the
// handlers an avatar binds. This module is the component-free handle avatars import.

/** The avatar a hold is on, and how far it is. */
export type HeldBot = { handle: string; phase: HoldPhase; progress: number; pointer: HoldPointer };

/** The press handlers an avatar spreads on its button. `onPointerDown` takes the avatar's tap
 *  (opening its conversation); `onClick` and `onContextMenu` mark the events the hold answered
 *  (`defaultPrevented`), and the avatar acts on the rest. */
export type BotAvatarHold = {
  onPointerDown: (event: React.PointerEvent<HTMLElement>, onTap: () => void) => void;
  onPointerMove: (event: React.PointerEvent<HTMLElement>) => void;
  onPointerUp: (event: React.PointerEvent<HTMLElement>) => void;
  onPointerCancel: (event: React.PointerEvent<HTMLElement>) => void;
  onLostPointerCapture: (event: React.PointerEvent<HTMLElement>) => void;
  onClick: (event: React.MouseEvent<HTMLElement>) => void;
  onContextMenu: (event: React.MouseEvent<HTMLElement>) => void;
  style?: CSSProperties;
};

export type BotHoldValue = {
  held: HeldBot | null;
  /** The handlers for `bot`'s avatar. Absent where nothing holds (no provider, not the Owner). */
  bind?: (bot: Bot) => BotAvatarHold;
};

export const BotHoldContext = createContext<BotHoldValue>({ held: null });

/** The hold of the Bots' avatars, for an avatar to draw and to bind. */
export const useBotHold = (): BotHoldValue => useContext(BotHoldContext);

/** Whether a press is live: the avatar is held, recording, or about to cancel. */
export const holdIsLive = (phase: HoldPhase | undefined) =>
  phase === 'pressing' || phase === 'listening' || phase === 'recording' || phase === 'armed';
