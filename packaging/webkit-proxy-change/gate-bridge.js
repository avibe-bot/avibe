// A stand-in for the phone App's bridge, for the rc gate cells: the page sees
// a shell with `direct` version 3. The test decides when `direct.status` is
// answered (the App holds that answer on a return until its port is open).
(() => {
  if (window.avibeNative) return;
  const pending = [];
  const handlers = {};
  const calls = [];
  const state = {
    hold: false,
    status: {preferred: 'direct', sequence: 1, closure: 0, path: 'ipv4'},
    handover: {closed: 0, kept: 0},
  };
  window.__gate = {
    hold(on) {
      state.hold = Boolean(on);
      let released = 0;
      if (!state.hold) while (pending.length) { pending.shift()(); released += 1; }
      return {ok: true, mode: state.hold ? 'held' : 'answering', closed: released};
    },
    snapshot() {
      const count = name => calls.filter(c => c.method === name).length;
      return {
        ok: true, visibility: document.visibilityState, href: location.pathname + location.search,
        status: count('direct.status'), handovers: count('direct.handover'), count: pending.length,
        title: document.title, text: (document.body?.innerText ?? '').replace(/\s+/g, ' ').slice(0, 200),
        signIn: Boolean(document.getElementById('gate-sign-in')),
      };
    },
    // A made-up return, for a WebView that does not report hidden when its
    // window is: the page's own listeners run as on a real one.
    pretend(visibility) {
      Object.defineProperty(document, 'visibilityState', {configurable: true, get: () => visibility});
      Object.defineProperty(document, 'hidden', {configurable: true, get: () => visibility !== 'visible'});
      document.dispatchEvent(new Event('visibilitychange'));
      return {ok: true, visibility: document.visibilityState, mode: 'pretended'};
    },
  };
  Object.defineProperty(window, 'avibeNative', {
    configurable: false,
    value: {
      contract: 1,
      platform: 'ios',
      shellVersion: 'gate (0)',
      capabilities: {direct: 3},
      call(method) {
        calls.push({method, at: Date.now()});
        if (method === 'direct.status') {
          return new Promise(resolve => {
            const answer = () => resolve({...state.status});
            if (state.hold) pending.push(answer); else answer();
          });
        }
        if (method === 'direct.handover') return Promise.resolve({...state.handover});
        return Promise.reject(Object.assign(new Error('unsupported'), {code: 'unsupported'}));
      },
      on(event, handler) {
        (handlers[event] ||= []).push(handler);
        return () => undefined;
      },
    },
  });
})();
