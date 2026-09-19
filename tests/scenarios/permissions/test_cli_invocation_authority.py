"""PERMISSIONS-026: an ordinary CLI command runs under the caller that invoked it.

Avibe already injects a remote caller's signed authorization snapshot into the
Agent subprocess environment. What was missing is the other half: every ``vibe``
command resolved to this machine's Owner, so a remote Viewer's Agent turn could
run management commands the same person's browser refuses.

The repair is one invocation-scoped authority resolved at ``main``, plus a role
floor for every parser leaf. These cases drive the real parser and the real
``main`` dispatch with the caller env written exactly the way the host writes it,
so the classification table, the env hop, the pre-effect refusal and the
authority's lifetime are covered at the entry point a user actually types.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from types import SimpleNamespace

import pytest

from core.caller_context import (
    AVIBE_CALLER_RESOURCE_CONTEXT_ENV,
    AVIBE_SESSION_ID_ENV,
    CALLER_CONTEXT_ENV_NAMES,
    CallerContext,
)
from storage.importer import ensure_sqlite_state
from storage.resource_access_service import (
    RESOURCE_USER_CONTEXT_METADATA_KEY,
    metadata_with_resource_user_context,
)
from vibe import cli
from vibe.authorization import (
    AuthorizationContext,
    current_invocation_authority,
    default_authorization_context,
)


_INSTANCE_ID = "inst-cli"


def _pair_instance(*, instance_id: str = _INSTANCE_ID, instance_kind: str = "organization") -> None:
    """Leave this installation paired the way a completed binding transition does.

    The caller env is not self-certifying. It crosses a process boundary and
    outlives the pairing it was minted under, so it is validated against the
    instance this machine is currently bound to — which means the fixtures have
    to be a real pairing rather than claims alone.
    """

    from config.v2_config import V2Config
    from storage import remote_access_authorization_service as binding

    config = V2Config.default()
    cloud = config.remote_access.vibe_cloud
    cloud.enabled = True
    cloud.instance_id = instance_id
    cloud.instance_kind = instance_kind
    cloud.instance_secret = "instance-secret"
    config.save()

    ensure_sqlite_state()
    started = binding.begin_instance_binding_transition(
        instance_id=instance_id,
        instance_kind=instance_kind,
    )
    assert binding.complete_instance_binding_transition(
        instance_id=instance_id,
        instance_kind=instance_kind,
        generation=started["generation"],
    )


@pytest.fixture(autouse=True)
def paired_instance():
    """Every case below runs on a currently valid Organization pairing."""

    _pair_instance()


def _snapshot(
    role: str,
    *,
    instance_kind: str = "organization",
    instance_id: str = _INSTANCE_ID,
) -> dict:
    """The signed snapshot the host records for a remote caller."""

    organization = instance_kind == "organization"
    context = AuthorizationContext(
        instance_role=role,
        subject=f"{role}-subject",
        email=f"{role}@example.com",
        instance_id=instance_id,
        instance_kind=instance_kind,
        instance_access_source="email",
        # A Personal instance has no Organization membership to carry.
        organization_id="org-1" if organization else None,
        organization_member_id="org-member" if organization else None,
        organization_role="member" if organization else None,
        is_remote=True,
    )
    return dict(metadata_with_resource_user_context({}, context)[RESOURCE_USER_CONTEXT_METADATA_KEY])


def _caller_env(
    role: str | None,
    *,
    instance_kind: str = "organization",
    instance_id: str = _INSTANCE_ID,
) -> dict[str, str]:
    """The subprocess env for a caller, or an empty env for a local invocation."""

    if role is None:
        return {}
    return CallerContext(
        session_id="ses_cli_caller",
        is_remote=True,
        resource_user_context=_snapshot(role, instance_kind=instance_kind, instance_id=instance_id),
    ).to_env()


def _parser_paths(parser: argparse.ArgumentParser, prefix: tuple[str, ...] = ()):
    """Every path the parser can reach, and whether it is a terminal command."""

    action = next(
        (item for item in parser._actions if isinstance(item, argparse._SubParsersAction)),
        None,
    )
    if action is None:
        yield prefix, True
        return
    yield prefix, False
    for name, sub in action.choices.items():
        yield from _parser_paths(sub, prefix + (name,))


@pytest.fixture
def invoke(monkeypatch):
    """Run the real CLI entry point under a caller env, and report its exit code."""

    def run(argv: list[str], *, caller: dict[str, str] | None = None) -> int:
        for name in CALLER_CONTEXT_ENV_NAMES:
            monkeypatch.delenv(name, raising=False)
        for name, value in (caller or {}).items():
            monkeypatch.setenv(name, value)
        monkeypatch.setattr(sys, "argv", ["vibe", *argv])
        with pytest.raises(SystemExit) as exited:
            cli.main()
        return int(exited.value.code or 0)

    return run


def _refusal(capsys) -> dict:
    payload = json.loads(capsys.readouterr().err.strip())
    assert payload["ok"] is False
    return payload


def test_permissions_026_every_cli_command_declares_a_role_floor():
    """PERMISSIONS-026: no command can be added without being classified."""
    paths = list(_parser_paths(cli.build_parser()))
    commands = {path for path, terminal in paths if terminal}
    reachable = {path for path, _ in paths}

    assert commands - set(cli._CLI_COMMAND_FLOORS) == set()
    # And nothing is classified that the parser cannot reach, so a renamed
    # command cannot leave a floor behind that silently guards nothing.
    assert set(cli._CLI_COMMAND_FLOORS) - reachable == set()
    # Only the two namespaces that are themselves runnable carry a floor
    # without being a leaf: bare ``vibe`` and bare ``vibe remote``.
    assert set(cli._CLI_COMMAND_FLOORS) & (reachable - commands) == {(), ("remote",)}


@pytest.mark.parametrize(
    ("caller", "expected_role", "expected_remote"),
    [
        ("local", None, False),
        ("editor", "editor", True),
        ("owner", "owner", True),
    ],
)
@pytest.mark.parametrize("bound_session", [False, True])
def test_permissions_026_authority_comes_from_the_caller_env(caller, expected_role, expected_remote, bound_session):
    """PERMISSIONS-026: the snapshot the host injected is the authority the CLI runs under."""
    env = _caller_env(None if caller == "local" else caller)
    if not bound_session:
        env.pop(AVIBE_SESSION_ID_ENV, None)
    authority = cli._cli_invocation_authority(env)

    if caller == "local":
        # A genuinely local invocation keeps standalone Owner administration.
        assert authority is None
        return
    assert authority.instance_role == expected_role
    assert authority.is_remote is expected_remote
    assert authority.email == f"{caller}@example.com"


@pytest.mark.parametrize("provenance", ["missing", "malformed", "not_an_object"])
@pytest.mark.parametrize("bound_session", [False, True])
def test_permissions_026_a_declared_remote_caller_without_provenance_fails_closed(provenance, bound_session):
    """PERMISSIONS-026: unusable provenance is anonymous remote, never local Owner."""
    env = _caller_env("editor")
    if not bound_session:
        env.pop(AVIBE_SESSION_ID_ENV, None)
    if provenance == "missing":
        env.pop(AVIBE_CALLER_RESOURCE_CONTEXT_ENV)
    elif provenance == "malformed":
        env[AVIBE_CALLER_RESOURCE_CONTEXT_ENV] = "{not json"
    else:
        env[AVIBE_CALLER_RESOURCE_CONTEXT_ENV] = json.dumps(["editor"])

    authority = cli._cli_invocation_authority(env)
    assert authority is not None
    assert authority.is_remote is True
    assert authority.instance_role is None
    assert authority.has_role("viewer") is False


def test_permissions_026_a_personal_pairing_admits_a_snapshot_without_organization_claims():
    """PERMISSIONS-026: validation is about the pairing, not about carrying Org claims."""
    _pair_instance(instance_kind="personal")

    authority = cli._cli_invocation_authority(_caller_env("editor", instance_kind="personal"))

    assert authority.instance_role == "editor"
    assert authority.is_remote is True
    assert authority.instance_kind == "personal"
    assert authority.organization_id is None


def test_permissions_026_unbound_background_editor_cannot_become_local_owner(invoke, monkeypatch, capsys):
    from core.caller_context import background_command_env

    env = background_command_env(
        session_id=None,
        source="scheduled_task",
        metadata={"resource_user_context": _snapshot("editor")},
        run_id="run_background",
    )
    assert AVIBE_SESSION_ID_ENV not in env
    # A forbidden command is refused by the real entry point before any restart.
    monkeypatch.setattr(cli, "cmd_restart", lambda *_a, **_kw: pytest.fail("must not restart"))
    assert invoke(["restart"], caller={key: value for key, value in env.items() if key in CALLER_CONTEXT_ENV_NAMES}) == 1
    assert _refusal(capsys)["code"] == "instance_access_forbidden"


@pytest.mark.parametrize("mismatch", ["instance_id", "instance_kind", "reconciling"])
def test_permissions_026_a_snapshot_from_another_pairing_is_anonymous_remote(mismatch):
    """PERMISSIONS-026: claims that no longer describe this installation carry no role.

    The environment outlives the pairing it was minted under. A snapshot naming
    another instance, another kind, or a binding that is mid-reconciliation is
    unusable here — and unusable means anonymous remote, not local Owner.
    """
    from storage import remote_access_authorization_service as binding

    if mismatch == "instance_id":
        env = _caller_env("owner", instance_id="inst-somewhere-else")
    elif mismatch == "instance_kind":
        env = _caller_env("owner", instance_kind="personal")
    else:
        # The snapshot still names the configured pairing; what it cannot name
        # is a binding that is mid-reconciliation and has published nothing.
        env = _caller_env("owner")
        binding.begin_instance_binding_transition(
            instance_id="inst-cli-reconciling",
            instance_kind="organization",
        )

    authority = cli._cli_invocation_authority(env)

    assert authority.is_remote is True
    assert authority.instance_role is None
    assert authority.has_role("viewer") is False


def test_permissions_026_a_stale_snapshot_is_refused_and_writes_nothing(invoke, capsys):
    """PERMISSIONS-026: the same env admits before the pairing moves and is refused after.

    Real ``main``, real signed snapshot, real binding transition. The refusal has
    to land in front of the effect, so the record the admitted call wrote is the
    same record the refused call leaves behind.
    """
    from core.vibe_agents import VibeAgentStore

    ensure_sqlite_state()
    store = VibeAgentStore()
    try:
        store.ensure_builtin_default_agent(backend="codex")
    finally:
        store.close()

    caller = _caller_env("member")
    assert invoke(["agent", "update", "codex", "--description", "admitted"], caller=caller) == 0
    store = VibeAgentStore()
    try:
        assert store.require("codex").description == "admitted"
    finally:
        store.close()

    # This installation re-pairs to another instance. The Agent process still
    # carries the environment the previous pairing minted.
    _pair_instance(instance_id="inst-cli-repaired")

    assert invoke(["agent", "update", "codex", "--description", "stale"], caller=caller) == 1
    payload = _refusal(capsys)
    assert payload["code"] == "instance_access_forbidden"
    assert payload["details"]["minimum_role"] == cli._CLI_COMMAND_FLOORS[("agent", "update")]

    store = VibeAgentStore()
    try:
        assert store.require("codex").description == "admitted"
    finally:
        store.close()


def test_permissions_026_a_stale_snapshot_cannot_reach_a_command_that_forwards_it(invoke, capsys):
    """PERMISSIONS-026: the commands that hand their own env to a service sit behind admission.

    Every producer that passes the caller snapshot on as an explicit context
    reads it from the same environment the invocation authority was validated
    from, and each declares an Editor floor. So the explicit precedence above is
    only ever fed claims this installation already accepted: once the pairing
    moves, the invocation is anonymous remote and the command never runs. The
    property is asserted where it matters -- nothing reserved, nothing queued --
    rather than inferred from the two mechanisms separately.
    """

    from sqlalchemy import func, select

    from core.vibe_agents import VibeAgentStore
    from storage.db import create_sqlite_engine
    from storage.models import agent_runs, agent_sessions

    ensure_sqlite_state()
    store = VibeAgentStore()
    try:
        store.ensure_builtin_default_agent(backend="codex")
    finally:
        store.close()

    caller = _caller_env("editor")
    _pair_instance(instance_id="inst-cli-repaired")

    engine = create_sqlite_engine()

    def counts() -> tuple[int, int]:
        with engine.connect() as conn:
            return (
                conn.execute(select(func.count()).select_from(agent_sessions)).scalar_one(),
                conn.execute(select(func.count()).select_from(agent_runs)).scalar_one(),
            )

    try:
        before = counts()
        argv = ["agent", "run", "--create-session", "--message", "hi", "--async", "--no-callback"]
        assert invoke(argv, caller=caller) == 1
        payload = _refusal(capsys)
        assert payload["code"] == "instance_access_forbidden"
        assert counts() == before
    finally:
        engine.dispose()


_CREATE_AGENT = ["agent", "create", "probe", "--backend", "codex"]


@pytest.mark.parametrize(
    ("role", "argv", "path", "admitted"),
    [
        ("viewer", ["session", "list"], ("session", "list"), True),
        ("viewer", ["agent", "list"], ("agent", "list"), False),
        ("editor", ["agent", "list"], ("agent", "list"), True),
        ("editor", _CREATE_AGENT, ("agent", "create"), False),
        ("editor", ["data", "retention"], ("data", "retention"), False),
        ("member", _CREATE_AGENT, ("agent", "create"), True),
        ("member", ["vault", "key", "export"], ("vault", "key", "export"), False),
        ("owner", ["vault", "key", "export"], ("vault", "key", "export"), True),
    ],
)
def test_permissions_026_each_namespace_admits_at_its_own_floor(
    monkeypatch, invoke, capsys, role, argv, path, admitted
):
    """PERMISSIONS-026: the floor decides admission, and a refusal never dispatches."""
    dispatched: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        cli,
        "_dispatch_parsed_command",
        lambda parser, args: dispatched.append(path) or sys.exit(0),
    )

    exit_code = invoke(argv, caller=_caller_env(role))

    if admitted:
        assert exit_code == 0
        assert dispatched == [path]
        return
    assert exit_code == 1
    assert dispatched == []
    payload = _refusal(capsys)
    assert payload["code"] == "instance_access_forbidden"
    assert payload["details"]["command"] == " ".join(("vibe", *path))
    assert payload["details"]["minimum_role"] == cli._CLI_COMMAND_FLOORS[path]


def test_permissions_026_a_refused_command_leaves_the_record_it_would_have_written(invoke, capsys):
    """PERMISSIONS-026: the refusal happens before the product effect, not after it."""
    from core.vibe_agents import VibeAgentStore

    ensure_sqlite_state()
    store = VibeAgentStore()
    try:
        store.ensure_builtin_default_agent(backend="codex")
        before = store.require("codex").description
    finally:
        store.close()

    assert invoke(["agent", "update", "codex", "--description", "rewritten"], caller=_caller_env("editor")) == 1
    assert _refusal(capsys)["code"] == "instance_access_forbidden"

    store = VibeAgentStore()
    try:
        assert store.require("codex").description == before
    finally:
        store.close()

    assert invoke(["agent", "update", "codex", "--description", "rewritten"], caller=_caller_env("member")) == 0
    store = VibeAgentStore()
    try:
        assert store.require("codex").description == "rewritten"
    finally:
        store.close()


@pytest.mark.parametrize("caller", ["editor", "member", "local"])
def test_permissions_026_an_existing_resource_guard_now_sees_the_invocation_caller(caller):
    """PERMISSIONS-026: namespace admission is not the answer; the resource's own policy still is.

    ``vibe vault edit`` is Editor work as a namespace, exactly as it is over HTTP,
    and the secret's own management policy decides the rest. That policy always
    existed — it simply never saw a CLI caller, because an omitted context
    resolved to Owner. Now it sees the invocation.
    """
    from storage import vault_service as vs
    from storage.db import create_sqlite_engine
    from storage.models import metadata as sqlite_metadata
    from storage.vault_crypto import Sealed
    from vibe.authorization import invocation_authority

    ensure_sqlite_state()
    engine = create_sqlite_engine()
    sqlite_metadata.create_all(engine)
    with engine.begin() as conn:
        vs.create_secret(conn, name="EDIT_KEY", sealed=Sealed(ciphertext="ct", nonce="n", wrap_meta="wm"))

    authority = None if caller == "local" else cli._cli_invocation_authority(_caller_env(caller))
    with invocation_authority(authority):
        with engine.begin() as conn:
            if caller == "editor":
                with pytest.raises(vs.VaultSecretAccessError):
                    vs.update_secret_metadata(conn, "EDIT_KEY", description="rewritten")
            else:
                assert vs.update_secret_metadata(conn, "EDIT_KEY", description="rewritten")

    with engine.connect() as conn:
        stored = vs.get_secret_meta(conn, "EDIT_KEY")
    assert (stored.get("description") == "rewritten") is (caller != "editor")


def test_permissions_026_an_explicit_context_still_outranks_the_invocation():
    """PERMISSIONS-026: validating the env changed who the ambient caller is, not the precedence.

    A stale snapshot leaves an anonymous remote invocation, which cannot manage
    a secret. A context the caller passes explicitly is still the one that
    answers, exactly as it did before the env was validated.
    """
    from storage import vault_service as vs
    from storage.db import create_sqlite_engine
    from storage.models import metadata as sqlite_metadata
    from storage.vault_crypto import Sealed
    from vibe.authorization import instance_owner_context, invocation_authority

    ensure_sqlite_state()
    engine = create_sqlite_engine()
    sqlite_metadata.create_all(engine)
    with engine.begin() as conn:
        vs.create_secret(conn, name="EXPLICIT_KEY", sealed=Sealed(ciphertext="ct", nonce="n", wrap_meta="wm"))

    stale = cli._cli_invocation_authority(_caller_env("owner", instance_id="inst-somewhere-else"))
    assert stale.instance_role is None

    with invocation_authority(stale):
        with engine.begin() as conn:
            with pytest.raises(vs.VaultSecretAccessError):
                vs.update_secret_metadata(conn, "EXPLICIT_KEY", description="ambient")
        with engine.begin() as conn:
            assert vs.update_secret_metadata(
                conn,
                "EXPLICIT_KEY",
                description="explicit",
                user_context=instance_owner_context(),
            )

    with engine.connect() as conn:
        assert vs.get_secret_meta(conn, "EXPLICIT_KEY").get("description") == "explicit"


def test_permissions_026_authority_lasts_exactly_one_invocation(monkeypatch, invoke):
    """PERMISSIONS-026: the authority is entered and reset around each dispatch, including on exit."""
    seen: list[object] = []
    monkeypatch.setattr(
        cli,
        "_dispatch_parsed_command",
        lambda parser, args: seen.append(current_invocation_authority()) or sys.exit(0),
    )

    assert current_invocation_authority() is None
    assert invoke(["agent", "list"], caller=_caller_env("editor")) == 0
    assert seen[-1].instance_role == "editor"
    # ``sys.exit`` unwinds through the context manager, so nothing survives it.
    assert current_invocation_authority() is None
    assert default_authorization_context().instance_role == "owner"

    # A later local invocation in the same process is local, not the previous caller.
    assert invoke(["agent", "list"], caller=None) == 0
    assert seen[-1] is None
    assert current_invocation_authority() is None


def test_permissions_026_a_below_floor_command_is_refused_while_a_remote_owner_is_not(monkeypatch, invoke):
    """PERMISSIONS-026: a remote caller is refused by role, not by being remote."""
    dispatched: list[str] = []
    monkeypatch.setattr(
        cli,
        "_dispatch_parsed_command",
        lambda parser, args: dispatched.append(args.command) or sys.exit(0),
    )

    assert invoke(["restart"], caller=_caller_env("editor")) == 1
    assert dispatched == []
    assert invoke(["restart"], caller=_caller_env("member")) == 0
    assert dispatched == ["restart"]


def test_permissions_026_internal_activation_entry_points_stay_owner_work(monkeypatch, invoke, capsys):
    """PERMISSIONS-026: the supervisor and installer argv are not an alternate door."""
    activated: list[str] = []
    monkeypatch.setattr(
        cli,
        "_dispatch_restart_supervisor",
        lambda argv: activated.append("restart-supervisor") or 0,
    )

    assert invoke(["__restart-supervisor"], caller=_caller_env("member")) == 1
    assert activated == []
    assert "__restart-supervisor: Instance role 'owner' is required" in capsys.readouterr().err

    assert invoke(["__restart-supervisor"], caller=None) == 0
    assert activated == ["restart-supervisor"]


def test_permissions_026_guided_remote_setup_refuses_before_a_pairing_key_is_solicited(
    monkeypatch, invoke, capsys
):
    """PERMISSIONS-026: ``vibe remote`` reads status at the management floor but pairs only as Owner."""
    from vibe import remote_access

    solicited: list[str] = []
    paired: list[str] = []
    monkeypatch.setattr(remote_access, "status", lambda: {"ok": True, "paired": False})
    monkeypatch.setattr(remote_access, "pair", lambda *a, **k: paired.append("pair") or {"ok": True})
    monkeypatch.setattr(cli, "_wait_for_pairing_key_ready", lambda: solicited.append("key") or False)

    assert invoke(["remote"], caller=_caller_env("member")) == 1
    payload = _refusal(capsys)
    assert payload["code"] == "instance_access_forbidden"
    assert payload["details"]["command"] == "vibe remote pair"
    # Nothing was solicited and nothing was spent: the refusal is in front of both.
    assert solicited == []
    assert paired == []

    # The status branch a Member is admitted for still answers.
    monkeypatch.setattr(remote_access, "status", lambda: {"ok": True, "paired": True, "device_name": "avibe"})
    assert invoke(["remote"], caller=_caller_env("member")) == 0
    assert paired == []


@pytest.fixture
def caller_environment(monkeypatch):
    """Put a real caller env on this process, the way an Agent subprocess has it."""

    def apply(role: str) -> dict[str, str]:
        env = _caller_env(role)
        for name in CALLER_CONTEXT_ENV_NAMES:
            monkeypatch.delenv(name, raising=False)
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        return env

    return apply


def test_permissions_026_an_admitted_restart_hands_the_supervisor_no_caller(
    monkeypatch, invoke, tmp_path, caller_environment
):
    """PERMISSIONS-026: the caller's authority ends at the process Avibe owns.

    A Member may restart, and the restart is admitted on that. What a Member may
    not do is enter ``__restart-supervisor``, which is Owner work — so the
    detached job cannot be handed the Member's provenance, or it would fail its
    own entry and leave the restart stuck at ``scheduled``. The job is created
    after the parent operation has already been admitted, so dropping the caller
    there costs the parent nothing.
    """
    from vibe import restart_supervisor

    caller = caller_environment("member")
    assert cli._cli_invocation_authority().instance_role == "member"

    monkeypatch.setattr(restart_supervisor, "get_restart_environment", lambda vibe_path=None: None)
    monkeypatch.setattr(restart_supervisor, "get_safe_cwd", lambda: str(tmp_path))
    monkeypatch.setattr(restart_supervisor, "_prune_restart_logs", lambda: None)
    spawned: dict = {}

    def fake_popen(command, **kwargs):
        spawned["command"] = command
        spawned["env"] = kwargs["env"]

        class Proc:
            pid = 45678

        return Proc()

    monkeypatch.setattr(restart_supervisor.subprocess, "Popen", fake_popen)

    result = restart_supervisor.schedule_restart(delay_seconds=0, vibe_path="/bin/vibe", trigger="agent")

    assert result["state"] == "scheduled"
    assert "__restart-supervisor" in spawned["command"]
    # ``None`` would have inherited this process silently, caller and all.
    assert spawned["env"] is not None
    assert set(spawned["env"]) & CALLER_CONTEXT_ENV_NAMES == set()
    assert spawned["env"]["PATH"] == os.environ["PATH"]
    # The parent kept its own authority; only the child's environment lost it.
    assert cli._cli_invocation_authority().instance_role == "member"
    assert os.environ[AVIBE_CALLER_RESOURCE_CONTEXT_ENV] == caller[AVIBE_CALLER_RESOURCE_CONTEXT_ENV]

    # The real child entry point, over the environment it was actually given.
    activated: list[str] = []
    monkeypatch.setattr(cli, "_dispatch_restart_supervisor", lambda argv: activated.append("ran") or 0)
    assert invoke(["__restart-supervisor"], caller=spawned["env"]) == 0
    assert activated == ["ran"]

    # And with the caller still attached it is refused, so the strip is what
    # admits the child rather than a floor that stopped guarding the door.
    assert invoke(["__restart-supervisor"], caller=caller) == 1
    assert activated == ["ran"]


def test_permissions_026_a_deferred_activation_runs_as_the_installation(
    monkeypatch, tmp_path, caller_environment
):
    """PERMISSIONS-026: the deferred activation spawn is the same boundary as the restart.

    It is the Windows path: activation happens after this process releases the
    launcher, so it is Avibe's own work by construction. The interpreter
    isolation this spawn already needs is unchanged — only the caller hop ends.
    """
    from vibe import upgrade

    caller_environment("member")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "checkout"))
    candidate = tmp_path / "generation" / "python"
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.touch()
    monkeypatch.setattr(upgrade, "_candidate_python", lambda launcher: candidate)
    spawned: dict = {}

    def fake_popen(command, **kwargs):
        spawned["command"] = command
        spawned["env"] = kwargs["env"]

        class Proc:
            pid = 99001

        return Proc()

    monkeypatch.setattr(upgrade.subprocess, "Popen", fake_popen)

    upgrade.defer_upgrade_activation(
        SimpleNamespace(
            launcher=tmp_path / "stable" / "vibe",
            candidate_launcher=tmp_path / "generation" / "vibe",
            source_generation=None,
        ),
        parent_pid=os.getpid(),
    )

    assert "__activate-upgrade" in spawned["command"]
    assert set(spawned["env"]) & CALLER_CONTEXT_ENV_NAMES == set()
    # The isolation this spawn already had is still what it was.
    assert "PYTHONPATH" not in spawned["env"]
    assert "PYTHONHOME" not in spawned["env"]
    assert spawned["env"]["PATH"] == os.environ["PATH"]


def test_permissions_026_a_runtime_owned_process_starts_without_the_caller(
    monkeypatch, tmp_path, caller_environment
):
    """PERMISSIONS-026: the service and UI processes Avibe starts are its own.

    A real child reports the environment it actually received, so this covers
    the ``env=None`` case too: the spawn materializes the environment instead of
    letting the child inherit this process — caller included.
    """
    from vibe import runtime

    monkeypatch.setattr(runtime.paths, "get_runtime_dir", lambda: tmp_path)
    caller_environment("editor")
    assert os.environ[AVIBE_CALLER_RESOURCE_CONTEXT_ENV]

    probe = "import json, os; print(json.dumps(sorted(k for k in os.environ if k.startswith('AVIBE_'))))"
    process = runtime.spawn_service_background_process(
        [sys.executable, "-c", probe],
        "service_stdout.log",
        "service_stderr.log",
    )
    assert process.wait(timeout=30) == 0

    stdout_path = tmp_path / "service_stdout.log"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not stdout_path.exists():
        time.sleep(0.05)
    inherited = set(json.loads(stdout_path.read_text(encoding="utf-8").strip()))

    assert inherited & CALLER_CONTEXT_ENV_NAMES == set()
    # Only the caller set was removed: the rest of the environment is intact.
    assert "AVIBE_HOME" in inherited
