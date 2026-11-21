import { createVoiceLevel, type VoiceLevel } from './voiceLevel';
import { requestVoiceInputStream, type VoiceInputStreamOptions } from './voiceInput';

export type VoiceInputTest = {
  level: VoiceLevel;
  stream: MediaStream;
  stop: () => void;
};

export type VoiceInputTestOptions = VoiceInputStreamOptions & {
  onStreamEnded?: () => void;
};

type AudioContextConstructor = new () => AudioContext;

const audioContextConstructor = (): AudioContextConstructor | null => {
  const candidate = globalThis.AudioContext;
  return typeof candidate === 'function' ? candidate : null;
};

/**
 * Opens the selected input for a local-only level test. The stream is never
 * connected to an output, encoded, persisted, or sent to the network.
 */
export const startVoiceInputTest = async (
  options: VoiceInputTestOptions = {},
): Promise<VoiceInputTest> => {
  const stream = await requestVoiceInputStream(options);
  const AudioContext = audioContextConstructor();
  if (!AudioContext) {
    stream.getTracks().forEach((track) => track.stop());
    throw new Error('Audio level testing unavailable');
  }

  let context: AudioContext | null = null;
  let analyser: AnalyserNode | null = null;
  let source: MediaStreamAudioSourceNode | null = null;
  let frame = 0;
  let stopped = false;
  const onEnded = () => {
    if (!stopped) options.onStreamEnded?.();
  };
  const stop = () => {
    if (stopped) return;
    stopped = true;
    // Partial construction must release every acquired resource and preserve the
    // original initialization error even if a cleanup operation itself fails.
    const release = (action: () => void) => { try { action(); } catch { /* Continue cleanup. */ } };
    release(() => cancelAnimationFrame(frame));
    release(() => source?.disconnect());
    release(() => analyser?.disconnect());
    for (const track of stream.getTracks()) {
      release(() => track.removeEventListener('ended', onEnded));
      release(() => track.stop());
    }
    release(() => { void context?.close().catch(() => undefined); });
  };

  try {
    context = new AudioContext();
    const inputAnalyser = context.createAnalyser();
    analyser = inputAnalyser;
    inputAnalyser.fftSize = 2048;
    source = context.createMediaStreamSource(stream);
    source.connect(inputAnalyser);
    const samples = new Float32Array(inputAnalyser.fftSize);
    const level = createVoiceLevel();
    stream.getTracks().forEach((track) => track.addEventListener('ended', onEnded, { once: true }));

    const read = () => {
      if (stopped) return;
      inputAnalyser.getFloatTimeDomainData(samples);
      let sum = 0;
      for (const sample of samples) sum += sample * sample;
      level.set(Math.sqrt(sum / samples.length));
      frame = requestAnimationFrame(read);
    };

    try {
      await context.resume();
    } catch {
      // A context can still begin rendering after the stream has opened.
    }
    read();
    return { level, stream, stop };
  } catch (error) {
    stop();
    throw error;
  }
};
