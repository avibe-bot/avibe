"""Citation handling in the Codex backend: source capture and emit-time resolution.

The grammar itself is covered by ``tests/test_citations.py``. What is specific to
this backend, and asserted here, is *where* a ``ref_id`` is allowed to mean
something: a ref is only unique inside its own native Codex thread, arrives in a
notification separate from the answer that cites it, and survives the ways a
conversation loses its live stream - a restarted process, a resume, a re-read, a
native fork - by being read back from the history Codex already recorded.

Every case runs against an isolated ``CODEX_HOME``. Hydration is part of the
path under test, so a case that did not build its own rollout fixture must see
an empty one rather than the developer's real Codex state.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.reply_enhancer import strip_silent_blocks
from modules.agents.codex import search_history
from modules.agents.codex.event_handler import (
    _MAX_CITATION_SOURCES_PER_THREAD,
    _MAX_CITATION_THREADS,
)
# The handler harness loads ``event_handler.py`` under its own module name, so a
# patch aimed at the real import path would miss the copy under test.
from tests.test_codex_event_handler import _MODULE as codex_event_handler
from tests.test_codex_event_handler import CodexEventHandler, _StubAgent

START, SEP, END = "\ue200", "\ue202", "\ue201"
UNRESOLVED = "(source unavailable)"

GUIDE_URL = "https://developers.openai.com/api/docs/guides/tools-web-search"
PROBE_URL = "https://example.com/citation-probe-source"


def marker(*ref_ids: str) -> str:
    return f"{START}cite{SEP}{SEP.join(ref_ids)}{END}"


def cached_sources(handler, thread_id: str) -> dict:
    """The sources one thread's cache entry currently holds.

    Reads the handler's own bookkeeping, which carries a thread's readiness
    alongside its sources, so a test about the bound does not have to know which
    of the two it is looking at.
    """
    entry = handler._search_sources_by_thread.get(thread_id)
    if entry is None:
        return {}
    return dict(getattr(entry, "sources", entry))


class IsolatedCodexHome:
    """Bind ``CODEX_HOME`` to an empty directory for the duration of one test."""

    def isolate_codex_home(self) -> Path:
        home = Path(tempfile.mkdtemp(prefix="avibe-codex-home-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(home, ignore_errors=True))
        patcher = mock.patch.dict(os.environ, {"CODEX_HOME": str(home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        return home

    def recorded_search(self, *results: dict, owner: str, turn: str = "turn-0") -> dict:
        """One ``item_completed`` row exactly as a rollout file records it.

        ``owner`` is the ``payload.thread_id`` the row carries, which is the
        *parent's* id for the rows a fork inherits - the reason scope is the
        file and not this field.
        """
        return {
            "timestamp": "2026-09-21T00:00:00.000Z",
            "ordinal": 1,
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "thread_id": owner,
                "turn_id": turn,
                "item": {
                    "type": "WebSearch",
                    "id": f"ws_{turn}",
                    "query": "native web search",
                    "action": {"type": "search", "queries": ["native web search"]},
                    "results": list(results),
                },
                "completed_at_ms": 1,
            },
        }

    def recorded_extension_search(
        self, *results: dict, owner: str, turn: str = "turn-0"
    ) -> dict:
        """The same row as the shipped binary writes it today.

        The native search tool lives in an extension namespace now, so a current
        rollout records its completed item as ``Extension`` carrying ``kind:
        "web.search"`` rather than naming the item ``WebSearch``. Both forms are
        in the local corpus (1023 old-form rows with results against 267 new-form
        ones), and every row codex-cli 0.154.0 writes is this one - captured from
        an isolated native turn against a loopback provider, not invented here.
        """
        row = self.recorded_search(*results, owner=owner, turn=turn)
        item = row["payload"]["item"]
        row["payload"]["item"] = {
            "type": "Extension",
            "kind": "web.search",
            "id": item["id"],
            "query": item["query"],
            "action": item["action"],
            "results": item["results"],
        }
        return row

    def record_history(
        self,
        home: Path,
        thread_id: str,
        rows: list,
        *,
        trailing: str = "",
    ) -> Path:
        """Write a rollout file for *thread_id* and index it the way Codex does."""
        sessions = home / "sessions" / "2026" / "09" / "21"
        sessions.mkdir(parents=True, exist_ok=True)
        path = sessions / f"rollout-2026-09-21T00-00-00-{thread_id}.jsonl"
        body = "".join(f"{json.dumps(row)}\n" for row in rows) + trailing
        path.write_text(body, encoding="utf-8")
        self.index_history(home, thread_id, str(path))
        return path

    def index_history(self, home: Path, thread_id: str, rollout_path: str) -> None:
        connection = sqlite3.connect(home / "state_5.sqlite")
        with connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS threads (id TEXT PRIMARY KEY, rollout_path TEXT)"
            )
            connection.execute(
                "INSERT OR REPLACE INTO threads (id, rollout_path) VALUES (?, ?)",
                (thread_id, rollout_path),
            )
        connection.close()

    def web_result(self, ref_id: str, url: str, title: str = "T") -> dict:
        """One ``results[]`` entry, keeping the fields the native tool sends."""
        return {
            "type": "text_result",
            "thumbnail_url": "https://images.example.com/thumb.png",
            "domain": "example.com",
            "ref_id": ref_id,
            "snippet": "snippet",
            "title": title,
            "url": url,
        }


def web_search(thread_id: str, *results: dict, turn_id: str = "turn-1") -> dict:
    """An ``item/completed`` notification for a finished web search."""
    return {
        "threadId": thread_id,
        "turnId": turn_id,
        "item": {"type": "webSearch", "query": "native web search", "results": list(results)},
    }


def agent_message(thread_id: str, text: str, turn_id: str = "turn-1") -> dict:
    return {
        "threadId": thread_id,
        "turnId": turn_id,
        "item": {"type": "agentMessage", "text": text},
    }


def turn_completed(thread_id: str, turn_id: str = "turn-1") -> dict:
    return {"threadId": thread_id, "turn": {"id": turn_id, "status": "completed"}}


def _request(session_id: str = "session-1"):
    return SimpleNamespace(
        base_session_id=session_id,
        session_key=f"slack::channel::{session_id}",
        working_path="/repo",
        context=SimpleNamespace(platform_specific={}),
        started_at=0,
    )


class CodexCitationCaptureTests(IsolatedCodexHome, unittest.IsolatedAsyncioTestCase):
    """``_record_search_sources``: what gets into the cache, and under which key."""

    def setUp(self):
        self.isolate_codex_home()
        self.handler = CodexEventHandler(_StubAgent())

    def sources(self, thread_id: str) -> dict:
        return cached_sources(self.handler, thread_id)

    def test_a_completed_search_is_stored_under_its_thread(self):
        params = web_search(
            "thread-a",
            {"ref_id": "turn0view0", "title": "Web search - OpenAI API", "url": GUIDE_URL},
        )

        self.handler._record_search_sources(params, params["item"])

        source = self.sources("thread-a")["turn0view0"]
        self.assertEqual((source.ref_id, source.title, source.url), ("turn0view0", "Web search - OpenAI API", GUIDE_URL))

    def test_the_camel_case_alias_is_accepted(self):
        """The app-server camel-cases most fields; ``ref_id`` is the observed one."""
        params = web_search("thread-a", {"refId": "turn0view0", "title": "T", "url": GUIDE_URL})

        self.handler._record_search_sources(params, params["item"])

        self.assertIn("turn0view0", self.sources("thread-a"))

    async def test_a_ref_id_means_nothing_outside_its_own_thread(self):
        """Two threads both start at ``turn0view0``; neither may see the other's."""
        for thread_id, url in (("thread-a", GUIDE_URL), ("thread-b", PROBE_URL)):
            params = web_search(thread_id, {"ref_id": "turn0view0", "title": "T", "url": url})
            self.handler._record_search_sources(params, params["item"])

        self.assertEqual(self.sources("thread-a")["turn0view0"].url, GUIDE_URL)
        self.assertEqual(self.sources("thread-b")["turn0view0"].url, PROBE_URL)

        text, citations = await self.handler._resolve_citations(
            f"Cited.{marker('turn0view0')}", {"threadId": "thread-b"}, _request()
        )
        self.assertEqual(text, f"Cited. [example.com]({PROBE_URL})")
        self.assertEqual([c["url"] for c in citations], [PROBE_URL])

    async def test_a_thread_that_never_searched_resolves_nothing(self):
        params = web_search("thread-a", {"ref_id": "turn0view0", "title": "T", "url": GUIDE_URL})
        self.handler._record_search_sources(params, params["item"])

        text, citations = await self.handler._resolve_citations(
            f"Cited.{marker('turn0view0')}", {"threadId": "thread-zzz"}, _request()
        )

        self.assertEqual(text, f"Cited. {UNRESOLVED}")
        self.assertIsNone(citations)

    def test_a_later_search_result_supersedes_the_same_ref_id(self):
        for url in (GUIDE_URL, PROBE_URL):
            params = web_search("thread-a", {"ref_id": "turn0view0", "title": "T", "url": url})
            self.handler._record_search_sources(params, params["item"])

        self.assertEqual(self.sources("thread-a")["turn0view0"].url, PROBE_URL)

    def test_unusable_results_are_skipped_rather_than_stored_partially(self):
        params = web_search(
            "thread-a",
            {"ref_id": "turn0view0", "url": GUIDE_URL},  # keeps: no title is fine
            {"ref_id": "no-url"},
            {"ref_id": "blank-url", "url": ""},
            {"ref_id": "", "url": GUIDE_URL},
            {"ref_id": 7, "url": GUIDE_URL},
            {"ref_id": "bad-url-type", "url": ["https://example.com"]},
            "not-a-dict",
            None,
        )

        self.handler._record_search_sources(params, params["item"])

        self.assertEqual(list(self.sources("thread-a")), ["turn0view0"])
        self.assertEqual(self.sources("thread-a")["turn0view0"].title, "")

    def test_a_notification_without_a_thread_or_a_result_list_is_ignored(self):
        for params in (
            {"item": {"type": "webSearch", "results": [{"ref_id": "r", "url": GUIDE_URL}]}},
            web_search("thread-a"),
            {"threadId": "thread-a", "item": {"type": "webSearch"}},
            {"threadId": "thread-a", "item": {"type": "webSearch", "results": "nope"}},
        ):
            self.handler._record_search_sources(params, params["item"])

        self.assertEqual(self.handler._search_sources_by_thread, {})

    def test_sources_are_evicted_oldest_first_within_a_thread(self):
        limit = _MAX_CITATION_SOURCES_PER_THREAD
        params = web_search(
            "thread-a",
            *[
                {"ref_id": f"turn0view{i}", "title": "T", "url": f"https://example.com/{i}"}
                for i in range(limit + 5)
            ],
        )

        self.handler._record_search_sources(params, params["item"])

        stored = list(self.sources("thread-a"))
        self.assertEqual(len(stored), limit)
        self.assertNotIn("turn0view0", stored)
        self.assertIn(f"turn0view{limit + 4}", stored)

    def test_a_reused_source_is_kept_over_an_untouched_one(self):
        """LRU, so the ref a long conversation keeps citing survives eviction."""
        for i in range(3):
            params = web_search("thread-a", {"ref_id": f"r{i}", "title": "T", "url": f"https://e.com/{i}"})
            self.handler._record_search_sources(params, params["item"])
        refreshed = web_search("thread-a", {"ref_id": "r0", "title": "T", "url": "https://e.com/0"})
        self.handler._record_search_sources(refreshed, refreshed["item"])

        self.assertEqual(list(self.sources("thread-a")), ["r1", "r2", "r0"])

    def test_threads_are_evicted_oldest_first(self):
        limit = _MAX_CITATION_THREADS
        for i in range(limit + 2):
            params = web_search(f"thread-{i}", {"ref_id": "turn0view0", "title": "T", "url": GUIDE_URL})
            self.handler._record_search_sources(params, params["item"])

        threads = list(self.handler._search_sources_by_thread)
        self.assertEqual(len(threads), limit)
        self.assertNotIn("thread-0", threads)
        self.assertIn(f"thread-{limit + 1}", threads)

    def test_an_active_thread_outlives_an_idle_one(self):
        for i in range(3):
            params = web_search(f"thread-{i}", {"ref_id": "r", "title": "T", "url": GUIDE_URL})
            self.handler._record_search_sources(params, params["item"])
        again = web_search("thread-0", {"ref_id": "r2", "title": "T", "url": PROBE_URL})
        self.handler._record_search_sources(again, again["item"])

        self.assertEqual(list(self.handler._search_sources_by_thread), ["thread-1", "thread-2", "thread-0"])


