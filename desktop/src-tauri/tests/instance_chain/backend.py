"""Start the immutable combined producer with socket-only provider mapping."""

from pathlib import Path
import json
import os
import queue
import subprocess
import threading


def start(checkout: Path, root: Path):
    source = (checkout / "scripts/desktop-enrollment-fixture.ts").read_text()
    needle = "process.env.SUPABASE_URL = provider;"
    assert source.count(needle) == 1
    # Keep the actual /api/config/auth response HTTPS, as production requires.
    # Only this test bootstrap's fetch socket is routed to its fake provider.
    replacement = """
  const providerOrigin = 'https://provider.fixture.test';
  const originalFetch = globalThis.fetch;
  await originalFetch(provider + '/auth/v1/otp');
  const dispatcherKey = Symbol.for('undici.globalDispatcher.1');
  const dispatcher = (globalThis as any)[dispatcherKey];
  if (!dispatcher?.dispatch) throw new Error('fixture_dispatcher_missing');
  (globalThis as any)[dispatcherKey] = {
    dispatch(options: any, handler: any) {
      const origin = String(options.origin);
      if (origin === providerOrigin) {
        return dispatcher.dispatch({ ...options, origin: provider }, handler);
      }
      if (new URL(origin).hostname !== '127.0.0.1') {
        handler.onError(new Error('fixture_egress_denied'));
        return false;
      }
      return dispatcher.dispatch(options, handler);
    },
    close: dispatcher.close.bind(dispatcher),
    destroy: dispatcher.destroy.bind(dispatcher),
  };
  globalThis.fetch = async (input, init) => {
    const original = new Request(input, init);
    const url = new URL(original.url);
    if (url.origin === providerOrigin) {
      const headers = new Headers(original.headers);
      headers.set('Host', url.host);
      return originalFetch(new Request(provider + url.pathname + url.search, {
        method: original.method, headers, redirect: 'manual',
        body: ['GET','HEAD'].includes(original.method) ? undefined : await original.arrayBuffer(),
      }));
    }
    if (url.hostname !== '127.0.0.1') throw new Error('fixture_egress_denied');
    return originalFetch(original);
  };
  process.env.SUPABASE_URL = providerOrigin;
"""
    script = checkout / "scripts/.desktop-native-fixture.ts"
    script.write_text(source.replace(needle, replacement))
    ready = queue.Queue()
    log = (root / "backend.log").open("w")
    process = subprocess.Popen(
        ["node", "--import", "tsx", str(script)],
        cwd=checkout,
        env={"PATH": os.environ["PATH"], "HOME": str(root), "TMPDIR": str(root)},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    def collect():
        for line in process.stdout:
            log.write(line)
            log.flush()
            try:
                value = json.loads(line)
                if value.get("event") == "desktop_enrollment_ready":
                    ready.put(value)
            except (ValueError, AttributeError):
                pass

    thread = threading.Thread(target=collect, daemon=True)
    thread.start()
    try:
        info = ready.get(timeout=60)
    except BaseException:
        process.terminate()
        process.wait(timeout=10)
        log.close()
        raise
    return process, info, log
