"""Catalog ownership tests use only the autouse test-owned Avibe home."""

from __future__ import annotations

import asyncio
import gc
import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from config import paths
from modules.agents.codex.agent import CodexAgent, CodexModelHubCatalogUnavailableError
from modules.agents.codex.transport import CodexTransport
from vibe import backend_model_catalog as catalogs


def publish(slug):
    return catalogs._publish_codex_hub_catalog(json.dumps({"models": [{"slug": slug}]}).encode())


def retained():
    return set((paths.get_runtime_dir() / "model-hub" / "codex").glob("standard-responses-*.json"))


def churn(count=6):
    for index in range(count):
        with publish(f"churn-{index}"):
            pass
    gc.collect()


def agent():
    value = CodexAgent.__new__(CodexAgent)
    value._model_hub_catalog = None
    value._model_hub_catalog_lock = asyncio.Lock()
    value._model_hub_catalog_generation = 0
    value.codex_config = SimpleNamespace(binary="fixture-codex", extra_args=[])
    value.controller = SimpleNamespace()
    return value


def test_same_content_reuses_inode_and_each_reader_protects_it():
    first = publish("中文")
    path = first.path
    inode = path.stat().st_ino
    second = publish("中文")
    assert first.path == second.path
    assert second.path.stat().st_ino == inode
    first.close()
    churn()
    assert path in retained()
    assert len(retained() - {path}) <= catalogs.CODEX_HUB_CATALOG_HISTORY_LIMIT
    second.close()
    churn()
    assert len(retained()) <= catalogs.CODEX_HUB_CATALOG_HISTORY_LIMIT


def test_many_active_generations_are_separate_from_bounded_history():
    active = [publish(f"active-{index}") for index in range(4)]
    protected = {value.path for value in active}
    # Deliberately ancient timestamps cannot turn a live reader into history.
    for path in protected:
        os.utime(path, (1, 1))
    churn(20)
    assert protected <= retained()
    assert len(retained() - protected) <= catalogs.CODEX_HUB_CATALOG_HISTORY_LIMIT
    for value in active:
        value.close()
    active.clear()
    churn()
    assert len(retained()) <= catalogs.CODEX_HUB_CATALOG_HISTORY_LIMIT


def test_cleanup_preserves_foreign_names_symlinks_directories_and_hardlinks(tmp_path):
    active = publish("active")
    directory = active.path.parent
    foreign = directory / "user-catalog.json"
    foreign.write_text("user content")
    malformed = directory / "standard-responses-old.json"
    malformed.write_text("released name prefix is insufficient")
    subdir = directory / ("standard-responses-" + "a" * 16 + ".json")
    subdir.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("outside")
    symlink = directory / ("standard-responses-" + "b" * 16 + ".json")
    symlink.symlink_to(outside)
    hardlink = directory / ("standard-responses-" + "c" * 16 + ".json")
    os.link(outside, hardlink)
    churn()
    assert foreign.read_text() == "user content"
    assert malformed.is_file() and subdir.is_dir() and symlink.is_symlink()
    assert hardlink.read_text() == outside.read_text() == "outside"


