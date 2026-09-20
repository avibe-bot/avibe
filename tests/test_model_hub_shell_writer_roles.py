"""Builtin argument roles, consumed by the real migration scan/apply boundary.

Shell text is data only. The imported home fixture isolates all paths, uses
FakeKeychain, and forbids subprocess/native credential access. The service's
fixture guard and adapter never inspect processes or start a real service.
"""

import asyncio

import psutil
import pytest

from core.handlers.model_hub.migration_journal import NativeFileEdit, TakeoverStateError
from core.handlers.model_hub.migration_shell import cleanup_shell_profile, read_shell_profiles
from core.handlers.model_hub.service import ModelHubError
from tests.scenarios.model_hub.test_model_hub_migration_scenarios import _service
from tests.test_model_hub_persisted_inventory import home  # noqa: F401


KEY = "OPENAI_API_KEY"


def case(label, line, writer, *, name=KEY, path=".profile"):
    return pytest.param(path, name, line + "\n", writer, id=label)


CASES = [
    case("printf-compact", "printf -vOPENAI_API_KEY '%s' fixture-new", True),
    case("printf-separate", "printf -v OPENAI_API_KEY '%s' fixture-new", True),
    case("printf-array", "printf -v 'OPENAI_API_KEY[0]' '%s' fixture-new", True),
    case("printf-format-data", "printf '%s' -vOPENAI_API_KEY", False),
    case("printf-endoptions-data", "printf -- -vOPENAI_API_KEY", False),
    case("read-compact-array", "read -aOPENAI_API_KEY", True),
    case("read-bundle-array", "read -raOPENAI_API_KEY", True),
    case("read-array-element", "read 'OPENAI_API_KEY[0]'", True),
    case("read-separate-array", "read -a OPENAI_API_KEY", True),
    case("read-combined-prompt-data", "read -rp OPENAI_API_KEY unrelated", False),
    case("read-separate-prompt-data", "read -r -p OPENAI_API_KEY unrelated", False),
    case("read-ignored-extra-names", "read -a unrelated OPENAI_API_KEY", False),
    case("read-default-reply", "read -r", True, name="REPLY"),
    case("getopts-endoptions", "getopts -- 'a:' OPENAI_API_KEY", True),
    case("getopts-separate", "getopts 'a:' OPENAI_API_KEY", True),
    case("getopts-optstring-data", "getopts -- OPENAI_API_KEY unrelated", False),
    case("getopts-implicit-arg", "getopts 'a:' unrelated -a fixture-new", True, name="OPTARG"),
    case("getopts-implicit-index", "getopts 'a:' unrelated -a fixture-new", True, name="OPTIND"),
    case("unset-array-element", "unset 'OPENAI_API_KEY[0]'", True),
    case("unset-function-data", "unset -f OPENAI_API_KEY", False),
    case("command-query-read", "command -v read OPENAI_API_KEY", False),
    case("command-query-printf", "command -V printf -v OPENAI_API_KEY '%s' fixture-new", False),
    case("command-writer-read", "command read OPENAI_API_KEY", True),
    case("declare-query-data", "declare -p OPENAI_API_KEY=fixture-new", False),
    case("declare-function-data", "declare -f OPENAI_API_KEY=fixture-new", False),
    case("declare-value", "declare -x OPENAI_API_KEY=fixture-new", True),
    case("export-no-value", "export OPENAI_API_KEY", False),
    case("env-split-string", "env -S 'OPENAI_API_KEY=fixture-new unrelated'", True),
    case("env-attached-split-string", "env -S'OPENAI_API_KEY=fixture-new unrelated'", True),
    case("env-assignment", "env OPENAI_API_KEY=fixture-new unrelated", True),
    case("env-option-data", "env -u OPENAI_API_KEY unrelated", False),
    case("echo-quoted-data", "echo 'printf -vOPENAI_API_KEY %s fixture-new'", False),
    case("include-ignored", "source ~/.other", False),
    case("mapfile-bundle-control", "mapfile -tn1 OPENAI_API_KEY", True),
    case("mapfile-callback-data", "mapfile -C 'echo OPENAI_API_KEY' unrelated", False),
    case("printf-percent-n", "printf '%n' OPENAI_API_KEY", True),
    case("printf-percent-n-second", "printf '%s%n' text OPENAI_API_KEY", True),
    case("printf-percent-n-other", "printf '%s%n' OPENAI_API_KEY unrelated", False),
    case("printf-percent-literal", "printf '%%n' OPENAI_API_KEY", False),
    case("printf-last-v-writer", "printf -vunrelated -vOPENAI_API_KEY '%s' fixture-new", True),
    case("printf-last-v-data", "printf -v OPENAI_API_KEY -v unrelated '%s' fixture-new", False),
    case("read-last-a-data", "read -a OPENAI_API_KEY -a unrelated", False),
    case("read-last-a-writer", "read -a unrelated -raOPENAI_API_KEY", True),
    case("eval-compact", "eval 'printf -vOPENAI_API_KEY %s fixture-new'", True),
    case("callback-compact", "mapfile -C 'printf -vOPENAI_API_KEY %s fixture-new' unrelated", True),
    case("function-compact", "f() { printf -vOPENAI_API_KEY '%s' fixture-new; }", True),
    case("conditional-compact", "if true; then read -raOPENAI_API_KEY; fi", True),
    case("zsh-read-p-destination", "read -p OPENAI_API_KEY", True, path=".zshrc"),
    case("zsh-read-question-destination", "read 'OPENAI_API_KEY?prompt'", True, path=".zshrc"),
    case("zsh-read-t-optional", "read -t OPENAI_API_KEY", True, path=".zshrc"),
    case("zsh-read-e-display", "read -e OPENAI_API_KEY", False, path=".zshrc"),
    case("export-p-assignment", "export -p OPENAI_API_KEY=fixture-new", True),
    case("export-f-function", "export -f OPENAI_API_KEY=fixture-new", False),
    case("typeset-p-query", "typeset -p OPENAI_API_KEY=fixture-new", False),
    case("declare-plain-attribute", "declare -x OPENAI_API_KEY", False),
    case("mapfile-default-other", "mapfile -tn1 unrelated", False),
    case("arithmetic-control", "let 'OPENAI_API_KEY=1'", True),
    case("loop-control", "for OPENAI_API_KEY in fixture; do :; done", True),
    case("declare-integer-rhs", "declare -i unrelated='OPENAI_API_KEY=1'", True),
    case("typeset-integer-rhs", "typeset -i unrelated='OPENAI_API_KEY=1'", True),
    case("declare-string-rhs", "declare unrelated='OPENAI_API_KEY=1'", False),
    case("declare-integer-query", "declare -pi unrelated='OPENAI_API_KEY=1'", False),
    case("declare-array-index", "declare 'unrelated[OPENAI_API_KEY=1]=x'", True),
    case("read-array-index", "read 'unrelated[OPENAI_API_KEY=1]'", True),
    case("printf-array-index", "printf -v 'unrelated[OPENAI_API_KEY=1]' '%s' x", True),
    case("echo-array-index-data", "echo 'unrelated[OPENAI_API_KEY=1]'", False),
    # Option arity, end markers and command-specific output/query modes.
    case("printf-quoted-option", "printf '-vOPENAI_API_KEY' '%s' x", True),
    case("printf-double-dash", "printf -vOPENAI_API_KEY -- '%s' x", True),
    case("printf-no-format", "printf -vOPENAI_API_KEY", False),
    case("printf-empty-format", "printf -vOPENAI_API_KEY ''", True),
    case("read-endoptions", "read -r -- OPENAI_API_KEY", True),
    case("read-attached-prompt", "read -rpOPENAI_API_KEY unrelated", False),
    case("read-combined-initial", "read -rei OPENAI_API_KEY unrelated", False),
    case("read-combined-delimiter", "read -rd OPENAI_API_KEY unrelated", False),
    case("read-attached-fd", "read -ru0 OPENAI_API_KEY", True),
    case("read-readline", "read -e OPENAI_API_KEY", True, path=".bashrc"),
    case("read-zero-timeout", "read -rt0 OPENAI_API_KEY", False, path=".bashrc"),
    case("getopts-argv-data", "getopts 'x:' unrelated -x OPENAI_API_KEY", False),
    case("getopts-no-name", "getopts OPENAI_API_KEY", False),
    case("unset-variable", "unset -v -- OPENAI_API_KEY", True),
    case("unset-function-array-data", "unset -f 'unrelated[OPENAI_API_KEY=1]'", False),
    case("command-combined-query", "command -pv printf -vOPENAI_API_KEY %s x", False),
    case("command-exec-options", "command -p -- printf -vOPENAI_API_KEY %s x", True),
    case("builtin-exec-options", "builtin -- printf -vOPENAI_API_KEY %s x", True),
    case("query-still-has-expansions", 'command -v "$(printf -vOPENAI_API_KEY %s x)"', True),
    case("declaration-combined-query", "declare -px OPENAI_API_KEY=fixture-new", False),
    case("declaration-functions-query", "declare -F OPENAI_API_KEY=fixture-new", False),
    case("declaration-endoptions", "declare -x -- 'OPENAI_API_KEY=fixture-new'", True),
    case("declaration-disable-integer", "declare -i +i unrelated='OPENAI_API_KEY=1'", False),
    case("declaration-combined-integer", "declare -xi unrelated='OPENAI_API_KEY=1'", True),
    case("readonly-p-assignment", "readonly -p OPENAI_API_KEY=fixture-new", True),
    case("readonly-function-data", "readonly -f OPENAI_API_KEY=fixture-new", False),
    case("local-integer", "f() { local -i unrelated='OPENAI_API_KEY=1'; }", True),
    # The format decoder locates arguments; it never renders or evaluates them.
    case("printf-width-slot", "printf '%*s%n' 8 text OPENAI_API_KEY", True),
    case("printf-width-data", "printf '%*s%n' 8 OPENAI_API_KEY unrelated", False),
    case("printf-precision-slot", "printf '%.*s%n' 8 text OPENAI_API_KEY", True),
    case("printf-reused-format", "printf '%s%n' first other second OPENAI_API_KEY", True),
    case("printf-n-and-v", "printf -vunrelated '%n' OPENAI_API_KEY", True),
    case("printf-time-data", "printf '%(%n)T' OPENAI_API_KEY", False),
    case("printf-invalid-format", "printf '%T%n' data OPENAI_API_KEY", False),
    case("printf-dynamic-format", 'printf "$FORMAT" OPENAI_API_KEY', True),
    case("printf-bash-numeric-data", "printf '%d' 'OPENAI_API_KEY=1'", False, path=".bashrc"),
    case("printf-zsh-numeric-expression", "printf '%d' 'OPENAI_API_KEY=1'", True, path=".zshrc"),
    case("printf-zsh-character-data", """printf '%d' "'OPENAI_API_KEY=1" """, False, path=".zshrc"),
    case("printf-zsh-positional", "printf '%2$n%1$s' text OPENAI_API_KEY", True, path=".zshrc"),
    case("printf-zsh-positional-data", "printf '%2$n%1$s' OPENAI_API_KEY unrelated", False, path=".zshrc"),
    case("printf-zsh-positional-width", "printf '%2$*1$s%3$n' 2 text OPENAI_API_KEY", True, path=".zshrc"),
    # env -S has an argv grammar, not shell grammar. Includes are still ignored.
    case("env-long-split", "env --split-string='OPENAI_API_KEY=fixture-new other'", True),
    case("env-bundle-split", "env -iS'OPENAI_API_KEY=fixture-new other'", True),
    case("env-split-command-data", "env -S 'echo OPENAI_API_KEY=fixture-new'", False),
    case("env-split-semicolon-data", "env -S 'echo; OPENAI_API_KEY=fixture-new'", False),
    case("env-split-comment-data", "env -S '# OPENAI_API_KEY=fixture-new'", False),
    case("env-split-embedded-hash", "env -S 'OTHER=x#x OPENAI_API_KEY=fixture-new other'", True),
    case("env-split-underscore", r"env -S 'OTHER=x\_OPENAI_API_KEY=fixture-new\_other'", True),
    case("env-split-stop", r"env -S 'echo\c OPENAI_API_KEY=fixture-new'", False),
    case("env-split-symbolic-empty", "env -S '${MAYBE_EMPTY} OPENAI_API_KEY=fixture-new other'", True),
    case("env-split-symbolic-name", "env -S '${PREFIX}_API_KEY=fixture-new other'", False),
    case("env-split-array-data", "env -S 'other[OPENAI_API_KEY=1]=x command'", False),
    case("env-nonshell-name-then-assignment", "env other-name=x OPENAI_API_KEY=fixture-new command", True),
    case("env-argv0-data", "env -a OPENAI_API_KEY=fixture-new command", False),
    case("env-chdir-data", "env -C OPENAI_API_KEY=fixture-new command", False),
    case("env-split-chdir-data", "env -S '-C OPENAI_API_KEY=fixture-new command'", False),
    case("env-two-splits-after-command", "env -S echo -S 'OPENAI_API_KEY=fixture-new command'", False),
    case("env-two-splits-before-command", "env -S '-i' -S 'OPENAI_API_KEY=fixture-new command'", True),
    # Default targets and callbacks never cause unrelated variables to be claimed.
    case("mapfile-implicit", "mapfile -tn1", True, name="MAPFILE"),
    case("readarray-endoptions", "readarray -tdx -- OPENAI_API_KEY", True),
    case("mapfile-delimiter-data", "mapfile -d OPENAI_API_KEY unrelated", False),
    case("mapfile-overridden-callback", "mapfile -C 'OPENAI_API_KEY=1' -C echo unrelated", False),
    case("mapfile-final-callback", "mapfile -C echo -C 'printf -vOPENAI_API_KEY %s x' unrelated", True),
    case("mapfile-default-other-name", "mapfile -t", False),
    # Fixed Zsh paths select Zsh argument roles even if the ambient shell differs.
    case("zsh-read-p", "read -p OPENAI_API_KEY", True, path=".zshenv"),
    case("zsh-read-t", "read -t OPENAI_API_KEY", True, path=".zprofile"),
    case("zsh-read-prompt", "read 'OPENAI_API_KEY?prompt'", True, path=".zlogin"),
    case("zsh-read-prompt-data", "read 'unrelated?OPENAI_API_KEY'", False, path=".zshrc"),
    case("zsh-read-prompt-default", "read '?prompt'", True, name="REPLY", path=".zshrc"),
    case("zsh-read-array-default", "read -A", True, name="reply", path=".zshrc"),
    case("zsh-read-array-ignores-rest", "read -A unrelated OPENAI_API_KEY", False, path=".zshrc"),
    case("zsh-read-echo-and-assign", "read -E OPENAI_API_KEY", True, path=".zshrc"),
    case("zsh-read-optional-bundle", "read -rk OPENAI_API_KEY", True, path=".zshrc"),
    case("zsh-declare-float", "typeset -F2 other='OPENAI_API_KEY=1'", True, path=".zshrc"),
    case("zsh-declare-query", "typeset -p1 OPENAI_API_KEY=fixture-new", False, path=".zshrc"),
    case("bash-login-read-prompt", "read -p OPENAI_API_KEY unrelated", False, path=".bash_login"),
    case("bash-profile-read-prompt", "read -p OPENAI_API_KEY unrelated", False, path=".bash_profile"),
]


