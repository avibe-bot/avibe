"""Settings › Experimental › Source filter (docs/plans/vibey-source-filter-hooks.md).

The owner boundary is the gateway's hosted-tool call: a turn frozen with the experiment on runs web_search and
web_fetch between real hook scripts (real subprocesses in a test-owned AVIBE_HOME), and what the call answers is read
back by Vibey's own reader. Only the search vendor's HTTP adapter is faked, as in the search-chain tests.
"""

from __future__ import annotations

import asyncio
import json
import sys
import textwrap
from dataclasses import replace

import pytest

from config.v2_config import ModelHubHostedToolConfig
from core.handlers.model_hub import source_filter as source_filter_module
from core.handlers.model_hub.hosted_tools import freeze_turn_tools, hop_hosted_tools
from modules.agents.vibey.tools import _hosted_result
from tests.test_model_hub_hosted_tools_resolution import OPENAI, _config, _evidence, _spec_for
from tests.test_model_hub_search_chain import answers, owner, wire


@pytest.fixture
def hooks(tmp_path, monkeypatch):
    """The hook folder of a test-owned AVIBE_HOME; ``write(name, body)`` installs one script."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "avibe-home"))
    folder = tmp_path / "avibe-home" / "hooks" / "source_filter"
    folder.mkdir(parents=True)

    def write(name: str, body: str) -> None:
        (folder / f"{name}.py").write_text(
            "import json, sys\nreq = json.load(sys.stdin)\n" + textwrap.dedent(body), encoding="utf-8",
        )

    write.folder = folder
    return write


def filtered_owner(tmp_path):
    service = owner(tmp_path)
    config = service.store.config
    config.hosted_tools["web_search"] = replace(config.hosted_tools["web_search"], source_filter=True)
    return service


async def search(service, query="query"):
    turn = service.freeze_turn_hosted_tools(service.store.load())
    assert turn.source_filter
    return await service.run_hosted_tool("web_search", {"arguments": {"query": query}}, turn=turn)


def test_the_experiment_makes_every_web_search_reach_the_gateway(tmp_path):
    """A native-capable hop declares the function under the source filter, so every call passes the hooks."""

    evidence = _evidence(tmp_path)
    plain = freeze_turn_tools(_config(), evidence=evidence, spec_for=_spec_for)
    assert hop_hosted_tools(plain, OPENAI, "gpt-6-astra", evidence=evidence, spec_for=_spec_for)["web_search"]["native"] == "assumed"
    on = _config({"web_search": ModelHubHostedToolConfig(source_filter=True)})
    turn = freeze_turn_tools(on, evidence=evidence, spec_for=_spec_for)
    assert turn.source_filter and turn.prefer_native is False
    assert hop_hosted_tools(turn, OPENAI, "gpt-6-astra", evidence=evidence, spec_for=_spec_for)["web_search"] == {
        "native": "unsupported", "replay": "unsupported",
    }


@pytest.mark.parametrize("raw,expected", [(True, True), (False, False), ("yes", False)])
def test_the_switch_round_trips_and_a_damaged_value_is_off(raw, expected):
    setting = ModelHubHostedToolConfig.from_payload({"enabled": True, "source_filter": raw}, tool="web_search")
    assert setting.source_filter is expected
    assert ("source_filter" in setting.to_payload()) is expected


@pytest.mark.asyncio
async def test_without_scripts_the_call_is_unchanged(tmp_path, hooks, monkeypatch):
    service = filtered_owner(tmp_path)
    calls = answers(monkeypatch, service, wire())
    answer = await search(service)
    assert calls[0][4]["query"] == "query"
    assert [s["url"] for s in answer["record"]["sources"]] == ["https://example.test/a"]
    assert service.source_filter_status()["hooks"]["before_search"] == {"found": False, "last_failure": None}


@pytest.mark.asyncio
async def test_hooks_rewrite_the_query_and_own_the_results_while_the_facts_stay(tmp_path, hooks, monkeypatch):
    """before rewrites what is sent; after may add any source; attempts and cost stay what really ran."""

    hooks("before_search", """
        json.dump({"action": "replace", "args": {"query": req["args"]["query"] + " site:docs.example"}}, sys.stdout)
    """)
    hooks("after_search", """
        assert req["origin"] == "gateway" and req["output"]["ok"], req
        results = [{"url": "https://pinned.example/handbook", "title": "Pinned"}] + req["output"]["result"]["results"]
        json.dump({"action": "replace", "output": {"ok": True, "result": {"results": results}}}, sys.stdout)
    """)
    service = filtered_owner(tmp_path)
    calls = answers(monkeypatch, service, wire())
    answer = await search(service)
    assert calls[0][4]["query"] == "query site:docs.example"
    assert [s["url"] for s in answer["record"]["sources"]] == ["https://pinned.example/handbook", "https://example.test/a"]
    assert answer["record"]["attempts"][0]["outcome"] == "completed"
    assert answer["execution"]["cost"]["usd"] > 0
    hosted = _hosted_result("web_search", answer)
    assert hosted.record.sources[0].title == "Pinned"


@pytest.mark.asyncio
async def test_a_before_answer_sends_nothing_upstream_and_still_passes_after(tmp_path, hooks, monkeypatch):
    hooks("before_search", """
        json.dump({"action": "respond", "output": {"ok": True, "result": {"results": [{"url": "https://cache.example/x"}]}}}, sys.stdout)
    """)
    hooks("after_search", """
        assert req["origin"] == "before_hook", req
        json.dump({"action": "pass"}, sys.stdout)
    """)
    service = filtered_owner(tmp_path)
    calls = answers(monkeypatch, service)
    answer = await search(service)
    assert calls == []
    # Nothing upstream ran, so nothing is charged.
    assert answer["execution"]["executor_source_id"] == "src_sourcefilter" and answer["execution"]["cost"]["usd"] == 0
    assert [s.url for s in _hosted_result("web_search", answer).record.sources] == ["https://cache.example/x"]


@pytest.mark.asyncio
@pytest.mark.parametrize("script,code,ran_upstream", [
    ('json.dump({"action": "block", "reason": "not on my list"}, sys.stdout)', "source_filter_blocked", False),
    ("sys.exit(3)", "source_filter_failed", False),
    ('print("not json")', "source_filter_failed", False),
    ('json.dump({"action": "replace"}, sys.stdout)', "source_filter_failed", False),
    ('json.dump({"action": []}, sys.stdout)', "source_filter_failed", False),
])
async def test_a_blocking_or_broken_before_hook_stops_the_call(tmp_path, hooks, monkeypatch, script, code, ran_upstream):
    hooks("before_search", script)
    service = filtered_owner(tmp_path)
    calls = answers(monkeypatch, service, wire())
    answer = await search(service)
    assert (calls != []) is ran_upstream
    assert answer["record"]["status"] == "failed" and answer["record"]["error_code"] == code
    hosted = _hosted_result("web_search", answer)
    assert hosted.record.sources == ()
    failure = service.source_filter_status()["hooks"]["before_search"]["last_failure"]
    assert (failure is not None) is (code == "source_filter_failed")


@pytest.mark.asyncio
async def test_a_failing_after_hook_never_releases_the_unfiltered_output(tmp_path, hooks, monkeypatch):
    hooks("after_search", "raise RuntimeError('boom')")
    service = filtered_owner(tmp_path)
    answers(monkeypatch, service, wire())
    answer = await search(service)
    assert answer["record"]["error_code"] == "source_filter_failed"
    assert answer["record"]["sources"] == []
    # The upstream call happened and is still billed and listed.
    assert answer["execution"]["cost"]["usd"] > 0
    assert answer["record"]["attempts"][0]["outcome"] == "completed"
    hooks.folder.joinpath("after_search.py").unlink()
    # Looked up at the hook point: once removed, it is skipped and its failure is no longer shown.
    assert service.source_filter_status()["hooks"]["after_search"] == {"found": False, "last_failure": None}


@pytest.mark.asyncio
async def test_a_hook_that_runs_too_long_is_stopped_and_blocks(tmp_path, hooks, monkeypatch):
    hooks("before_search", "import time\ntime.sleep(30)")
    service = filtered_owner(tmp_path)
    service._source_filter_hooks.timeout = 0.5
    calls = answers(monkeypatch, service, wire())
    answer = await search(service)
    assert calls == []
    assert answer["record"]["error_code"] == "source_filter_failed"
    assert "timed out" in service.source_filter_status()["hooks"]["before_search"]["last_failure"]["reason"]


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
async def test_nothing_a_timed_out_hook_started_outlives_the_call(tmp_path, hooks, monkeypatch):
    marker = tmp_path / "late-write"
    hooks("before_search", f'''
        import subprocess, time
        subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1.5); open({str(marker)!r}, 'w').write('x')"])
        time.sleep(30)
    ''')
    service = filtered_owner(tmp_path)
    service._source_filter_hooks.timeout = 0.5
    answers(monkeypatch, service, wire())
    assert (await search(service))["record"]["error_code"] == "source_filter_failed"
    await asyncio.sleep(2.5)
    assert not marker.exists()


@pytest.mark.asyncio
async def test_after_fetch_may_rewrite_the_page_and_its_urls(tmp_path, hooks):
    """A fetch output with the script's own URL, final URL and content is a readable, bounded fetch record."""

    hooks("after_fetch", """
        result = {"url": "https://mirror.example/page", "final_url": "https://mirror.example/page?v=2",
                  "title": "Mirror", "content": "x" * 200000}
        json.dump({"action": "replace", "output": {"ok": True, "result": result}}, sys.stdout)
    """)

    async def run(arguments):
        return {"record": source_filter_module.fetch_record_for(
            {"ok": False, "error": {"code": "fetch_unavailable", "message": "down"}}, requested_url=arguments["url"],
        )}

    answer = await source_filter_module.filtered_call(
        source_filter_module.SourceFilterHooks(), "web_fetch", {"url": "https://origin.example/page"}, run,
        lambda key, **params: key,
    )
    record = _hosted_result("web_fetch", answer).record
    assert (record.status, record.requested_url, record.final_url, record.title) == (
        "completed", "https://mirror.example/page", "https://mirror.example/page?v=2", "Mirror",
    )
    assert record.truncated and record.content.startswith("xxx")


def test_the_settings_status_names_the_folder_and_each_script(tmp_path, hooks):
    hooks("after_fetch", 'json.dump({"action": "pass"}, sys.stdout)')
    status = source_filter_module.SourceFilterHooks().status()
    assert status["dir"] == str(hooks.folder)
    assert {name for name, hook in status["hooks"].items() if hook["found"]} == {"after_fetch"}


def test_a_hook_sees_none_of_avibes_secrets(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    env = source_filter_module.hook_env()
    assert "OPENAI_API_KEY" not in env and env["AVIBE_HOME"] == str(tmp_path)
    assert sys.executable
    assert json.dumps(env)