def test_cleanup_failure_does_not_break_publication_or_existing_reader(monkeypatch, caplog):
    active = publish("active")
    path = active.path
    real_unlink = Path.unlink

    def denied(value, *args, **kwargs):
        if catalogs._CODEX_HUB_CATALOG_NAME.fullmatch(value.name):
            raise PermissionError("fixture cleanup denial")
        return real_unlink(value, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", denied)
        churn()
        latest = publish("new-success")
        assert path.is_file()
        assert json.loads(latest.path.read_bytes())["models"][0]["slug"] == "new-success"
        assert "fixture cleanup denial" in caplog.text
        assert "Could not reclaim Codex Model Hub catalog" in caplog.text
    latest.close()
    churn()
    assert len(retained() - {path}) <= 1


def test_failed_publication_or_export_cannot_select_old_catalog(monkeypatch):
    active = publish("old")
    monkeypatch.setattr(catalogs, "write_atomic", lambda *_: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        publish("new")
    assert active.path.is_file()
    monkeypatch.setattr(
        catalogs, "_export_codex_bundled_catalog",
        lambda *_: (_ for _ in ()).throw(RuntimeError("export failed")),
    )
    with pytest.raises(RuntimeError, match="export failed"):
        catalogs.prepare_codex_hub_catalog("fixture-codex")
    assert retained() == {active.path}


def test_concurrent_publication_and_cleanup_preserve_every_returned_pin():
    barrier = threading.Barrier(8)

    def launch(index):
        barrier.wait(timeout=5)
        value = publish(f"concurrent-{index % 3}")
        catalogs.prune_codex_hub_catalogs(value.path.parent)
        assert value.path.is_file()
        return value

    with ThreadPoolExecutor(max_workers=8) as workers:
        values = list(workers.map(launch, range(8)))
    protected = {value.path for value in values}
    churn()
    assert protected <= retained()
    for value in values:
        value.close()
    values.clear()
    gc.collect()
    churn()
    assert len(retained()) <= 1


def test_cleaner_cannot_enter_between_file_publication_and_pin(monkeypatch):
    written = threading.Event()
    finish = threading.Event()
    write = catalogs.write_atomic
    published_paths = []

    def paused_write(path, data):
        write(path, data)
        published_paths.append(path)
        written.set()
        assert finish.wait(timeout=5)

    monkeypatch.setattr(catalogs, "write_atomic", paused_write)
    with ThreadPoolExecutor(max_workers=1) as worker:
        preparation = worker.submit(publish, "paused-publication")
        try:
            assert written.wait(timeout=2)
            path = published_paths[0]
            catalogs.prune_codex_hub_catalogs(path.parent)
            assert path.is_file()
        finally:
            finish.set()
        catalog = preparation.result(timeout=2)
    monkeypatch.setattr(catalogs, "write_atomic", write)
    churn()
    assert catalog.path in retained()
    catalog.close()
    churn()
    assert len(retained()) <= 1


def test_separate_process_cannot_clean_starting_or_cached_generation():
    active = publish("parent")
    code = (
        "from vibe.backend_model_catalog import _publish_codex_hub_catalog\n"
        "for i in range(8):\n"
        " with _publish_codex_hub_catalog(('"
        '{"models":[{"slug":"child-%s"}]}' + "' % i).encode()): pass\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=15,
        env=os.environ.copy(),
    )
    assert result.returncode == 0, result.stderr
    assert active.path in retained()
    assert len(retained() - {active.path}) <= 1


@pytest.mark.asyncio
async def test_cache_invalidation_during_pending_launch_keeps_local_reference(monkeypatch):
    value = agent()
    monkeypatch.setattr(catalogs, "_export_codex_bundled_catalog", lambda *_: b'{"models":[{"slug":"first"}]}')
    starting = (await value.prepare_model_hub_runtime()).retain()
    path = starting.path
    await value.invalidate_model_hub_runtime()
    churn()
    assert value._model_hub_catalog is None and path in retained()
    starting.close()
    churn()
    assert len(retained()) <= 1


@pytest.mark.asyncio
async def test_invalidated_export_releases_unpublished_pin(monkeypatch):
    value = agent()
    entered = threading.Event()
    released = threading.Event()

    def export(*_):
        entered.set()
        assert released.wait(timeout=5)
        return b'{"models":[{"slug":"raced"}]}'

    monkeypatch.setattr(catalogs, "_export_codex_bundled_catalog", export)
    preparation = asyncio.create_task(value.prepare_model_hub_runtime())
    assert await asyncio.to_thread(entered.wait, 2)
    await value.invalidate_model_hub_runtime()
    released.set()
    with pytest.raises(CodexModelHubCatalogUnavailableError, match="generation changed"):
        await preparation
    assert value._model_hub_catalog is None
    del preparation
    churn()
    assert len(retained()) <= 1


@pytest.mark.asyncio
async def test_cancelled_export_closes_its_eventual_result(monkeypatch):
    value = agent()
    entered = threading.Event()
    finish = threading.Event()
    closed = asyncio.Event()
    loop = asyncio.get_running_loop()
    release = catalogs._release_codex_hub_catalog

    def export(*_):
        entered.set()
        assert finish.wait(timeout=5)
        return b'{"models":[{"slug":"cancelled"}]}'

    def observe_close(path, descriptor):
        release(path, descriptor)
        loop.call_soon_threadsafe(closed.set)

    monkeypatch.setattr(catalogs, "_export_codex_bundled_catalog", export)
    monkeypatch.setattr(catalogs, "_release_codex_hub_catalog", observe_close)
    preparation = asyncio.create_task(value.prepare_model_hub_runtime())
    assert await asyncio.to_thread(entered.wait, 2)
    preparation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await preparation
    finish.set()
    await asyncio.wait_for(closed.wait(), 5)
    assert value._model_hub_catalog is None
    churn()
    assert len(retained()) <= 1


_STDIO_CONSUMER = """
import json, os, sys
for line in sys.stdin:
    request = json.loads(line)
    if request.get("method") == "test/exit":
        break
    if "id" in request:
        print(json.dumps({"id": request["id"], "result": {}}), flush=True)
    if request.get("method") == "test/closeStdout":
        os.close(1)
"""


@pytest.mark.asyncio
async def test_agent_launch_keeps_pin_across_invalidation_and_reuses_transport(monkeypatch, tmp_path):
    value = agent()
    value._transports = {}
    value._transport_locks = {}
    value._transport_cwd_inodes = {}
    initialized = asyncio.Event()
    finish = asyncio.Event()
    spawn = asyncio.create_subprocess_exec
    argv = []
    monkeypatch.setattr(catalogs, "_export_codex_bundled_catalog", lambda *_: b'{"models":[{"slug":"agent"}]}')

    async def fixture_spawn(*args, **kwargs):
        argv.extend(args)
        return await spawn(sys.executable, "-u", "-c", _STDIO_CONSUMER, **kwargs)

    async def pause_governor_start(self):
        await original_start(self)
        initialized.set()
        await finish.wait()

    original_start = CodexTransport.start
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fixture_spawn)
    monkeypatch.setattr(CodexTransport, "start", pause_governor_start)
    # An extra argument must not override the path whose lifetime we protect.
    value.codex_config.extra_args = ["-c", 'model_catalog_json="/obsolete/catalog.json"']
    launch = SimpleNamespace(
        channel="hub", fingerprint="hub:test",
        gateway_base_url="http://127.0.0.1:1", gateway_token="fixture-only",
    )
    starting = asyncio.create_task(value._get_or_create_transport(str(tmp_path), launch))
    try:
        await asyncio.wait_for(initialized.wait(), 5)
        path = value._model_hub_catalog.path
        assert argv[-1] == f"model_catalog_json={json.dumps(str(path))}"
        await value.invalidate_model_hub_runtime()
        churn()
        assert path in retained()
        finish.set()
        transport = await starting
        assert (await value._get_or_create_transport(str(tmp_path), launch)) is transport
        assert path in retained()
        await transport.stop()
        await transport._catalog_exit_task
        churn()
        assert len(retained()) <= 1
    finally:
        finish.set()
        transport = await starting
        await transport.stop()


@pytest.mark.asyncio
async def test_stdout_eof_keeps_pin_until_real_process_exit(monkeypatch, tmp_path):
    spawn = asyncio.create_subprocess_exec

    async def fixture_spawn(*_args, **kwargs):
        return await spawn(sys.executable, "-u", "-c", _STDIO_CONSUMER, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fixture_spawn)
    catalog = publish("stdout-live")
    path = catalog.path
    transport = CodexTransport("fixture", str(tmp_path), model_hub_catalog=catalog)
    catalog.close()
    try:
        await transport.start()
        await transport.send_request("test/closeStdout", {})
        await asyncio.wait_for(transport.wait_closed(), 5)
        assert transport._process.returncode is None
        churn()
        assert path.is_file()
        transport._process.stdin.close()
        await asyncio.wait_for(transport._catalog_exit_task, 5)
        assert transport._process.returncode == 0
        assert transport._model_hub_catalog is None
        churn()
        assert len(retained()) <= 1
    finally:
        await transport.stop()


@pytest.mark.asyncio
async def test_cancel_during_real_spawn_keeps_pin_until_child_is_reaped(monkeypatch, tmp_path):
    spawn = asyncio.create_subprocess_exec
    created = asyncio.Event()
    finish_spawn = asyncio.Event()
    processes = []

    async def fixture_spawn(*_args, **kwargs):
        proc = await spawn(sys.executable, "-u", "-c", _STDIO_CONSUMER, **kwargs)
        processes.append(proc)
        created.set()
        await finish_spawn.wait()
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fixture_spawn)
    catalog = publish("cancel-spawn")
    path = catalog.path
    transport = CodexTransport("fixture", str(tmp_path), model_hub_catalog=catalog)
    catalog.close()
    task = asyncio.create_task(transport.start())
    try:
        await asyncio.wait_for(created.wait(), 5)
        task.cancel()
        churn()
        assert processes[0].returncode is None and path.is_file()
        finish_spawn.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert processes[0].returncode == 0
        assert transport._model_hub_catalog is None
        churn()
        assert len(retained()) <= 1
    finally:
        finish_spawn.set()
        await transport.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["spawn", "handshake", "cancel-handshake"])
