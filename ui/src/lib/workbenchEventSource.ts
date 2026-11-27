import { apiFetch, withApiDeadline } from './apiFetch';

/** A transport adapter. ApiContext remains the owner of reconnect and catch-up. */
export class WorkbenchEventSource extends EventTarget {
  readonly CONNECTING = 0;
  readonly OPEN = 1;
  readonly CLOSED = 2;
  readonly withCredentials = false;
  readonly url: string;
  readyState: number = this.CONNECTING;
  onerror: ((event: Event) => void) | null = null;
  onopen: ((event: Event) => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  private readonly controller = new AbortController();
  private transport: EventSource | WebSocket | null = null;
  private readonly eventTypes = new Set(['open', 'error', 'message']);
  private buffer = '';
  private lastEventId = '';
  private failed = false;

  constructor(url: string) {
    super();
    this.url = new URL(url, window.location.href).href;
    void this.connect();
  }

  override addEventListener(type: string, listener: EventListenerOrEventListenerObject | null, options?: boolean | AddEventListenerOptions): void {
    if (!this.eventTypes.has(type)) {
      this.eventTypes.add(type);
      if (this.transport instanceof EventSource) this.transport.addEventListener(type, this.relay);
    }
    super.addEventListener(type, listener, options);
  }

  private emit(event: Event): void {
    if (this.readyState === this.CLOSED) return;
    this.dispatchEvent(event);
    if (this.readyState === this.CLOSED) return;
    if (event.type === 'error') this.onerror?.(event);
    else if (event.type === 'open') this.onopen?.(event);
    else if (event.type === 'message') this.onmessage?.(event as MessageEvent);
  }

  private relay = (event: Event): void => {
    if (this.controller.signal.aborted) return;
    this.readyState = event.type === 'error' ? this.CONNECTING : (this.transport?.readyState ?? this.CONNECTING);
    this.emit(event instanceof MessageEvent
      ? new MessageEvent(event.type, { data: event.data, lastEventId: event.lastEventId, origin: event.origin })
      : new Event(event.type));
  };

  private fail(): void {
    if (this.failed || this.controller.signal.aborted) return;
    this.failed = true;
    this.readyState = this.CONNECTING;
    this.emit(new Event('error'));
  }

  private async connect(): Promise<void> {
    try {
      const sessionUrl = new URL('/api/session', this.url);
      const session = await withApiDeadline(15_000, this.controller.signal, async (signal) => {
        const response = await apiFetch(sessionUrl, { signal, cache: 'no-store' });
        if (!response.ok) throw new Error('Event transport admission failed');
        return await response.json() as {
          remote?: boolean; authenticated?: boolean; authorization_state?: string;
          event_stream_transport?: string;
        };
      });
      if (this.controller.signal.aborted) return;
      if (typeof session.remote !== 'boolean'
        || (session.remote && (session.authenticated !== true
          || (session.authorization_state !== undefined && session.authorization_state !== 'current')))) {
        throw new Error('Event transport session unavailable');
      }
      if (session.event_stream_transport === undefined) {
        // Only an old, successfully admitted Runtime selects SSE. A failed WS
        // handshake (including an auth rejection) never downgrades transport.
        const source = new EventSource(this.url);
        this.transport = source;
        for (const type of this.eventTypes) source.addEventListener(type, this.relay);
        return;
      }
      if (session.event_stream_transport !== 'websocket-sse-v1') throw new Error('Unknown event transport');
      const url = new URL(this.url);
      url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
      const socket = new WebSocket(url);
      this.transport = socket;
      socket.onopen = () => {
        if (this.controller.signal.aborted) return;
        this.readyState = this.OPEN;
        this.emit(new Event('open'));
      };
      socket.onmessage = (event) => {
        if (!this.controller.signal.aborted && typeof event.data === 'string') this.consume(event.data);
      };
      socket.onerror = () => this.fail();
      socket.onclose = () => this.fail();
    } catch {
      this.fail();
    }
  }

  private consume(chunk: string): void {
    this.buffer += chunk;
    let separator: RegExpMatchArray | null;
    while ((separator = /\r?\n\r?\n/.exec(this.buffer)) !== null) {
      const frame = this.buffer.slice(0, separator.index);
      this.buffer = this.buffer.slice(separator.index! + separator[0].length);
      let type = 'message';
      const data: string[] = [];
      for (const line of frame.split(/\r?\n/)) {
        const colon = line.indexOf(':');
        const field = colon === -1 ? line : line.slice(0, colon);
        const value = colon === -1 ? '' : line.slice(colon + 1).replace(/^ /, '');
        if (field === 'event') type = value || 'message';
        else if (field === 'data') data.push(value);
        else if (field === 'id' && !value.includes('\0')) this.lastEventId = value;
      }
      if (data.length) {
        this.emit(new MessageEvent(type, {
          data: data.join('\n'), lastEventId: this.lastEventId, origin: new URL(this.url).origin,
        }));
      }
      if (this.controller.signal.aborted) return;
    }
  }

  close(): void {
    this.controller.abort();
    this.transport?.close();
    this.transport = null;
    this.buffer = '';
    this.readyState = this.CLOSED;
  }
}

// ApiContext consumes the standard EventSource listener surface. Named SSE
// events are MessageEvents even though EventTarget's generic overload is wider.
export function createWorkbenchEventSource(url: string): EventSource {
  return new WorkbenchEventSource(url) as unknown as EventSource;
}