class CodexCitationResolutionTests(IsolatedCodexHome, unittest.IsolatedAsyncioTestCase):
    """The product path: notifications in, delivered message plus sidecar out."""

    def setUp(self):
        self.isolate_codex_home()
        self.agent = _StubAgent()
        self.handler = CodexEventHandler(self.agent)
        self.request = _request()
        self.agent._turn_registry.register_turn("turn-1", self.request)

    def result_call(self):
        self.agent.emit_result_message.assert_awaited()
        return self.agent.emit_result_message.await_args

    async def test_a_search_that_lands_after_the_answer_still_resolves_it(self):
        """Resolution is deferred to emit time precisely for this ordering."""
        await self.handler._on_item_completed(
            agent_message("thread-a", f"Native search exists.{marker('turn0view0')}"), self.request
        )
        await self.handler._on_item_completed(
            web_search("thread-a", {"ref_id": "turn0view0", "title": "Web search - OpenAI API", "url": GUIDE_URL}),
            self.request,
        )
        await self.handler._on_turn_completed(turn_completed("thread-a"), self.request)

        call = self.result_call()
        self.assertEqual(call.args[1], f"Native search exists. [developers.openai.com]({GUIDE_URL})")
        self.assertEqual(
            call.kwargs["citations"],
            [
                {
                    "index": 1,
                    "ref_id": "turn0view0",
                    "title": "Web search - OpenAI API",
                    "url": GUIDE_URL,
                    "label": "developers.openai.com",
                }
            ],
        )

    async def test_a_search_from_an_earlier_turn_is_still_citable(self):
        """Thread-scoped, not turn-scoped: the cache outlives the turn that filled it."""
        await self.handler._on_item_completed(
            web_search("thread-a", {"ref_id": "turn0view0", "title": "T", "url": GUIDE_URL}),
            self.request,
        )
        await self.handler._on_turn_completed(turn_completed("thread-a"), self.request)
        self.agent.emit_result_message.reset_mock()

        later = _request()
        self.agent._turn_registry.register_turn("turn-2", later)
        await self.handler._on_item_completed(
            agent_message("thread-a", f"As established.{marker('turn0view0')}", turn_id="turn-2"), later
        )
        await self.handler._on_turn_completed(turn_completed("thread-a", turn_id="turn-2"), later)

        self.assertEqual(self.result_call().args[1], f"As established. [developers.openai.com]({GUIDE_URL})")

    async def test_an_answer_without_markers_carries_no_sidecar(self):
        await self.handler._on_item_completed(agent_message("thread-a", "Plain answer."), self.request)
        await self.handler._on_turn_completed(turn_completed("thread-a"), self.request)

        call = self.result_call()
        self.assertEqual(call.args[1], "Plain answer.")
        self.assertIsNone(call.kwargs["citations"])

    async def test_an_unresolvable_ref_is_labelled_and_carries_no_sidecar(self):
        await self.handler._on_item_completed(
            agent_message("thread-a", f"Claimed.{marker('turn9view9')}"), self.request
        )
        await self.handler._on_turn_completed(turn_completed("thread-a"), self.request)

        call = self.result_call()
        self.assertEqual(call.args[1], f"Claimed. {UNRESOLVED}")
        self.assertIsNone(call.kwargs["citations"])

    async def test_a_repeated_completion_does_not_duplicate_the_links(self):
        await self.handler._on_item_completed(
            web_search("thread-a", {"ref_id": "turn0view0", "title": "T", "url": GUIDE_URL}),
            self.request,
        )
        await self.handler._on_item_completed(
            agent_message("thread-a", f"Cited.{marker('turn0view0')}"), self.request
        )
        await self.handler._on_turn_completed(turn_completed("thread-a"), self.request)
        await self.handler._on_turn_completed(turn_completed("thread-a"), self.request)

        self.assertEqual(self.agent.emit_result_message.await_count, 1)
        self.assertEqual(self.result_call().args[1].count(f"[developers.openai.com]({GUIDE_URL})"), 1)

    async def test_an_intermediate_message_is_flushed_with_its_own_sidecar(self):
        await self.handler._on_item_completed(
            web_search("thread-a", {"ref_id": "turn0view0", "title": "T", "url": GUIDE_URL}),
            self.request,
        )
        await self.handler._on_item_completed(
            agent_message("thread-a", f"Progress.{marker('turn0view0')}"), self.request
        )
        await self.handler._on_item_completed(agent_message("thread-a", "Final answer."), self.request)

        self.agent.controller.emit_agent_message.assert_awaited_once()
        call = self.agent.controller.emit_agent_message.await_args
        self.assertEqual(call.args[2], f"Progress. [developers.openai.com]({GUIDE_URL})")
        self.assertEqual([c["url"] for c in call.kwargs["citations"]], [GUIDE_URL])

    async def test_an_intermediate_message_without_citations_keeps_its_original_call(self):
        """No empty sidecar kwarg: an uncited flush is the call it always was."""
        await self.handler._on_item_completed(agent_message("thread-a", "Progress."), self.request)
        await self.handler._on_item_completed(agent_message("thread-a", "Final answer."), self.request)

        self.agent.controller.emit_agent_message.assert_awaited_once_with(
            self.request.context, "assistant", "Progress.", parse_mode="markdown"
        )

    async def test_a_generated_image_appended_after_resolution_keeps_both(self):
        """Citations are resolved before the image footer is appended, not instead of it."""
        await self.handler._on_item_completed(
            web_search("thread-a", {"ref_id": "turn0view0", "title": "T", "url": GUIDE_URL}),
            self.request,
        )
        await self.handler._on_item_completed(
            agent_message("thread-a", f"Cited.{marker('turn0view0')}"), self.request
        )
        self.handler._append_generated_images = lambda text, params, request: f"{text}\n\n![img](a.png)"

        await self.handler._on_turn_completed(turn_completed("thread-a"), self.request)

        call = self.result_call()
        self.assertEqual(call.args[1], f"Cited. [developers.openai.com]({GUIDE_URL})\n\n![img](a.png)")
        self.assertEqual(len(call.kwargs["citations"]), 1)

    async def test_an_untrusted_source_cannot_inject_a_link(self):
        """Titles and URLs come from the open web; the sidecar must stay clean."""
        await self.handler._on_item_completed(
            web_search(
                "thread-a",
                {"ref_id": "turn0view0", "title": "Bad​actor\n\n", "url": "javascript:alert(1)"},
                {"ref_id": "turn0view1", "title": "中文標題", "url": PROBE_URL},
            ),
            self.request,
        )
        await self.handler._on_item_completed(
            agent_message("thread-a", f"Two.{marker('turn0view0', 'turn0view1')}"), self.request
        )
        await self.handler._on_turn_completed(turn_completed("thread-a"), self.request)

        call = self.result_call()
        self.assertEqual(call.args[1], f"Two. [example.com]({PROBE_URL}) {UNRESOLVED}")
        self.assertEqual(
            call.kwargs["citations"],
            [{"index": 1, "ref_id": "turn0view1", "title": "中文標題", "url": PROBE_URL, "label": "example.com"}],
        )

    async def test_a_hidden_block_neither_numbers_nor_leaks_its_source(self):
        """``<silent>`` content is removed downstream, so it may not be attributed.

        Numbering a hidden marker would open the reader's sidecar at index 2 and
        describe a badge whose link was stripped with the block.
        """
        await self.handler._on_item_completed(
            web_search(
                "thread-a",
                {"ref_id": "turn0view0", "title": "T", "url": GUIDE_URL},
                {"ref_id": "turn0view1", "title": "T", "url": PROBE_URL},
            ),
            self.request,
        )
        await self.handler._on_item_completed(
            agent_message(
                "thread-a",
                f"<silent>internal{marker('turn0view0')}</silent>"
                f"Visible.{marker('turn0view1')}",
            ),
            self.request,
        )
        await self.handler._on_turn_completed(turn_completed("thread-a"), self.request)

        call = self.result_call()
        self.assertEqual(
            [(c["index"], c["url"]) for c in call.kwargs["citations"]], [(1, PROBE_URL)]
        )
        self.assertEqual(
            strip_silent_blocks(call.args[1]), f"Visible. [example.com]({PROBE_URL})"
        )

    async def test_the_unresolved_label_is_localized(self):
        self.agent.controller._t = lambda key: "（来源不可用）" if key == "message.citationUnresolved" else key

        await self.handler._on_item_completed(
            agent_message("thread-a", f"Claimed.{marker('turn9view9')}"), self.request
        )
        await self.handler._on_turn_completed(turn_completed("thread-a"), self.request)

        self.assertEqual(self.result_call().args[1], "Claimed. （来源不可用）")

    async def test_an_empty_answer_still_completes_the_turn(self):
        self.agent.emit_result_message = AsyncMock()

        await self.handler._on_turn_completed(turn_completed("thread-a"), self.request)

        call = self.result_call()
        self.assertIsNone(call.args[1])
        self.assertIsNone(call.kwargs["citations"])


