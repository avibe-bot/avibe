// Whether this page's real-time audio renders, and at what sample rate.
//
// WebKit can stop rendering real-time audio for a whole web process at one configuration: a
// context reports `running` and its clock stands still, an oscillator's included, while the
// microphone itself still captures. The owner's iPhone (Home Screen app, 2026-11-07): new
// 48 kHz contexts, the default there, moved 0 in 800 ms, in a new document too, round after round.
// On the same microphone stream, contexts made at 16 kHz and 44.1 kHz rendered and their worklets
// got the microphone's sound (180 and 540 frames in 1.6 s, peaks at -6 dB); a MediaRecorder got
// it too, and decoding that recording worked. A take whose context stalls goes on in a context at
// another rate, live words and all, and is recorded without Web Audio only where none renders
// (lib/voiceRecording).
//
// What was found is the page's: the rate that renders is used from the next take on, and a page
// where none renders records its takes without Web Audio from their first moment, until a context
// is found rendering again. Only a `running` context is judged: a `suspended` one is the autoplay
// policy waiting for a gesture, not a stall.

/** A running context whose clock has not moved in this long renders nothing. A rendering iPhone's
 *  clock moves within one render quantum, and its first microphone frames come in 50–110 ms. */
export const VOICE_ENGINE_STALL_MS = 400;
/** The rates tried when a context stalls, after a fresh one at the device's default (a stale
 *  context, not a stalled engine): 16 kHz first, the capture's own rate, so its worklet resamples
 *  nothing; then 44.1 kHz. */
export const VOICE_ENGINE_FALLBACK_RATES: readonly number[] = [16_000, 44_100];

/**
 * Whether contexts at other rates are tried. WebKit connects a microphone to a context at any
 * rate and resamples it; Firefox refuses a MediaStream source at a rate other than the
 * microphone's, and Chromium has not been seen to stall. iOS browsers are all WebKit.
 */
export const voiceEngineCanChangeRate = (): boolean => {
  if (typeof navigator === 'undefined') return false;
  const ua = navigator.userAgent ?? '';
  if (/iPhone|iPad|iPod/.test(ua)) return true;
  return /AppleWebKit\//.test(ua) && !/Chrome\/|Chromium\/|Edg\/|Firefox\//.test(ua);
};
/** How often a clock being judged is read. */
const POLL_MS = 50;

export type VoiceEngineVerdict = 'healthy' | 'stalled' | 'unknown';

let stalled = false;
let preferredRate: number | undefined;
const checks = new WeakMap<AudioContext, Promise<VoiceEngineVerdict>>();

/** No context this page tried renders: takes are recorded without Web Audio. */
export const voiceEngineStalled = (): boolean => stalled;

/** The sample rate that renders where the default stalled, or undefined for the default. */
export const preferredVoiceSampleRate = (): number | undefined => preferredRate;

/** What a search found: `rate` rendered (undefined for the default), or nothing did. */
export const recordVoiceEngine = (found: { rate: number | undefined } | null): void => {
  stalled = found === null;
  if (found) preferredRate = found.rate;
};

/**
 * Watches `context`'s clock from now: `healthy` as soon as it moves, `stalled` if it has not moved
 * within `VOICE_ENGINE_STALL_MS` of running, `unknown` if it does not run within that time (the
 * autoplay policy) or closes. One check per context at a time.
 */
export function checkVoiceEngine(context: AudioContext): Promise<VoiceEngineVerdict> {
  const pending = checks.get(context);
  if (pending) return pending;
  const check = new Promise<VoiceEngineVerdict>((resolve) => {
    const startedAt = Date.now();
    let runningAt: number | null = null;
    let clockAt = 0;
    const read = () => {
      const state = context.state as string;
      if (state === 'closed') {
        resolve('unknown');
        return;
      }
      if (state === 'running') {
        if (runningAt === null) {
          runningAt = Date.now();
          clockAt = context.currentTime;
        } else if (context.currentTime > clockAt) {
          resolve('healthy');
          return;
        } else if (Date.now() - runningAt >= VOICE_ENGINE_STALL_MS) {
          resolve('stalled');
          return;
        }
      } else if (Date.now() - startedAt >= VOICE_ENGINE_STALL_MS) {
        // Not running yet: the autoplay policy, or an interruption. Nothing to judge.
        resolve('unknown');
        return;
      }
      setTimeout(read, POLL_MS);
    };
    read();
  });
  checks.set(context, check);
  void check.finally(() => {
    if (checks.get(context) === check) checks.delete(context);
  });
  return check;
}
