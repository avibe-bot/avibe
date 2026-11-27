declare module '@event-transport/show-sdk' {
  export function subscribeShowEvents(options?: {
    streamPath?: string;
    afterId?: string;
    eventStreamTransport?: 'websocket-sse-v1';
  }): EventSource;
}
