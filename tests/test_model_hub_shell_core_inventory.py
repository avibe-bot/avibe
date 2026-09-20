"""Pinned core argument roles; shell fixture strings are never executed."""

import asyncio
import hashlib
from pathlib import Path
import socket

import pytest

from core.handlers.model_hub import migration_shell as shell
from tests.scenarios.model_hub.test_model_hub_migration_scenarios import _service
from tests.test_model_hub_persisted_inventory import home  # noqa: F401
from tests.test_model_hub_shell_writer_roles import (
    KEY,
    forbid_process_inventory,  # noqa: F401
    seed,
    test_builtin_roles_guard_exact_cleanup as check_cleanup,
    test_builtin_roles_reach_scan_and_apply as check_service,
)


def case(role, label, line, writer, *, name=KEY, path=".bashrc"):
    return pytest.param(role, path, name, line + "\n", writer, id=f"{role}-{label}")


CASES = [
    *[
        case("wait", label, line, writer)
        for label, line, writer in [
            ("separate", "wait -n -p OPENAI_API_KEY", True),
            ("compact", "wait -npOPENAI_API_KEY", True),
            ("without-jobs", "wait -p OPENAI_API_KEY", True),
            ("last-writer", "wait -p other -pOPENAI_API_KEY", True),
            ("last-data", "wait -p OPENAI_API_KEY -p other", False),
            ("array", "wait -p 'OPENAI_API_KEY[0]'", True),
            ("index", "wait -p 'other[OPENAI_API_KEY=1]'", True),
            ("dynamic", 'wait "-$FLAGS" OPENAI_API_KEY -n', True),
            ("operand", "wait OPENAI_API_KEY", False),
            ("end", "wait -- -p OPENAI_API_KEY", False),
            ("invalid", "wait -Q -p OPENAI_API_KEY", False),
            ("quoted", "echo 'wait -npOPENAI_API_KEY'", False),
        ]
    ],
    *[
        case("compgen-target", label, line, writer)
        for label, line, writer in [
            ("separate", "compgen -V OPENAI_API_KEY -A variable", True),
            ("compact", "compgen -VOPENAI_API_KEY -W fixture", True),
            ("no-matches", "compgen -V OPENAI_API_KEY", True),
            ("array-invalid", "compgen -V 'OPENAI_API_KEY[0]' -W fixture", False),
            ("word-data", "compgen -W 'OPENAI_API_KEY=new'", False),
            ("invalid", "complete -V OPENAI_API_KEY -W fixture", False),
        ]
    ],
    *[
        case("trap", label, line, writer)
        for label, line, writer in [
            ("assignment", "trap 'OPENAI_API_KEY=new' EXIT", True),
            ("command", "trap -- 'read OPENAI_API_KEY' EXIT", True),
            ("echo", "trap 'echo OPENAI_API_KEY=new' EXIT", False),
            ("query", "trap -p 'OPENAI_API_KEY=new' EXIT", False),
            ("reset", "trap - EXIT", False),
            ("missing-signal", "trap 'OPENAI_API_KEY=new'", False),
        ]
    ],
    *[
        case("bind", label, line, writer)
        for label, line, writer in [
            ("command", r"""bind -x '"\C-x":OPENAI_API_KEY=new'""", True),
            ("with-display", r"""bind -X -x '"\C-x":OPENAI_API_KEY=new'""", True),
            ("macro", r"""bind '"\C-x":"OPENAI_API_KEY=new"'""", False),
            ("echo", r"""bind -x '"\C-x":echo OPENAI_API_KEY=new'""", False),
            ("unbind-first", r"""bind -r '\C-x' -x '"\C-x":OPENAI_API_KEY=new'""", False),
            ("query", "bind -X", False),
        ]
    ],
    *[
        case(f"{command}-{role}", label, line, writer)
        for command in ("complete", "compgen")
        for role, label, options, writer in [
            ("source", "assignment", "-C 'OPENAI_API_KEY=new'", True),
            ("source", "echo", "-C 'echo OPENAI_API_KEY=new'", False),
            ("expansion", "parameter", "-W '${OPENAI_API_KEY:=new}'", True),
            ("expansion", "arithmetic", "-W '$((OPENAI_API_KEY=1))'", True),
            ("expansion", "substitution", "-W '$(read OPENAI_API_KEY)'", True),
            ("expansion", "word-data", "-W 'OPENAI_API_KEY=new'", False),
            ("expansion", "prefix-data", "-P '${OPENAI_API_KEY:=new}'", False),
            ("expansion", "glob-data", "-G '${OPENAI_API_KEY:=new}'", False),
            ("source", "function-name", "-F 'OPENAI_API_KEY=new'", False),
        ]
        for line in [f"{command} {options}" + (" fixture" if command == "complete" else "")]
    ],
    case("complete-source", "query", "complete -p -C 'OPENAI_API_KEY=new' fixture", False),
    case("complete-expansion", "remove", "complete -r -W '${OPENAI_API_KEY:=new}' fixture", False),
    case("complete-source", "missing-name", "complete -C 'OPENAI_API_KEY=new'", False),
    case("jobs", "read", "jobs -x read OPENAI_API_KEY", True),
    case("jobs", "printf", "jobs -x printf -vOPENAI_API_KEY %s fixture", True),
    case("jobs", "not-source", "jobs -x OPENAI_API_KEY=new", False),
    case("jobs", "echo", "jobs -x echo 'OPENAI_API_KEY=new'", False),
    case("jobs", "not-x", "jobs read OPENAI_API_KEY", False),
    case("jobs", "invalid", "jobs -lx read OPENAI_API_KEY", False),
    case("fc", "editor", "fc -e 'printf -vOPENAI_API_KEY %s fixture' 1", True),
    case("fc", "list", "fc -le 'printf -vOPENAI_API_KEY %s fixture' 1", False),
    case("fc", "history", "fc -se 'printf -vOPENAI_API_KEY %s fixture' 1", False),
    case("fc", "echo", "fc -e 'echo OPENAI_API_KEY=new' 1", False),
    case("coproc", "named", "coproc OPENAI_API_KEY { :; }", True),
    case("coproc", "pid", "coproc WORKER { :; }", True, name="WORKER_PID"),
    case("coproc", "default", "coproc { :; }", True, name="COPROC"),
    case("coproc", "default-pid", "coproc { :; }", True, name="COPROC_PID"),
    case("coproc", "argument", "coproc echo OPENAI_API_KEY", False),
    case("coproc", "echo", "echo 'coproc OPENAI_API_KEY { :; }'", False),
    *[
        case("fd", f"allocate-{index}", f": {{OPENAI_API_KEY}}{operator}fixture", True)
        for index, operator in enumerate((">", "<", ">>", "<>", ">|", ">&", "<<<"))
    ],
    case("fd", "close", ": {OPENAI_API_KEY}>&-", False),
    case("fd", "close-input", ": {OPENAI_API_KEY}<&-", False),
    case("fd", "whitespace", ": {OPENAI_API_KEY} >fixture", False),
    case("fd", "quoted", ": '{OPENAI_API_KEY}'>fixture", False),
    *[
        case("include", f"{command}-{index}", line, False)
        for command in ("source", ".")
        for index, line in enumerate((
            f"{command} OPENAI_API_KEY=new",
            f"{command} 'read OPENAI_API_KEY'",
            f"{command} other 'OPENAI_API_KEY=new'",
        ))
    ],
    *[
        case("code-worklist", f"{depth}-{command}", "eval " * depth + f"{command} OPENAI_API_KEY", writer)
        for depth in (1, 12, 16, 32, 100)
        for command, writer in (("read", True), ("echo", False))
    ],
    *[
        case(role, label, line, writer, path=".zshrc")
        for role, label, line, writer in [
            ("print-target", "separate", "print -v OPENAI_API_KEY fixture-new", True),
            ("print-target", "compact", "print -vOPENAI_API_KEY fixture-new", True),
            ("print-target", "bundle", "print -rvOPENAI_API_KEY -- fixture-new", True),
            ("print-target", "empty", "print -v OPENAI_API_KEY", True),
            ("print-target", "ordinary", "print -- OPENAI_API_KEY", False),
            ("print-target", "unrelated", "print -v unrelated OPENAI_API_KEY", False),
            ("print-target", "R-data", "print -R -v OPENAI_API_KEY fixture-new", False),
            ("print-target", "conflict", "print -zv OPENAI_API_KEY fixture-new", False),
            ("print-format", "n", "print -f '%n' OPENAI_API_KEY", True),
            ("print-format", "number", "print -f '%d' 'OPENAI_API_KEY=2'", True),
            ("print-format", "data", "print -f '%s' OPENAI_API_KEY", False),
            ("print-format", "history", "print -S 'export OPENAI_API_KEY=new'", False),
            ("set-array", "separate", "set -A OPENAI_API_KEY fixture-new", True),
            ("set-array", "compact", "set -AOPENAI_API_KEY fixture-new", True),
            ("set-array", "plus", "set +A OPENAI_API_KEY fixture-new", True),
            ("set-array", "clear", "set -A OPENAI_API_KEY", True),
            ("set-array", "query", "set -A", False),
            ("set-array", "positionals", "set -- OPENAI_API_KEY fixture-new", False),
            ("set-array", "unrelated", "set -A unrelated OPENAI_API_KEY", False),
            ("tied", "second", "typeset -T unrelated=fixture-new OPENAI_API_KEY", True),
            ("tied", "first", "typeset -T OPENAI_API_KEY unrelated", True),
            ("tied", "query", "typeset -pT unrelated=fixture-new OPENAI_API_KEY", False),
            ("getln", "target", "getln OPENAI_API_KEY", True),
            ("getln", "echo", "getln -e OPENAI_API_KEY", False),
            ("shift", "count", "shift 'OPENAI_API_KEY=1' unrelated", True),
            ("shift", "array", "typeset -a OPENAI_API_KEY\nshift 1 OPENAI_API_KEY", True),
            ("shift", "default", "typeset -a OPENAI_API_KEY\nshift OPENAI_API_KEY", True),
            ("shift", "unrelated", "shift 1 unrelated", False),
            ("read-timeout", "evaluation", "read -t '1+(OPENAI_API_KEY=2)' unrelated", True),
            ("read-timeout", "reference", "read -t '1+OPENAI_API_KEY' unrelated", False),
            ("test-fd", "evaluation", "test -t 'OPENAI_API_KEY=1'", True),
            ("test-fd", "bracket", "[ -t 'OPENAI_API_KEY=1' ]", True),
            ("test-fd", "decimal", "test 'OPENAI_API_KEY=2' -eq 2", False),
            ("test-fd", "string", "[ 'OPENAI_API_KEY=2' = 'OPENAI_API_KEY=2' ]", False),
            ("zsh-trap", "source", "trap 'export OPENAI_API_KEY=new' DEBUG", True),
            ("zsh-trap", "read", "trap -- 'read OPENAI_API_KEY' DEBUG", True),
            ("zsh-trap", "data", "trap 'echo OPENAI_API_KEY' DEBUG", False),
            ("zsh-trap", "reset", "trap - DEBUG", False),
            ("zsh-trap", "query", "trap", False),
            ("emulate", "source", "emulate zsh -c 'export OPENAI_API_KEY=new'", True),
            ("emulate", "read", "emulate sh -c 'read OPENAI_API_KEY'", True),
            ("emulate", "query", "emulate -l zsh", False),
            ("emulate", "invalid", "emulate -L zsh -c 'OPENAI_API_KEY=2'", False),
            ("emulate", "data", "emulate zsh -c 'echo OPENAI_API_KEY'", False),
            ("emulate", "local-zsh", "emulate zsh -c 'read -p OPENAI_API_KEY'", True),
            ("emulate", "local-sh", "emulate sh -c 'read -p OPENAI_API_KEY unrelated'", True),
            ("zsh-fc", "source", "fc -e 'OPENAI_API_KEY=new; :'", True),
            ("zsh-fc", "read", "fc -e 'read OPENAI_API_KEY; :'", True),
            ("zsh-fc", "list", "fc -l -e 'OPENAI_API_KEY=new; :'", False),
            ("zsh-fc", "data", "fc -e 'echo OPENAI_API_KEY'", False),
            ("zsh-fc", "no-editor", "fc -e -", False),
            ("zmodload", "target", "zmodload -FL -P OPENAI_API_KEY zsh/parameter", True),
            ("zmodload", "unrelated", "zmodload -FL -P unrelated zsh/parameter", False),
            ("zmodload", "query", "zmodload -FL zsh/parameter", False),
            ("zmodload", "existence", "zmodload -FLe -P OPENAI_API_KEY zsh/parameter", False),
            ("zsh-prefix", "noglob", "noglob read OPENAI_API_KEY", True),
            ("zsh-prefix", "exec", "exec builtin read OPENAI_API_KEY", True),
            ("zsh-prefix", "argv0", "exec -a fixture-process builtin read OPENAI_API_KEY", True),
            ("zsh-prefix", "dash", "- builtin read OPENAI_API_KEY", True),
            ("zsh-prefix", "noglob-data", "noglob echo 'OPENAI_API_KEY=new'", False),
            ("zsh-prefix", "exec-data", "exec echo 'OPENAI_API_KEY=new'", False),
            ("zsh-prefix", "argv0-data", "exec -a OPENAI_API_KEY=new echo", False),
            ("zsh-prefix", "dash-data", "- echo 'OPENAI_API_KEY=new'", False),
        ]
    ],
    *[
        case("numeric-declaration", f"{index}-{label}", f"{command} {operand}", writer, path=".zshrc")
        for index, command in enumerate(("integer", "float", "typeset -i", "typeset -E", "typeset -F"))
        for label, operand, writer in (
            ("coercion", KEY, True),
            ("assignment", KEY + "=2", True),
            ("rhs", "unrelated='OPENAI_API_KEY=2'", True),
            ("query", "-p " + KEY, False),
            ("reference", "unrelated='OPENAI_API_KEY'", False),
        )
    ],
    *[
        case("zsh-control", f"{command}-{label}", f"{command} {operand}", writer, path=".zshrc")
        for command in ("break", "continue", "return", "exit", "bye", "logout")
        for label, operand, writer in (
            ("evaluation", "'OPENAI_API_KEY=1'", True),
            ("reference", "OPENAI_API_KEY", False),
            ("invalid-arity", "'OPENAI_API_KEY=1' extra", False),
        )
    ],
    *[
        case("declaration", f"{path}-{command}-{writer}", line, writer, path=path)
        for path in (".bashrc", ".zshrc")
        for command in ("declare", "typeset", "local", "export", "readonly")
        for line, writer in (
            (f"{command} -- OPENAI_API_KEY=new", True),
            (f"{command} -p OPENAI_API_KEY", False),
        )
    ],
    *[
        case("wrapper", f"{path}-{command}-{writer}", f"{command} {body}", writer, path=path)
        for path in (".bashrc", ".zshrc")
        for command in ("command", "builtin")
        for body, writer in (("read OPENAI_API_KEY", True), ("echo OPENAI_API_KEY=new", False))
    ],
    *[
        case(role, f"{path}-{index}", line, writer, path=path)
        for path in (".bashrc", ".zshrc")
        for index, (role, line, writer) in enumerate([
            ("read-output", "read OPENAI_API_KEY", True),
            ("read-output", "read unrelated", False),
            ("getopts", "getopts x OPENAI_API_KEY", True),
            ("getopts", "getopts OPENAI_API_KEY unrelated", False),
            ("unset", "unset OPENAI_API_KEY", True),
            ("unset", "unset -f OPENAI_API_KEY", False),
            ("printf-target", "printf -vOPENAI_API_KEY %s value", True),
            ("printf-target", "printf %s OPENAI_API_KEY", False),
            ("printf-format", "printf '%n' OPENAI_API_KEY", True),
            ("printf-format", "printf '%s' OPENAI_API_KEY", False),
            ("arithmetic", "let 'OPENAI_API_KEY=1'", True),
            ("arithmetic", "let 'OPENAI_API_KEY+1'", False),
            ("arithmetic", "(( OPENAI_API_KEY=1 ))", True),
            ("arithmetic", "(( OPENAI_API_KEY+1 ))", False),
            ("substitution", 'echo "$(read OPENAI_API_KEY)"', True),
            ("substitution", 'echo "$(echo OPENAI_API_KEY)"', False),
            ("parameter-assignment", 'echo "${OPENAI_API_KEY:=new}"', True),
            ("parameter-assignment", 'echo "${OPENAI_API_KEY:-new}"', False),
            ("assignment", 'OPENAI_API_KEY="$VALUE"', True),
            ("assignment", "echo 'OPENAI_API_KEY=value'", False),
            ("eval-source", "eval -- 'read OPENAI_API_KEY'", True),
            ("eval-source", "eval -- 'echo OPENAI_API_KEY'", False),
            ("eval-source", "eval -Q 'read OPENAI_API_KEY'", False),
            ("loop-target", "for OPENAI_API_KEY in value; do :; done", True),
            ("loop-target", "for other in OPENAI_API_KEY; do :; done", False),
            ("nested", "if true; then read OPENAI_API_KEY; fi", True),
            ("nested", "if true; then echo OPENAI_API_KEY; fi", False),
            ("nested", "f() { read OPENAI_API_KEY; }", True),
            ("nested", "f() { echo OPENAI_API_KEY; }", False),
            ("prefix", "! read OPENAI_API_KEY", True),
            ("prefix", "! echo OPENAI_API_KEY", False),
        ])
    ],
    *[
        case(role, f"{command}-{writer}", line, writer)
        for command in ("mapfile", "readarray")
        for role, line, writer in (
            ("array-reader", f"{command} OPENAI_API_KEY", True),
            ("array-reader", f"{command} unrelated", False),
            ("array-callback", f"{command} -C 'read OPENAI_API_KEY' unrelated", True),
            ("array-callback", f"{command} -C 'echo OPENAI_API_KEY' unrelated", False),
        )
    ],
    case("test-fd", "invalid-extra", "test foo -t 'OPENAI_API_KEY=1'", False, path=".zshrc"),
    case("test-fd", "logical", "test -n x -a -t 'OPENAI_API_KEY=1'", True, path=".zshrc"),
    case("print-format", "format-before-R", "print -f %s -R -vOPENAI_API_KEY data", True, path=".zshrc"),
    case("include", "expansion-before-filename", 'source "$(read OPENAI_API_KEY)"', True),
    case("coproc", "parenthesized", "coproc OPENAI_API_KEY ( : )", True),
    case("coproc", "body", "coproc other { read OPENAI_API_KEY; }", True),
    case("coproc", "condition-body", "coproc other while read OPENAI_API_KEY; do :; done", True),
    case("coproc", "condition-data", "coproc other while echo OPENAI_API_KEY; do :; done", False),
    *[
        case(role, f"dynamic-{index}", line, True, path=path)
        for index, (role, path, line) in enumerate([
            ("compgen-target", ".bashrc", 'compgen "$FLAGS" OPENAI_API_KEY'),
            ("complete-source", ".bashrc", """complete "$FLAGS" -C 'read OPENAI_API_KEY'"""),
            ("complete-source", ".bashrc", """complete "$FLAGS" 'read OPENAI_API_KEY' fixture"""),
            ("compgen-expansion", ".bashrc", """compgen "$FLAGS" '${OPENAI_API_KEY:=new}'"""),
            ("trap", ".bashrc", """trap "$FLAGS" 'read OPENAI_API_KEY' EXIT"""),
            ("bind", ".bashrc", """bind "$FLAGS" '"key":read OPENAI_API_KEY'"""),
            ("fc", ".bashrc", """fc "$FLAGS" 'read OPENAI_API_KEY' 1"""),
            ("print-target", ".zshrc", 'print "$FLAGS" OPENAI_API_KEY'),
            ("set-array", ".zshrc", 'set "$FLAGS" OPENAI_API_KEY'),
            ("zmodload", ".zshrc", 'zmodload "$FLAGS" -P OPENAI_API_KEY zsh/parameter'),
        ])
    ],
    *[
        case("numeric-declaration", f"alias-{command}", f"{command} -i OPENAI_API_KEY", True, path=".zshrc")
        for command in ("declare", "local", "export", "readonly")
    ],
    *[
        case("tied", f"alias-{command}", f"{command} -T other=new OPENAI_API_KEY", True, path=".zshrc")
        for command in ("declare", "local", "export", "readonly")
    ],
    case("numeric-declaration", "integer-invalid", "integer -E OPENAI_API_KEY", False, path=".zshrc"),
    case("numeric-declaration", "float-invalid", "float -i OPENAI_API_KEY", False, path=".zshrc"),
    case("bind", "whitespace-command", """bind -x '"key" "read OPENAI_API_KEY"'""", True),
    case("bind", "missing-separator", """bind -x '"key"read OPENAI_API_KEY'""", False),
    case("jobs", "x-before-format", "jobs -xl read OPENAI_API_KEY", True),
    case("jobs", "later-invalid-x", "jobs -xlx read OPENAI_API_KEY", False),
    case("complete-source", "invalid-action", "complete -A invalid -C 'read OPENAI_API_KEY' fixture", False),
    case("zmodload", "no-list", "zmodload -F -P OPENAI_API_KEY zsh/parameter", False, path=".zshrc"),
    case("wait", "not-zsh", "wait -p OPENAI_API_KEY", False, path=".zshrc"),
    case("print-target", "R-same-bundle", "print -RvOPENAI_API_KEY data", True, path=".zshrc"),
    case("print-format", "R-format-same-bundle", "print -Rf '%n' OPENAI_API_KEY", True, path=".zshrc"),
    *[
        case("emulate", f"core-table-{mode}-{index}", f"emulate {mode} -c '{body}'", writer, path=".zshrc")
        for mode in ("sh", "ksh", "csh", "unrecognized", '"$MODE"')
        for index, (body, writer) in enumerate((
            ("read -p OPENAI_API_KEY", True),
            ("print -vOPENAI_API_KEY data", True),
            ("wait -p OPENAI_API_KEY", False),
            ("mapfile OPENAI_API_KEY", False),
            ("echo OPENAI_API_KEY", False),
        ))
    ],
]


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("network is not part of the shell inventory fixture")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.mark.parametrize("role,filename,name,line,writer", CASES)
def test_core_roles_cleanup(home, role, filename, name, line, writer):
    check_cleanup(home, filename, name, line, writer)