class CodexCitationReadinessTests(IsolatedCodexHome, unittest.IsolatedAsyncioTestCase):
    """When a narration message that cites something may be delivered.

    A search and the answer that cites it are two notifications, and nothing in
    the transport promises the search arrives first. An intermediate message is
    therefore held while a ref it names has no source yet - and only then. The
    hold ends at the first terminal boundary the turn reaches, so it is bounded
    by the turn rather than by a guess about ordering; it never reorders the
    narration around it, and a message that cites nothing is never delayed by
    anything except messages already ahead of it.
    """

    def setUp(self):
        self.isolate_codex_home()
        self.agent = _StubAgent()
        self.handler = CodexEventHandler(self.agent)
        self.request = _request()
        self.agent._turn_registry.register_turn("turn-1", self.request)
        self.emitted = self.agent.controller.emit_agent_message
        # One list for both surfaces: several cases below are about the order a
        # reader sees, which per-mock await lists cannot express.
        self.delivered: list[str] = []
        self.emitted.side_effect = lambda *a, **k: self.delivered.append(a[2])
        self.agent.emit_result_message.side_effect = lambda *a, **k: self.delivered.append(a[1])

    async def item(self, params: dict) -> None:
        await self.handler._on_item_completed(params, self.request)

    def guide_search(self, ref_id: str = "turn0view0") -> dict:
        return web_search("thread-a", {"ref_id": ref_id, "title": "T", "url": GUIDE_URL})

    async def test_a_cited_narration_waits_for_the_search_that_defines_it(self):
        """Review C's sequence: the citing message is flushed before the search."""
        await self.item(agent_message("thread-a", f"Progress.{marker('turn0view0')}"))
        await self.item(agent_message("thread-a", "Final answer."))

        self.assertEqual(self.delivered, [])

        await self.item(self.guide_search())

        self.assertEqual(self.delivered, [f"Progress. [developers.openai.com]({GUIDE_URL})"])
        self.assertEqual([c["url"] for c in self.emitted.await_args.kwargs["citations"]], [GUIDE_URL])

        await self.handler._on_turn_completed(turn_completed("thread-a"), self.request)

        self.assertEqual(self.delivered[-1], "Final answer.")

    async def test_a_held_narration_is_delivered_when_the_turn_completes(self):
        """The wait is bounded by the turn: a ref that never arrives still ships."""
        await self.item(agent_message("thread-a", f"Progress.{marker('turn9view9')}"))
        await self.item(agent_message("thread-a", "Final answer."))
        self.assertEqual(self.delivered, [])

        await self.handler._on_turn_completed(turn_completed("thread-a"), self.request)

        self.assertEqual(self.delivered, [f"Progress. {UNRESOLVED}", "Final answer."])

    async def test_an_interrupted_turn_still_delivers_what_it_narrated(self):
        await self.item(agent_message("thread-a", f"Progress.{marker('turn9view9')}"))
        await self.item(agent_message("thread-a", "Final answer."))

        await self.handler._on_turn_completed(
            {"threadId": "thread-a", "turn": {"id": "turn-1", "status": "interrupted"}},
            self.request,
        )

        self.assertEqual(self.delivered, [f"Progress. {UNRESOLVED}"])

    async def test_a_failed_turn_delivers_its_narration_before_the_error(self):
        await self.item(agent_message("thread-a", f"Progress.{marker('turn9view9')}"))
        await self.item(agent_message("thread-a", "Final answer."))

        await self.handler._on_turn_completed(
            {
                "threadId": "thread-a",
                "turn": {"id": "turn-1", "status": "failed", "error": {"message": "boom"}},
            },
            self.request,
        )

        self.assertEqual(self.delivered[0], f"Progress. {UNRESOLVED}")

    async def test_a_terminal_error_delivers_the_held_narration(self):
        await self.item(agent_message("thread-a", f"Progress.{marker('turn9view9')}"))
        await self.item(agent_message("thread-a", "Final answer."))

        await self.handler._on_error({"turnId": "turn-1", "error": {"message": "boom"}}, self.request)

        self.assertEqual(self.delivered[0], f"Progress. {UNRESOLVED}")

    async def test_a_retried_error_is_not_a_boundary(self):
        """``willRetry`` means the turn is still running, so nothing settles."""
        await self.item(agent_message("thread-a", f"Progress.{marker('turn0view0')}"))
        await self.item(agent_message("thread-a", "Final answer."))

        await self.handler._on_error(
            {"turnId": "turn-1", "willRetry": True, "error": {"message": "transient"}},
            self.request,
        )
        self.assertEqual(self.delivered, [])

        await self.item(self.guide_search())

        self.assertEqual(self.delivered, [f"Progress. [developers.openai.com]({GUIDE_URL})"])

    async def test_a_superseded_turn_settles_without_speaking(self):
        """A hidden turn's queue is discarded, exactly as its result candidate is."""
        await self.item(agent_message("thread-a", f"Progress.{marker('turn9view9')}"))
        await self.item(agent_message("thread-a", "Final answer."))

        self.handler.clear_pending("turn-1")
        await self.handler._on_turn_completed(turn_completed("thread-a"), self.request)

        self.assertEqual(self.delivered, [])

    async def test_an_uncited_narration_is_never_delayed(self):
        await self.item(agent_message("thread-a", "Progress."))
        await self.item(agent_message("thread-a", "Final answer."))

        self.assertEqual(self.delivered, ["Progress."])
        self.emitted.assert_awaited_once_with(
            self.request.context, "assistant", "Progress.", parse_mode="markdown"
        )

    async def test_a_resolvable_citation_is_not_delayed_either(self):
        await self.item(self.guide_search())
        await self.item(agent_message("thread-a", f"Progress.{marker('turn0view0')}"))
        await self.item(agent_message("thread-a", "Final answer."))

        self.assertEqual(self.delivered, [f"Progress. [developers.openai.com]({GUIDE_URL})"])

    async def test_nothing_overtakes_a_held_narration(self):
        """Holding one message must not let the messages behind it change places."""
        self.agent._get_formatter = lambda context: SimpleNamespace(
            format_toolcall=lambda name, payload: f"[{name}]"
        )
        await self.item(agent_message("thread-a", f"First.{marker('turn0view0')}"))
        await self.item(agent_message("thread-a", "Second."))
        await self.item(
            {
                "threadId": "thread-a",
                "turnId": "turn-1",
                "item": {
                    "type": "commandExecution",
                    "command": "ls",
                    "status": "completed",
                    "exitCode": 0,
                    "aggregatedOutput": "",
                },
            }
        )
        await self.item(agent_message("thread-a", "Third."))

        self.assertEqual(self.delivered, [])

        await self.item(self.guide_search())

        self.assertEqual(
            self.delivered,
            [f"First. [developers.openai.com]({GUIDE_URL})", "[bash]", "Second."],
        )

    async def test_a_second_held_message_resolves_against_its_own_ref(self):
        """The queue drains as far as it can, and stops at the first unready entry."""
        await self.item(agent_message("thread-a", f"First.{marker('turn0view0')}"))
        await self.item(agent_message("thread-a", f"Second.{marker('turn0view1')}"))
        await self.item(agent_message("thread-a", "Third."))

        await self.item(
            web_search("thread-a", {"ref_id": "turn0view1", "title": "T", "url": PROBE_URL})
        )
        self.assertEqual(self.delivered, [])

        await self.item(self.guide_search())

        self.assertEqual(
            self.delivered,
            [
                f"First. [developers.openai.com]({GUIDE_URL})",
                f"Second. [example.com]({PROBE_URL})",
            ],
        )


