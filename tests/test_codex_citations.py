"""Citation handling in the Codex backend: source capture and emit-time resolution.

The grammar itself is covered by ``tests/test_citations.py``. What is specific to
this backend, and asserted here, is *where* a ``ref_id`` is allowed to mean
something: a ref is only unique inside its own native Codex thread, arrives in a
notification separate from the answer that cites it, and lives in a bounded cache
that must never grow without limit or leak across threads.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules.agents.codex.event_handler import (
    _MAX_CITATION_SOURCES_PER_THREAD,
    _MAX_CITATION_THREADS,
)
from tests.test_codex_event_handler import CodexEventHandler, _StubAgent

START, SEP, END = "\ue200", "\ue202", "\ue201"
UNRESOLVED = "(source unavailable)"

GUIDE_URL = "https://developers.openai.com/api/docs/guides/tools-web-search"
PROBE_URL = "https://example.com/citation-probe-source"


def marker(*ref_ids: str) -> str:
    return f"{START}cite{SEP}{SEP.join(ref_ids)}{END}"


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


class CodexCitationCaptureTests(unittest.TestCase):
    """``_record_search_sources``: what gets into the cache, and under which key."""

    def setUp(self):
        self.handler = CodexEventHandler(_StubAgent())

    def sources(self, thread_id: str) -> dict:
        return dict(self.handler._search_sources_by_thread.get(thread_id) or {})

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

    def test_a_ref_id_means_nothing_outside_its_own_thread(self):
        """Two threads both start at ``turn0view0``; neither may see the other's."""
        for thread_id, url in (("thread-a", GUIDE_URL), ("thread-b", PROBE_URL)):
            params = web_search(thread_id, {"ref_id": "turn0view0", "title": "T", "url": url})
            self.handler._record_search_sources(params, params["item"])

        self.assertEqual(self.sources("thread-a")["turn0view0"].url, GUIDE_URL)
        self.assertEqual(self.sources("thread-b")["turn0view0"].url, PROBE_URL)

        text, citations = self.handler._resolve_citations(
            f"Cited.{marker('turn0view0')}", {"threadId": "thread-b"}, _request()
        )
        self.assertEqual(text, f"Cited. [example.com]({PROBE_URL})")
        self.assertEqual([c["url"] for c in citations], [PROBE_URL])

    def test_a_thread_that_never_searched_resolves_nothing(self):
        params = web_search("thread-a", {"ref_id": "turn0view0", "title": "T", "url": GUIDE_URL})
        self.handler._record_search_sources(params, params["item"])

        text, citations = self.handler._resolve_citations(
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


class CodexCitationResolutionTests(unittest.IsolatedAsyncioTestCase):
    """The product path: notifications in, delivered message plus sidecar out."""

    def setUp(self):
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


if __name__ == "__main__":
    unittest.main()
