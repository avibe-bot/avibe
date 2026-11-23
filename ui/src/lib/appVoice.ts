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
//
// And, from TestFlight 52.1, when a hold's microphone may start (`audio_asr.app_hold_start_ms`,
// the earliest in ms after touch-down; unset, the App's own default) and whether the App may
// prepare it before (`audio_asr.app_hold_prepare_at_touch`), for every surface.

let enabled = true;
let mainAtRecognition = false;
/** `audio_asr.app_hold_start_ms`: the earliest a hold's microphone may start, in ms after
 *  touch-down; undefined leaves the App's own default. */
let startAfterMs: number | undefined;
/** `audio_asr.app_hold_prepare_at_touch`: whether the App may prepare before the start. */
let preparesAtTouch = true;

const holdStartMs = (value: unknown): number | undefined => (
  typeof value === 'number' && Number.isInteger(value) && value >= 0 && value <= 500 ? value : undefined
);
const listeners = new Set<() => void>();

/** Takes both from a `/api/asr/status` body. One without a field is from an instance that has
 *  no such setting: the App's path stays on, and the main Bot's button stays at touch-down. */
export const noteAppVoice = (status: unknown): void => {
  if (typeof status !== 'object' || status === null) return;
  const said = status as {
    app_hold?: unknown;
    app_hold_main_at_touch?: unknown;
    app_hold_start_ms?: unknown;
    app_hold_prepare_at_touch?: unknown;
  };
  const nextEnabled = 'app_hold' in said ? said.app_hold !== false : enabled;
  const nextMain = 'app_hold_main_at_touch' in said ? said.app_hold_main_at_touch === false : mainAtRecognition;
  const nextStart = 'app_hold_start_ms' in said ? holdStartMs(said.app_hold_start_ms) : startAfterMs;
  const nextPrepare = 'app_hold_prepare_at_touch' in said ? said.app_hold_prepare_at_touch !== false : preparesAtTouch;
  if (
    nextEnabled === enabled && nextMain === mainAtRecognition
    && nextStart === startAfterMs && nextPrepare === preparesAtTouch
  ) return;
  enabled = nextEnabled;
  mainAtRecognition = nextMain;
  startAfterMs = nextStart;
  preparesAtTouch = nextPrepare;
  for (const listener of [...listeners]) listener();
};

/** The earliest a hold's microphone may start in the App, as the instance set it. */
export const appHoldStartMs = (): number | undefined => startAfterMs;

/** Whether the App may prepare its microphone at touch-down, before the start. */
export const appHoldPreparesAtTouch = (): boolean => preparesAtTouch;

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
