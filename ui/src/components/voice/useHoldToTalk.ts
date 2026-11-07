import { useCallback, useEffect, useRef, useState, type CSSProperties, type MouseEvent as ReactMouseEvent, type PointerEvent as ReactPointerEvent } from 'react';

import { apiFetch } from '../../lib/apiFetch';
import { primeCloudToken } from '../../lib/avibeFetch';
import {
  nativeHapticImpact,
  nativeHapticNotify,
  nativeHapticSelection,
  nativeVoicePermission,
} from '../../lib/native';
import { IS_APPLE } from '../../lib/platform';
import { ShowPageVoiceDictation } from '../../lib/showPageVoiceDictation';
import type { VoiceContextSource } from '../../lib/voiceContext';
import type { VoiceTakeKeep } from '../../lib/voiceTakeKeeper';
import { createVoiceLevel } from '../../lib/voiceLevel';
import { VOICE_MESSAGE_METADATA, type VoiceMessageMetadata } from '../../lib/voiceMessage';
import { isVoiceCaptureActive, prewarmVoiceCapture, type VoiceCaptureState } from '../../lib/voiceRecording';
import { emitVoiceTelemetry, type VoiceTelemetryEvent, type VoiceTelemetryOutcome } from '../../lib/voiceTelemetry';
import { VoiceTranscriptionError } from '../../lib/voiceTranscription';

// Press and hold to talk (ui/design: mobile/main-bot-voice/*, composer/hold-*): one gesture
// and one take, from the press to the sent message. A press that reaches `holdMs` opens the
// microphone; the shipped dictation pipeline (the one Chat and the desktop pet use) streams
// the transcript; releasing sends it, sliding into the cancel zone and releasing cancels.
// There is no time limit. A take cut off on the way keeps what was heard until the user
// sends it, retries, or discards it. While its words are being finalized, the card's × (or Esc)
// stops waiting for them (ui/design: composer/hold-*/f-settling). The surface (the tab bar's main
// Bot, a composer) draws the phases; this hook owns the gesture, the microphone, the transcript,
// and the send.

/** Where a hold is: pressing (not yet `holdMs`), listening (the microphone is open, no words
 *  yet), recording, armed (released now, it cancels), settling (released, the final text and the
 *  send are on their way), or interrupted (the take ended without a send; its text is kept). */
export type HoldPhase = 'idle' | 'pressing' | 'listening' | 'recording' | 'armed' | 'settling' | 'interrupted';

/** Why a take is interrupted: the connection or the microphone dropped, the transcription
 *  failed, the system took the press (background, focus, a stolen pointer), or the send failed. */
export type HoldInterruptReason = 'dropped' | 'failed' | 'system' | 'send-failed';

/** How the last press ended, for the surface's toast or note. `kept`: cancelled while its words
 *  were finalized, and its recording kept on the device for the pending voice items. `too-short`:
 *  nothing said (a slip, or silence). `no-sound`: the microphone delivered no sound at all.
 *  `mic-busy`: the system, or another app, held the microphone. */
export type HoldOutcome = 'sent' | 'cancelled' | 'kept' | 'too-short' | 'no-sound' | 'mic-busy' | 'asking' | 'denied' | 'unavailable';

export type HoldGesture = {
  /** How long a press is held before the microphone opens. */
  holdMs: number;
  /** When a desktop's ring starts filling (`progress` stays 0 before it). */
  ringDelayMs: number;
  /** Moving this far before `holdMs` gives the press back (a scroll, a click). */
  slopPx: number;
  /** Touch: slide up to cancel. Mouse: move out of `bounds`. Esc cancels in both. */
  cancel: 'slide-up' | 'leave';
  /** Slide-up: how far up arms cancel. Leave: how far outside the bounds. */
  cancelPx: number;
  /** Below this (slide-up) or within this of the bounds (leave), an armed take disarms. */
  uncancelPx: number;
};

/** A finger (owner decision, 2026-11-04): the microphone opens at 200 ms; a surface that draws the
 *  press's progress shows it from 100 ms. */
