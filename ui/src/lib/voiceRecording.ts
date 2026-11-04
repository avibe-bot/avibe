import { VoiceTranscriptionError } from './voiceTranscription';

const PCM_SAMPLE_RATE = 16_000;
const PCM_WORKLET_NAME = 'avibe-voice-pcm-capture';
const PCM_DELIVERY_MS = 250;
// The microphone's loudness reaches the page this often, apart from the PCM: a meter draws it
// as it is spoken, not a quarter second late.
export const VOICE_LEVEL_INTERVAL_MS = 32;
const PCM_BYTES_PER_SAMPLE = 2;
const WAV_HEADER_BYTES = 44;
const DEFAULT_SEGMENT_OVERLAP_MS = 500;
export const VOICE_MIN_SEGMENT_MS = 5_000;
export const VOICE_CAPTURE_STOP_TIMEOUT_MS = 5_000;
/** How long a take waits for a suspended audio context to start rendering before it rebuilds. */
export const VOICE_AUDIO_START_TIMEOUT_MS = 2_000;
/**
 * A rendering capture delivers its first chunk of frames a quarter second after it starts
 * (on an iPhone, sound in its first 50-110 ms). A take with a live, unmuted microphone that has delivered
 * none after this window is a graph that does not render, though its context may report
 * `running`: it is rebuilt, then given a fresh microphone, and failed visibly if still silent.
 */
export const VOICE_RENDER_WINDOW_MS = 1_000;

export const voiceWavMaxSamples = (maxFileBytes?: number | null): number | null => {
  if (maxFileBytes == null || !Number.isFinite(maxFileBytes)) return null;
  return Math.max(
    0,
    Math.floor((Math.floor(maxFileBytes) - WAV_HEADER_BYTES) / PCM_BYTES_PER_SAMPLE),
  );
};

export const voicePcmDeliverySamples = (
  sampleRate: number,
  segmentSamples: number,
): number => Math.max(
  1,
  Math.min(segmentSamples, Math.round(sampleRate * PCM_DELIVERY_MS / 1000)),
);

const PCM_WORKLET_SOURCE = `
class AvibeVoicePcmCapture extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const processorOptions = options.processorOptions || {};
    this.targetSampleRate = processorOptions.targetSampleRate || 16000;
    this.chunkSamples = processorOptions.chunkSamples || this.targetSampleRate;
    // Loudness (RMS of the input) every levelMs; off unless asked for.
    this.levelSamples = processorOptions.levelMs
      ? Math.max(1, Math.round(sampleRate * processorOptions.levelMs / 1000))
      : 0;
    this.levelSum = 0;
    this.levelCount = 0;
    this.output = new Int16Array(this.chunkSamples);
    this.outputOffset = 0;
    this.phase = 0;
    this.sum = 0;
    this.sumCount = 0;
    this.previousSample = 0;
    this.hasPreviousSample = false;
    this.nextOutputPosition = 0;
    this.totalInputSamples = 0;
    this.totalOutputSamples = 0;
    this.stopped = false;
    this.port.onmessage = (event) => {
      if (event.data && event.data.type === 'stop') this.finish();
    };
  }

  emitSample(sample) {
    const normalized = Math.max(-1, Math.min(1, sample));
    this.output[this.outputOffset++] = normalized < 0
      ? Math.round(normalized * 32768)
      : Math.round(normalized * 32767);
    this.totalOutputSamples += 1;
    if (this.outputOffset === this.output.length) this.flush();
  }

  flush() {
    if (!this.outputOffset) return;
    const samples = this.outputOffset === this.output.length
      ? this.output
      : this.output.slice(0, this.outputOffset);
    this.port.postMessage({ type: 'samples', samples: samples.buffer }, [samples.buffer]);
    this.output = new Int16Array(this.chunkSamples);
    this.outputOffset = 0;
  }

  finish() {
    if (this.stopped) return;
    if (this.targetSampleRate <= sampleRate && this.sumCount) {
      this.emitSample(this.sum / this.sumCount);
      this.sum = 0;
      this.sumCount = 0;
    } else if (this.targetSampleRate > sampleRate && this.hasPreviousSample) {
      const expectedOutputSamples = Math.round(
        this.totalInputSamples * this.targetSampleRate / sampleRate,
      );
      while (this.totalOutputSamples < expectedOutputSamples) {
        this.emitSample(this.previousSample);
      }
    }
    this.flush();
    this.stopped = true;
    this.port.postMessage({ type: 'stopped' });
  }

  processDownsample(sample) {
    this.sum += sample;
    this.sumCount += 1;
    this.phase += this.targetSampleRate;
    if (this.phase >= sampleRate) {
      this.phase -= sampleRate;
      this.emitSample(this.sum / this.sumCount);
      this.sum = 0;
      this.sumCount = 0;
    }
  }

  processUpsample(sample) {
    const currentPosition = this.totalInputSamples;
    if (!this.hasPreviousSample) {
      this.previousSample = sample;
      this.hasPreviousSample = true;
      this.emitSample(sample);
      this.nextOutputPosition = sampleRate / this.targetSampleRate;
    } else {
      const previousPosition = currentPosition - 1;
      while (this.nextOutputPosition <= currentPosition) {
        const fraction = Math.max(
          0,
          Math.min(1, this.nextOutputPosition - previousPosition),
        );
        this.emitSample(
          this.previousSample + (sample - this.previousSample) * fraction,
        );
        this.nextOutputPosition += sampleRate / this.targetSampleRate;
      }
      this.previousSample = sample;
    }
    this.totalInputSamples += 1;
  }

  process(inputs) {
    if (this.stopped) return false;
    const channels = inputs[0];
    if (!channels || !channels.length) return true;
    const frameCount = channels[0].length;
    for (let frame = 0; frame < frameCount; frame += 1) {
      let sample = 0;
      for (let channel = 0; channel < channels.length; channel += 1) {
        sample += channels[channel][frame] || 0;
      }
      const monoSample = sample / channels.length;
      if (this.targetSampleRate <= sampleRate) this.processDownsample(monoSample);
      else this.processUpsample(monoSample);
      if (this.levelSamples) {
        this.levelSum += monoSample * monoSample;
        this.levelCount += 1;
        if (this.levelCount >= this.levelSamples) {
          this.port.postMessage({ type: 'level', rms: Math.sqrt(this.levelSum / this.levelCount) });
          this.levelSum = 0;
          this.levelCount = 0;
        }
      }
    }
    return true;
  }
}

registerProcessor('${PCM_WORKLET_NAME}', AvibeVoicePcmCapture);
`;

