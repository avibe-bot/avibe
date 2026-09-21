from __future__ import annotations

from core.handlers.model_hub.adapter import RawCallOutcome, RawOutcomeKind
from core.handlers.model_hub.classification import classify_outcome
from core.handlers.model_hub.provenance import (
    BoundedProvenanceStore,
    PreparedGatewayRoute,
    TurnCorrelationRegistry,
)
from core.run_settlement import SETTLED_BY_STOPPED, SETTLED_BY_TERMINAL_RESULT


def _registry(tmp_path):
    return TurnCorrelationRegistry(
        BoundedProvenanceStore(tmp_path / "provenance.json")
    )


def _success(*, model_id: str = "upstream", source_id: str = "source") -> RawCallOutcome:
    return RawCallOutcome(
        kind=RawOutcomeKind.SUCCESS,
        http_status=200,
        error_code=None,
        redacted_message=None,
        stream_started=False,
        model_id=model_id,
        source_id=source_id,
    )


def _launch(
    registry: TurnCorrelationRegistry,
    *,
    turn_id: str,
    requested_model_id: str,
    resolved_model_id: str = "shared-upstream",
    source_id: str = "source",
    gateway_request_model_id: str | None = None,
) -> tuple[str, str, dict[str, str]]:
    token = registry.credentials(
        "codex",
        "codex-process",
        turn_id,
        request_scoped=True,
    )
    route_handle = registry.prepare_gateway_turn(
        backend="codex",
        token=token,
        turn_id=turn_id,
        requested_model_id=requested_model_id,
        resolved_model_id=resolved_model_id,
        source_id=source_id,
        gateway_request_model_id=gateway_request_model_id,
        via_mapping=True,
    )
    metadata = registry.gateway_request_metadata(
        backend="codex",
        token=token,
        turn_id=turn_id,
    )
    return token, route_handle, metadata


def test_explicit_route_handles_are_process_scoped_and_no_turn_inference(
    tmp_path,
):
    registry = _registry(tmp_path)
    token = registry.credentials(
        "codex",
        "codex-process",
        None,
        request_scoped=True,
    )

    assert registry.authenticates("codex", token)
    assert registry.gateway_request_metadata(
        backend="codex",
        token=token,
        turn_id=None,
    ) == {}
    assert registry._traces == {}

    token, route_handle, metadata = _launch(
        registry,
        turn_id="turn-1",
        requested_model_id="alias-a",
    )
    assert metadata == {
        "avibe_route_id": route_handle,
        "avibe_turn_id": "turn-1",
    }
    assert route_handle != token
    assert not registry.authenticates("codex", route_handle)


def test_untracked_metadata_requires_an_exact_registered_route(tmp_path):
    registry = _registry(tmp_path)
    token = registry.credentials("codex", "codex-process", None, request_scoped=True)
    route = PreparedGatewayRoute("untracked-alias", "upstream", "source", "untracked-alias")
    assert registry.gateway_request_metadata(
        backend="codex", token=token, turn_id=None, route=route,
    ) == {}
    handle = registry.prepare_gateway_turn(
        backend="codex", token=token, turn_id=None,
        requested_model_id=route.requested_model_id,
        resolved_model_id=route.resolved_model_id,
        source_id=route.source_id,
        gateway_request_model_id=route.gateway_request_model_id,
        via_mapping=False,
    )
    metadata = registry.gateway_request_metadata(
        backend="codex", token=token, turn_id=None, route=route,
    )
    assert metadata == {"avibe_route_id": handle, "avibe_turn_id": ""}
    with registry.gateway_terminalizer(
        backend="codex", token=token, request_metadata=metadata,
    ) as untracked:
        assert untracked.resolution_model("untracked-alias") == "untracked-alias"
        assert untracked.turn_id is None
    assert not registry._traces
    assert not registry._turn_scopes
    scope = registry._scopes[("codex", "codex-process")]
    assert not scope.active_turns
    assert not scope.prepared_routes
    assert len(scope.route_tokens) == len(registry._credentials) == 1


