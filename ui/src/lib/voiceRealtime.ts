import {
  CloudUnavailableError,
  openAvibeWebSocket,
  type AvibeWebSocket,
} from './avibeFetch';

export const VOICE_REALTIME_PATH = '/api/cloud/voice/realtime';
export const VOICE_REALTIME_PROTOCOL = 'avibe-asr-v1';
export const VOICE_REALTIME_HANDSHAKE_TIMEOUT_MS = 8_000;
export const VOICE_REALTIME_FINISH_TIMEOUT_MS = 20_000;
const MAX_PENDING_AUDIO_FRAMES = 128;

export type VoiceRealtimePreview = { text: string; stash: string };
export type VoiceRealtimeFinal = { text: string; cleanup: 'success' | 'fallback' };

export type VoiceRealtimeOptions = {
  before: string;
  after: string;
  language?: string;
  signal?: AbortSignal;
  openSocket?: (
    path: string,
    protocol: string,
    signal?: AbortSignal,
  ) => Promise<AvibeWebSocket>;
  onReady?: () => void;
  onPreview?: (preview: VoiceRealtimePreview) => void;
};

const asError = (code: string): Error => {
  const error = new Error(code);
  error.name = 'VoiceRealtimeError';
  return error;
};

export const encodeVoiceRealtimePcm = (samples: Int16Array<ArrayBuffer>): string => {
  const bytes = new Uint8Array(samples.buffer, samples.byteOffset, samples.byteLength);
  let binary = '';
  const blockSize = 0x8000;
  for (let offset = 0; offset < bytes.length; offset += blockSize) {
    const block = bytes.subarray(offset, offset + blockSize);
    binary += String.fromCharCode(...block);
  }
  return btoa(binary);
};

export const parseVoiceRealtimeMessage = (value: unknown):
  | { type: 'ready'; protocol: string }
  | { type: 'preview'; text: string; stash: string }
  | { type: 'final'; text: string; cleanup: 'success' | 'fallback' }
  | { type: 'error'; code: string }
  | { type: 'finished' }
  | null => {
  if (typeof value !== 'string') return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(value);
  } catch {
    return null;
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return null;
  const message = parsed as Record<string, unknown>;
  if (message.type === 'ready' && message.protocol === VOICE_REALTIME_PROTOCOL) {
    return { type: 'ready', protocol: VOICE_REALTIME_PROTOCOL };
  }
  if (
    message.type === 'preview'
    && typeof message.text === 'string'
    && typeof message.stash === 'string'
  ) {
    return { type: 'preview', text: message.text, stash: message.stash };
  }
  if (
    message.type === 'final'
    && typeof message.text === 'string'
    && (message.cleanup === 'success' || message.cleanup === 'fallback')
  ) {
    return { type: 'final', text: message.text, cleanup: message.cleanup };
  }
  if (message.type === 'error' && typeof message.code === 'string') {
    return { type: 'error', code: message.code };
  }
  if (message.type === 'finished') return { type: 'finished' };
  return null;
};

const waitWithTimeout = <T>(promise: Promise<T>, timeoutMs: number): Promise<T> => {
  let timer: ReturnType<typeof setTimeout> | null = null;
  return new Promise<T>((resolve, reject) => {
    timer = setTimeout(() => reject(asError('realtime_timeout')), timeoutMs);
    promise.then(
      (value) => {
        if (timer != null) clearTimeout(timer);
        resolve(value);
      },
      (error) => {
        if (timer != null) clearTimeout(timer);
        reject(error);
      },
    );
  });
};

export class VoiceRealtimeSession {
  private readonly options: VoiceRealtimeOptions;
  private readonly openSocket: NonNullable<VoiceRealtimeOptions['openSocket']>;
  private socket: AvibeWebSocket | null = null;
  private ready = false;
  private finished = false;
  private aborted = false;
  private startPromise: Promise<void> | null = null;
  private finalPromise: Promise<VoiceRealtimeFinal> | null = null;
  private finalResolve: ((value: VoiceRealtimeFinal) => void) | null = null;
  private finalReject: ((error: unknown) => void) | null = null;
  private terminalError: unknown | null = null;
  private pendingAudio: string[] = [];

  constructor(options: VoiceRealtimeOptions) {
    this.options = options;
    this.openSocket = options.openSocket ?? openAvibeWebSocket;
  }

  async start(): Promise<void> {
    if (this.startPromise) return this.startPromise;
    this.startPromise = this.connect();
    return this.startPromise;
  }