@pytest.fixture(autouse=True)
def forbid_process_inventory(home, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("real process inventory reached")

    monkeypatch.setattr(psutil, "process_iter", forbidden)
    monkeypatch.setattr(psutil, "pids", forbidden)


def seed(home, filename, name, line):
    path = home / filename
    before = (f"# 保留偏好\r\nexport {name}=fixture-literal\r\n" + line).encode()
    path.write_bytes(before)
    if name != KEY:
        config = home / ".codex/config.toml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(f'[model_providers.fixture]\nenv_key = "{name}"\n')
    return path, before


@pytest.mark.parametrize("filename,name,line,writer", CASES)
def test_builtin_roles_guard_exact_cleanup(home, filename, name, line, writer):
    path, before = seed(home, filename, name, line)
    profile = next(p for p in read_shell_profiles(home, frozenset({name})) if p.path == path)
    assert len(profile.assignments) == 1
    if writer:
        assert profile.values == {}
        assert any(issue.names == (name,) and issue.reason == "dynamic_shell" for issue in profile.issues)
        with pytest.raises(TakeoverStateError):
            cleanup_shell_profile(profile, {name: "fixture-literal"})
    else:
        assert profile.values == {name: "fixture-literal"}
        assert not profile.issues
        edit = NativeFileEdit.from_payload(cleanup_shell_profile(profile, profile.values).to_payload())
        edit.apply()
        assert path.read_bytes() == ("# 保留偏好\r\n" + line).encode()
        edit.apply(reverse=True)
    assert path.read_bytes() == before


@pytest.mark.parametrize("filename,name,line,writer", CASES)
def test_builtin_roles_reach_scan_and_apply(home, tmp_path, filename, name, line, writer):
    path, before = seed(home, filename, name, line)
    service, store, adapter = _service(tmp_path, migration_home=home)
    [row] = service.migration_scan()["items"]
    if writer:
        assert row["notes_key"] == "settings.models.migration.blocked.dynamic_shell"
        assert row["proposed_action"] == "reauth"
        assert not row["selected"]
        with pytest.raises(ModelHubError):
            asyncio.run(service.migration_apply([row["id"]]))
        assert path.read_bytes() == before
        assert not adapter.provisioned and not adapter.oauth_provisioned and not adapter.transient_refs
        assert not store.config.sources
    else:
        assert row["proposed_action"] == "import"
        assert asyncio.run(service.migration_apply([row["id"]]))["applied"] == 1
        assert len(adapter.provisioned) == len(store.config.sources) == 1
        assert path.read_bytes() == ("# 保留偏好\r\n" + line).encode()


def test_profile_dialect_never_comes_from_runtime_shell(home, monkeypatch):
    import os

    monkeypatch.setenv("SHELL", "/fixture/not-a-shell")
    original_get = os.environ.get

    def guarded(name, *args):
        assert name not in {"SHELL", KEY}, "runtime shell/credential read"
        return original_get(name, *args)

    monkeypatch.setattr(os.environ, "get", guarded)
    (home / ".bashrc").write_text("read -p OPENAI_API_KEY unrelated\n")
    (home / ".zshrc").write_text("read -p OPENAI_API_KEY\n")
    profiles = {p.path.name: p for p in read_shell_profiles(home, frozenset({KEY}))}
    assert profiles[".bashrc"].issues == ()
    assert profiles[".zshrc"].issues[0].names == (KEY,)


@pytest.mark.parametrize("writer", [
    "printf -vOPENAI_API_KEY %s value",
    "read -raOPENAI_API_KEY",
    "printf '%n' OPENAI_API_KEY",
    "declare -i unrelated='OPENAI_API_KEY=1'",
    "env -S 'OPENAI_API_KEY=fixture-new command'",
])
def test_unrelated_consented_backend_can_still_migrate(home, tmp_path, writer):
    path = home / ".profile"
    kept = "export OPENAI_API_KEY=fixture-codex\n" + writer + "\n"
    path.write_text(kept + "export ANTHROPIC_API_KEY=fixture-claude\n")
    service, store, adapter = _service(tmp_path, migration_home=home)
    rows = {row["backend"]: row for row in service.migration_scan()["items"]}
    assert rows["codex"]["notes_key"].endswith(".dynamic_shell")
    assert rows["claude"]["proposed_action"] == "import"
    result = asyncio.run(service.migration_apply([rows["claude"]["id"]]))
    assert result["applied"] == 1
    assert len(adapter.provisioned) == len(store.config.sources) == 1
    assert path.read_text() == kept
