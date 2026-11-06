// A Bot's avatar in one moment of a hold (ui/design: sidebar/bots/hold-*, mobile/workbench-tab/hold-*).
// The strip, the card, and the toast are the shipped ones (`BotStrip`, `BotHoldLayer`,
// `MainBotVoiceToast`): the hold every avatar shares is given in the state `hold` describes, as
// `BotHoldProvider` gives it from a live `useHoldToTalk`.
import type { ReactNode } from 'react';

import type { HoldPointer } from '@/components/voice/ComposerHold';
import { MainBotVoiceToast, type HoldView } from '@/components/voice/MainBotVoice';
import { BotHoldLayer } from '@/components/workbench/bots/BotHold';
import { BotHoldContext } from '@/components/workbench/bots/BotHoldContext';
import { useBots } from '@/context/BotsContext';
import { meterFor } from './meter';

const noop = () => {};

export function HeldBot({
  handle,
  hold,
  pointer,
  children,
}: {
  handle: string;
  /** The hold as `useHoldToTalk` reports it; `progress` is a mouse press's ring. */
  hold: Partial<HoldView> & { progress?: number };
  pointer: HoldPointer;
  children: ReactNode;
}) {
  const { botForHandle, displayName } = useBots();
  const bot = botForHandle(handle);
  const view: HoldView = {
    phase: 'idle',
    interruptReason: null,
    text: '',
    stash: '',
    seconds: 0,
    outcome: null,
    canRetry: true,
    sendInterrupted: noop,
    retry: noop,
    discard: noop,
    dismiss: noop,
    cancelSettling: noop,
    ...hold,
    level: hold.level ?? meterFor(hold.phase ?? 'idle'),
  };
  const held = view.phase === 'idle' ? null : { handle, phase: view.phase, progress: hold.progress ?? 0, pointer };
  const face = bot ? { name: displayName(bot), avatar: bot.avatar } : null;
  return (
    <BotHoldContext.Provider value={{ held }}>
      {children}
      {face && (
        <>
          <BotHoldLayer hold={view} bot={face} handle={handle} pointer={pointer} />
          <MainBotVoiceToast hold={view} bot={face} placement="shell" onView={noop} />
        </>
      )}
    </BotHoldContext.Provider>
  );
}