@pytest.mark.parametrize("role,filename,name,line,writer", CASES)
def test_core_roles_actual_apply(home, tmp_path, role, filename, name, line, writer):
    check_service(home, tmp_path, filename, name, line, writer)


@pytest.mark.parametrize("role,filename,name,line,writer", [c for c in CASES if c.values[-1]])
def test_core_standalone_writer_scan(home, tmp_path, role, filename, name, line, writer):
    path, _ = seed(home, filename, name, line)
    path.write_text(line)
    profiles = shell.read_shell_profiles(home, frozenset({name}))
    profile = next(p for p in profiles if p.path == path)
    assert profile.values == {}
    assert any(name in issue.names for issue in profile.issues)
    service, store, adapter = _service(tmp_path, migration_home=home)
    rows = service.migration_scan()["items"]
    assert rows and all(row["proposed_action"] == "reauth" for row in rows)
    assert not adapter.provisioned and not store.config.sources


@pytest.mark.parametrize("line", [
    "wait -p OPENAI_API_KEY",
    "compgen -VOPENAI_API_KEY -W fixture",
    "trap 'read OPENAI_API_KEY' EXIT",
    "eval " * 32 + "read OPENAI_API_KEY",
])
def test_core_independent_backend(home, tmp_path, line):
    path = home / ".bashrc"
    kept = "export OPENAI_API_KEY=fixture-codex\n" + line + "\n"
    path.write_text(kept + "export ANTHROPIC_API_KEY=fixture-claude\n")
    service, store, adapter = _service(tmp_path, migration_home=home)
    rows = {row["backend"]: row for row in service.migration_scan()["items"]}
    assert rows["codex"]["notes_key"].endswith(".dynamic_shell")
    assert rows["claude"]["proposed_action"] == "import"
    assert asyncio.run(service.migration_apply([rows["claude"]["id"]]))["applied"] == 1
    assert len(adapter.provisioned) == len(store.config.sources) == 1
    assert path.read_text() == kept