def test_legacy_codex_route_credentials_are_revoked_on_explicit_promotion(
    tmp_path,
):
    registry = _registry(tmp_path)
    token = registry.credentials("codex", "codex-process", "legacy-turn")
    route_token = registry.prepare_gateway_turn(
        backend="codex",
        token=token,
        turn_id="legacy-turn",
        requested_model_id="legacy-alias",
        resolved_model_id="shared-upstream",
        source_id="source",
        via_mapping=False,
    )
    assert registry.authenticates("codex", route_token)

    promoted = registry.credentials(
        "codex",
        "codex-process",
        "explicit-turn",
        request_scoped=True,
    )
    assert promoted == token
    assert not registry.authenticates("codex", route_token)
    assert len(registry._credentials) == 1

    explicit_route = registry.prepare_gateway_turn(
        backend="codex",
        token=promoted,
        turn_id="explicit-turn",
        requested_model_id="explicit-alias",
        resolved_model_id="shared-upstream",
        source_id="source",
        via_mapping=False,
    )
    assert explicit_route != promoted
    assert not registry.authenticates("codex", explicit_route)


def test_aliases_sharing_an_upstream_get_distinct_explicit_routes(tmp_path):
    registry = _registry(tmp_path)
    token_a, route_a, metadata_a = _launch(
        registry,
        turn_id="turn-a",
        requested_model_id="alias-a",
    )
    token_b, route_b, metadata_b = _launch(
        registry,
        turn_id="turn-b",
        requested_model_id="alias-b",
    )

    assert token_a == token_b
    assert route_a != route_b
    assert metadata_a["avibe_route_id"] == route_a
    assert metadata_b["avibe_route_id"] == route_b

    with registry.gateway_terminalizer(
        backend="codex",
        token=token_a,
        request_metadata=metadata_a,
    ) as first, registry.gateway_terminalizer(
        backend="codex",
        token=token_b,
        request_metadata=metadata_b,
    ) as second:
        assert first.resolution_model("shared-upstream") == "alias-a"
        assert second.resolution_model("shared-upstream") == "alias-b"
        assert first.turn_id == "turn-a"
        assert second.turn_id == "turn-b"
        first.mark_downstream_canceled()
        second.mark_downstream_canceled()


def test_same_turn_concurrent_attempts_keep_separate_request_identity(tmp_path):
    registry = _registry(tmp_path)
    token, _route, metadata = _launch(
        registry,
        turn_id="turn-concurrent",
        requested_model_id="alias",
    )

    with registry.gateway_terminalizer(
        backend="codex",
        token=token,
        request_metadata=metadata,
    ) as first, registry.gateway_terminalizer(
        backend="codex",
        token=token,
        request_metadata=metadata,
    ) as second:
        assert first.resolution_model("shared-upstream") == "alias"
        assert second.resolution_model("shared-upstream") == "alias"
        trace = registry._traces["turn-concurrent"]
        assert len(trace.pending_attempts) == 2
        first.begin_attempt(
            source_id="source-a",
            resolved_model_id="shared-upstream",
            channel="hub",
            via_mapping=True,
        )
        second.begin_attempt(
            source_id="source-b",
            resolved_model_id="shared-upstream",
            channel="hub",
            via_mapping=True,
        )
        assert len(trace.pending_attempts) == 2
        first.finish_attempt(
            outcome=_success(source_id="source-a"),
            decision=classify_outcome(_success(source_id="source-a")),
        )
        second.finish_attempt(
            outcome=_success(source_id="source-b"),
            decision=classify_outcome(_success(source_id="source-b")),
        )

    registry.settle(
        "turn-concurrent",
        settled_by=SETTLED_BY_TERMINAL_RESULT,
    )
    assert registry.store.get("turn-concurrent")["outcome"] == "served"


def test_late_request_on_same_route_never_claims_newer_turn(tmp_path):
    registry = _registry(tmp_path)
    token, route_handle, metadata_old = _launch(
        registry,
        turn_id="turn-old",
        requested_model_id="same-alias",
    )
    registry.settle("turn-old", settled_by=SETTLED_BY_TERMINAL_RESULT)
    _token, replacement_handle, metadata_new = _launch(
        registry,
        turn_id="turn-new",
        requested_model_id="same-alias",
    )
    assert replacement_handle == route_handle

    with registry.gateway_terminalizer(
        backend="codex",
        token=token,
        request_metadata=metadata_old,
    ) as late:
        assert late.resolution_model("shared-upstream") == "same-alias"
        assert late.turn_id is None
        late.mark_downstream_canceled()

    # Off its own route it routes by the model the request names, and claims a
    # turn no more and no less than on it: what a settled turn cannot do is own
    # a request, which has nothing to do with the model the request names.
    with registry.gateway_terminalizer(
        backend="codex",
        token=token,
        request_metadata=metadata_old,
    ) as stale_switch:
        assert stale_switch.resolution_model("other-alias") == "other-alias"
        assert stale_switch.turn_id is None
        stale_switch.mark_downstream_canceled()

    with registry.gateway_terminalizer(
        backend="codex",
        token=token,
        request_metadata=metadata_new,
    ) as current:
        assert current.resolution_model("shared-upstream") == "same-alias"
        assert current.turn_id == "turn-new"