export const voicePcmWorkletSource = PCM_WORKLET_SOURCE;

type AudioContextConstructor = new () => AudioContext;

let prewarmedAudioContext: AudioContext | null = null;
let prewarmedAudioContextConstructor: AudioContextConstructor | null = null;
let prewarmPromise: Promise<void> | null = null;
// The page has been hidden since the prewarmed context was made. On iOS the app's audio stops
// in the background, and a context that lived through it can come back reporting `running`
// while it renders nothing: every take on it would be silent until the app is relaunched.
let prewarmedAcrossHidden = false;
let watchingVisibility = false;

const watchVisibility = (): void => {
  if (watchingVisibility || typeof document === 'undefined') return;
  watchingVisibility = true;
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') prewarmedAcrossHidden = true;
  });
  // A page restored from the back/forward cache was hidden too.
  window.addEventListener('pageshow', (event) => {
    if (event.persisted) prewarmedAcrossHidden = true;
  });
};

const audioContextConstructor = (): AudioContextConstructor | null => {
  const constructor = globalThis.AudioContext;
  return typeof constructor === 'function' ? constructor : null;
};

/**
 * Whether a prewarmed context can still carry a take. WebKit moves a context to its own
 * `interrupted` state when the system takes the audio session (the microphone permission
 * alert, a call, Siri, the app leaving); such a context renders nothing, and its `resume()`
 * waits for an interruption that may never end. Reusing it would leave every later take
 * silent for the life of the page, so it is replaced instead.
 */
const canReuseAudioContext = (context: AudioContext): boolean => (
  context.state === 'running' || context.state === 'suspended'
);

/** Asks the context to render and reports whether it does within `timeoutMs`. */
const startAudioContext = async (context: AudioContext, timeoutMs: number): Promise<boolean> => {
  if (context.state === 'running') return true;
  if (!canReuseAudioContext(context)) return false;
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    await Promise.race([
      context.resume().catch(() => undefined),
      new Promise<void>((resolve) => {
        timer = setTimeout(resolve, timeoutMs);
      }),
    ]);
  } finally {
    if (timer !== undefined) clearTimeout(timer);
  }
  return (context.state as AudioContextState) === 'running';
};

const clearPrewarmedAudioContext = (context: AudioContext): void => {
  if (prewarmedAudioContext !== context) return;
  prewarmedAudioContext = null;
  prewarmedAudioContextConstructor = null;
  prewarmPromise = null;
};

const closePrewarmedAudioContext = (context: AudioContext): void => {
  clearPrewarmedAudioContext(context);
  if (context.state !== 'closed') void context.close().catch(() => undefined);
};