@pytest.mark.parametrize("depth", [16, 100, 1100])
@pytest.mark.parametrize("writer", [True, False])
def test_written_code_work_is_linear_in_context_count(monkeypatch, depth, writer):
    original = shell._WrittenCode.visit
    calls = 0

    def counted(self, *args):
        nonlocal calls
        calls += 1
        assert calls <= 2 * (depth + 1)
        return original(self, *args)

    monkeypatch.setattr(shell._WrittenCode, "visit", counted)
    text = "eval " * depth + ("read" if writer else "echo") + " OPENAI_API_KEY"
    assert shell._written_names(text, frozenset({KEY})) == ({KEY} if writer else set())


@pytest.mark.parametrize("writer", [True, False])
def test_duplicate_code_contexts_are_visited_once(monkeypatch, writer):
    original = shell._WrittenCode.visit
    calls = 0

    def counted(self, *args):
        nonlocal calls
        calls += 1
        assert calls <= 4
        return original(self, *args)

    monkeypatch.setattr(shell._WrittenCode, "visit", counted)
    body = ("read" if writer else "echo") + " OPENAI_API_KEY"
    text = "; ".join([f"trap '{body}' EXIT"] * 200)
    assert shell._written_names(text, frozenset({KEY})) == ({KEY} if writer else set())