def test_invalid_identity_and_live_route_mismatch_fail_closed_without_poisoning(
    tmp_path,
):
    registry = _registry(tmp_path)
    token, route_a, metadata_a = _launch(
        registry,
        turn_id="turn-a",
        requested_model_id="alias-a",
    )
    _token, route_b, metadata_b = _launch(
        registry,
        turn_id="turn-b",
        requested_model_id="alias-b",
    )

    cases = [
        None,
        {"avibe_route_id": route_a},
        {"avibe_route_id": "foreign-route", "avibe_turn_id": "turn-a"},
        {"avibe_route_id": "路由-非ASCII", "avibe_turn_id": "turn-a"},
    ]
    for request_metadata in cases:
        with registry.gateway_terminalizer(
            backend="codex",
            token=token,
            request_metadata=request_metadata,
        ) as terminalizer:
            model = terminalizer.resolution_model(
                "wrong-wire-model" if request_metadata else "shared-upstream"
            )
            assert model is None
            terminalizer.mark_downstream_canceled()

    with registry.gateway_terminalizer(
        backend="codex",
        token=token,
        request_metadata={
            "avibe_route_id": route_b,
            "avibe_turn_id": metadata_a["avibe_turn_id"],
        },
    ) as mismatch:
        assert mismatch.resolution_model("shared-upstream") is None
        mismatch.mark_downstream_canceled()

    assert not registry._traces["turn-a"].ambiguous
    assert not registry._traces["turn-b"].ambiguous


def test_live_handle_routes_another_model_and_keeps_its_turn(tmp_path):
    """A verified handle authenticates a process and a turn, never a model.

    Codex re-serialises a thread under the OUTGOING model before the first turn
    on a new one, so that hop arrives on the new turn's handle naming the old
    model. Refusing it here stranded the thread for good. Whether Avibe
    configured the named model is resolution's question; routing only has to
    hand the request on and keep it attributed to the turn that made it.
    """

    registry = _registry(tmp_path)
    token, _route, metadata = _launch(
        registry,
        turn_id="turn-new",
        requested_model_id="alias-new",
        gateway_request_model_id="alias-new",
    )

    with registry.gateway_terminalizer(
        backend="codex",
        token=token,
        request_metadata=metadata,
    ) as compaction:
        assert compaction.resolution_model("alias-old") == "alias-old"
        assert compaction.turn_id == "turn-new"
        compaction.begin_attempt(
            source_id="source",
            resolved_model_id="shared-upstream",
            channel="hub",
            via_mapping=True,
        )
        compaction.finish_attempt(
            outcome=_success(),
            decision=classify_outcome(_success()),
        )

    with registry.gateway_terminalizer(
        backend="codex",
        token=token,
        request_metadata=metadata,
    ) as turn:
        assert turn.resolution_model("alias-new") == "alias-new"
        assert turn.turn_id == "turn-new"
        turn.begin_attempt(
            source_id="source",
            resolved_model_id="shared-upstream",
            channel="hub",
            via_mapping=True,
        )
        turn.finish_attempt(
            outcome=_success(),
            decision=classify_outcome(_success()),
        )

    trace = registry._traces["turn-new"]
    assert not trace.ambiguous
    assert "turn-new" not in registry._scopes[("codex", "codex-process")].ambiguous_turns


