// Whether, in the iOS App, the App recognises a hold itself and records every voice take
// (lib/nativeVoiceHold, lib/nativeVoiceCapture). The instance can turn that off
// (`audio_asr.app_hold`, said by `/api/asr/status`): its pages then recognise a hold
// themselves and record tap and Show Page takes through the page, as they did before. An off
// switch that needs no new build: it takes effect at the next status read (a conversation
// opened, the App reopened).
//
// And where the main Bot's button in the tab bar opens the microphone
// (`audio_asr.app_hold_main_at_touch`, said the same way): at touch-down, as the composer does,
// unless the instance says no. Then the App starts recording there when it recognises the hold,
// and a tap on that button opens no microphone.

let enabled = true;
let mainAtRecognition = false;
const listeners = new Set<() => void>();

/** Takes both from a `/api/asr/status` body. One without a field is from an instance that has
 *  no such setting: the App's path stays on, and the main Bot's button stays at touch-down. */
export const noteAppVoice = (status: unknown): void => {
  if (typeof status !== 'object' || status === null) return;
  const said = status as { app_hold?: unknown; app_hold_main_at_touch?: unknown };
  const nextEnabled = 'app_hold' in said ? said.app_hold !== false : enabled;
  const nextMain = 'app_hold_main_at_touch' in said ? said.app_hold_main_at_touch === false : mainAtRecognition;
  if (nextEnabled === enabled && nextMain === mainAtRecognition) return;
  enabled = nextEnabled;
  mainAtRecognition = nextMain;
  for (const listener of [...listeners]) listener();
};

export const appVoiceEnabled = (): boolean => enabled;

/** Whether the App is to start recording on the main Bot's button only when it recognises the
 *  hold there, not at touch-down. */
export const mainBotStartsAtRecognition = (): boolean => mainAtRecognition;

/** Called when either changes; the returned function stops it. */
export const subscribeAppVoice = (listener: () => void): (() => void) => {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
};
