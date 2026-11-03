import { useCallback, useEffect, useRef, useState, type CSSProperties, type MouseEvent as ReactMouseEvent, type PointerEvent as ReactPointerEvent } from 'react';

import { apiFetch } from '../../lib/apiFetch';
import { primeCloudToken } from '../../lib/avibeFetch';
import { IS_APPLE } from '../../lib/platform';
import { ShowPageVoiceDictation } from '../../lib/showPageVoiceDictation';
import { VOICE_MESSAGE_METADATA, type VoiceMessageMetadata } from '../../lib/voiceMessage';
import { isVoiceCaptureActive, prewarmVoiceCapture } from '../../lib/voiceRecording';
import { VoiceTranscriptionError } from '../../lib/voiceTranscription';

// Press and hold to talk (ui/design: mobile/main-bot-voice/*, composer/hold-*): one gesture
// and one take, from the press to the sent message. A press that reaches `holdMs` opens the
// microphone; the shipped dictation pipeline (the one Chat and the desktop pet use) streams
// the transcript; releasing sends it, sliding into the cancel zone and releasing cancels.
// There is no time limit. A take cut off on the way keeps what was heard until the user
// sends it, retries, or discards it. The surface (the tab bar's main Bot, a composer) draws
// the phases; this hook owns the gesture, the microphone, the transcript, and the send.

/** Where a hold is: pressing (not yet `holdMs`), listening (the microphone is open, no words
 *  yet), recording, armed (released now, it cancels), settling (released, the final text and the
 *  send are on their way), or interrupted (the take ended without a send; its text is kept). */
export type HoldPhase = 'idle' | 'pressing' | 'listening' | 'recording' | 'armed' | 'settling' | 'interrupted';

/** Why a take is interrupted: the connection or the microphone dropped, the transcription
 *  failed, the system took the press (background, focus, a stolen pointer), or the send failed. */
export type HoldInterruptReason = 'dropped' | 'failed' | 'system' | 'send-failed';

/** How the last press ended, for the surface's toast or note. */
export type HoldOutcome = 'sent' | 'cancelled' | 'too-short' | 'asking' | 'denied' | 'unavailable';

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

export const HOLD_GESTURE: HoldGesture = { holdMs: 300, ringDelayMs: 0, slopPx: 10, cancel: 'slide-up', cancelPx: 80, uncancelPx: 60 };
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
};

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
};

const vibrate = () => {
  try {
    navigator.vibrate?.(10);
  } catch {
    // Haptics are a nicety; a browser that refuses them changes nothing.
  }
};