const loadVoicePcmWorklet = async (context: AudioContext): Promise<void> => {
  const moduleUrl = URL.createObjectURL(
    new Blob([PCM_WORKLET_SOURCE], { type: 'text/javascript' }),
  );
  try {
    await context.audioWorklet.addModule(moduleUrl);
  } finally {
    URL.revokeObjectURL(moduleUrl);
  }
};

const requestAudioContextResume = (context: AudioContext): void => {
  if (context.state === 'running') return;
  try {
    // Invoke resume synchronously from the microphone pointer/shortcut handler.
    // The promise may settle later, but the browser's transient activation is
    // attached to this call itself.
    void context.resume().catch(() => undefined);
  } catch {
    // start() retries resume after the worklet and stream are connected.
  }
};

/**
 * Compile the capture worklet before the user presses the microphone button.
 * This does not request microphone permission or connect a MediaStream; it only
 * keeps the audio graph ready so the first spoken words do not race worklet
 * compilation and AudioContext startup.
 */
export const prewarmVoiceCapture = (): Promise<void> => {
  const constructor = audioContextConstructor();
  if (!constructor) return Promise.resolve();

  if (
    prewarmedAudioContext
    && (
      prewarmedAudioContextConstructor !== constructor
      || !canReuseAudioContext(prewarmedAudioContext)
      || prewarmedAcrossHidden
    )
  ) {
    // Rebuilt here, inside the press, where a new context may start.
    closePrewarmedAudioContext(prewarmedAudioContext);
  }
  if (prewarmPromise) {
    if (prewarmedAudioContext) requestAudioContextResume(prewarmedAudioContext);
    return prewarmPromise;
  }

  let context: AudioContext;
  try {
    context = new constructor();
  } catch (error) {
    return Promise.reject(error);
  }
  prewarmedAudioContext = context;
  prewarmedAudioContextConstructor = constructor;
  prewarmedAcrossHidden = false;
  watchVisibility();
  requestAudioContextResume(context);
  prewarmPromise = (async () => {
    try {
      await loadVoicePcmWorklet(context);
    } catch (error) {
      closePrewarmedAudioContext(context);
      throw error;
    }
  })();
  return prewarmPromise;
};

const acquireAudioContext = async (): Promise<{
  context: AudioContext;
  prewarmed: boolean;
}> => {
  const constructor = audioContextConstructor();
  if (!constructor) throw new Error('AudioContext unavailable');

  if (
    prewarmedAudioContext
    && prewarmedAudioContextConstructor !== constructor
  ) {
    closePrewarmedAudioContext(prewarmedAudioContext);
  }
  if (prewarmPromise) await prewarmPromise.catch(() => undefined);
  if (
    prewarmedAudioContext
    && (!canReuseAudioContext(prewarmedAudioContext) || prewarmedAcrossHidden)
  ) {
    closePrewarmedAudioContext(prewarmedAudioContext);
  }
  if (
    prewarmedAudioContext
    && prewarmedAudioContextConstructor === constructor
  ) {
    return { context: prewarmedAudioContext, prewarmed: true };
  }
  return { context: new constructor(), prewarmed: false };
};

type VoicePcmCaptureHandlers = {
  onSamples: (samples: Int16Array<ArrayBuffer>) => void;
  /** The input's loudness (RMS, 0–1) every `VOICE_LEVEL_INTERVAL_MS`. */
  onLevel?: (rms: number) => void;
  onStopped: () => void;
  onError: (error: unknown) => void;
};

type VoicePcmCapture = {
  start: () => Promise<void>;
  stop: () => void;
};

type VoicePcmCaptureFactory = (
  stream: MediaStream,
  sampleRate: number,
  segmentSamples: number,
  handlers: VoicePcmCaptureHandlers,
  stopTimeoutMs?: number,
) => VoicePcmCapture;

class AudioWorkletPcmCapture implements VoicePcmCapture {
  private stream: MediaStream;
  /** A microphone this capture opened itself (a silent one replaced): it stops it. */
  private ownedStream: MediaStream | null = null;
  private readonly sampleRate: number;
  private readonly segmentSamples: number;
  private readonly handlers: VoicePcmCaptureHandlers;
  private readonly stopTimeoutMs: number;
  private context: AudioContext | null = null;
  private sharedContext = false;
  private source: MediaStreamAudioSourceNode | null = null;
  private node: AudioWorkletNode | null = null;
  private stopTimer: ReturnType<typeof setTimeout> | null = null;
  private stopping = false;
  private stopped = false;
  private failureReported = false;
  private rendered = false;
  private renderTimer: ReturnType<typeof setTimeout> | null = null;
  private recoveries = 0;
  private recovering = false;