def test_code_identity_keeps_role_and_local_dialect():
    analysis = shell._WrittenCode(frozenset({KEY}))
    analysis.add("expansion", "OPENAI_API_KEY=new", "bash")
    assert analysis.run() == set()
    analysis.add("source", "OPENAI_API_KEY=new", "bash")
    assert analysis.run() == {KEY}
    analysis = shell._WrittenCode(frozenset({KEY}))
    analysis.add("source", "read -p OPENAI_API_KEY unrelated", "posix")
    assert analysis.run() == set()
    analysis.add("source", "read -p OPENAI_API_KEY unrelated", "zsh")
    assert analysis.run() == {KEY}


@pytest.mark.parametrize("command,dialect", [
    ("wait", "bash"), ("compgen", "bash"), ("complete", "bash"), ("bind", "bash"),
    ("fc", "bash"), ("print", "zsh"), ("zmodload", "zsh"),
])
@pytest.mark.parametrize("count", [4, 10, 20])
def test_catalogue_dynamic_options_project_one_value_role(monkeypatch, command, dialect, count):
    original = shell._options
    calls = 0

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        assert calls <= 12 * (count + 2) ** 2
        return original(*args, **kwargs)

    monkeypatch.setattr(shell, "_options", counted)
    flags = " ".join(f'"-$FLAGS{index}"' for index in range(count))
    assert shell._written_names(f"{command} {flags} unrelated", frozenset({KEY}), dialect=dialect) == set()


