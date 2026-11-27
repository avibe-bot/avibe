import { WorkbenchEventSource } from '../../src/lib/workbenchEventSource';
import { subscribeShowEvents } from '@event-transport/show-sdk';

export type Family = 'workbench' | 'sdk';
export type Client = 'old' | 'new';
export type StreamState = {
  opened: number;
  errors: number;
  readyState: number;
  events: Array<{ type: string; data: string; id: string }>;
};
export type StartOptions = { family: Family; client?: Client; url?: string; afterId?: string };
export type FixtureApi = {
  start(options: StartOptions): void;
  close(): void;
  state(): StreamState;
  probe(): Promise<{ milliseconds: number; body: string }>;
};

declare global {
  interface Window {
    eventTransport: FixtureApi;
    eventTransportReady: boolean;
  }
}

let source: EventSource | WorkbenchEventSource | undefined;
let state: StreamState = { opened: 0, errors: 0, readyState: 0, events: [] };

window.eventTransport = {
  start({ family, client = 'new', url, afterId }) {
    source?.close();
    const endpoint = url ?? (family === 'workbench' ? '/api/events' : '/show/fixture/__show/events?stream=1');
    state = { opened: 0, errors: 0, readyState: 0, events: [] };
    const ownedState = state;
    const next = client === 'old'
      ? new EventSource(endpoint)
      : family === 'workbench'
        ? new WorkbenchEventSource(endpoint)
        : subscribeShowEvents({ streamPath: endpoint, afterId });
    source = next;
    next.onopen = () => { ownedState.opened += 1; };
    next.onerror = () => { ownedState.errors += 1; };
    for (const type of ['show.event', 'fixture.event', 'authorization.revoked']) {
      next.addEventListener(type, (event: Event) => {
        const message = event as MessageEvent;
        ownedState.events.push({ type, data: message.data, id: message.lastEventId });
      });
    }
  },
  close() { source?.close(); },
  state() { return { ...state, readyState: source?.readyState ?? 2 }; },
  async probe() {
    const started = performance.now();
    const response = await fetch(`/probe?nonce=${crypto.randomUUID()}`, { cache: 'no-store' });
    return { milliseconds: performance.now() - started, body: await response.text() };
  },
};
window.eventTransportReady = true;
