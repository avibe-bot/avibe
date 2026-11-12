// Appended only on the new instance fixtures. No change to the original probe.
export const instanceScript = String.raw`
window.instanceProbe = {
  pending: new Map(),
  async fetch(id, host) {
    if (this.pending.has(id)) throw new Error('duplicate operation');
    const controller = new AbortController();
    const item = {controller, promise: null};
    this.pending.set(id, item);
    item.promise = (async () => {
      const timer = setTimeout(() => controller.abort(), 8000);
      try {
        const response = await fetch('https://' + host + '/fetch?id=' + encodeURIComponent(id), {
          cache: 'no-store', signal: controller.signal, credentials: 'omit'
        });
        if (!response.ok) return {ok:false, id, outcome:'failed', error:'HTTP ' + response.status};
        const result = await response.json();
        return {ok:true, ...result, outcome:'routed'};
      } catch (error) {
        return {ok:false, id, outcome:controller.signal.aborted ? 'bounded_failure' : 'failed', error:String(error)};
      } finally { clearTimeout(timer); }
    })();
    try { return {...await item.promise, settled:true}; }
    finally { this.pending.delete(id); }
  },
  async cancel(id) {
    const item = this.pending.get(id);
    if (item) { item.controller.abort(); await item.promise; }
    return {ok:true, settled:true, id};
  }
};
`;