const permissionState = async (): Promise<PermissionState | null> => {
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
    armed.current = false;
    if (!mounted.current) return;
    setText('');
    setStash('');
    setSeconds(0);
    setProgress(0);
    setInterruptReason(null);
    setCanRetry(false);
  }, [stopClock]);

  /** The press is over with nothing to keep: back to rest, saying how it ended. */
  const end = useCallback((how: HoldOutcome | null) => {
    take.current = null;
    releasePointer();
    reset();
    toPhase('idle');
    if (how && mounted.current) setOutcome(how);
  }, [releasePointer, reset, toPhase]);

  /** The take ended without a send: keep what was heard until the user decides. */
  const interrupt = useCallback((reason: HoldInterruptReason, heard: string) => {
    stopClock();
    releasePointer();
    armed.current = false;
    if (!mounted.current) return;
    setInterruptReason(reason);
    setText(heard);
    setStash('');
    setCanRetry(Boolean(take.current?.dictation.canRetry()));
    toPhase('interrupted');
  }, [releasePointer, stopClock, toPhase]);

  const deliver = useCallback(async (words: string) => {
    toPhase('settling');
    const ok = await latest.current.send(words, VOICE_MESSAGE_METADATA).catch(() => false);
    if (!mounted.current) return;
    if (ok) {
      take.current = null;
      reset();
      setText(words);
      toPhase('idle');
      setOutcome('sent');
      return;
    }
    interrupt('send-failed', words);
    // A failed send has nothing to transcribe again: it is sent again.
    setCanRetry(false);
  }, [interrupt, reset, toPhase]);

  const settle = useCallback((current: Take, words: string) => {
    if (take.current !== current || current.ending === 'cancel') return;
    if (current.ending === 'system') {
      interrupt('system', words.trim() || `${current.heard.text}${current.heard.stash}`.trim());
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
    if (error instanceof VoiceTranscriptionError && error.code === 'empty') {
      end('too-short');
      return;
    }
    const failed = error instanceof VoiceTranscriptionError && (error.code === 'failed' || error.code === 'too_large');
    const heard = `${current.heard.text}${current.heard.stash}`.trim();
    interrupt(current.ending === 'system' ? 'system' : failed ? 'failed' : 'dropped', heard);
  }, [end, interrupt]);

  const startTake = useCallback((granted: boolean) => {
    const dictation = new ShowPageVoiceDictation({
      before: '',
      after: '',
      maxFileBytes: maxFileBytes.current,
      onPreviewParts: (preview) => {
        if (take.current !== current) return;
        current.heard = preview;
        if (!mounted.current) return;
        setText(preview.text);
        setStash(preview.stash);
        if (phaseRef.current === 'listening' && `${preview.text}${preview.stash}`.trim()) toPhase('recording');
      },
    });
    const current: Take = { dictation, startedAt: Date.now(), started: false, ending: 'none', granted, heard: { text: '', stash: '' } };
    take.current = current;
    // The last take's words (kept for its toast) are not this one's.
    setText('');
    setStash('');
    toPhase('listening');
    vibrate();
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
  }, [fail, settle, toPhase]);

  const holdReached = useCallback(async () => {
    const current = press.current;
    if (!current) return;
    cancelAnimationFrame(current.frame);
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
    if (permission === 'prompt') {
      // The system's question takes this press; the next one records.
      end('asking');
      void navigator.mediaDevices?.getUserMedia({ audio: true }).then(
        (stream) => stream.getTracks().forEach((track) => track.stop()),
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
      current.ending = 'cancel';
      current.dictation.abort();
      end(!current.granted ? 'asking' : how === 'send' ? 'too-short' : 'cancelled');
      return;
    }
    if (how === 'cancel') {
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
    toPhase('settling');
    current.dictation.finish();
  }, [end, releasePointer, stopClock, toPhase]);

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
  const live = phase === 'pressing' || phase === 'listening' || phase === 'recording' || phase === 'armed';
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

  // Leaving the page ends the take; nothing is sent from a surface that is gone.
  useEffect(() => () => {
    const current = take.current;
    take.current = null;
    if (current) {
      current.ending = 'cancel';
      current.dictation.abort();
    }
    if (clock.current) clearInterval(clock.current);
    if (press.current) {
      clearTimeout(press.current.timer);
      cancelAnimationFrame(press.current.frame);
    }
  }, []);

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
    try {
      element.setPointerCapture?.(event.pointerId);
    } catch {
      // Without capture the hold still works while the pointer stays on the element.
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
    };
    setOutcome(null);
    setProgress(0);
    toPhase('pressing');
  }, [holdReached, toPhase]);

  const onPointerMove = useCallback((event: ReactPointerEvent<HTMLElement>) => {
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
    if (next) vibrate();
    const heard = `${take.current.heard.text}${take.current.heard.stash}`.trim();
    toPhase(next ? 'armed' : heard ? 'recording' : 'listening');
  }, [end, toPhase]);

  const onPointerUp = useCallback((event: ReactPointerEvent<HTMLElement>) => {
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

  const onPointerCancel = useCallback((event: ReactPointerEvent<HTMLElement>) => {
    if (press.current && event.pointerId === press.current.pointerId) systemTook();
  }, [systemTook]);

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
    toPhase('settling');
    void current.dictation.retry({ before: '', after: '' }).then(
      (words) => {
        if (take.current !== current) return;
        if (!words.trim()) end('too-short');
        else void deliver(words.trim());
      },
      () => {
        if (take.current === current) interrupt('failed', text);
      },
    );
  }, [deliver, end, interrupt, text, toPhase]);

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