  constructor(
    stream: MediaStream,
    sampleRate: number,
    segmentSamples: number,
    handlers: VoicePcmCaptureHandlers,
    stopTimeoutMs = VOICE_CAPTURE_STOP_TIMEOUT_MS,
  ) {
    this.stream = stream;
    this.sampleRate = sampleRate;
    this.segmentSamples = segmentSamples;
    this.handlers = handlers;
    this.stopTimeoutMs = Math.max(0, stopTimeoutMs);
  }

  async start(): Promise<void> {
    try {
      const acquired = await acquireAudioContext();
      this.context = acquired.context;
      this.sharedContext = acquired.prewarmed;
      this.stream.getTracks().forEach((track) => {
        track.addEventListener('ended', this.handleTrackEnded, { once: true });
      });
      if (this.stream.getTracks().some((track) => track.readyState === 'ended')) {
        this.stop();
      }
      if (await this.connect(acquired.context, acquired.prewarmed)) {
        this.watchRendering();
        return;
      }
      // The context does not render. A prewarmed one may have been taken by the system since
      // it was made (on iOS, a trip to the background can do this), and it would
      // keep this take and every later one silent. Rebuild once on a context made now: the
      // microphone is open, so the browser lets it start without a fresh gesture.
      const constructor = audioContextConstructor();
      if (!constructor) throw new Error('AudioContext unavailable');
      await this.closeContext();
      const context = new constructor();
      this.context = context;
      this.sharedContext = false;
      if (await this.connect(context, false)) {
        this.watchRendering();
        return;
      }
      throw new Error('voice audio context did not start');
    } catch (error) {
      this.detachNodes();
      await this.closeContext();
      throw error;
    }
  }

  /** Builds the graph on `context`; true once it renders, or once the capture stopped meanwhile. */
  private async connect(context: AudioContext, prewarmed: boolean): Promise<boolean> {
    let moduleUrl: string | null = null;
    try {
      if (!prewarmed) {
        moduleUrl = URL.createObjectURL(
          new Blob([PCM_WORKLET_SOURCE], { type: 'text/javascript' }),
        );
        await context.audioWorklet.addModule(moduleUrl);
      }
      if (this.stopping) {
        this.finish();
        return true;
      }

      const node = new AudioWorkletNode(context, PCM_WORKLET_NAME, {
        numberOfInputs: 1,
        numberOfOutputs: 1,
        outputChannelCount: [1],
        processorOptions: {
          targetSampleRate: this.sampleRate,
          // File-sized segments are assembled on the main thread. Deliver
          // smaller chunks so a processor failure cannot erase a whole segment.
          chunkSamples: voicePcmDeliverySamples(this.sampleRate, this.segmentSamples),
          ...(this.handlers.onLevel ? { levelMs: VOICE_LEVEL_INTERVAL_MS } : {}),
        },
      });
      node.port.onmessage = (event: MessageEvent<{
        type?: string;
        samples?: ArrayBuffer;
        rms?: number;
      }>) => {
        if (event.data.type === 'samples' && event.data.samples) {
          this.rendered = true;
          this.handlers.onSamples(new Int16Array(event.data.samples));
        } else if (event.data.type === 'level' && typeof event.data.rms === 'number') {
          this.handlers.onLevel?.(event.data.rms);
        } else if (event.data.type === 'stopped') {
          this.finish();
        }
      };
      node.onprocessorerror = () => {
        this.reportFailure(new Error('voice PCM processor stopped unexpectedly'));
        this.finish();
      };
      this.node = node;
      this.source = context.createMediaStreamSource(this.stream);
      this.source.connect(node);
      node.connect(context.destination);
      if (await startAudioContext(context, VOICE_AUDIO_START_TIMEOUT_MS)) return true;
      this.disconnectNodes();
      if (this.stopping) {
        // Stopped while waiting: a context that never rendered has no PCM to flush.
        await this.closeContext();
        this.finish();
        return true;
      }
      return false;
    } finally {
      if (moduleUrl) URL.revokeObjectURL(moduleUrl);
    }
  }

  private async closeContext(): Promise<void> {
    const context = this.context;
    const sharedContext = this.sharedContext;
    this.context = null;
    this.sharedContext = false;
    if (!context) return;
    if (sharedContext) closePrewarmedAudioContext(context);
    else if (context.state !== 'closed') await context.close().catch(() => undefined);
  }

  stop(): void {
    if (this.stopping || this.stopped) return;
    this.stopping = true;
    this.clearRenderWatch();
    this.ownedStream?.getTracks().forEach((track) => track.stop());
    this.stopTimer = setTimeout(() => {
      this.reportFailure(new Error('voice PCM processor did not stop in time'));
      this.finish();
    }, this.stopTimeoutMs);
    if (!this.node) return;
    this.node.port.postMessage({ type: 'stop' });
  }

