"""Opt-in paired-source browser probe against the real isolated Runtime routes.

Build ui/e2e/event-transport first, then set EVENT_TRANSPORT_BROWSER=1. This
uses test-owned HOME/config/SQLite, fresh headless browser profiles, and a
loopback ephemeral uvicorn listener. It never starts the installed service.
"""

import asyncio
import json
import os
from pathlib import Path
import socket

import pytest
import uvicorn
from starlette.responses import HTMLResponse, Response


@pytest.mark.skipif(os.environ.get("EVENT_TRANSPORT_BROWSER") != "1", reason="requires compiled paired SDK/UI browser fixture")
@pytest.mark.parametrize("browser_name", ["chromium", "webkit"])
async def test_real_runtime_with_production_ui_and_sdk(monkeypatch, tmp_path, browser_name):
    from core.show_session_events import ShowSessionEventStore
    from tests.test_ui_show_pages import _create_agent_session, _create_show_page
    from tests.ui_server_test_helpers import _save_config
    from vibe import ui_server
    from vibe.sse_broker import broker

    root = Path(__file__).resolve().parents[1]
    fixture = root / "ui/e2e/event-transport/.build/fixture.js"
    assert fixture.is_file(), "Build the paired SDK/UI fixture before this opt-in test"
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "avibe"))
    _save_config(tmp_path)
    _create_agent_session("fixture")
    _create_show_page("fixture", "private")
    store = ShowSessionEventStore()
    try:
        for index in range(2):
            store.append("fixture", {
                "id": f"show_evt_{index}", "type": "assistant.mark.created",
                "mark": {"target": "heading", "body": f"真实事件 {index} 🐈", "createdAt": "2026-11-27T01:00:00Z"},
            })
    finally:
        store.close()
    config = ui_server._show_runtime_config_payload(
        "fixture", base_path="/show/fixture/", authenticated=True, include_write_token=False,
    )
    # Only fixture static assets bypass product routes. Session capability,
    # subscriptions, authorization, persistence and replay use the real app.
    monkeypatch.setattr(ui_server.app.router, "routes", list(ui_server.app.router.routes))

    @ui_server.app.get("/__event-fixture")
    async def document():
        return HTMLResponse(
            '<!doctype html><meta charset="utf-8"><script>globalThis.__AVIBE_SHOW__='
            + json.dumps(config) + '</script><script src="/fixture.js"></script>'
        )

    @ui_server.app.get("/fixture.js")
    async def javascript():
        return Response(fixture.read_bytes(), media_type="text/javascript")

    ui_server.app.router.routes[:] = [*ui_server.app.router.routes[-2:], *ui_server.app.router.routes[:-2]]

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(ui_server.app, lifespan="off", log_level="error"))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    baseline = broker.subscriber_count()
    program = r'''
import { chromium, webkit } from './ui/node_modules/playwright/index.mjs';
const kind = process.env.EVENT_BROWSER;
const browser = await ({chromium, webkit}[kind]).launch({
  headless: true,
  ...(kind === 'webkit' && process.env.EVENT_TRANSPORT_WEBKIT
    ? {executablePath: process.env.EVENT_TRANSPORT_WEBKIT} : {}),
});
const context = await browser.newContext();
const {installBrowserIsolation} = await import('./ui/scripts/safe-browser.mjs');
await installBrowserIsolation(context);
const origin = process.env.EVENT_ORIGIN;
try {
  const pages = await Promise.all(Array.from({length:8}, () => context.newPage()));
  await Promise.all(pages.map(async page => {
    await page.goto(origin + '/__event-fixture');
    await page.waitForFunction(() => window.eventTransportReady);
  }));
  await Promise.all(pages.map(async (page, index) => {
    await page.evaluate(family => window.eventTransport.start({family, afterId:'show_evt_0'}), index < 4 ? 'workbench' : 'sdk');
    await page.waitForFunction(() => window.eventTransport.state().readyState === 1);
  }));
  for (const page of pages.slice(4)) {
    await page.waitForFunction(() => window.eventTransport.state().events.length === 1);
    const events = await page.evaluate(() => window.eventTransport.state().events);
    if (events[0].id !== 'show_evt_1' || !events[0].data.includes('真实事件 1 🐈')) throw Error('real persisted replay mismatch');
  }
  const timings = [];
  for (const page of pages) {
    timings.push(await page.evaluate(async () => {
      const start = performance.now();
      const r = await fetch('/api/session', {cache:'no-store'});
      const body = await r.json();
      if (!r.ok || body.event_stream_transport !== 'websocket-sse-v1') throw Error('capability request failed');
      return performance.now() - start;
    }));
  }
  if (Math.max(...timings) >= 1000) throw Error('ordinary request queued: ' + JSON.stringify(timings));
  await Promise.all(pages.map(page => page.close()));
  console.log(JSON.stringify({browser:kind, version:browser.version(), streams:8, workbench:4, sdk:4, fetchMilliseconds:timings, unicodeReplay:true}));
} finally { await context.close(); await browser.close(); }
'''
    try:
        while not server.started:
            await asyncio.sleep(0.01)
        process = await asyncio.create_subprocess_exec(
            "node", "--input-type=module", "-e", program,
            cwd=root, env={**os.environ, "EVENT_BROWSER": browser_name, "EVENT_ORIGIN": f"http://127.0.0.1:{port}"},
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), 60)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        assert process.returncode == 0, stderr.decode()
        result = json.loads(stdout.decode().strip().splitlines()[-1])
        print(json.dumps(result, ensure_ascii=False))
        for _ in range(100):
            if broker.subscriber_count() == baseline:
                break
            await asyncio.sleep(0.01)
        assert broker.subscriber_count() == baseline
    finally:
        server.should_exit = True
        await task