class CodexCitationHistoryTests(IsolatedCodexHome, unittest.IsolatedAsyncioTestCase):
    """Recovering sources a live stream never delivered.

    A conversation loses the ``item/completed`` notifications behind its earlier
    answers whenever the process restarts, the thread is resumed with
    ``excludeTurns: True``, it is re-read with ``includeTurns: False``, or it is
    forked - ``thread/fork`` returns an id and nothing more. Each case below runs
    the real consumption path (item in, delivered message and sidecar out)
    against a handler that never saw those notifications.
    """

    def setUp(self):
        self.home = self.isolate_codex_home()
        self.agent = _StubAgent()
        self.handler = CodexEventHandler(self.agent)
        self.request = _request()
        self.agent._turn_registry.register_turn("turn-1", self.request)

    async def answer(self, thread_id: str, text: str, *, turn_id: str = "turn-1", request=None):
        """Deliver one answer through the ordinary item/completed + turn path."""
        request = request or self.request
        await self.handler._on_item_completed(
            agent_message(thread_id, text, turn_id=turn_id), request
        )
        await self.handler._on_turn_completed(
            turn_completed(thread_id, turn_id=turn_id), request
        )
        self.agent.emit_result_message.assert_awaited()
        return self.agent.emit_result_message.await_args

    async def next_answer(self, thread_id: str, text: str):
        """A later Avibe turn in the same conversation, with its own request."""
        self._turns = getattr(self, "_turns", 1) + 1
        turn_id = f"turn-{self._turns}"
        request = _request(f"session-{self._turns}")
        self.agent._turn_registry.register_turn(turn_id, request)
        return await self.answer(thread_id, text, turn_id=turn_id, request=request)

    async def test_a_fresh_handler_cites_a_search_it_never_saw(self):
        """The restart case: the notification is gone, the recorded result is not."""
        self.record_history(
            self.home,
            "thread-a",
            [self.recorded_search(self.web_result("turn0view0", GUIDE_URL), owner="thread-a")],
        )

        call = await self.answer("thread-a", f"As established.{marker('turn0view0')}")

        self.assertEqual(call.args[1], f"As established. [developers.openai.com]({GUIDE_URL})")
        self.assertEqual([c["url"] for c in call.kwargs["citations"]], [GUIDE_URL])

    async def test_the_history_shape_the_shipped_binary_writes_is_read(self):
        """The current native rollout names the item ``Extension``/``web.search``.

        Reproduced against the real 0.154.0 app-server: with standalone search
        active, the recorded ``item_completed`` row carries the extension name
        and kind, not ``WebSearch``. A reader that only knows the older name
        recovers nothing from any history this binary writes.
        """
        self.record_history(
            self.home,
            "thread-a",
            [
                self.recorded_extension_search(
                    self.web_result("turn0search0", PROBE_URL), owner="thread-a"
                )
            ],
        )

        call = await self.answer("thread-a", f"Probed.{marker('turn0search0')}")

        self.assertEqual(call.args[1], f"Probed. [example.com]({PROBE_URL})")
        self.assertEqual([c["url"] for c in call.kwargs["citations"]], [PROBE_URL])

    async def test_both_recorded_shapes_are_read_from_one_history(self):
        """A conversation spanning the rename keeps every source it recorded."""
        self.record_history(
            self.home,
            "thread-a",
            [
                self.recorded_search(
                    self.web_result("turn0view0", GUIDE_URL), owner="thread-a"
                ),
                self.recorded_extension_search(
                    self.web_result("turn1search0", PROBE_URL),
                    owner="thread-a",
                    turn="turn-1",
                ),
            ],
        )

        call = await self.answer(
            "thread-a",
            f"Old.{marker('turn0view0')} New.{marker('turn1search0')}",
        )

        self.assertEqual(
            [c["url"] for c in call.kwargs["citations"]], [GUIDE_URL, PROBE_URL]
        )

    async def test_a_fork_cites_the_parent_history_it_carries(self):
        """A fork's file opens with the parent's rows, still under the parent's id."""
        self.record_history(
            self.home,
            "thread-fork",
            [
                self.recorded_search(
                    self.web_result("turn0view0", GUIDE_URL),
                    owner="thread-parent",
                    turn="turn-parent",
                ),
                self.recorded_search(
                    self.web_result("turn1view0", PROBE_URL),
                    owner="thread-fork",
                    turn="turn-own",
                ),
            ],
        )

        call = await self.answer(
            "thread-fork", f"Both.{marker('turn0view0', 'turn1view0')}"
        )

        self.assertEqual(
            call.args[1],
            f"Both. [developers.openai.com]({GUIDE_URL}) [example.com]({PROBE_URL})",
        )

    async def test_the_same_ref_id_in_another_thread_keeps_its_own_source(self):
        """Ref tokens repeat across threads, so each thread reads only its own file."""
        for thread_id, url in (("thread-a", GUIDE_URL), ("thread-b", PROBE_URL)):
            self.record_history(
                self.home,
                thread_id,
                [self.recorded_search(self.web_result("turn0view0", url), owner=thread_id)],
            )

        first = await self.answer("thread-a", f"A.{marker('turn0view0')}")
        self.assertEqual([c["url"] for c in first.kwargs["citations"]], [GUIDE_URL])

        second_request = _request("session-2")
        self.agent._turn_registry.register_turn("turn-2", second_request)
        second = await self.answer(
            "thread-b", f"B.{marker('turn0view0')}", turn_id="turn-2", request=second_request
        )
        self.assertEqual([c["url"] for c in second.kwargs["citations"]], [PROBE_URL])

    async def test_history_is_read_once_per_thread_not_once_per_message(self):
        """Cross-turn: the read is a memo, so a long conversation pays for it once."""
        self.record_history(
            self.home,
            "thread-a",
            [self.recorded_search(self.web_result("turn0view0", GUIDE_URL), owner="thread-a")],
        )

        with mock.patch.object(
            codex_event_handler,
            "read_thread_search_sources",
            wraps=search_history.read_thread_search_sources,
        ) as reader:
            await self.answer("thread-a", f"First.{marker('turn0view0')}")
            later = _request("session-2")
            self.agent._turn_registry.register_turn("turn-2", later)
            call = await self.answer(
                "thread-a", f"Second.{marker('turn0view0')}", turn_id="turn-2", request=later
            )

        self.assertEqual(reader.call_count, 1)
        self.assertEqual(call.args[1], f"Second. [developers.openai.com]({GUIDE_URL})")

    async def test_an_evicted_thread_is_read_back_rather_than_lost(self):
        """The cache is a memo over history, so eviction costs a read, not the link."""
        self.record_history(
            self.home,
            "thread-a",
            [self.recorded_search(self.web_result("turn0view0", GUIDE_URL), owner="thread-a")],
        )
        params = web_search("thread-a", {"ref_id": "turn0view0", "title": "T", "url": GUIDE_URL})
        self.handler._record_search_sources(params, params["item"])
        for i in range(_MAX_CITATION_THREADS + 1):
            noise = web_search(f"other-{i}", {"ref_id": "r", "title": "T", "url": PROBE_URL})
            self.handler._record_search_sources(noise, noise["item"])
        self.assertNotIn("thread-a", self.handler._search_sources_by_thread)

        call = await self.answer("thread-a", f"Still cited.{marker('turn0view0')}")

        self.assertEqual(call.args[1], f"Still cited. [developers.openai.com]({GUIDE_URL})")

    async def test_a_partially_written_last_row_does_not_lose_the_rest(self):
        """Codex may be mid-append; an unparseable tail is not evidence about the head."""
        self.record_history(
            self.home,
            "thread-a",
            [self.recorded_search(self.web_result("turn0view0", GUIDE_URL), owner="thread-a")],
            trailing='{"type": "event_msg", "payload": {"item": {"type": "WebSea',
        )

        call = await self.answer("thread-a", f"Cited.{marker('turn0view0')}")

        self.assertEqual([c["url"] for c in call.kwargs["citations"]], [GUIDE_URL])

    async def test_a_thread_with_no_recorded_history_degrades_to_the_label(self):
        """Nothing indexed, nothing invented."""
        call = await self.answer("thread-a", f"Claimed.{marker('turn0view0')}")

        self.assertEqual(call.args[1], f"Claimed. {UNRESOLVED}")
        self.assertIsNone(call.kwargs["citations"])

    async def test_a_recorded_result_without_a_url_is_not_given_one(self):
        """A ref_id is a name, never a source: an entry with no URL stays unresolved."""
        self.record_history(
            self.home,
            "thread-a",
            [
                self.recorded_search(
                    {"type": "web", "ref_id": "turn0view0", "title": "No link", "snippet": "s"},
                    owner="thread-a",
                )
            ],
        )

        call = await self.answer("thread-a", f"Claimed.{marker('turn0view0')}")

        self.assertEqual(call.args[1], f"Claimed. {UNRESOLVED}")
        self.assertIsNone(call.kwargs["citations"])

    @unittest.skipIf(
        hasattr(os, "geteuid") and os.geteuid() == 0,
        "root can read a mode-000 file, so the failure branch cannot be reached",
    )
    async def test_an_unreadable_history_file_is_reported_and_degrades(self):
        """A read that fails leaves real evidence in the log and invents nothing."""
        path = self.record_history(
            self.home,
            "thread-a",
            [self.recorded_search(self.web_result("turn0view0", GUIDE_URL), owner="thread-a")],
        )
        path.chmod(0o000)
        self.addCleanup(path.chmod, 0o600)

        with self.assertLogs(search_history.logger, level="WARNING") as logs:
            call = await self.answer("thread-a", f"Claimed.{marker('turn0view0')}")

        self.assertTrue(any(str(path) in line for line in logs.output), logs.output)
        self.assertEqual(call.args[1], f"Claimed. {UNRESOLVED}")
        self.assertIsNone(call.kwargs["citations"])

    async def test_a_live_search_does_not_hide_the_recorded_ones(self):
        """A cache entry says what this process saw, not what the thread contains.

        One live search creates the thread's entry. The refs an earlier answer
        cited are not in it, and they are the ones a resume or a restart lost -
        so a live result must not be read as "this thread is fully known".
        """
        self.record_history(
            self.home,
            "thread-a",
            [self.recorded_search(self.web_result("turn0view0", GUIDE_URL), owner="thread-a")],
        )
        live = web_search("thread-a", self.web_result("turn1view0", PROBE_URL))
        self.handler._record_search_sources(live, live["item"])

        call = await self.answer("thread-a", f"Both.{marker('turn0view0', 'turn1view0')}")

        self.assertEqual(
            call.args[1],
            f"Both. [developers.openai.com]({GUIDE_URL}) [example.com]({PROBE_URL})",
        )

    async def test_the_source_bound_never_discards_the_ref_being_resolved(self):
        """Bounding the cache may cost a re-read; it may never cost the link."""
        results = [
            self.web_result(f"turn0view{i}", f"https://example.com/{i}")
            for i in range(_MAX_CITATION_SOURCES_PER_THREAD + 1)
        ]
        self.record_history(
            self.home, "thread-a", [self.recorded_search(*results, owner="thread-a")]
        )
        live = web_search("thread-a", *results)
        self.handler._record_search_sources(live, live["item"])
        self.assertNotIn("turn0view0", cached_sources(self.handler, "thread-a"))

        call = await self.answer("thread-a", f"The oldest one.{marker('turn0view0')}")

        self.assertEqual(call.args[1], "The oldest one. [example.com](https://example.com/0)")

    async def test_an_empty_history_that_later_records_a_search_is_re_read(self):
        """A history is a growing file, so "not there yet" is not "not there"."""
        path = self.record_history(self.home, "thread-a", [])

        first = await self.answer("thread-a", f"First.{marker('turn0view0')}")
        self.assertEqual(first.args[1], f"First. {UNRESOLVED}")

        with path.open("a", encoding="utf-8") as handle:
            row = self.recorded_search(self.web_result("turn0view0", GUIDE_URL), owner="thread-a")
            handle.write(f"{json.dumps(row)}\n")

        call = await self.next_answer("thread-a", f"Second.{marker('turn0view0')}")

        self.assertEqual(call.args[1], f"Second. [developers.openai.com]({GUIDE_URL})")

    async def test_a_history_that_appears_later_is_found(self):
        """Codex indexes the thread when it writes it; a lookup miss is not final."""
        first = await self.answer("thread-a", f"First.{marker('turn0view0')}")
        self.assertEqual(first.args[1], f"First. {UNRESOLVED}")

        self.record_history(
            self.home,
            "thread-a",
            [self.recorded_search(self.web_result("turn0view0", GUIDE_URL), owner="thread-a")],
        )

        call = await self.next_answer("thread-a", f"Second.{marker('turn0view0')}")

        self.assertEqual(call.args[1], f"Second. [developers.openai.com]({GUIDE_URL})")

    @unittest.skipIf(
        hasattr(os, "geteuid") and os.geteuid() == 0,
        "root can read a mode-000 file, so the failure branch cannot be reached",
    )
    async def test_an_unreadable_history_is_re_read_once_it_can_be_read(self):
        """A read that failed learned nothing, so it may not be remembered as absence.

        Restoring the mode changes neither size nor mtime, so nothing about the
        file itself says to look again: it is the incomplete read that must not
        settle into negative truth.
        """
        path = self.record_history(
            self.home,
            "thread-a",
            [self.recorded_search(self.web_result("turn0view0", GUIDE_URL), owner="thread-a")],
        )
        path.chmod(0o000)
        self.addCleanup(path.chmod, 0o600)

        with self.assertLogs(search_history.logger, level="WARNING"):
            first = await self.answer("thread-a", f"First.{marker('turn0view0')}")
        self.assertEqual(first.args[1], f"First. {UNRESOLVED}")

        path.chmod(0o600)

        call = await self.next_answer("thread-a", f"Second.{marker('turn0view0')}")

        self.assertEqual(call.args[1], f"Second. [developers.openai.com]({GUIDE_URL})")

    async def test_a_missing_ref_does_not_rescan_an_unchanged_history(self):
        """The cost of a ref that is genuinely absent is paid once per history."""
        self.record_history(
            self.home,
            "thread-a",
            [self.recorded_search(self.web_result("turn0view0", GUIDE_URL), owner="thread-a")],
        )
        scans: list[bool] = []
        real = search_history.read_thread_search_sources

        def spy(*args, **kwargs):
            read = real(*args, **kwargs)
            scans.append(read.scanned)
            return read

        with mock.patch.object(codex_event_handler, "read_thread_search_sources", spy):
            await self.answer("thread-a", f"First.{marker('turn9view9')}")
            call = await self.next_answer("thread-a", f"Second.{marker('turn9view9')}")

        self.assertEqual(scans, [True, False])
        self.assertEqual(call.args[1], f"Second. {UNRESOLVED}")

    async def test_an_absence_does_not_outlive_the_history_that_proved_it(self):
        """A scan directed at one ref may not vindicate another ref's absence.

        The second answer asks only about ``turn9view9``, so its scan of the
        grown file learns nothing about ``turn0view0`` - it never looked. If both
        absences lean on one "history I have already read" mark, that scan moves
        the mark forward and the third answer takes the grown file for the one
        that proved ``turn0view0`` missing, leaving the search now sitting in it
        unread. Only the history an absence was actually proved against may
        suppress a re-read.
        """
        path = self.record_history(self.home, "thread-a", [])

        first = await self.answer("thread-a", f"First.{marker('turn0view0')}")
        self.assertEqual(first.args[1], f"First. {UNRESOLVED}")

        with path.open("a", encoding="utf-8") as handle:
            row = self.recorded_search(self.web_result("turn0view0", GUIDE_URL), owner="thread-a")
            handle.write(f"{json.dumps(row)}\n")

        second = await self.next_answer("thread-a", f"Second.{marker('turn9view9')}")
        self.assertEqual(second.args[1], f"Second. {UNRESOLVED}")

        call = await self.next_answer("thread-a", f"Third.{marker('turn0view0')}")

        self.assertEqual(call.args[1], f"Third. [developers.openai.com]({GUIDE_URL})")

    async def test_the_latest_recorded_definition_of_a_ref_wins(self):
        """A rollout file may redefine a ref; the newest row is the trustworthy one."""
        self.record_history(
            self.home,
            "thread-a",
            [
                self.recorded_search(self.web_result("turn0view0", GUIDE_URL), owner="thread-a"),
                self.recorded_search(
                    self.web_result("turn0view0", PROBE_URL), owner="thread-a", turn="turn-1"
                ),
            ],
        )

        call = await self.answer("thread-a", f"Cited.{marker('turn0view0')}")

        self.assertEqual(call.args[1], f"Cited. [example.com]({PROBE_URL})")

    def test_a_read_carries_only_the_refs_it_was_asked_for(self):
        """The read is directed by the message, which is what bounds it."""
        self.record_history(
            self.home,
            "thread-a",
            [
                self.recorded_search(
                    *[
                        self.web_result(f"turn0view{i}", f"https://example.com/{i}")
                        for i in range(20)
                    ],
                    owner="thread-a",
                )
            ],
        )

        read = search_history.read_thread_search_sources("thread-a", wanted={"turn0view3"})

        self.assertEqual(
            {ref: source.url for ref, source in read.sources.items()},
            {"turn0view3": "https://example.com/3"},
        )
        self.assertTrue(read.scanned)
        self.assertTrue(read.complete)

    def test_an_unchanged_history_is_not_scanned_again(self):
        self.record_history(
            self.home,
            "thread-a",
            [self.recorded_search(self.web_result("turn0view0", GUIDE_URL), owner="thread-a")],
        )

        first = search_history.read_thread_search_sources("thread-a", wanted={"nope"})
        self.assertTrue(first.scanned)
        self.assertEqual(first.sources, {})

        again = search_history.read_thread_search_sources(
            "thread-a", wanted={"nope"}, unchanged=first.fingerprint
        )

        self.assertFalse(again.scanned)
        self.assertEqual(again.fingerprint, first.fingerprint)

    def test_a_changed_history_is_scanned_again(self):
        path = self.record_history(
            self.home,
            "thread-a",
            [self.recorded_search(self.web_result("turn0view0", GUIDE_URL), owner="thread-a")],
        )
        first = search_history.read_thread_search_sources("thread-a", wanted={"turn1view0"})

        with path.open("a", encoding="utf-8") as handle:
            row = self.recorded_search(self.web_result("turn1view0", PROBE_URL), owner="thread-a")
            handle.write(f"{json.dumps(row)}\n")

        again = search_history.read_thread_search_sources(
            "thread-a", wanted={"turn1view0"}, unchanged=first.fingerprint
        )

        self.assertTrue(again.scanned)
        self.assertEqual(again.sources["turn1view0"].url, PROBE_URL)

    def test_an_unsafe_thread_id_never_reaches_the_filesystem(self):
        """The id is interpolated into a lookup and a path, so it is validated first."""
        for thread_id in ("../../etc/passwd", "a/b", "thread a", "", "thread;drop"):
            read = search_history.read_thread_search_sources(thread_id, wanted={"turn0view0"})
            self.assertEqual(read.sources, {}, thread_id)
            self.assertFalse(read.scanned, thread_id)

if __name__ == "__main__":
    unittest.main()
