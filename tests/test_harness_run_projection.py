"""Harness Runs payload projection: resolved session, originating definition.

Runs were the last harness list rendered straight from the raw ``agent_runs``
row — the headline was a run-id hash and the bound session was an unresolvable
string. ``SQLiteBackgroundTaskStore._enrich_runs`` closes that gap with the same
session summary Tasks/Watches already get, so a workbench run links to its chat,
an IM run shows its channel, and a run whose session was deleted says so.

See ``docs/plans/harness-runs-readability.md`` §3 (frozen contract) and §5.
"""

from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import event, update

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from storage import workbench_sessions_service
from storage.background import SQLiteBackgroundTaskStore
from storage.db import create_sqlite_engine
from storage.models import agent_runs, agent_sessions, scope_settings
from storage.sessions_service import SQLiteSessionsService
from storage.settings_service import upsert_scope

NOW = "2026-07-26T00:00:00Z"

# Tables the projection reads. Counting statements that touch them separates
# enrichment cost from the list query itself (which only reads agent_runs).
_ENRICHMENT_TABLES = ("agent_sessions", "scopes", "run_definitions")


def _build_schema(db_path: Path) -> None:
    SQLiteSessionsService(db_path).close()


def _make_workbench_session(conn, tmp_path: Path, native_id: str, title: str) -> str:
    scope_id = upsert_scope(conn, platform="avibe", scope_type="project", native_id=native_id, now=NOW)
    conn.execute(
        scope_settings.insert().values(
            scope_id=scope_id,
            enabled=1,
            role=None,
            workdir=str(tmp_path),
            agent_name=None,
            agent_backend=None,
            agent_variant=None,
            model=None,
            reasoning_effort=None,
            require_mention=None,
            settings_version=1,
            settings_json="{}",
            created_at=NOW,
            updated_at=NOW,
        )
    )
    session = workbench_sessions_service.create_session(
        conn, scope_id=scope_id, agent_backend="claude", agent_name="default"
    )
    conn.execute(update(agent_sessions).where(agent_sessions.c.id == session["id"]).values(title=title))
    return session["id"]


def _run(run_id: str, **overrides) -> dict:
    payload = {
        "id": run_id,
        "run_type": "agent_run",
        "status": "succeeded",
        "created_at": NOW,
        "updated_at": NOW,
    }
    payload.update(overrides)
    return payload


def _enrichment_query_count(store: SQLiteBackgroundTaskStore, call) -> int:
    """Statements the projection issues against the tables it joins."""
    seen: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        lowered = statement.lower()
        if any(table in lowered for table in _ENRICHMENT_TABLES):
            seen.append(statement)

    event.listen(store.engine, "before_cursor_execute", _record)
    try:
        call()
    finally:
        event.remove(store.engine, "before_cursor_execute", _record)
    return len(seen)


def test_run_payload_resolves_every_session_state(tmp_path: Path) -> None:
    """Workbench / IM / deleted / unbound — the four states §4.2 renders."""
    db_path = tmp_path / "vibe.sqlite"
    _build_schema(db_path)
    engine = create_sqlite_engine(db_path)
    try:
        with engine.begin() as conn:
            workbench_id = _make_workbench_session(conn, tmp_path, "proj_runs", "重构鉴权模块")
            upsert_scope(
                conn, platform="slack", scope_type="channel", native_id="C0123", now=NOW, display_name="#dev-ops"
            )
    finally:
        engine.dispose()

    store = SQLiteBackgroundTaskStore(db_path)
    try:
        store.enqueue_run(_run("run_wb", session_id=workbench_id, message="巡检构建产物"))
        store.enqueue_run(_run("run_im", session_key="slack::channel::C0123", message="周报推送"))
        store.enqueue_run(_run("run_gone", session_id="ses_deleted_forever", message="orphan"))
        store.enqueue_run(_run("run_none", message="no session at all"))
        runs = {run["id"]: run for run in store.list_runs_page(page_request=None).items}
    finally:
        store.close()

    workbench = runs["run_wb"]
    assert workbench["session_is_workbench"] is True
    assert workbench["session_title"] == "重构鉴权模块"
    assert workbench["session_label"] == "重构鉴权模块"
    assert workbench["session_platform"] == "avibe"
    assert workbench["session_scope_kind"] == "project"

    im = runs["run_im"]
    assert im["session_is_workbench"] is False  # IM transcripts are scope-keyed; never linked
    assert im["session_platform"] == "slack"
    assert im["session_label"] == "#dev-ops"  # display name, not the raw channel id

    # A run outlives its session. The summary stays all-null so the UI can say
    # "session deleted" instead of printing an id that opens nothing.
    deleted = runs["run_gone"]
    assert deleted["session_id"] == "ses_deleted_forever"
    assert deleted["session_label"] is None
    assert deleted["session_title"] is None
    assert deleted["session_is_workbench"] is False

    unbound = runs["run_none"]
    assert unbound["session_id"] is None
    assert unbound["session_label"] is None


