"""The injected Show Page script keeps a loaded page across a background drop.

Vite's HMR client reloads the page whenever its socket closes and a ping then
succeeds. These tests run the script Avibe injects into every Show Page in Node
with a fake document and WebSocket, and stand in for Vite's client the way it
uses the socket: a close after open means "reload".
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from vibe import ui_server

NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is required")

HARNESS = r"""
const docListeners = {};
const document = {
  visibilityState: 'visible',
  addEventListener(type, fn) { (docListeners[type] ||= []).push(fn); },
  removeEventListener(type, fn) { docListeners[type] = (docListeners[type] || []).filter((f) => f !== fn); },
};
function setVisibility(state) {
  document.visibilityState = state;
  for (const fn of [...(docListeners.visibilitychange || [])]) fn();
}
const sockets = [];
class FakeSocket extends EventTarget {
  constructor(url, protocols) {
    super();
    this.url = url; this.protocols = protocols; this.readyState = 0; this.sent = [];
    this.protocol = '';
    sockets.push(this);
  }
  send(data) { if (this.readyState !== 1) throw new Error('not open'); this.sent.push(data); }
  close() { this.readyState = 3; }
  open() { this.readyState = 1; this.dispatchEvent(new Event('open')); }
  drop(code = 1006) {
    this.readyState = 3;
    this.dispatchEvent(Object.assign(new Event('close'), { code, reason: '', wasClean: false }));
  }
  deliver(data) { this.dispatchEvent(new MessageEvent('message', { data })); }
}
globalThis.document = document;
globalThis.window = { addEventListener() {}, location: { href: 'https://x/show/ses123/' } };
const page = { reloads: 0, fetched: [], entryStamp: null };
globalThis.location = { reload() { page.reloads += 1; } };
globalThis.fetch = async (url) => {
  page.fetched.push(url);
  return { ok: true, text: async () => `<script>var next={"contentStamp":"${page.entryStamp}"};</script>` };
};
const settle = () => new Promise((resolve) => setTimeout(resolve, 0));
globalThis.WebSocket = FakeSocket;

SCRIPT

// Vite's client as it uses the socket (vite/dist/client: createWebSocketModuleRunnerTransport).
const vite = { reloads: 0, messages: [] };
const hmr = new WebSocket('wss://x/show/ses123/__vite_hmr?token=t', 'vite-hmr');
let isOpened = false;
hmr.addEventListener('open', () => { isOpened = true; });
hmr.addEventListener('close', () => { if (isOpened) vite.reloads += 1; });
hmr.addEventListener('message', ({ data }) => vite.messages.push(data));

const STEPS
const result = { reloads: vite.reloads, messages: vite.messages, sockets: sockets.length,
  hmrReadyState: hmr.readyState, hmrOpen: hmr.readyState === hmr.OPEN,
  sent: sockets.map((s) => s.sent), protocols: sockets.map((s) => s.protocols),
  pageReloads: page.reloads, fetched: page.fetched };
console.log(JSON.stringify(result));
"""


def _injected_js() -> str:
    script = ui_server._show_runtime_config_script(
        "ses123",
        base_path="/show/ses123/",
        authenticated=True,
        include_write_token=False,
    )
    assert script.startswith("<script>") and script.endswith("</script>")
    return script[len("<script>") : -len("</script>")]


def _run(steps: str) -> dict:
    source = HARNESS.replace("SCRIPT", _injected_js()).replace("const STEPS", steps)
    completed = subprocess.run(
        [NODE, "--input-type=module", "-e", source],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_returning_from_the_background_does_not_reload_a_loaded_show_page():
    result = _run(
        """
sockets[0].open();
sockets[0].deliver('{"type":"connected"}');
setVisibility('hidden');
sockets[0].drop();
setVisibility('visible');
sockets[1]?.open();
hmr.send('{"type":"ping"}');
sockets[1]?.deliver('{"type":"update"}');
"""
    )
    assert result["reloads"] == 0
    assert result["sockets"] == 2
    assert result["protocols"] == ["vite-hmr", "vite-hmr"]
    # Live updates keep flowing on the new socket, and sends reach it.
    assert result["messages"] == ['{"type":"connected"}', '{"type":"update"}']
    assert result["hmrOpen"] is True
    assert result["sent"] == [[], ['{"type":"ping"}']]
    assert result["pageReloads"] == 0


def _resume_with_entry_stamp(entry_stamp: str) -> dict:
    return _run(
        f"""