  /** From the moment its context reports `running`: no frame within the window, and the graph does not render. */
  private watchRendering(): void {
    if (this.stopping || this.stopped) return;
    this.clearRenderWatch();
    this.rendered = false;
    this.renderTimer = setTimeout(this.handleNoFrames, VOICE_RENDER_WINDOW_MS);
  }

  private clearRenderWatch(): void {
    if (this.renderTimer !== null) clearTimeout(this.renderTimer);
    this.renderTimer = null;
  }

  private readonly handleNoFrames = (): void => {
    this.renderTimer = null;
    if (this.rendered || this.stopping || this.stopped || this.recovering) return;
    // A muted or disabled track is the system (or the page) holding the sound back, which a
    // new graph does not change: only a live microphone with a graph delivering nothing is.
    const live = this.stream.getAudioTracks().some((track) => (
      track.readyState === 'live' && !track.muted && track.enabled
    ));
    if (!live) return;
    if (this.recoveries >= 2) {
      // Rebuilt and given a fresh microphone, and still nothing: say so instead of recording it.
      this.reportFailure(new VoiceTranscriptionError('empty'));
      this.stop();
      return;
    }
    void this.recover();
  };

  /** Within the same take: first a fresh context on the same microphone, then a fresh microphone. */
  private async recover(): Promise<void> {
    const reacquire = this.recoveries > 0;
    this.recoveries += 1;
    this.recovering = true;
    try {
      this.disconnectNodes();
      await this.closeContext();
      if (reacquire) this.replaceStream(await navigator.mediaDevices.getUserMedia({ audio: true }));
      if (this.stopping) {
        this.finish();
        return;
      }
      const constructor = audioContextConstructor();
      if (!constructor) throw new Error('AudioContext unavailable');
      const context = new constructor();
      this.context = context;
      this.sharedContext = false;
      if (!(await this.connect(context, false))) throw new Error('voice audio context did not start');
      if (this.stopped) return;
      // A release during the rebuild found no graph to tell; this one flushes and finishes.
      if (this.stopping) this.node?.port.postMessage({ type: 'stop' });
      else this.watchRendering();
    } catch (error) {
      this.reportFailure(error instanceof Error ? error : new Error(String(error)));
      this.finish();
    } finally {
      this.recovering = false;
    }
  }

  private replaceStream(stream: MediaStream): void {
    this.stream.getTracks().forEach((track) => {
      track.removeEventListener('ended', this.handleTrackEnded);
      track.stop();
    });
    this.stream = stream;
    this.ownedStream = stream;
    stream.getTracks().forEach((track) => {
      track.addEventListener('ended', this.handleTrackEnded, { once: true });
    });
    if (this.stopping) stream.getTracks().forEach((track) => track.stop());
  }

  private readonly handleTrackEnded = (): void => {
    if (!this.stopping && !this.stopped) {
      this.reportFailure(new Error('voice microphone track ended unexpectedly'));
    }
    this.stop();
  };

  private reportFailure(error: Error): void {
    if (this.failureReported) return;
    this.failureReported = true;
    this.handlers.onError(error);
  }

  private finish(): void {
    if (this.stopped) return;
    this.stopped = true;
    this.clearRenderWatch();
    this.ownedStream?.getTracks().forEach((track) => track.stop());
    if (this.stopTimer !== null) clearTimeout(this.stopTimer);
    this.stopTimer = null;
    this.detachNodes();
    const context = this.context;
    const sharedContext = this.sharedContext;
    this.context = null;
    this.sharedContext = false;
    if (context && !sharedContext && context.state !== 'closed') {
      void context.close().catch(() => undefined);
    }
    this.handlers.onStopped();
  }

  private detachNodes(): void {
    this.stream.getTracks().forEach((track) => {
      track.removeEventListener('ended', this.handleTrackEnded);
    });
    this.disconnectNodes();
  }

  private disconnectNodes(): void {
    this.source?.disconnect();
    this.node?.disconnect();
    this.node?.port.close();
    this.source = null;
    this.node = null;
  }
}

const defaultCaptureFactory: VoicePcmCaptureFactory = (
  stream,
  sampleRate,
  segmentSamples,
  handlers,
  stopTimeoutMs,
) => new AudioWorkletPcmCapture(
  stream,
  sampleRate,
  segmentSamples,
  handlers,
  stopTimeoutMs,
);

export const deleteMapValueIfCurrent = <Key, Value>(
  map: Map<Key, Value>,
  key: Key,
  value: Value,
): boolean => {
  if (map.get(key) !== value) return false;
  return map.delete(key);
};

export type VoiceCaptureClaim = {
  isCurrent(): boolean;
  release(): void;
  /**
   * Resolves once the cross-window capture lock is held. Existing callers can
   * start immediately in a browser without Web Locks; desktop callers await
   * this before opening the microphone.
   */
  ready: Promise<void>;
};