export const HOLD_GESTURE: HoldGesture = { holdMs: 200, ringDelayMs: 100, slopPx: 10, cancel: 'slide-up', cancelPx: 80, uncancelPx: 60 };
/** A mouse holds longer than a finger, so a slow click or a trackpad press never opens the
 *  microphone: 350 ms, with the ring from 100 ms (owner decision, 2026-11-04); leaving the held
 *  box (or Esc) cancels (ui/design: composer/hold-desktop/b-pressing). Every surface a mouse can
 *  hold uses these numbers: a composer and a Bot's avatar alike. */
export const MOUSE_HOLD_GESTURE: Partial<HoldGesture> = {
  holdMs: 350,
  ringDelayMs: 100,
  slopPx: 6,
  cancel: 'leave',
  cancelPx: 16,
  uncancelPx: 0,
};
/** A take shorter than this is a slip, not a message. */
export const HOLD_MIN_TAKE_MS = 500;

export type HoldToTalkOptions = {
  /** Sends the final text; resolves true once the message is accepted. */
  send: (text: string, metadata: VoiceMessageMetadata) => Promise<boolean>;
  /** Whether this press may become a hold; false leaves it to the system. `pointerType` is the
   *  press's (`mouse`, `touch`, `pen`): one surface can get both on a touchscreen laptop. */
  canHold?: (pointerType: string) => boolean;
  /** A press released before `holdMs` without moving: called inside pointerup, so it may focus. */
  onTap?: () => void;
  /** The gesture's numbers, or the numbers for a press's `pointerType` (a mouse and a finger
   *  on one surface hold differently). Read when the press starts. */
  gesture?: Partial<HoldGesture> | ((pointerType: string) => Partial<HoldGesture>);
  /** The box `leave` measures against, when it is not the pressed element. */
  bounds?: () => DOMRect | null;
  /** The system taking the press (background, window blur, a lost or cancelled pointer):
   *  cancel the take, or keep what was heard as an interrupted take. */
  onSystemInterrupt?: 'cancel' | 'interrupt';
  /** The conversation the take is spoken into (its Agent's last reply, the user's previous
   *  message), read when the microphone opens and sent to recognition; it may still be loading. */
  context?: () => VoiceContextSource;
  /** Where a take's words go, read when the microphone opens: the take is kept on the device
   *  (lib/voiceTakeStore) until they land, so a reload or a surface that goes away loses none
   *  of it. The intent is always `send`. */
  keep?: () => Omit<VoiceTakeKeep, 'intent'> | null;
};

const HOLD_STYLE: CSSProperties = {
  touchAction: 'none',
  WebkitTouchCallout: 'none',
  WebkitUserSelect: 'none',
  userSelect: 'none',
};

type Press = {
  pointerId: number;
  /** The numbers this press holds by, resolved for its pointer when it started. */
  gesture: HoldGesture;
  x: number;
  y: number;
  element: HTMLElement;
  startedAt: number;
  timer: ReturnType<typeof setTimeout>;
  frame: number;
  /** Stops the window following a press the element has not captured (a mouse before `holdMs`). */
  unfollow: (() => void) | null;
};

/** What the gesture reads from a pointer event, React's or the window's. */
type PointerAt = { pointerId: number; clientX: number; clientY: number };

type Take = {
  dictation: ShowPageVoiceDictation;
  startedAt: number;
  /** The microphone is open and the pipeline running. */
  started: boolean;
  /** The release (or the system) has decided how this take ends. */
  ending: 'none' | 'send' | 'cancel' | 'system';
  /** The permission answer before opening: a release before the pipeline starts is a prompt then. */
  granted: boolean;
  heard: { text: string; stash: string };
  /** How many endings this take has logged (a take retried after an interruption ends again). */
  reports: number;
};

/** A hold's outcome as the reliability log names it. */
const TELEMETRY_OUTCOME: Record<HoldOutcome, VoiceTelemetryOutcome> = {
  sent: 'sent',
  cancelled: 'cancelled',
  kept: 'kept',
  'too-short': 'too_short',
  'no-sound': 'no_frames',
  'mic-busy': 'muted',
  asking: 'asking',
  denied: 'denied',
  unavailable: 'unavailable',
};

/** How a hold ended, for the reliability log (`dictation_finalized`, entry `hold`), with what its
 *  microphone delivered; a press that never became a take logs its outcome alone. */