def test_handle_routes_every_model_its_process_names_in_any_order(tmp_path):
    """A handle authorizes no model, so no order of them can starve one.

    Bounding what a live turn may name brings back the bug the bound was meant
    to contain. Counted, the first other model a process names spends the
    allowance and the outgoing model's re-serialisation is refused behind it;
    ordered, a turn that names its own model first refuses the hop that had to
    precede it. Both stranded the thread again from the other side. Whether a
    named model is configured at all is resolution's question, and it answers
    it from this agent's menu.
    """

    registry = _registry(tmp_path)
    token, _route, metadata = _launch(
        registry,
        turn_id="turn-many",
        requested_model_id="alias",
        gateway_request_model_id="alias",
    )

    def route(gateway_model_id: str) -> tuple[str | None, str | None]:
        with registry.gateway_terminalizer(
            backend="codex",
            token=token,
            request_metadata=metadata,
        ) as terminalizer:
            model = terminalizer.resolution_model(gateway_model_id)
            terminalizer.mark_downstream_canceled()
            return model, terminalizer.turn_id

    # Some other configured model first, then the outgoing one the thread is
    # re-serialised under, then the turn's own route.
    assert route("alias-other") == ("alias-other", "turn-many")
    assert route("alias-old") == ("alias-old", "turn-many")
    assert route("alias") == ("alias", "turn-many")
    # The route's upstream target is a name in another namespace, so it routes
    # as itself like any other id the handle did not mint.
    assert route("shared-upstream") == ("shared-upstream", "turn-many")
    # A turn's own request closes the handle to nothing: a retry of the
    # migration hop behind it is the same request on the same handle.
    assert route("alias-old") == ("alias-old", "turn-many")
    trace = registry._traces["turn-many"]
    assert not trace.ambiguous
    assert "turn-many" not in registry._scopes[("codex", "codex-process")].ambiguous_turns


def test_outgoing_model_survives_an_upstream_name_collision(tmp_path):
    """Menu ids and upstream target ids are separate namespaces that collide.

    A route proves one spelling of itself: the id Avibe told the launch to
    send. An outgoing menu model spelled like the new route's upstream target
    is not that, and reading it as this route's would send the outgoing
    model's re-serialisation down the new route — another source, another
    model — which is the aliasing a per-route handle exists to prevent.
    """

    registry = _registry(tmp_path)
    token, _route, metadata = _launch(
        registry,
        turn_id="turn-new",
        requested_model_id="alias-new",
        # The new route relays through a source whose upstream model happens to
        # be spelled like the menu model the thread is migrating away from.
        resolved_model_id="gpt-5.5",
        gateway_request_model_id="alias-new",
    )

    with registry.gateway_terminalizer(
        backend="codex",
        token=token,
        request_metadata=metadata,
    ) as migration:
        assert migration.resolution_model("gpt-5.5") == "gpt-5.5"
        assert migration.turn_id == "turn-new"
        migration.mark_downstream_canceled()

    with registry.gateway_terminalizer(
        backend="codex",
        token=token,
        request_metadata=metadata,
    ) as turn:
        assert turn.resolution_model("alias-new") == "alias-new"
        assert turn.turn_id == "turn-new"
        turn.mark_downstream_canceled()


def test_retirement_revokes_explicit_auth_and_preserves_exact_closed_fact(tmp_path):
    registry = _registry(tmp_path)
    token, _route, metadata = _launch(
        registry,
        turn_id="turn-exact",
        requested_model_id="alias",
    )
    _launch(
        registry,
        turn_id="turn-peer",
        requested_model_id="peer",
    )
    with registry.gateway_terminalizer(
        backend="codex",
        token=token,
        request_metadata=metadata,
    ) as terminalizer:
        assert terminalizer.resolution_model("shared-upstream") == "alias"
        terminalizer.fail("protocol_error")

    registry.close_turn_admission(
        "turn-exact",
        settled_by=SETTLED_BY_TERMINAL_RESULT,
    )
    registry.retire_scope(
        "codex",
        "codex-process",
        terminal_turn_id="turn-exact",
    )
    assert not registry.authenticates("codex", token)
    registry.settle("turn-exact", settled_by=SETTLED_BY_TERMINAL_RESULT)
    assert registry.store.get("turn-exact")["outcome"] == "failed_terminal"


def test_legacy_retirement_with_peers_does_not_bypass_exact_scope_guard(tmp_path):
    registry = _registry(tmp_path)
    token = registry.credentials("codex", "codex-process", "turn-legacy")
    registry.begin_gateway_request(
        backend="codex",
        token=token,
        requested_model_id="legacy-alias",
    )
    registry.credentials("codex", "codex-process", "turn-peer")
    registry.retire_scope(
        "codex",
        "codex-process",
        terminal_turn_id="turn-legacy",
    )
    registry.settle(
        "turn-legacy",
        settled_by=SETTLED_BY_TERMINAL_RESULT,
    )
    assert registry.store.get("turn-legacy") is None


