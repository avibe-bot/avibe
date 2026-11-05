import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { Composer } from '../../src/components/workbench/Composer';
import { ToastProvider } from '../../src/context/ToastProvider';
import i18n from '../../src/i18n';
import '../../src/index.css';

// A composer whose network the test steers (`window.voiceNet`): the cloud token, the realtime
// socket, avibe.bot's dictation route, and the local relay can each work, fail, or hang, and
// change between takes. Every request the take makes is logged (`window.voiceNetLog`), so a
// spec can say which path carried which segment.

void i18n.changeLanguage('en');

type Net = {
  /** `/api/cloud/token`: a token, a refusal (503), or no answer at all. */
  token: 'ok' | 'fail' | 'hang';
  /** The realtime socket: works, is refused at the handshake, or dies two seconds in. */
  socket: 'ok' | 'refuse' | 'kill-mid';
  /** avibe.bot's dictation route from the browser: answers, or the network fails. */
  cloud: 'ok' | 'down';
  /** The local relay (`/api/asr/transcribe`): answers, or the network fails. */
  relay: 'ok' | 'down';
  /** Segments whose next cloud request fails once with a 503 (a transient service failure). */
  cloudFailOnce: number[];
};

type DictationRequest = { path: 'cloud' | 'relay'; sequence: number; final: boolean; receipts: string[] };

declare global {
  interface Window {
    voiceNet: Net;
    voiceNetLog: {
      tokens: string[];
      sockets: string[];
      dictations: DictationRequest[];
      sent: string[];
    };
  }
}

window.voiceNet = { token: 'ok', socket: 'ok', cloud: 'ok', relay: 'ok', cloudFailOnce: [] };
window.voiceNetLog = { tokens: [], sockets: [], dictations: [], sent: [] };

const CLOUD = 'https://cloud.test';
/** 16 kHz mono WAV of five seconds: the recorder cuts a segment every five seconds. */
const SEGMENT_BYTES = 44 + 16_000 * 2 * 5;
const FINAL_TEXT = 'Every word, over HTTP';
const REALTIME_TEXT = 'Every word, live';

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status,
  headers: { 'Content-Type': 'application/json' },
});

const never = () => new Promise<Response>(() => undefined);

const answerDictation = (request: DictationRequest): Response => {
  window.voiceNetLog.dictations.push(request);
  if (!request.final) return json({ receipt: `receipt-${request.sequence}`, sequence: request.sequence });
  return json({ text: FINAL_TEXT, cleanup: 'success' });
};

window.fetch = async (input, init) => {
  const url = new URL(
    typeof input === 'string' ? input : input instanceof URL ? input.href : input.url,
    location.origin,
  );
  const net = window.voiceNet;
  if (url.origin === CLOUD && url.pathname === '/api/cloud/voice/dictations') {
    if (net.cloud === 'down') throw new TypeError('Load failed');
    const form = init?.body as FormData;
    const sequence = Number(form.get('sequence'));
    const failOnce = net.cloudFailOnce.indexOf(sequence);
    if (failOnce >= 0) {
      net.cloudFailOnce.splice(failOnce, 1);
      return json({ error: 'asr_unavailable' }, 503);
    }
    return answerDictation({
      path: 'cloud',
      sequence,
      final: form.get('final') === 'true',
      receipts: form.getAll('receipt').map(String),
    });
  }
  switch (url.pathname) {
    case '/api/asr/status':
      return json({ available: true, max_file_bytes: SEGMENT_BYTES });
    case '/api/cloud/token': {
      if (net.token === 'hang') return never();
      if (net.token === 'fail') return json({ error: 'cloud_unavailable' }, 503);
      const token = `token-${window.voiceNetLog.tokens.length + 1}`;
      window.voiceNetLog.tokens.push(token);
      return json({ token, base_url: CLOUD, expires_at: Math.floor(Date.now() / 1000) + 3600, capabilities: [] });
    }
    case '/api/asr/transcribe': {
      if (net.relay === 'down') throw new TypeError('Load failed');
      const body = JSON.parse(String(init?.body ?? '{}')) as { sequence: number; final: boolean; receipts?: string[] };
      return answerDictation({ path: 'relay', sequence: body.sequence, final: body.final, receipts: body.receipts ?? [] });
    }
    case '/api/csrf-token':
      return json({ csrf_token: 'fixture' });
    case '/api/asr/telemetry':
      return json({ ok: true });
    default:
      return json({ error: 'not_found' }, 404);
  }
};

type Listener = (event: unknown) => void;

/** The realtime socket, as avibe.bot speaks it (`avibe-asr-v1`), with the failures `voiceNet`
 *  asks for. */
class FakeRealtimeSocket {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;
  readyState = 0;
  private readonly mode = window.voiceNet.socket;
  private readonly listeners = new Map<string, Set<Listener>>();
  private frames = 0;

  constructor(_url: string, protocol: string) {
    window.voiceNetLog.sockets.push(protocol);
    setTimeout(() => {
      if (this.mode === 'refuse') this.drop();
      else {
        this.readyState = 1;
        this.emit('open', {});
      }
    }, 40);
  }

  addEventListener(type: string, listener: Listener) {
    const set = this.listeners.get(type) ?? new Set();
    set.add(listener);
    this.listeners.set(type, set);
  }

  removeEventListener(type: string, listener: Listener) {
    this.listeners.get(type)?.delete(listener);
  }

  send(data: string) {
    if (this.readyState !== 1) throw new DOMException('closed', 'InvalidStateError');
    const message = JSON.parse(data) as { type: string };
    if (message.type === 'start') this.message({ type: 'ready', protocol: 'avibe-asr-v1' });
    if (message.type === 'audio') {
      this.frames += 1;
      if (this.mode === 'kill-mid' && this.frames === 8) {
        this.drop();
        return;
      }
      if (this.mode === 'ok') this.message({ type: 'preview', text: REALTIME_TEXT, stash: '' });
    }
    if (message.type === 'finish') {
      this.message({ type: 'final', text: REALTIME_TEXT, cleanup: 'success' });
      this.message({ type: 'finished' });
      setTimeout(() => this.close(1000, 'complete'), 60);
    }
  }

  close(code = 1000, reason = '') {
    if (this.readyState === 3) return;
    this.readyState = 3;
    setTimeout(() => this.emit('close', { code, reason, wasClean: code === 1000 }));
  }

  private drop() {
    this.readyState = 3;
    this.emit('error', {});
    this.emit('close', { code: 1006, reason: '', wasClean: false });
  }

  private message(body: unknown) {
    setTimeout(() => {
      if (this.readyState === 1) this.emit('message', { data: JSON.stringify(body) });
    }, 20);
  }

  private emit(type: string, event: unknown) {
    for (const listener of this.listeners.get(type) ?? []) listener(event);
  }
}

window.WebSocket = FakeRealtimeSocket as unknown as typeof WebSocket;

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ToastProvider>
      <div className="flex min-h-dvh flex-col justify-end p-3">
        <Composer
          sessionId="voice-network-session"
          onSend={(text) => {
            window.voiceNetLog.sent.push(text);
            return true;
          }}
          voiceTarget={{ name: 'Fix the cache', title: 'To “Fix the cache”' }}
          onSearchAgents={async () => []}
          onSearchSessions={async () => []}
        />
      </div>
    </ToastProvider>
  </StrictMode>,
);