globalThis.__AVIBE_SHOW__.contentStamp = '3-100';
page.entryStamp = '{entry_stamp}';
sockets[0].open();
setVisibility('hidden');
sockets[0].drop();
setVisibility('visible');
sockets[1]?.open();
await settle(); await settle(); await settle();
"""
    )


def test_a_quiet_reconnect_reloads_once_when_the_source_changed_while_away():
    result = _resume_with_entry_stamp("4-200")
    assert result["fetched"] == ["/show/ses123/"]
    assert result["pageReloads"] == 1
    assert result["reloads"] == 0


def test_a_quiet_reconnect_keeps_the_page_when_the_source_is_unchanged():
    result = _resume_with_entry_stamp("3-100")
    assert result["fetched"] == ["/show/ses123/"]
    assert result["pageReloads"] == 0
    assert result["reloads"] == 0


def test_content_stamp_follows_page_source_not_api_data(monkeypatch, tmp_path):
    monkeypatch.setattr(ui_server.paths, "get_show_page_dir", lambda _sid: tmp_path)
    (tmp_path / "src" / "components").mkdir(parents=True)
    (tmp_path / "api").mkdir()
    (tmp_path / "index.html").write_text("<html></html>")
    app_file = tmp_path / "src" / "components" / "App.tsx"
    app_file.write_text("export default 1")
    first = ui_server._show_page_content_stamp("ses123")
    assert first

    (tmp_path / "api" / "cache.json").write_text("{}")
    assert ui_server._show_page_content_stamp("ses123") == first

    os.utime(app_file, ns=(1, 10**19))
    edited = ui_server._show_page_content_stamp("ses123")
    assert edited != first

    (tmp_path / "src" / "New.tsx").write_text("export default 2")
    assert ui_server._show_page_content_stamp("ses123") != edited

    payload = ui_server._show_runtime_config_payload(
        "ses123", base_path="/show/ses123/", authenticated=True, include_write_token=True
    )
    assert payload["contentStamp"] == ui_server._show_page_content_stamp("ses123")
    public = ui_server._show_runtime_config_payload(
        "ses123", base_path="/p/share/", authenticated=False, include_write_token=False
    )
    assert "contentStamp" not in public


def test_a_drop_reported_while_still_hidden_reconnects_once_visible():
    result = _run(
        """
sockets[0].open();
setVisibility('hidden');
sockets[0].drop();
const whileHidden = sockets.length;
setVisibility('visible');
sockets[1]?.open();
vite.messages.push('sockets-while-hidden:' + whileHidden);
"""
    )
    assert result["reloads"] == 0
    assert result["messages"] == ["sockets-while-hidden:1"]
    assert result["sockets"] == 2


def test_a_drop_while_the_page_stays_visible_still_reaches_vite():
    # A runtime restart: Vite must still see it and recover by reloading.
    result = _run(
        """
sockets[0].open();
sockets[0].drop();
"""
    )
    assert result["reloads"] == 1
    assert result["sockets"] == 1


def test_a_reconnect_that_does_not_open_hands_the_drop_to_vite():
    result = _run(
        """
sockets[0].open();
setVisibility('hidden');
sockets[0].drop();
setVisibility('visible');
sockets[1].drop();
"""
    )
    assert result["reloads"] == 1
    assert result["sockets"] == 2


def test_other_sockets_are_native():
    result = _run(
        """
const ping = new WebSocket('wss://x/show/ses123/__vite_hmr', 'vite-ping');
const plain = new WebSocket('wss://x/app/socket');
vite.messages.push(String(ping instanceof FakeSocket), String(plain instanceof FakeSocket), String(plain.protocols));
"""
    )
    assert result["messages"] == ["true", "true", "undefined"]