def test_run_payload_falls_back_to_deliver_key(tmp_path: Path) -> None:
    """``create_per_run`` runs mint a session per fire, so the target scope lives
    in ``deliver_key`` — same precedence as ``_session_summary``."""
    db_path = tmp_path / "vibe.sqlite"
    _build_schema(db_path)
    engine = create_sqlite_engine(db_path)
    try:
        with engine.begin() as conn:
            upsert_scope(
                conn, platform="feishu", scope_type="channel", native_id="oc_9", now=NOW, display_name="值班群"
            )
    finally:
        engine.dispose()

    store = SQLiteBackgroundTaskStore(db_path)
    try:
        store.enqueue_run(_run("run_deliver", deliver_key="feishu::channel::oc_9", message="hi"))
        run = store.get_run("run_deliver")
    finally:
        store.close()

    assert run["session_platform"] == "feishu"
    assert run["session_label"] == "值班群"
    assert run["session_is_workbench"] is False


def test_run_payload_names_originating_definition(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    _build_schema(db_path)

    store = SQLiteBackgroundTaskStore(db_path)
    try:
        store.upsert_watch(
            {
                "id": "watch_ci",
                "name": "CI 红灯监听",
                "command": "true",
                "prompt": "hello",
                "enabled": True,
                "created_at": NOW,
                "updated_at": NOW,
            }
        )
        store.upsert_scheduled_task(
            {
                "id": "task_nightly",
                "name": "夜间巡检",
                "prompt": "hello",
                "schedule_type": "cron",
                "cron": "0 3 * * *",
                "timezone": "UTC",
                "enabled": True,
                "created_at": NOW,
                "updated_at": NOW,
            }
        )
        store.remove_task("task_nightly")  # soft delete; the run must still name it

        store.enqueue_run(_run("run_w", run_type="watch", definition_id="watch_ci"))
        store.enqueue_run(_run("run_t", run_type="scheduled", definition_id="task_nightly"))
        store.enqueue_run(_run("run_orphan", definition_id="definition_that_never_existed"))
        store.enqueue_run(_run("run_free"))
        runs = {run["id"]: run for run in store.list_runs_page(page_request=None).items}
    finally:
        store.close()

    assert runs["run_w"]["definition_name"] == "CI 红灯监听"
    assert runs["run_w"]["definition_kind"] == "watch"
    assert runs["run_w"]["definition_deleted"] is False

    # definition_type is "scheduled" in the column; every surface calls it a task.
    assert runs["run_t"]["definition_name"] == "夜间巡检"
    assert runs["run_t"]["definition_kind"] == "task"
    assert runs["run_t"]["definition_deleted"] is True  # so the UI drops the link

    for orphan in ("run_orphan", "run_free"):
        assert runs[orphan]["definition_name"] is None
        assert runs[orphan]["definition_kind"] is None
        assert runs[orphan]["definition_deleted"] is False


def test_run_payload_resolves_callback_session(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    _build_schema(db_path)
    engine = create_sqlite_engine(db_path)
    try:
        with engine.begin() as conn:
            reporter = _make_workbench_session(conn, tmp_path, "proj_cb", "编排会话")
    finally:
        engine.dispose()

    store = SQLiteBackgroundTaskStore(db_path)
    try:
        store.enqueue_run(_run("run_cb", callback_session_id=reporter, message="delegated work"))
        store.enqueue_run(_run("run_no_cb", message="no callback"))
        runs = {run["id"]: run for run in store.list_runs_page(page_request=None).items}
    finally:
        store.close()

    callback = runs["run_cb"]["callback_session"]
    assert callback["session_title"] == "编排会话"
    assert callback["session_is_workbench"] is True
    assert runs["run_no_cb"]["callback_session"] is None


def test_get_run_enriches_identically_to_the_list(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    _build_schema(db_path)
    engine = create_sqlite_engine(db_path)
    try:
        with engine.begin() as conn:
            session_id = _make_workbench_session(conn, tmp_path, "proj_detail", "详情会话")
    finally:
        engine.dispose()

    store = SQLiteBackgroundTaskStore(db_path)
    try:
        store.upsert_watch(
            {
                "id": "watch_detail",
                "name": "磁盘水位",
                "command": "true",
                "prompt": "hello",
                "enabled": True,
                "created_at": NOW,
                "updated_at": NOW,
            }
        )
        store.enqueue_run(
            _run("run_detail", run_type="watch", definition_id="watch_detail", session_id=session_id)
        )
        listed = store.list_runs_page(page_request=None).items[0]
        detail = store.get_run("run_detail")
    finally:
        store.close()

    projected = ("session_title", "session_label", "session_is_workbench", "definition_name", "definition_kind")
    assert {key: detail[key] for key in projected} == {key: listed[key] for key in projected}


def test_run_enrichment_is_batched(tmp_path: Path) -> None:
    """No N+1: doubling the page must not double the projection's queries."""
    db_path = tmp_path / "vibe.sqlite"
    _build_schema(db_path)
    engine = create_sqlite_engine(db_path)
    session_ids: list[str] = []
    try:
        with engine.begin() as conn:
            for index in range(24):
                session_ids.append(_make_workbench_session(conn, tmp_path, f"proj_{index}", f"会话 {index}"))
    finally:
        engine.dispose()

    store = SQLiteBackgroundTaskStore(db_path)
    try:
        for index, session_id in enumerate(session_ids):
            store.upsert_watch(
                {
                    "id": f"watch_{index}",
                    "name": f"监听 {index}",
                    "command": "true",
                    "prompt": "hello",
                    "enabled": True,
                    "created_at": NOW,
                    "updated_at": NOW,
                }
            )
            store.enqueue_run(
                _run(
                    f"run_{index:02d}",
                    run_type="watch",
                    definition_id=f"watch_{index}",
                    session_id=session_id,
                    session_key=f"slack::channel::C{index}",
                )
            )

        def _page(limit: int):
            from storage.pagination import make_page_request

            return lambda: store.list_runs_page(page_request=make_page_request(page=1, limit=limit))

        small = _enrichment_query_count(store, _page(6))
        large = _enrichment_query_count(store, _page(24))
        page = store.list_runs_page(page_request=None)
    finally:
        store.close()

    assert small == large, f"query count grew with page size ({small} → {large}): the projection is N+1"
    assert large <= 4, f"expected a handful of batched queries, got {large}"
    # The batch actually resolved every row — a constant query count would also
    # be satisfied by resolving nothing.
    assert len(page.items) == 24
    assert all(run["session_title"] and run["definition_name"] for run in page.items)


def test_exclude_run_type_keeps_list_and_counts_consistent(tmp_path: Path) -> None:
    """D1 hides watcher heartbeats by default; the badges must agree with the rows."""
    db_path = tmp_path / "vibe.sqlite"
    _build_schema(db_path)

    store = SQLiteBackgroundTaskStore(db_path)
    try:
        for index in range(5):
            store.enqueue_run(_run(f"noise_{index}", run_type="watch_runtime", status="succeeded"))
        store.enqueue_run(_run("real_ok", run_type="watch", status="succeeded"))
        store.enqueue_run(_run("real_bad", run_type="agent_run", status="failed"))
        # A run type nobody has heard of yet must survive the default exclusion.
        store.enqueue_run(_run("future", run_type="brand_new_kind", status="succeeded"))

        unfiltered = store.count_runs_by_status()
        listed = store.list_runs_page(exclude_run_type=["watch_runtime"], page_request=None).items
        counts = store.count_runs_by_status(exclude_run_type=["watch_runtime"])
        total = store.count_runs(exclude_run_type=["watch_runtime"])
        failed_only = store.list_runs_page(
            status="failed", exclude_run_type=["watch_runtime"], page_request=None
        ).items
        heartbeats = store.list_runs_page(run_type="watch_runtime", page_request=None).items
    finally:
        store.close()

    assert unfiltered["all"] == 8  # nothing is deleted, only hidden
    assert {run["id"] for run in listed} == {"real_ok", "real_bad", "future"}
    assert total == len(listed) == 3
    assert counts["all"] == 3
    assert counts["succeeded"] == 2
    assert counts["failed"] == 1
    assert [run["id"] for run in failed_only] == ["real_bad"]
    # Selecting the type explicitly brings the hidden rows back — reversible, not truncated.
    assert len(heartbeats) == 5


# Projected values that are UI vocabulary rather than the user's own text: the
# UI renders each through i18n as an icon or a chip, and each has its own filter
# control. Matching the raw English token would find nothing for someone reading
# the interface in Chinese, so search deliberately skips them.
# ``request_type`` is the legacy alias of the ``run_type`` column — same value,
# same translated chip, so it is excluded for the same reason.
_UI_VOCABULARY = {"request_type", "session_platform", "session_scope_kind", "definition_kind"}


def _projected_text(payload: dict) -> dict[str, str]:
    """Every user-visible string on a run row that ``_enrich_runs`` invented.

    Derived by subtracting the ``agent_runs`` columns from the payload rather
    than by listing field names, so a projection added later turns up here on
    its own. Nested objects (``callback_session``) are walked, because a label
    is no less visible for being one level down.
    """
    stored = {column.name for column in agent_runs.columns}
    found: dict[str, str] = {}

    def walk(node: dict, prefix: str) -> None:
        for key, value in node.items():
            path = f"{prefix}{key}"
            if isinstance(value, dict):
                walk(value, f"{path}.")
            elif isinstance(value, str) and value and key not in stored and key not in _UI_VOCABULARY:
                found[path] = value

    walk(payload, "")
    return found


def test_every_projected_label_is_searchable(tmp_path: Path) -> None:
    """The loop-ender.

    Review found search missing one projected field per round — the definition
    name, then the session label, with ``callback_session`` next in line —
    because the predicate list and the enrichment list were maintained by hand
    in two places. This test checks no list of names. It enriches a row, asks
    the payload which strings the projection invented, and requires every one of
    them to be findable by typing it. A projection added without a matching
    predicate fails here instead of costing a review round.
    """
    db_path = tmp_path / "vibe.sqlite"
    _build_schema(db_path)
    engine = create_sqlite_engine(db_path)
    try:
        with engine.begin() as conn:
            bound = _make_workbench_session(conn, tmp_path, "proj_probe", "重构鉴权模块")
            reporter = _make_workbench_session(conn, tmp_path, "proj_reporter", "编排调度会话")
            upsert_scope(
                conn, platform="slack", scope_type="channel", native_id="C0123", now=NOW, display_name="#dev-ops"
            )
    finally:
        engine.dispose()

    store = SQLiteBackgroundTaskStore(db_path)
    try:
        store.upsert_watch(
            {
                "id": "watch_probe",
                "name": "磁盘水位巡检",
                "command": "true",
                "prompt": "hello",
                "enabled": True,
                "created_at": NOW,
                "updated_at": NOW,
            }
        )
        # One run touching every projection site at once, and one on the
        # key-bound path, which resolves a label the run never stores.
        store.enqueue_run(
            _run(
                "run_probe",
                run_type="watch",
                definition_id="watch_probe",
                session_id=bound,
                callback_session_id=reporter,
            )
        )
        store.enqueue_run(_run("run_keyed", session_key="slack::channel::C0123::thread::1712.9"))
        payloads = {run["id"]: run for run in store.list_runs_page(page_request=None).items}
        projected = {run_id: _projected_text(payload) for run_id, payload in payloads.items()}
        hits = {
            (run_id, path): _search_run_ids(store, value)
            for run_id, fields in projected.items()
            for path, value in fields.items()
        }
    finally:
        store.close()

    # The walk is not vacuous: it reached every site, including the nested one.
    # Asserted against the fixture's values, not against field names, so this
    # keeps holding when a site is renamed or a new one appears.
    planted = {"重构鉴权模块", "编排调度会话", "磁盘水位巡检", "#dev-ops"}
    seen = {value for fields in projected.values() for value in fields.values()}
    assert planted <= seen, f"projection walk missed {planted - seen}"

    # The whole point: every invented string finds its own row, and only it.
    for (run_id, path), found in sorted(hits.items()):
        assert found == {run_id}, f"{run_id}.{path} is displayed but search returns {found or 'nothing'}"

    # A stale exclusion is a bug too — if one of these stops being projected, the
    # reason to skip it went with it, and the next reader inherits a name that
    # explains nothing. Checked against the raw payload: `projected` is the
    # already-filtered view, so asking it would only confirm the filter ran.
    present = {key for payload in payloads.values() for key in payload}
    present |= {
        key
        for payload in payloads.values()
        for value in payload.values()
        if isinstance(value, dict)
        for key in value
    }
    assert _UI_VOCABULARY <= present, f"search exclusion no longer projected: {_UI_VOCABULARY - present}"


def _search_run_ids(store: SQLiteBackgroundTaskStore, term: str) -> set[str]:
    """Rows for a search term, cross-checked against the two count paths the
    status badges read. All three go through ``_runs_query``, so a predicate that
    reaches the list but not the counts is a bug the badges would expose."""
    found = store.list_runs_page(query=term, page_request=None).items
    total = store.count_runs(query=term)
    assert store.count_runs_by_status(query=term)["all"] == total == len(found), (
        f"list/count disagreement for {term!r}"
    )
    return {run["id"] for run in found}


def test_search_finds_runs_by_the_session_label_they_display(tmp_path: Path) -> None:
    """§4.2 renders a resolved session label, not the stored id/key. Searching
    that label has to find the row — the id it is stored under is exactly the
    string the projection exists to stop showing the user."""
    db_path = tmp_path / "vibe.sqlite"
    _build_schema(db_path)
    engine = create_sqlite_engine(db_path)
    try:
        with engine.begin() as conn:
            workbench_id = _make_workbench_session(conn, tmp_path, "proj_search", "重构鉴权模块")
            other_id = _make_workbench_session(conn, tmp_path, "proj_other", "无关会话")
            upsert_scope(
                conn, platform="slack", scope_type="channel", native_id="C0123", now=NOW, display_name="#dev-ops"
            )
            # Prefix-collides with the channel above on the native id, and its
            # own native id is a prefix of the Telegram pair below.
            upsert_scope(
                conn, platform="telegram", scope_type="channel", native_id="-100123", now=NOW, display_name="值班群"
            )
            upsert_scope(
                conn,
                platform="telegram",
                scope_type="channel",
                native_id="-1001234",
                now=NOW,
                display_name="产品群",
            )
    finally:
        engine.dispose()

    store = SQLiteBackgroundTaskStore(db_path)
    try:
        store.enqueue_run(_run("run_wb", session_id=workbench_id))
        store.enqueue_run(_run("run_other", session_id=other_id))
        store.enqueue_run(_run("run_im", session_key="slack::channel::C0123"))
        # The common shape: a threaded key, whose scope key is only a prefix.
        store.enqueue_run(_run("run_thread", session_key="slack::channel::C0123::thread::1712.9"))
        store.enqueue_run(_run("run_duty", session_key="telegram::channel::-100123"))
        store.enqueue_run(_run("run_product", session_key="telegram::channel::-1001234"))
        # No session_key at all — create_per_run mints one per fire, so the label
        # comes from deliver_key.
        store.enqueue_run(_run("run_deliver", deliver_key="slack::channel::C0123"))

        by_title = _search_run_ids(store, "重构鉴权")
        by_display_name = _search_run_ids(store, "dev-ops")
        by_native_id = _search_run_ids(store, "C0123")
        by_duty = _search_run_ids(store, "值班群")
        by_product = _search_run_ids(store, "产品群")
        by_nothing = _search_run_ids(store, "no-such-channel-anywhere")
        labels = {run["id"]: run["session_label"] for run in store.list_runs_page(page_request=None).items}
    finally:
        store.close()

    # A workbench run is found by the title the row shows, and does not drag in
    # the other workbench run — the semi-join filters, it does not just exist.
    assert by_title == {"run_wb"}

    # Every run bound to #dev-ops, however it is bound: session-keyed, threaded,
    # or deliver-keyed. The display name is a scopes column the run never stores.
    assert by_display_name == {"run_im", "run_thread", "run_deliver"}
    # Typing the raw id still works (it is in the key), and finds the same set.
    assert by_native_id == by_display_name

    # Prefix collision: "-100123" is a prefix of "-1001234". Matching on the key
    # prefix alone would make 值班群 return the 产品群 run as well.
    assert by_duty == {"run_duty"}
    assert by_product == {"run_product"}

    assert by_nothing == set()

    # The searched strings are the rendered ones — otherwise this test could
    # pass while the UI displays something else entirely.
    assert labels["run_wb"] == "重构鉴权模块"
    assert labels["run_thread"] == labels["run_deliver"] == "#dev-ops"
    assert (labels["run_duty"], labels["run_product"]) == ("值班群", "产品群")


def test_search_finds_runs_by_the_definition_name_they_display(tmp_path: Path) -> None:
    """A textless run shows its task/watch name as the headline (§4.1), so that
    name has to be searchable — otherwise the list cannot find what it shows."""
    db_path = tmp_path / "vibe.sqlite"
    _build_schema(db_path)

    store = SQLiteBackgroundTaskStore(db_path)
    try:
        store.upsert_watch(
            {
                "id": "watch_disk",
                "name": "磁盘水位巡检",
                "command": "true",
                "prompt": "hello",
                "enabled": True,
                "created_at": NOW,
                "updated_at": NOW,
            }
        )
        store.upsert_scheduled_task(
            {
                "id": "task_digest",
                "name": "夜间日报",
                "prompt": "hello",
                "schedule_type": "cron",
                "cron": "0 3 * * *",
                "timezone": "UTC",
                "enabled": True,
                "created_at": NOW,
                "updated_at": NOW,
            }
        )
        store.remove_task("task_digest")  # the run still displays the name, so it stays searchable

        # No message/prompt text: the name is the only thing these rows show.
        store.enqueue_run(_run("run_disk", run_type="watch_runtime", definition_id="watch_disk"))
        store.enqueue_run(_run("run_digest", run_type="task_run", definition_id="task_digest"))
        store.enqueue_run(_run("run_other", message="unrelated work"))

        by_watch_name = _search_run_ids(store, "磁盘水位")
        by_deleted_task_name = _search_run_ids(store, "夜间日报")
        by_message = _search_run_ids(store, "unrelated")
        by_nothing = _search_run_ids(store, "no-such-text-anywhere")
        headlines = {
            run["id"]: run["definition_name"]
            for run in store.list_runs_page(page_request=None).items
        }
    finally:
        store.close()

    assert by_watch_name == {"run_disk"}
    assert by_deleted_task_name == {"run_digest"}  # soft-deleted, still displayed, still findable
    assert by_message == {"run_other"}  # the raw-column predicate still works
    # A definition-less run must not be dragged in by the semi-join, and an
    # unmatched term must still return nothing (the predicate is not always-true).
    assert by_nothing == set()

    # The thing searched for is the thing rendered: every hit's search term is
    # the headline the row actually shows.
    assert headlines["run_disk"] == "磁盘水位巡检"
    assert headlines["run_digest"] == "夜间日报"


def test_a_named_but_missing_session_stays_deleted(tmp_path: Path) -> None:
    """A run that names a session is describing *that* session.

    A ``create_per_run`` execution stores both its own fresh ``session_id`` and
    the definition's ``deliver_key``. When that session is later deleted, the
    key fallback used to resolve the delivery channel and fill
    ``session_platform``, so the row read as a live IM binding — the UI's
    "deleted" state (plan §4.2) was unreachable for exactly the runs that reach
    it most often. The fallback is for runs that never named a session.
    """
    db_path = tmp_path / "vibe.sqlite"
    _build_schema(db_path)
    engine = create_sqlite_engine(db_path)
    try:
        with engine.begin() as conn:
            upsert_scope(
                conn, platform="slack", scope_type="channel", native_id="C9", now=NOW, display_name="#releases"
            )
    finally:
        engine.dispose()

    deliver_key = "slack::channel::C9"
    store = SQLiteBackgroundTaskStore(db_path)
    try:
        store.enqueue_run(_run("run_gone", session_id="sess_deleted", deliver_key=deliver_key))
        store.enqueue_run(_run("run_keyonly", deliver_key=deliver_key))
        rows = {run["id"]: run for run in store.list_runs_page(page_request=None).items}

        gone = rows["run_gone"]
        assert gone["session_id"] == "sess_deleted", "the id the user needs to see is still on the row"
        assert gone["session_platform"] is None, "a deleted session must not borrow the delivery channel"
        assert gone["session_label"] is None
        assert gone["session_title"] is None

        # The same key on a run that never named a session still resolves --
        # this is the create_per_run definition's own label, not a fallback.
        assert rows["run_keyonly"]["session_label"] == "#releases"
        assert rows["run_keyonly"]["session_platform"] == "slack"

        # Both are still findable by the channel they deliver to.
        assert _search_run_ids(store, "#releases") == {"run_gone", "run_keyonly"}
    finally:
        store.close()


def test_search_matches_the_whitespace_the_row_displays(tmp_path: Path) -> None:
    """Row titles collapse whitespace; the stored message does not.

    ``runRowTitle`` shows the message's first non-empty line with runs of
    whitespace collapsed, and HTML would collapse them anyway. So the phrase on
    screen can differ from the column by its spacing, and a literal LIKE finds
    nothing for a user typing what they just read.
    """
    db_path = tmp_path / "vibe.sqlite"
    _build_schema(db_path)
    store = SQLiteBackgroundTaskStore(db_path)
    try:
        store.enqueue_run(_run("run_spaced", message="Summarize\tyesterday's   PRs\nand the rest"))
        store.enqueue_run(_run("run_other", message="Nothing to see"))

        # Typed exactly as the row renders it.
        assert _search_run_ids(store, "Summarize yesterday's PRs") == {"run_spaced"}
        # A single token is unaffected by the flexible pattern.
        assert _search_run_ids(store, "Summarize") == {"run_spaced"}
        # Wildcards in the term are still escaped, not honoured as syntax.
        assert _search_run_ids(store, "Summarize%PRs") == set()
    finally:
        store.close()


def test_run_types_facet_reports_what_the_ledger_holds(tmp_path: Path) -> None:
    """The selector's options come from the data, not only from a hardcoded list.

    ``webhook`` is written by the scheduler and preserved by the compatibility
    importer, but the UI's list omitted it — and search skips ``run_type`` on
    purpose (it is a translated chip), so those rows were visible under All and
    reachable by no filter at all.
    """
    db_path = tmp_path / "vibe.sqlite"
    _build_schema(db_path)
    store = SQLiteBackgroundTaskStore(db_path)
    try:
        assert store.list_run_types() == []
        for index, run_type in enumerate(["watch", "webhook", "agent_run", "webhook"]):
            store.enqueue_run(_run(f"run_{index}", run_type=run_type))
        # Distinct and ordered, so the option list is stable between loads.
        assert store.list_run_types() == ["agent_run", "watch", "webhook"]
        # Unfiltered on purpose: a facet narrowed to the current filter would
        # remove the very option the user needs to switch to.
        assert store.count_runs(run_type="webhook") == 2
        assert store.list_run_types() == ["agent_run", "watch", "webhook"]
    finally:
        store.close()