const reportHold = (
  outcome: VoiceTelemetryOutcome,
  current: Take | null,
  extra: Pick<VoiceTelemetryEvent, 'interruptReason'> = {},
): void => {
  try {
    const event: VoiceTelemetryEvent = { event: 'dictation_finalized', outcome, entry: 'hold', providerStage: 'finalization', ...extra };
    if (current) {
      current.reports += 1;
      event.dictationId = current.dictation.id;
      event.captureClass = current.dictation.captureClass();
      event.attemptCount = current.reports;
      event.retry = current.reports > 1;
      if (current.started) event.totalDurationMs = Math.max(0, Date.now() - current.startedAt);
    }
    emitVoiceTelemetry(event);
  } catch {
    // Instrumentation cannot change how the hold ended.
  }
};

/** Why a take that could not be transcribed stopped, for the card: the network or the service
 *  failed (`dropped`), recognition did (`failed`), or the system took the press (`system`). */
const failureReason = (take: Take, error: unknown): HoldInterruptReason => {
  if (take.ending === 'system') return 'system';
  const failed = error instanceof VoiceTranscriptionError && (error.code === 'failed' || error.code === 'too_large' || error.code === 'empty');
  return failed ? 'failed' : 'dropped';
};

const vibrate = () => {
  try {
    navigator.vibrate?.(10);
  } catch {
    // Haptics are a nicety; a browser that refuses them changes nothing.
  }
};

const impactFeedback = (style: 'light' | 'medium') => {
  void nativeHapticImpact(style).then((handled) => {
    if (!handled) vibrate();
  });
};

const cancelFeedback = () => {
  void nativeHapticNotify('warning').then((handled) => {
    if (!handled) vibrate();
  });
};

const selectionFeedback = () => {
  void nativeHapticSelection().then((handled) => {
    if (!handled) vibrate();
  });
};

// The user allowed the microphone when a hold asked, in this page. iOS Safari can go on
// answering `prompt` to the permission query after that, which would turn every later press
// into another ask that never records; the stream it handed over is the better answer.
let allowedOnAsk = false;

const permissionState = async (): Promise<PermissionState | null> => {
  const native = await nativeVoicePermission();
  if (native) return native;
  try {
    const status = await navigator.permissions?.query({ name: 'microphone' as PermissionName });
    return status?.state ?? null;
  } catch {
    return null;
  }
};

/** Distance from a point to a box: 0 inside. */
const outside = (box: DOMRect, x: number, y: number) =>
  Math.hypot(Math.max(box.left - x, 0, x - box.right), Math.max(box.top - y, 0, y - box.bottom));

