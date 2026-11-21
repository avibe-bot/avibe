// Whether, in the iOS App, the App recognises a hold itself and records every voice take
// (lib/nativeVoiceHold, lib/nativeVoiceCapture). The instance can turn that off
// (`audio_asr.app_hold`, said by `/api/asr/status`): its pages then recognise a hold
// themselves and record tap and Show Page takes through the page, as they did before. An off
// switch that needs no new build: it takes effect at the next status read (a conversation
// opened, the App reopened).

let enabled = true;
const listeners = new Set<() => void>();

/** Takes the switch from a `/api/asr/status` body. One without the field is from an instance
 *  that has no such switch: the App's path stays on. */
export const noteAppVoice = (status: unknown): void => {
  if (typeof status !== 'object' || status === null || !('app_hold' in status)) return;
  const next = (status as { app_hold?: unknown }).app_hold !== false;
  if (next === enabled) return;
  enabled = next;
  for (const listener of [...listeners]) listener();
};

export const appVoiceEnabled = (): boolean => enabled;

/** Called when the switch changes; the returned function stops it. */
export const subscribeAppVoice = (listener: () => void): (() => void) => {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
};