type ActiveVoiceCapture = {
  token: symbol;
  finish: () => void;
};

let activeVoiceCapture: ActiveVoiceCapture | null = null;
const VOICE_CAPTURE_LOCK = 'avibe.voice-capture';

/**
 * Give the newest explicit voice action sole ownership of the browser mic.
 * Replacing an owner asks it to finish first, preserving captured speech while
 * preventing hidden Chat and Show Page recorders from running concurrently.
 */
export const claimVoiceCapture = (finish: () => void): VoiceCaptureClaim => {
  const token = Symbol('voice-capture');
  let finished = false;
  const finishOnce = () => {
    if (finished) return;
    finished = true;
    try {
      finish();
    } catch {
      // Ownership has already moved; a stale recorder cannot block the new one.
    }
  };
  const previous = activeVoiceCapture;
  activeVoiceCapture = { token, finish: finishOnce };
  previous?.finish();
  let released = false;
  let revoked = false;
  let releaseLock: (() => void) | null = null;
  let resolveReady!: () => void;
  let rejectReady!: (error: unknown) => void;
  const ready = new Promise<void>((resolve, reject) => {
    resolveReady = resolve;
    rejectReady = reject;
  });
  // A claim can be replaced before its caller reaches the microphone step.
  // The caller still observes the rejection when it awaits ready.
  void ready.catch(() => undefined);
  const locks = typeof navigator !== 'undefined' ? navigator.locks : undefined;
  if (!locks) {
    resolveReady();
  } else {
    const lockHold = new Promise<void>((resolve) => {
      releaseLock = resolve;
    });
    void locks.request(
      VOICE_CAPTURE_LOCK,
      { steal: true },
      async () => {
        if (released || activeVoiceCapture?.token !== token) {
          rejectReady(new DOMException('Voice capture replaced', 'AbortError'));
          return;
        }
        resolveReady();
        await lockHold;
      },
    ).catch((error: unknown) => {
      // A newer claim steals this lock. The old surface finishes through its
      // existing replacement path, preserving the captured speech.
      if (!released && error instanceof DOMException && error.name === 'AbortError') {
        revoked = true;
        finishOnce();
      }
      rejectReady(error);
    });
  }
  return {
    isCurrent: () => !released && !revoked && activeVoiceCapture?.token === token,
    release: () => {
      if (released) return;
      released = true;
      if (activeVoiceCapture?.token === token) activeVoiceCapture = null;
      releaseLock?.();
    },
    ready,
  };
};

/** Whether some voice action holds the microphone now (a tap dictation, a hold, a Show Page). A
 *  press-and-hold does not start over one: replacing it would end a take the user is still giving. */
export const isVoiceCaptureActive = (): boolean => activeVoiceCapture !== null;

export const isVoiceControlDisabled = (
  disabled: boolean,
  recording: boolean,
  transcribing: boolean,
  retained = false,
): boolean => transcribing || (!recording && (disabled || retained));

export type VoiceRecordingStopReason = 'finish' | 'abort' | 'error';

export type VoiceRecordingSegmentMetadata = {
  durationMs: number;
  overlapMs?: number;
  final: boolean;
};

export type VoiceRecordingStopMetadata = {
  requestedAt: number;
};

export type VoiceRecordingStoppedMetadata = {
  pendingSegmentCount: number;
};

export type VoiceRecordingPipelineOptions = {
  stream: MediaStream;
  segmentMs: number;
  onSegment: (blob: Blob, metadata: VoiceRecordingSegmentMetadata) => void;
  onPcm?: (samples: Int16Array<ArrayBuffer>) => void;
  /** The microphone's loudness (RMS, 0–1) every `VOICE_LEVEL_INTERVAL_MS` while it records;
   *  a meter's, never stored. */
  onLevel?: (rms: number) => void;
  onStopRequested?: (
    reason: VoiceRecordingStopReason,
    metadata: VoiceRecordingStopMetadata,
  ) => void;
  onStopped: (
    reason: VoiceRecordingStopReason,
    metadata: VoiceRecordingStoppedMetadata,
  ) => void;
  onError?: (error: unknown) => void;
  sampleRate?: number;
  maxFileBytes?: number | null;
  overlapMs?: number;
  stopTimeoutMs?: number;
  createCapture?: VoicePcmCaptureFactory;
};