export function useHoldToTalk(options: HoldToTalkOptions) {
  const latest = useRef(options);
  useEffect(() => {
    latest.current = options;
  });

  const [phase, setPhase] = useState<HoldPhase>('idle');
  const [interruptReason, setInterruptReason] = useState<HoldInterruptReason | null>(null);
  const [text, setText] = useState('');
  const [stash, setStash] = useState('');
  const [seconds, setSeconds] = useState(0);
  const [progress, setProgress] = useState(0);
  const [outcome, setOutcome] = useState<HoldOutcome | null>(null);
  const [available, setAvailable] = useState<boolean | null>(null);
  const [canRetry, setCanRetry] = useState(false);
  // Settling, the take's words are still being transcribed (not yet on their way as a message):
  // the card offers to stop waiting for them.
  const [finalizing, setFinalizingState] = useState(false);
  // Live recognition stopped working during this take (lib/showPageVoiceDictation onLiveChange):
  // the card says so, and that the take is still being kept.
  const [liveUnavailable, setLiveUnavailable] = useState(false);
  // How this take is recorded (lib/voiceRecording): through Web Audio, without it (no live words,
  // no level), or without it and getting no sound from the microphone.
  const [captureState, setCaptureState] = useState<VoiceCaptureState>('live');
  // The microphone's loudness for the card's meter: written as the take records, read each frame.
  const [level] = useState(createVoiceLevel);
  const maxFileBytes = useRef<number | null>(null);
  const availableRef = useRef<boolean | null>(null);

  const phaseRef = useRef<HoldPhase>('idle');
  const press = useRef<Press | null>(null);
  const take = useRef<Take | null>(null);
  const clock = useRef<ReturnType<typeof setInterval> | null>(null);
  const armed = useRef(false);
  // Set while this hook releases the pointer itself, so its lostpointercapture is not the system's.
  const releasing = useRef(false);
  // The last pointer press was the gesture's (a tap it answered, a hold, a press given back, or a
  // press while a take is pending).
  const handled = useRef(false);
  const mounted = useRef(true);

  const toPhase = useCallback((next: HoldPhase) => {
    phaseRef.current = next;
    if (mounted.current) setPhase(next);
  }, []);
  const finalizingRef = useRef(false);
  const setFinalizing = useCallback((next: boolean) => {
    finalizingRef.current = next;
    if (mounted.current) setFinalizingState(next);
  }, []);

  // Whether voice can work at all here: read once, as the composer does.
  useEffect(() => {
    mounted.current = true;
    let alive = true;
    apiFetch('/api/asr/status')
      .then((response) => (response.ok ? response.json() : null))
      .then((data) => {
        if (!alive) return;
        const ok = Boolean(data?.available);
        availableRef.current = ok;
        setAvailable(ok);
        maxFileBytes.current = typeof data?.max_file_bytes === 'number' && data.max_file_bytes > 0 ? Math.floor(data.max_file_bytes) : null;
        if (ok) primeCloudToken();
      })
      .catch(() => {
        if (!alive) return;
        availableRef.current = false;
        setAvailable(false);
      });
    return () => {
      alive = false;
      mounted.current = false;
    };
  }, []);

  const stopClock = useCallback(() => {
    if (clock.current) clearInterval(clock.current);
    clock.current = null;
  }, []);

  const releasePointer = useCallback(() => {
    const current = press.current;
    press.current = null;
    if (!current) return;
    clearTimeout(current.timer);
    cancelAnimationFrame(current.frame);
    current.unfollow?.();
    releasing.current = true;
    try {
      if (current.element.hasPointerCapture?.(current.pointerId)) current.element.releasePointerCapture(current.pointerId);
    } catch {
      // The pointer is already gone.
    } finally {
      releasing.current = false;
    }
  }, []);

  const reset = useCallback(() => {
    stopClock();
    level.reset();
    armed.current = false;
    setFinalizing(false);
    if (!mounted.current) return;
    setText('');
    setStash('');
    setSeconds(0);
    setProgress(0);
    setInterruptReason(null);
    setCanRetry(false);
    setLiveUnavailable(false);
    setCaptureState('live');
  }, [level, setFinalizing, stopClock]);

  /** The press is over with nothing to keep: back to rest, saying how it ended. */
  const end = useCallback((how: HoldOutcome | null) => {
    if (how) reportHold(TELEMETRY_OUTCOME[how], take.current);
    // Nothing of the take is kept: its stored copy goes too (a no-op once it was discarded).
    take.current?.dictation.abort();
    take.current = null;
    releasePointer();
    reset();
    toPhase('idle');
    if (how && mounted.current) setOutcome(how);
  }, [releasePointer, reset, toPhase]);

  /** The take ended without a send: keep what was heard until the user decides. */
  const interrupt = useCallback((reason: HoldInterruptReason, heard: string) => {
    reportHold('interrupted', take.current, { interruptReason: reason });
    stopClock();
    level.reset();
    releasePointer();
    armed.current = false;
    setFinalizing(false);
    if (!mounted.current) return;
    setInterruptReason(reason);
    setText(heard);
    setStash('');
    setCanRetry(Boolean(take.current?.dictation.canRetry()));
    toPhase('interrupted');
  }, [level, releasePointer, setFinalizing, stopClock, toPhase]);

  const deliver = useCallback(async (words: string) => {
    const current = take.current;
    // The words are on their way as a message: a cancel could no longer take it back.
    setFinalizing(false);
    toPhase('settling');
    const ok = await latest.current.send(words, VOICE_MESSAGE_METADATA).catch(() => false);
    // The words landed: the kept copy goes, whether or not this surface is still here.
    if (ok) {
      current?.dictation.landed();
      reportHold('sent', current);
    }
    if (!mounted.current) return;
    if (ok) {
      if (take.current === current) take.current = null;
      reset();
      setText(words);
      toPhase('idle');
      setOutcome('sent');
      return;
    }
    interrupt('send-failed', words);
    // A failed send has nothing to transcribe again: it is sent again.
    setCanRetry(false);
  }, [interrupt, reset, setFinalizing, toPhase]);

  const settle = useCallback((current: Take, words: string) => {
    if (take.current !== current || current.ending === 'cancel') return;
    // Only a release sends. A take that ended any other way — the system took the press, or
    // another voice surface took the microphone mid-hold and finished it — keeps its words.
    if (current.ending !== 'send') {
      current.ending = 'system';
      const heard = words.trim() || `${current.heard.text}${current.heard.stash}`.trim();
      if (heard) interrupt('system', heard);
      else end('cancelled');
      return;
    }
    if (!words.trim()) {
      end('too-short');
      return;
    }
    void deliver(words.trim());
  }, [deliver, end, interrupt]);

  const fail = useCallback((current: Take, error: unknown) => {
    if (take.current !== current || current.ending === 'cancel') return;
    if (error instanceof VoiceTranscriptionError && error.code === 'cancelled') return;
    if (!current.started) {
      // The microphone never opened while the press was still on: refused, or no microphone.
      end('denied');
      return;
    }
    // Nothing recognized in a take the microphone heard as silence: nothing to keep. One that
    // held sound loud enough for speech is a recognition that failed, and keeps its recording.
    if (error instanceof VoiceTranscriptionError && error.code === 'empty' && !current.dictation.voiced()) {
      // A microphone that delivered no sound at all, or one the system held, is not a silence.
      const delivered = current.dictation.captureClass();
      end(delivered === 'no_frames' ? 'no-sound' : delivered === 'muted' ? 'mic-busy' : 'too-short');
      return;
    }
    const heard = `${current.heard.text}${current.heard.stash}`.trim();
    interrupt(failureReason(current, error), heard);
  }, [end, interrupt]);

  const startTake = useCallback((granted: boolean) => {
    const keep = latest.current.keep?.();
    const dictation = new ShowPageVoiceDictation({
      entry: 'hold',
      before: '',
      after: '',
      context: latest.current.context?.(),
      maxFileBytes: maxFileBytes.current,
      ...(keep ? { keep: { ...keep, intent: 'send' as const } } : {}),
      onLiveChange: (live) => {
        if (take.current === current && mounted.current) setLiveUnavailable(!live);
      },
      onCaptureState: (state) => {
        if (take.current === current && mounted.current) setCaptureState(state);
      },
      onLevel: (rms) => {
        if (take.current === current) level.set(rms);
      },
      onPreviewParts: (preview) => {
        if (take.current !== current) return;
        current.heard = preview;
        if (!mounted.current) return;
        setText(preview.text);
        setStash(preview.stash);
        if (phaseRef.current === 'listening' && `${preview.text}${preview.stash}`.trim()) toPhase('recording');
      },
    });
    const current: Take = { dictation, startedAt: Date.now(), started: false, ending: 'none', granted, heard: { text: '', stash: '' }, reports: 0 };
    take.current = current;
    // The last take's words (kept for its toast) are not this one's.
    setText('');
    setStash('');
    toPhase('listening');
    impactFeedback('medium');
    setSeconds(0);
    clock.current = setInterval(() => {
      if (mounted.current) setSeconds(Math.floor((Date.now() - current.startedAt) / 1000));
    }, 250);
    void dictation.done.then((words) => settle(current, words), (error: unknown) => fail(current, error));
    void dictation.start().then(() => {
      if (take.current !== current) {
        dictation.abort();
        return;
      }
      current.started = true;
      // The take runs from here: setup time is not speech, so it cannot carry a slip past the minimum.
      current.startedAt = Date.now();
      // A release (or the system) may come before the microphone is ready.
      if (current.ending === 'send' || current.ending === 'system') dictation.finish();
    }, (error: unknown) => {
      fail(current, error);
      dictation.abort();
    });
  }, [fail, level, settle, toPhase]);

  const holdReached = useCallback(async () => {
    const current = press.current;
    if (!current) return;
    cancelAnimationFrame(current.frame);
    if (current.unfollow) {
      // A hold now: the pointer is the element's until the press ends, wherever it goes. If the
      // capture is refused, the window goes on following it.
      try {
        current.element.setPointerCapture?.(current.pointerId);
        current.unfollow();
        current.unfollow = null;
      } catch {
        // Followed by the window instead.
      }
    }
    setProgress(1);
    if (availableRef.current === false) {
      end('unavailable');
      return;
    }
    const permission = await permissionState();
    if (press.current !== current) return;
    if (permission === 'denied') {
      end('denied');
      return;
    }
    if (permission === 'prompt' && !allowedOnAsk) {
      // The system's question takes this press; the next one records. The answer can come
      // long after the press ended: the stream it brings is only proof, and is closed at once.
      end('asking');
      void navigator.mediaDevices?.getUserMedia({ audio: true }).then(
        (stream) => {
          allowedOnAsk = true;
          stream.getTracks().forEach((track) => track.stop());
        },
        () => undefined,
      );
      return;
    }
    startTake(permission === 'granted');
  }, [end, startTake]);

  /** Release (or Esc, or the system) decides how the take ends. */
  const finishTake = useCallback((how: 'send' | 'cancel' | 'system') => {
    const current = take.current;
    if (!current || current.ending !== 'none') return;
    current.ending = how;
    releasePointer();
    if (!current.started && how !== 'cancel') {
      // Over before the microphone opened: a permission prompt took the press, or it was a slip.
      // The system takes it for the prompt even when the query said granted: iOS asks again
      // once the microphone has been idle for about a minute.
      current.ending = 'cancel';
      current.dictation.abort();
      end(how === 'system' || !current.granted ? 'asking' : 'too-short');
      return;
    }
    if (how === 'cancel') {
      cancelFeedback();
      current.dictation.abort();
      end('cancelled');
      return;
    }
    if (how === 'send' && Date.now() - current.startedAt < HOLD_MIN_TAKE_MS) {
      current.ending = 'cancel';
      current.dictation.abort();
      end('too-short');
      return;
    }
    stopClock();
    armed.current = false;
    impactFeedback('light');
    setFinalizing(true);
    toPhase('settling');
    current.dictation.finish();
  }, [end, releasePointer, setFinalizing, stopClock, toPhase]);

  /** The card's × (or Esc) while the take's words are finalized: stop waiting for them. Every
   *  request and retry stops, nothing is sent, and the card closes. A take that held speech long
   *  enough stays on the device for the pending voice items (`kept`); a slip is discarded. */
  const cancelSettling = useCallback(() => {
    const current = take.current;
    if (!current || phaseRef.current !== 'settling' || !finalizingRef.current) return;
    current.ending = 'cancel';
    take.current = null;
    cancelFeedback();
    end(null);
    // Said once the take is judged (at once, or when its last audio is in), unless a new press
    // has begun meanwhile.
    void current.dictation.setAside().then((kept) => {
      reportHold(kept ? 'kept' : 'cancelled', current);
      if (mounted.current && !take.current && phaseRef.current === 'idle') setOutcome(kept ? 'kept' : 'cancelled');
    });
  }, [end]);

  const systemTook = useCallback(() => {
    if (press.current && !take.current) {
      end(null);
      return;
    }
    if (!take.current || take.current.ending !== 'none') return;
    // Before the microphone opens, the system taking the press is the permission prompt.
    if (latest.current.onSystemInterrupt === 'interrupt' || !take.current.started) finishTake('system');
    else finishTake('cancel');
  }, [end, finishTake]);

  // While a press is live: Esc cancels wherever focus is, and the page leaving is the system's.
  // While its words are finalized, Esc stops waiting for them, as the card's × does.
  const live = phase === 'pressing' || phase === 'listening' || phase === 'recording' || phase === 'armed';
  const stoppable = phase === 'settling' && finalizing;
  useEffect(() => {
    if (!stoppable) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      event.preventDefault();
      cancelSettling();
    };
    window.addEventListener('keydown', onKey, true);
    return () => window.removeEventListener('keydown', onKey, true);
  }, [cancelSettling, stoppable]);
  useEffect(() => {
    if (!live) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      event.preventDefault();
      if (take.current) finishTake('cancel');
      else end(null);
    };
    const onHidden = () => {
      if (document.visibilityState === 'hidden') systemTook();
    };
    window.addEventListener('keydown', onKey, true);
    window.addEventListener('blur', systemTook);
    document.addEventListener('visibilitychange', onHidden);
    return () => {
      window.removeEventListener('keydown', onKey, true);
      window.removeEventListener('blur', systemTook);
      document.removeEventListener('visibilitychange', onHidden);
    };
  }, [end, finishTake, live, systemTook]);

  // Leaving the page ends the take; nothing is sent from a surface that is gone. What was said
  // is not lost with it: a take the user meant to keep (recording, settling, or cut off) stays
  // on the device, for the pending voice items near its conversation's composer.
  useEffect(() => () => {
    const current = take.current;
    take.current = null;
    if (current) {
      if (current.started && current.ending !== 'cancel') current.dictation.detach();
      else current.dictation.abort();
      current.ending = 'cancel';
    }
    if (clock.current) clearInterval(clock.current);
    if (press.current) {
      clearTimeout(press.current.timer);
      cancelAnimationFrame(press.current.frame);
      press.current.unfollow?.();
    }
  }, []);

  const onPointerMove = useCallback((event: PointerAt) => {
    const current = press.current;
    if (!current || event.pointerId !== current.pointerId) return;
    const { slopPx, cancel, cancelPx, uncancelPx } = current.gesture;
    if (!take.current) {
      // Before the hold: moving gives the press back to the page.
      if (phaseRef.current === 'pressing' && Math.hypot(event.clientX - current.x, event.clientY - current.y) > slopPx) end(null);
      return;
    }
    if (take.current.ending !== 'none') return;
    const distance = cancel === 'slide-up'
      ? current.y - event.clientY
      : outside(latest.current.bounds?.() ?? current.element.getBoundingClientRect(), event.clientX, event.clientY);
    const next = armed.current ? distance > uncancelPx : distance > cancelPx;
    if (next === armed.current) return;
    armed.current = next;
    if (next) selectionFeedback();
    const heard = `${take.current.heard.text}${take.current.heard.stash}`.trim();
    toPhase(next ? 'armed' : heard ? 'recording' : 'listening');
  }, [end, toPhase]);

  const onPointerUp = useCallback((event: PointerAt) => {
    const current = press.current;
    if (!current || event.pointerId !== current.pointerId) return;
    if (!take.current) {
      if (phaseRef.current !== 'pressing') return;
      // Released before the hold: a tap, handled inside the gesture so it may focus.
      end(null);
      latest.current.onTap?.();
      return;
    }
    finishTake(armed.current ? 'cancel' : 'send');
  }, [end, finishTake]);

  const onPointerCancel = useCallback((event: Pick<PointerAt, 'pointerId'>) => {
    if (press.current && event.pointerId === press.current.pointerId) systemTook();
  }, [systemTook]);

  const onPointerDown = useCallback((event: ReactPointerEvent<HTMLElement>) => {
    if (press.current) return;
    handled.current = false;
    if (event.button !== 0 || (IS_APPLE && event.ctrlKey)) return;
    // A take still settling or waiting after a cut-off owns the element: its click must not
    // navigate away and drop it.
    if (phaseRef.current !== 'idle') {
      handled.current = true;
      return;
    }
    if (latest.current.canHold && !latest.current.canHold(event.pointerType)) return;
    // A tap dictation (or another hold) has the microphone: this press is the system's.
    if (isVoiceCaptureActive()) return;
    handled.current = true;
    const element = event.currentTarget;
    let unfollow: (() => void) | null = null;
    if (event.pointerType === 'mouse') {
      // A mouse press stays the browser's until it becomes a hold: its mouseup and click reach
      // what was pressed, so an editor places and shows its caret as on any click. The element
      // captures the pointer at `holdMs`; until then the window follows it off the element.
      window.addEventListener('pointermove', onPointerMove);
      window.addEventListener('pointerup', onPointerUp);
      window.addEventListener('pointercancel', onPointerCancel);
      unfollow = () => {
        window.removeEventListener('pointermove', onPointerMove);
        window.removeEventListener('pointerup', onPointerUp);
        window.removeEventListener('pointercancel', onPointerCancel);
      };
    } else {
      try {
        element.setPointerCapture?.(event.pointerId);
      } catch {
        // Without capture the hold still works while the pointer stays on the element.
      }
    }
    void prewarmVoiceCapture().catch(() => undefined);
    const startedAt = Date.now();
    const requested = latest.current.gesture;
    const gesture: HoldGesture = { ...HOLD_GESTURE, ...(typeof requested === 'function' ? requested(event.pointerType) : requested) };
    const { holdMs, ringDelayMs } = gesture;
    const tick = () => {
      const current = press.current;
      if (!current || current.startedAt !== startedAt) return;
      const elapsed = Date.now() - startedAt;
      setProgress(Math.max(0, Math.min(1, (elapsed - ringDelayMs) / Math.max(1, holdMs - ringDelayMs))));
      current.frame = requestAnimationFrame(tick);
    };
    press.current = {
      pointerId: event.pointerId,
      gesture,
      x: event.clientX,
      y: event.clientY,
      element,
      startedAt,
      timer: setTimeout(() => void holdReached(), holdMs),
      frame: requestAnimationFrame(tick),
      unfollow,
    };
    setOutcome(null);
    setProgress(0);
    toPhase('pressing');
  }, [holdReached, onPointerCancel, onPointerMove, onPointerUp, toPhase]);

  const onLostPointerCapture = useCallback((event: ReactPointerEvent<HTMLElement>) => {
    if (releasing.current) return;
    if (press.current && event.pointerId === press.current.pointerId) systemTook();
  }, [systemTook]);

  // The click after a press the gesture answered is spent; any other click (a press the hook left
  // to the system, the keyboard) is the element's own, and stays its default.
  const onClick = useCallback((event: ReactMouseEvent<HTMLElement>) => {
    if (!handled.current || event.detail === 0) return;
    handled.current = false;
    event.preventDefault();
  }, []);

  // Whether a press can be taken at all (no take is pending). It does not ask `canHold`, so
  // `bind.style` is the touch styles whenever this is true: a surface whose `canHold` refuses
  // at times (an editable field with text) sets those styles itself instead of spreading it.
  const holdable = phase !== 'interrupted';
  const onContextMenu = useCallback((event: ReactMouseEvent<HTMLElement>) => {
    // A live press is the hold, not the system's menu (Android fires one on a long press).
    // Otherwise the menu is the element's: a right-click on an idle composer still pastes.
    const at = phaseRef.current;
    if (at === 'pressing' || at === 'listening' || at === 'recording' || at === 'armed') event.preventDefault();
  }, []);

  const sendInterrupted = useCallback(() => {
    if (phaseRef.current !== 'interrupted') return;
    const words = text.trim();
    if (!words) {
      take.current?.dictation.abort();
      end(null);
      return;
    }
    void deliver(words);
  }, [deliver, end, text]);

  const retry = useCallback(() => {
    const current = take.current;
    if (phaseRef.current !== 'interrupted' || !current || !current.dictation.canRetry()) return;
    current.ending = 'send';
    setFinalizing(true);
    toPhase('settling');
    void current.dictation.retry({ before: '', after: '' }).then(
      (words) => {
        if (take.current !== current) return;
        if (!words.trim()) end('too-short');
        else void deliver(words.trim());
      },
      (error: unknown) => {
        if (take.current !== current) return;
        if (error instanceof VoiceTranscriptionError && error.code === 'cancelled') return;
        interrupt(failureReason(current, error), text);
      },
    );
  }, [deliver, end, interrupt, setFinalizing, text, toPhase]);

  const discard = useCallback(() => {
    take.current?.dictation.abort();
    end(null);
  }, [end]);

  const dismiss = useCallback(() => setOutcome(null), []);

  return {
    phase,
    interruptReason,
    text,
    stash,
    seconds,
    progress,
    outcome,
    available,
    holdable,
    canRetry,
    liveUnavailable,
    /** How the take is recorded: through Web Audio, without it (no live words, no level), or
     *  without it and getting no sound from the microphone (lib/voiceRecording). */
    captureState,
    level,
    /** Settling, while the take's words are still being transcribed: stops waiting for them (the
     *  card's ×). Null otherwise, and once the words are on their way as a message. */
    cancelSettling: stoppable ? cancelSettling : null,
    bind: {
      onPointerDown,
      onPointerMove,
      onPointerUp,
      onPointerCancel,
      onLostPointerCapture,
      onContextMenu,
      onClick,
      style: holdable ? HOLD_STYLE : undefined,
    },
    sendInterrupted,
    retry,
    discard,
    dismiss,
  };
}