  private async connect(): Promise<void> {
    const socket = await this.openSocket(
      VOICE_REALTIME_PATH,
      VOICE_REALTIME_PROTOCOL,
      this.options.signal,
    );
    this.socket = socket;
    const readyPromise = new Promise<void>((resolve, reject) => {
      const onMessage = (event: MessageEvent) => {
        const message = parseVoiceRealtimeMessage(event.data);
        if (!message) return;
        if (message.type === 'ready') {
          this.ready = true;
          socket.removeEventListener('message', onMessage);
          this.options.onReady?.();
          resolve();
        }
      };
      socket.addEventListener('message', onMessage);
      socket.addEventListener('error', () => reject(asError('realtime_socket_error')), { once: true });
      socket.addEventListener('close', () => reject(asError('realtime_closed')), { once: true });
    });
    socket.addEventListener('message', (event) => this.handleMessage(event));
    socket.addEventListener('error', () => this.rejectFinal(asError('realtime_socket_error')), { once: true });
    socket.addEventListener('close', () => {
      if (!this.finished && !this.aborted) this.rejectFinal(asError('realtime_closed'));
    }, { once: true });
    await waitWithTimeout(new Promise<void>((resolve, reject) => {
      const sendStart = () => {
        try {
          socket.send(JSON.stringify({
            type: 'start',
            before: this.options.before,
            after: this.options.after,
            ...(this.options.language ? { language: this.options.language } : {}),
          }));
          resolve();
        } catch (error) {
          reject(error);
        }
      };
      if (socket.readyState === WebSocket.OPEN) sendStart();
      else socket.addEventListener('open', sendStart, { once: true });
    }), VOICE_REALTIME_HANDSHAKE_TIMEOUT_MS);
    await waitWithTimeout(readyPromise, VOICE_REALTIME_HANDSHAKE_TIMEOUT_MS);
    while (this.pendingAudio.length > 0 && this.socket?.readyState === WebSocket.OPEN) {
      this.socket.send(JSON.stringify({ type: 'audio', audio: this.pendingAudio.shift() }));
    }
  }

  private handleMessage(event: MessageEvent): void {
    const message = parseVoiceRealtimeMessage(event.data);
    if (!message) return;
    if (message.type === 'preview') {
      this.options.onPreview?.(message);
    } else if (message.type === 'final') {
      this.finished = true;
      this.finalResolve?.({ text: message.text, cleanup: message.cleanup });
      this.finalResolve = null;
      this.finalReject = null;
    } else if (message.type === 'error') {
      this.rejectFinal(asError(message.code));
    }
  }

  sendPcm(samples: Int16Array<ArrayBuffer>): boolean {
    if (this.finished || this.aborted) {
      return false;
    }
    const audio = encodeVoiceRealtimePcm(samples);
    if (!this.ready || this.socket?.readyState !== WebSocket.OPEN) {
      if (this.pendingAudio.length >= MAX_PENDING_AUDIO_FRAMES) return false;
      this.pendingAudio.push(audio);
      return true;
    }
    try {
      this.socket.send(JSON.stringify({ type: 'audio', audio }));
      return true;
    } catch {
      this.rejectFinal(asError('realtime_socket_error'));
      return false;
    }
  }

  finish(): Promise<VoiceRealtimeFinal> {
    if (this.finalPromise) return this.finalPromise;
    const pending = new Promise<VoiceRealtimeFinal>((resolve, reject) => {
      this.finalResolve = resolve;
      this.finalReject = reject;
      if (this.terminalError) {
        reject(this.terminalError);
        this.finalResolve = null;
        this.finalReject = null;
        return;
      }
      void this.start().then(() => {
        if (this.aborted || this.finished || !this.socket) return;
        this.socket.send(JSON.stringify({ type: 'finish' }));
      }).catch(reject);
    });
    this.finalPromise = waitWithTimeout(pending, VOICE_REALTIME_FINISH_TIMEOUT_MS);
    return this.finalPromise;
  }

  abort(): void {
    this.aborted = true;
    this.socket?.close(1000, 'aborted');
    this.rejectFinal(asError('realtime_aborted'));
  }

  private rejectFinal(error: unknown): void {
    if (this.finished || this.aborted) return;
    this.terminalError ??= error;
    this.finalReject?.(error);
    this.finalResolve = null;
    this.finalReject = null;
  }
}

export const isVoiceRealtimeEnabled = (): boolean =>
  import.meta.env.VITE_VOICE_REALTIME_ENABLED === 'true';

export const voiceRealtimeError = (error: unknown): Error => {
  if (error instanceof CloudUnavailableError) return error;
  return error instanceof Error ? error : asError('realtime_failed');
};