const wavHeader = (
  sampleRate: number,
  sampleCount: number,
): Uint8Array<ArrayBuffer> => {
  const header = new Uint8Array(44);
  const view = new DataView(header.buffer);
  const writeAscii = (offset: number, value: string) => {
    for (let index = 0; index < value.length; index += 1) {
      view.setUint8(offset + index, value.charCodeAt(index));
    }
  };
  const dataBytes = sampleCount * 2;
  writeAscii(0, 'RIFF');
  view.setUint32(4, 36 + dataBytes, true);
  writeAscii(8, 'WAVE');
  writeAscii(12, 'fmt ');
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  writeAscii(36, 'data');
  view.setUint32(40, dataBytes, true);
  return header;
};

const wavBlob = (
  sampleRate: number,
  sampleCount: number,
  chunks: Int16Array<ArrayBuffer>[],
): Blob => new Blob(
  [wavHeader(sampleRate, sampleCount), ...chunks],
  { type: 'audio/wav' },
);

const tailPcmChunks = (
  chunks: Int16Array<ArrayBuffer>[],
  tailSamples: number,
): Int16Array<ArrayBuffer>[] => {
  const tail: Int16Array<ArrayBuffer>[] = [];
  let remaining = tailSamples;
  for (let index = chunks.length - 1; index >= 0 && remaining > 0; index -= 1) {
    const chunk = chunks[index];
    if (!chunk) continue;
    const take = Math.min(remaining, chunk.length);
    tail.unshift(chunk.subarray(chunk.length - take));
    remaining -= take;
  }
  return tail;
};

/**
 * Captures mono PCM on the audio rendering thread, then frames it by sample
 * count. Even if the main thread is blocked, queued PCM chunks are converted
 * into provider-safe files instead of one oversized MediaRecorder blob.
 */
export class VoiceRecordingPipeline {
  private readonly options: VoiceRecordingPipelineOptions;
  private readonly sampleRate: number;
  private readonly segmentSamples: number;
  private readonly overlapSamples: number;
  private readonly capture: VoicePcmCapture;
  private segmentChunks: Int16Array<ArrayBuffer>[] = [];
  private segmentSampleCount = 0;
  private segmentFreshSampleCount = 0;
  private segmentLeadingOverlapSamples = 0;
  private visibilityDocument: Document | null = null;
  private started = false;
  private stopping: VoiceRecordingStopReason | null = null;
  private stopped = false;
  private streamStopped = false;
  private emittedSegmentCount = 0;
  private segmentCountAtStop: number | null = null;

  constructor(options: VoiceRecordingPipelineOptions) {
    this.options = options;
    this.sampleRate = options.sampleRate ?? PCM_SAMPLE_RATE;
    const durationSamples = Math.max(
      1,
      Math.round(this.sampleRate * options.segmentMs / 1000),
    );
    const maxFileSamples = voiceWavMaxSamples(options.maxFileBytes);
    const minimumFileSamples = Math.min(
      durationSamples,
      Math.max(1, Math.round(this.sampleRate * VOICE_MIN_SEGMENT_MS / 1000)),
    );
    if (maxFileSamples != null && maxFileSamples < minimumFileSamples) {
      throw new Error('configured ASR file limit is too small for a practical WAV segment');
    }
    this.segmentSamples = maxFileSamples == null
      ? durationSamples
      : Math.min(durationSamples, maxFileSamples);
    this.overlapSamples = Math.min(
      Math.max(
        0,
        Math.round(this.sampleRate * (options.overlapMs ?? DEFAULT_SEGMENT_OVERLAP_MS) / 1000),
      ),
      Math.floor(this.segmentSamples / 2),
    );
    const createCapture = options.createCapture ?? defaultCaptureFactory;
    this.capture = createCapture(options.stream, this.sampleRate, this.segmentSamples, {
      onSamples: (samples) => this.handleSamples(samples),
      ...(options.onLevel
        ? {
            onLevel: (rms: number) => {
              // A stopping capture is flushing what was heard: the microphone is closed.
              if (!this.stopped && !this.stopping) options.onLevel?.(rms);
            },
          }
        : {}),
      onStopped: () => this.handleCaptureStopped(),
      onError: (error) => this.handleCaptureError(error),
    }, options.stopTimeoutMs);
  }

  async start(): Promise<boolean> {
    if (this.started || this.stopped) {
      throw new Error('voice recording pipeline already started');
    }
    this.started = true;
    // Bind before asynchronous capture setup so a tab switch during addModule
    // or resume cannot be missed. A hidden document immediately enters finish.
    this.bindVisibilityStop();
    try {
      await this.capture.start();
    } catch (error) {
      if (this.stopped) return false;
      if (this.stopping) {
        this.complete(this.pendingSegmentCount());
        return false;
      }
      this.unbindVisibilityStop();
      this.stopStream();
      this.stopped = true;
      throw error;
    }
    return !this.stopped && !this.stopping;
  }

  finish(): void {
    this.stop('finish');
  }

  abort(): void {
    this.stop('abort');
  }