def test_explicit_promotion_clears_legacy_scope_poison_for_new_turns(tmp_path):
    registry = _registry(tmp_path)
    token = registry.credentials("codex", "codex-process", "legacy-turn")
    registry.begin_gateway_request(
        backend="codex",
        token=token,
        requested_model_id="legacy-alias",
    )
    registry.credentials("codex", "codex-process", None)
    assert registry._scopes[("codex", "codex-process")].untracked_use
    assert registry._traces["legacy-turn"].ambiguous

    promoted = registry.credentials(
        "codex",
        "codex-process",
        "explicit-turn",
        request_scoped=True,
    )
    assert not registry._scopes[("codex", "codex-process")].untracked_use
    _token, _route, metadata = _launch(
        registry,
        turn_id="explicit-turn",
        requested_model_id="explicit-alias",
    )
    assert promoted == _token
    with registry.gateway_terminalizer(
        backend="codex",
        token=promoted,
        request_metadata=metadata,
    ) as terminalizer:
        assert terminalizer.resolution_model("shared-upstream") == "explicit-alias"
        outcome = _success(source_id="source")
        terminalizer.begin_attempt(
            source_id="source",
            resolved_model_id="shared-upstream",
            channel="hub",
            via_mapping=True,
        )
        terminalizer.finish_attempt(
            outcome=outcome,
            decision=classify_outcome(outcome),
        )
    registry.settle(
        "explicit-turn",
        settled_by=SETTLED_BY_TERMINAL_RESULT,
    )
    assert registry.store.get("explicit-turn")["outcome"] == "served"


def test_explicit_route_retention_is_bounded_by_routes_not_completed_turns(tmp_path):
    registry = _registry(tmp_path)
    for number in range(40):
        token, _route, metadata = _launch(
            registry,
            turn_id=f"turn-{number}",
            requested_model_id="same-alias",
        )
        with registry.gateway_terminalizer(
            backend="codex",
            token=token,
            request_metadata=metadata,
        ) as terminalizer:
            assert terminalizer.resolution_model("shared-upstream") == "same-alias"
            terminalizer.mark_downstream_canceled()
        registry.settle(
            f"turn-{number}",
            settled_by=SETTLED_BY_TERMINAL_RESULT,
        )

    scope = registry._scopes[("codex", "codex-process")]
    assert len(scope.route_tokens) == 1
    assert len(registry._credentials) == 1
    assert scope.active_turns == set()
    assert scope.prepared_routes == {}
    assert registry._traces == {}


def test_native_dispatcher_calls_still_work_after_explicit_hub_mode(tmp_path):
    registry = _registry(tmp_path)
    token, _route, metadata = _launch(
        registry,
        turn_id="hub-turn",
        requested_model_id="alias",
    )
    with registry.gateway_terminalizer(
        backend="codex",
        token=token,
        request_metadata=metadata,
    ) as terminalizer:
        assert terminalizer.resolution_model("shared-upstream") == "alias"
        terminalizer.mark_downstream_canceled()
    registry.settle("hub-turn", settled_by=SETTLED_BY_TERMINAL_RESULT)

    registry.begin_native_attempt(
        backend="codex",
        process_scope="codex-process",
        turn_id="native-turn",
        requested_model_id="native",
        source_id="native-source",
        resolved_model_id="native",
        via_mapping=False,
    )
    registry.settle("native-turn", settled_by=SETTLED_BY_TERMINAL_RESULT)
    assert registry.store.get("native-turn")["outcome"] == "served"

    registry.mark_no_candidate(
        backend="codex",
        process_scope="codex-process",
        turn_id="empty-turn",
        requested_model_id="native",
        supply_state="waiting",
    )
    registry.settle("empty-turn", settled_by=SETTLED_BY_TERMINAL_RESULT)
    assert registry.store.get("empty-turn")["outcome"] == "no_candidate"


def test_admission_close_after_terminalizer_open_keeps_owned_request(tmp_path):
    registry = _registry(tmp_path)
    token, _route, metadata = _launch(
        registry,
        turn_id="drain-turn",
        requested_model_id="alias",
    )
    terminalizer = registry.gateway_terminalizer(
        backend="codex",
        token=token,
        request_metadata=metadata,
    )
    registry.close_turn_admission(
        "drain-turn",
        settled_by=SETTLED_BY_TERMINAL_RESULT,
    )
    assert terminalizer.resolution_model("shared-upstream") == "alias"
    assert terminalizer.turn_id == "drain-turn"
    terminalizer.mark_downstream_canceled()