def test_pinned_upstream_census_and_role_coverage():
    """An independent upstream-name digest, not a census of parser dispatch."""
    ledger = (Path(__file__).parents[1] / "docs/plans/model-hub-shell-core-coverage.md").read_text()
    commands = {"bash": {}, "zsh": {}, "syntax": {}}
    bindings = {}
    for line in ledger.splitlines():
        cells = [cell.strip().strip("`") for cell in line.strip("|").split("|")]
        if line.startswith("| ") and len(cells) == 5 and cells[0] in commands:
            dialect, name, classification, roles, evidence = cells
            assert name not in commands[dialect], (dialect, name)
            assert classification in {"role", "none", "excluded"}
            assert evidence
            commands[dialect][name] = (classification, set(roles.split(",")) if roles != "-" else set())
        elif line.startswith("| ") and len(cells) == 2 and cells[0] not in {"Role", "---"}:
            role, handler = cells
            assert role not in bindings
            assert hasattr(shell, handler), handler
            bindings[role] = handler
    # Bash includes help-only reserved.def entries; Zsh excludes 3 debug
    # conditionals. Digests were computed directly from pinned upstream source.
    for dialect, count, digest in (
        ("bash", 77, "fc4f5f0bd0d9a5a3942e2561f683af19150b8ea87f5628c2127f494bd6535e4f"),
        ("zsh", 76, "7cbb7e282474cd2085c5ac22929cd0405c4cb08cbb1eabb66afe367144559312"),
    ):
        assert len(commands[dialect]) == count
        assert hashlib.sha256("\n".join(sorted(commands[dialect])).encode()).hexdigest() == digest
    covered = {}
    for item in CASES:
        role, _, _, _, writer = item.values
        covered.setdefault(role, set()).add(writer)
    assert set(bindings) == set(covered)
    assert all(outcomes == {True, False} for outcomes in covered.values())
    for census in commands.values():
        for classification, roles in census.values():
            assert bool(roles) == (classification == "role")
            assert roles <= bindings.keys()