  private stop(reason: VoiceRecordingStopReason): void {
    if (!this.requestStop(reason)) return;
    // Mark the capture as stopping before releasing tracks so an implementation
    // that synchronously emits "ended" does not turn an intentional stop into
    // a device-loss failure. Buffered PCM still flushes before completion.
    this.capture.stop();
    this.stopStream();
  }

  private requestStop(reason: VoiceRecordingStopReason): boolean {
    if (this.stopping || this.stopped) return false;
    this.stopping = reason;
    this.segmentCountAtStop = this.emittedSegmentCount;
    this.options.onStopRequested?.(reason, { requestedAt: Date.now() });
    return true;
  }

  private handleSamples(samples: Int16Array<ArrayBuffer>): void {
    if (this.stopped || this.stopping === 'abort') return;
    this.options.onPcm?.(samples);
    let offset = 0;
    while (offset < samples.length) {
      const available = this.segmentSamples - this.segmentSampleCount;
      const take = Math.min(available, samples.length - offset);
      this.segmentChunks.push(samples.subarray(offset, offset + take));
      this.segmentSampleCount += take;
      this.segmentFreshSampleCount += take;
      offset += take;
      if (this.segmentSampleCount === this.segmentSamples) {
        this.emitSegment(true);
      }
    }
  }

  private emitSegment(retainOverlap = false, final = false): void {
    if (!this.segmentSampleCount) return;
    const sampleCount = this.segmentSampleCount;
    const chunks = this.segmentChunks;
    const leadingOverlapSamples = this.segmentLeadingOverlapSamples;
    const retainedSamples = retainOverlap ? this.overlapSamples : 0;
    this.segmentChunks = tailPcmChunks(chunks, retainedSamples);
    this.segmentSampleCount = retainedSamples;
    this.segmentFreshSampleCount = 0;
    this.segmentLeadingOverlapSamples = retainedSamples;
    this.emittedSegmentCount += 1;
    const overlapMs = Math.round(leadingOverlapSamples * 1000 / this.sampleRate);
    this.options.onSegment(
      wavBlob(this.sampleRate, sampleCount, chunks),
      {
        durationMs: Math.round(sampleCount * 1000 / this.sampleRate),
        ...(overlapMs > 0 ? { overlapMs } : {}),
        final,
      },
    );
  }

  private handleCaptureError(error: unknown): void {
    if (this.stopped || this.stopping === 'abort') return;
    if (!this.stopping) this.requestStop('error');
    else this.stopping = 'error';
    this.options.onError?.(error);
  }

  private handleCaptureStopped(): void {
    if (this.stopped) return;
    this.requestStop('finish');
    if (this.stopping !== 'abort' && this.segmentFreshSampleCount > 0) {
      this.emitSegment(false, true);
    }
    else {
      this.segmentChunks = [];
      this.segmentSampleCount = 0;
      this.segmentFreshSampleCount = 0;
      this.segmentLeadingOverlapSamples = 0;
    }
    this.complete(this.pendingSegmentCount());
  }

  private pendingSegmentCount(): number {
    if (this.stopping === 'abort') return 0;
    return this.emittedSegmentCount - (this.segmentCountAtStop ?? this.emittedSegmentCount);
  }

  private complete(pendingSegmentCount: number): void {
    if (this.stopped) return;
    this.stopped = true;
    this.unbindVisibilityStop();
    this.stopStream();
    this.options.onStopped(
      this.stopping ?? 'finish',
      { pendingSegmentCount },
    );
  }

  // The microphone is never held for a page the user cannot see: hidden (another app, a locked
  // screen, a background tab) or leaving (pagehide, which also covers the back/forward cache)
  // finishes the recording, and the tracks stop at once; what was heard still flushes.
  private bindVisibilityStop(): void {
    if (typeof document === 'undefined') return;
    this.visibilityDocument = document;
    document.addEventListener('visibilitychange', this.handleVisibilityChange);
    if (typeof window !== 'undefined') window.addEventListener('pagehide', this.handlePageHide);
    if (document.visibilityState === 'hidden') this.finish();
  }

  private unbindVisibilityStop(): void {
    this.visibilityDocument?.removeEventListener('visibilitychange', this.handleVisibilityChange);
    this.visibilityDocument = null;
    if (typeof window !== 'undefined') window.removeEventListener('pagehide', this.handlePageHide);
  }

  private readonly handleVisibilityChange = (): void => {
    if (this.visibilityDocument?.visibilityState === 'hidden') this.finish();
  };

  private readonly handlePageHide = (): void => {
    this.finish();
  };

  private stopStream(): void {
    if (this.streamStopped) return;
    this.streamStopped = true;
    this.options.stream.getTracks().forEach((track) => track.stop());
  }
}
