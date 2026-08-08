import io

from core.memory import ui_access
from core.memory.ui_access import build_ui_read_proof, verify_ui_read_proof
from vibe import runtime


def test_ui_read_proof_is_bound_to_method_path_and_user_key() -> None:
    secret = "ui-controller-secret"
    proof = build_ui_read_proof(
        secret,
        method="GET",
        path="/internal/memory/profile",
        user_key="avibe:local",
    )

    assert verify_ui_read_proof(
        secret,
        proof,
        method="GET",
        path="/internal/memory/profile",
        user_key="avibe:local",
    )
    assert not verify_ui_read_proof(
        secret,
        proof,
        method="POST",
        path="/internal/memory/search",
        user_key="avibe:local",
    )
    assert not verify_ui_read_proof(
        "wrong-secret",
        proof,
        method="GET",
        path="/internal/memory/profile",
        user_key="avibe:local",
    )


def test_ui_read_secret_is_consumed_from_stdin_without_entering_child_environment(
    monkeypatch,
) -> None:
    secret = "ui-controller-secret"
    monkeypatch.setattr(ui_access, "_process_secret", None)
    monkeypatch.setattr(ui_access.sys, "stdin", io.StringIO(f"{secret}\n"))
    monkeypatch.setenv(ui_access.MEMORY_UI_SECRET_STDIN_ENV, "1")

    assert ui_access.initialize_process_ui_read_secret() == secret
    assert ui_access.MEMORY_UI_SECRET_STDIN_ENV not in ui_access.os.environ

    child_env = runtime._memory_ui_child_env(
        {"PATH": "/bin"},
        memory_ui_secret=secret,
    )
    assert child_env == {
        "PATH": "/bin",
        ui_access.MEMORY_UI_SECRET_STDIN_ENV: "1",
    }
    assert secret not in child_env.values()
