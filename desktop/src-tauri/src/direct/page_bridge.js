(token, version, asksBeforeLeaving) => {
  // The page's `direct` capability in a window of another computer's
  // instance (docs/plans/ios-app.md section 4, as the phone App answers it).
  // Top frame only, once per document. `token` is this window's own: only
  // this script knows it, so a frame inside the page cannot ask in its name.
  if (window.self !== window.top || window.avibeNative) return;

  // What the shell last delivered. Before anything arrives the answer is the
  // tunnel, which is what a window without a route uses: `direct.status()`
  // never waits for the shell.
  let status = null;
  // The shell counts closures for the window; a document counts from its own
  // first delivery, so one that loads late does not reconnect for the past.
  let closureBase = null;
  let preference = null;
  // Deliveries can arrive out of order: the choice with the highest order
  // is the latest one made, whenever it arrives.
  let preferenceOrder = -1;
  const handlers = new Map();
  const pending = new Map();
  let asking = false;
  // This document's mark. It goes with every request, and the shell hands
  // the answer to the document that bears it and to no other: one that has
  // taken this one's place in the window gets nothing of what this one
  // asked.
  const mark = crypto.randomUUID();
  Object.defineProperty(window, '__AVIBE_DIRECT_DOCUMENT__', { value: mark });

  const emit = (event, payload) => {
    for (const handler of Array.from(handlers.get(event) ?? [])) {
      try { handler(payload); } catch { /* one listener cannot stop the rest */ }
    }
  };

  const statusNow = () => (status
    ? { ...status }
    : { preferred: 'tunnel', sequence: 0, closure: 0, path: null });

  const deliver = (message) => {
    if (!message || typeof message !== 'object') return;
    const order = typeof message.preferenceOrder === 'number' ? message.preferenceOrder : 0;
    if (message.preference && typeof message.preference === 'object' && order >= preferenceOrder) {
      // The page hears a choice that changed while it was open (the app's
      // menu, another window, or the app setting it back itself), as the
      // phone App tells it: a window event. Not the first one it is given,
      // which it reads with `direct.preference()`.
      const changed = preference !== null && JSON.stringify(preference) !== JSON.stringify(message.preference);
      preference = message.preference;
      preferenceOrder = order;
      if (changed) {
        const detail = { ...preference };
        emit('direct.preferenceChanged', detail);
        window.dispatchEvent(new CustomEvent('avibe:native:direct.preferenceChanged', { detail }));
      }
    }
    const next = message.status;
    if (next && typeof next === 'object' && (status === null || next.sequence > status.sequence)) {
      if (closureBase === null) closureBase = next.closure;
      status = { ...next, closure: Math.max(0, next.closure - closureBase) };
      const { path_scope: _scope, ...event } = status;
      emit('direct.transportChanged', event);
    }
    const reply = message.reply;
    if (reply && typeof reply === 'object' && pending.has(reply.id)) {
      const request = pending.get(reply.id);
      pending.delete(reply.id);
      clearTimeout(request.timer);
      if (reply.error && typeof reply.error === 'object') {
        request.reject({ code: String(reply.error.code), message: 'The app refused the request.' });
      } else {
        request.resolve(reply.result);
      }
    }
  };

  // A request is a link the shell refuses to follow. Where the WebView asks
  // the page before leaving it (`asksBeforeLeaving`: WebView2 does, WebKit
  // does not), that question is not the page's to answer: nothing is being
  // left. Elsewhere nothing is ever kept from the page.
  if (asksBeforeLeaving) {
    window.addEventListener('beforeunload', (event) => {
      if (!asking) return;
      asking = false;
      event.stopImmediatePropagation();
    }, true);
  }

  // `patience`: how long the shell may take. A measurement is bounded by the
  // shell at 30 s, so it is waited for a little longer than that.
  const ask = (method, params, patience = 10000) => new Promise((resolve, reject) => {
    if (pending.size >= 16) {
      reject({ code: 'unavailable', message: 'Too many requests are waiting for the app.' });
      return;
    }
    const id = crypto.randomUUID();
    const timer = setTimeout(() => {
      pending.delete(id);
      reject({ code: 'unavailable', message: 'The app did not answer.' });
    }, patience);
    pending.set(id, { resolve, reject, timer });
    const link = document.createElement('a');
    link.href = `avibe-app://direct?request=${encodeURIComponent(JSON.stringify({ token, id, document: mark, method, params }))}`;
    (document.body ?? document.documentElement).append(link);
    if (asksBeforeLeaving) {
      asking = true;
      setTimeout(() => { asking = false; }, 50);
    }
    try { link.click(); } finally { link.remove(); }
  });

  const call = (method, params = {}) => {
    switch (method) {
      case 'direct.status':
        return Promise.resolve(statusNow());
      case 'direct.preference':
        return preference ? Promise.resolve({ ...preference }) : ask(method, {});
      case 'direct.handover':
        return ask(method, {});
      case 'direct.measure':
        return ask(method, {}, 35000);
      case 'direct.setPreference':
        if (params?.mode !== 'automatic' && params?.mode !== 'tunnel' && params?.mode !== 'direct') {
          return Promise.reject({ code: 'invalid_argument', message: 'The mode is automatic, tunnel or direct.' });
        }
        return ask(method, { mode: params.mode });
      default:
        return Promise.reject({ code: 'unsupported', message: 'The desktop app answers direct only.' });
    }
  };

  const on = (event, handler) => {
    if (!handlers.has(event)) handlers.set(event, new Set());
    handlers.get(event).add(handler);
    return () => { handlers.get(event)?.delete(handler); };
  };

  Object.defineProperty(window, '__AVIBE_DIRECT_DELIVER__', { value: deliver });
  Object.defineProperty(window, 'avibeNative', {
    value: Object.freeze({
      contract: 1,
      platform: 'desktop',
      shellVersion: version,
      capabilities: Object.freeze({ direct: 3, directPreference: 2, directMeasure: 1 }),
      call,
      on,
    }),
  });

  // The shell may have delivered before this script ran.
  const early = window.__AVIBE_DIRECT_EARLY__;
  if (early) {
    delete window.__AVIBE_DIRECT_EARLY__;
    deliver(early);
  }
}