async def test_failed_start_releases_transport_reference(monkeypatch, tmp_path, failure):
    catalog = publish(failure)
    path = catalog.path
    transport = CodexTransport("fixture-codex", str(tmp_path), model_hub_catalog=catalog)
    catalog.close()
    exited = asyncio.Event()
    entered = asyncio.Event()
    process = SimpleNamespace(pid=123, returncode=None, stdin=None, stdout=None, stderr=None)

    async def wait():
        await exited.wait()
        return 1

    async def spawn(*_, **__):
        if failure == "spawn":
            raise OSError("fixture spawn failed")
        return process

    async def handshake(*_):
        entered.set()
        if failure == "cancel-handshake":
            await asyncio.Event().wait()
        raise RuntimeError("fixture handshake failed")

    async def stop():
        process.returncode = 1
        exited.set()
        transport._release_model_hub_catalog()
        transport._cleanup_tasks()

    process.wait = wait
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(transport, "send_request", handshake)
    monkeypatch.setattr(transport, "stop", stop)
    monkeypatch.setattr(transport, "_reader_loop", AsyncMock())
    monkeypatch.setattr(transport, "_stderr_reader", AsyncMock())
    monkeypatch.setattr("modules.agents.codex.transport.process_identity", lambda *_: {})
    monkeypatch.setattr("modules.agents.codex.transport.log_process_snapshot", lambda *_args, **_kw: None)
    task = asyncio.create_task(transport.start())
    if failure == "cancel-handshake":
        await entered.wait()
        churn()
        assert path.is_file()
        task.cancel()
    with pytest.raises((OSError, RuntimeError, asyncio.CancelledError)):
        await task
    if transport._catalog_exit_task is not None:
        await transport._catalog_exit_task
    assert transport._model_hub_catalog is None
    del task
    churn()
    assert len(retained()) <= 1
