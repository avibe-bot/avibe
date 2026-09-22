"""Hermetic grammar and journal contracts for persisted shell migration."""

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from core.handlers.model_hub.migration_journal import NativeFileEdit, TakeoverStateError
from core.handlers.model_hub.migration_shell import (
    SHELL_PROFILE_NAMES,
    ShellAssignment,
    ShellIssue,
    cleanup_shell_profile,
    read_shell_profiles,
)


KEY = "OPENAI_API_KEY"
OTHER = "ANTHROPIC_API_KEY"
NAMES = frozenset({KEY, OTHER})


def _profile(tmp_path, content: str | bytes):
    path = tmp_path / ".bashrc"
    path.write_bytes(content.encode() if isinstance(content, str) else content)
    return next(profile for profile in read_shell_profiles(tmp_path, NAMES) if profile.path == path)


@pytest.mark.parametrize("line,value", [
    ("OPENAI_API_KEY=fixture\n", "fixture"),
    ("export OPENAI_API_KEY=fixture\n", "fixture"),
    ("\t export\tOPENAI_API_KEY='fixture space'  # retain semantics\n", "fixture space"),
    ('OPENAI_API_KEY="fixture space"\r\n', "fixture space"),
    ("OPENAI_API_KEY='fixture # $HOME `echo value` $(command) {literal}'\n",
     "fixture # $HOME `echo value` $(command) {literal}"),
    ('OPENAI_API_KEY="fixture \\$HOME \\`quoted\\` \\\\ \\""\n', 'fixture $HOME `quoted` \\ "'),
    ('OPENAI_API_KEY="literal\\q"\n', "literal\\q"),
    (r"OPENAI_API_KEY=fixture\ space\#hash" + "\n", "fixture space#hash"),
    ("OPENAI_API_KEY=fixture#hash\n", "fixture#hash"),
    ("OPENAI_API_KEY='前缀'\"密钥\"suffix\r\n", "前缀密钥suffix"),
    ("OPENAI_API_KEY=\n", ""),
    ("OPENAI_API_KEY=''\n", ""),
    ('OPENAI_API_KEY=""', ""),
    ("OPENAI_API_KEY=fixture", "fixture"),
])
def test_complete_literal_line_has_exact_value_and_byte_span(tmp_path, line, value):
    prefix = "# 中文上下文\r\n"
    profile = _profile(tmp_path, prefix + line)
    assert profile.values == {KEY: value}
    assert profile.issues == ()
    assignment, = profile.assignments
    assert assignment.start == len(prefix.encode())
    assert assignment.end == len((prefix + line).encode())
    assert profile.before[assignment.start:assignment.end] == line.encode()
    assert cleanup_shell_profile(profile, {KEY: value}).after == prefix.encode()


@pytest.mark.parametrize("text", [
    'OPENAI_API_KEY="$LIVE_KEY"\n',
    "OPENAI_API_KEY=${LIVE_KEY:-fallback}\n",
    "OPENAI_API_KEY=$(printf fixture)\n",
    'OPENAI_API_KEY="prefix$(printf fixture)suffix"\n',
    "OPENAI_API_KEY=`printf fixture`\n",
    "OPENAI_API_KEY=~/.key\n",
    "OPENAI_API_KEY=$'fixture\\n'\n",
    "OPENAI_API_KEY=$((1 + 2))\n",
    "OPENAI_API_KEY={one,two}\n",
    "OPENAI_API_KEY+=fixture\n",
    "OPENAI_API_KEY[0]=fixture\n",
    "OPENAI_API_KEY=fixture command\n",
    "OPENAI_API_KEY=fixture; echo done\n",
    "OPENAI_API_KEY=fixture OTHER=value\n",
    'export "OPENAI_API_KEY=fixture"\n',
    "export OPENAI_API_KEY=fixture ANTHROPIC_API_KEY=other\n",
    "readonly OPENAI_API_KEY=fixture\n",
    "declare -x OPENAI_API_KEY=fixture\n",
    "typeset OPENAI_API_KEY=fixture\n",
    "local OPENAI_API_KEY=fixture\n",
    "env OPENAI_API_KEY=fixture command\n",
    "builtin export OPENAI_API_KEY=fixture\n",
    "command export OPENAI_API_KEY=fixture\n",
    "if true; then\n  OPENAI_API_KEY=fixture\nfi\n",
    "if true; then export OPENAI_API_KEY=fixture; fi\n",
    "for item in one two; do\nOPENAI_API_KEY=fixture\ndone\n",
    "while true; do\nOPENAI_API_KEY=fixture\ndone\n",
    "until true; do\nOPENAI_API_KEY=fixture\ndone\n",
    "case x in\nx)\nOPENAI_API_KEY=fixture\n;;\nesac\n",
    "f() {\nOPENAI_API_KEY=fixture\n}\n",
    "f()\n{\nOPENAI_API_KEY=fixture\n}\n",
    "function f {\nOPENAI_API_KEY=fixture\n}\n",
    "function f { export OPENAI_API_KEY=fixture; }\n",
    "{\nOPENAI_API_KEY=fixture\n}\n",
    "(\nOPENAI_API_KEY=fixture\n)\n",
    "true &&\nOPENAI_API_KEY=fixture\n",
    "true ||\n# comment\nOPENAI_API_KEY=fixture\n",
    "true |\nOPENAI_API_KEY=fixture\n",
    "OPENAI_API_KEY=\\\nfixture\n",
    "export \\\nOPENAI_API_KEY=fixture\n",
    'OPENAI_API_KEY="multi\nline"\n',
    "OPENAI_API_KEY='unterminated\n",
    "cat <<EOF\nOPENAI_API_KEY=fixture\nEOF\n",
    "cat <<'EOF'\nexport OPENAI_API_KEY=fixture\nEOF\n",
    "cat <<-EOF\n\tOPENAI_API_KEY=fixture\n\tEOF\n",
    "eval 'export OPENAI_API_KEY=fixture'\n",
    "printf -v OPENAI_API_KEY '%s' fixture\n",
    "read -r OPENAI_API_KEY\n",
    "unset OPENAI_API_KEY\n",
    "for OPENAI_API_KEY in fixture; do :; done\n",
    'echo "${OPENAI_API_KEY:=fixture}"\n',
    "let 'OPENAI_API_KEY=1'\n",
    "(( OPENAI_API_KEY = 1 ))\n",
    'echo "$(export OPENAI_API_KEY=fixture)"\n',
    "! {\nOPENAI_API_KEY=fixture\n}\n",
    "if true; then if true; then :; fi\nOPENAI_API_KEY=fixture\nfi\n",
    "if if true; then :; fi; then\nOPENAI_API_KEY=fixture\nfi\n",
    "((\nOPENAI_API_KEY = 1\n))\n",
    "[[\nOPENAI_API_KEY=fixture\n]]\n",
    "foreach x (one two)\nOPENAI_API_KEY=fixture\nend\n",
    "repeat 3 {\nOPENAI_API_KEY=fixture\n}\n",
    "coproc worker {\nOPENAI_API_KEY=fixture\n}\n",
    "{ :; } always {\nOPENAI_API_KEY=fixture\n}\n",
    "true &&\\\nOPENAI_API_KEY=fixture\n",
    "OP\\\nENAI_API_KEY=fixture\n",
    "export \\\r\nOPENAI_API_KEY=fixture\r\n",
    'echo "$(( OPENAI_API_KEY = 1 ))"\n',
    'echo "$(echo "$(export OPENAI_API_KEY=fixture)")"\n',
    "cat <<EOF\nOPENAI_API_KEY=fixture\n",
    'echo "${OTHER:-$(export OPENAI_API_KEY=fixture)}"\n',
    "2>/dev/null OPENAI_API_KEY=fixture\n",
    "eval " * 15 + "export OPENAI_API_KEY=fixture\n",
])
def test_nonliteral_or_non_top_level_writers_are_targeted_issues(tmp_path, text):
    profile = _profile(tmp_path, text)
    assert KEY not in profile.values
    assert profile.assignments == ()
    assert any(KEY in issue.names and issue.reason == "dynamic_shell" for issue in profile.issues)
    assert all(issue.line and issue.line >= 1 for issue in profile.issues)
    with pytest.raises(TakeoverStateError, match="manual migration"):
        cleanup_shell_profile(profile, {KEY: "fixture"})
    assert profile.path.read_bytes() == text.encode()


@pytest.mark.parametrize("text", [
    "# export OPENAI_API_KEY=fixture\n",
    'echo "OPENAI_API_KEY=fixture"\n',
    "echo OPENAI_API_KEY=fixture\n",
    "printf '%s' 'export OPENAI_API_KEY=fixture'\n",
    'echo "$OPENAI_API_KEY"\n',
    "echo ${OPENAI_API_KEY:-default}\n",
    "export OPENAI_API_KEY\n",
    "alias example='export OPENAI_API_KEY=fixture'\n",
    "OTHER='OPENAI_API_KEY=fixture'\n",
    'OTHER="if ; ( ) { source file; OPENAI_API_KEY=fixture"\n',
    "echo 'if'\necho '{'\necho 'f()'\necho '<<EOF'\n",
    'echo "multiline\nOPENAI_API_KEY=fixture\nstring"\n',
    "eval \"echo 'OPENAI_API_KEY=fixture'\"\n",
    'echo "$(echo OPENAI_API_KEY=fixture)"\n',
    'echo "$( (echo \'OPENAI_API_KEY=fixture\') )"\n',
    ". ~/.other\n",
    "source ~/.other\n",
    "source 'OPENAI_API_KEY=fixture'\n",
    "test -f ~/.bashrc && . ~/.bashrc\n",
    "if [ -f ~/.bashrc ]; then\n. ~/.bashrc\nfi\n",
    'export PATH="$HOME/bin:$PATH"\n',
    'cd "$HOME"\nset -o vi\numask 022\n',
    '[[ -n "$HOME" ]]\n',
    '[[ -n "$HOME"\n&& -d "$HOME" ]]\n',
    'if [[ -n "$HOME" ]]; then :; fi\n',
    "env printf '%s' OPENAI_API_KEY=fixture\n",
    "read -p OPENAI_API_KEY unrelated\n",
    "printf '%s' -v OPENAI_API_KEY\n",
    'echo "${OTHER:-OPENAI_API_KEY=fixture}"\n',
    'f() if true; then echo "OPENAI_API_KEY=quoted"; fi\n',
    'echo 2>/dev/null OPENAI_API_KEY=fixture\n',
])
def test_references_comments_and_quoted_data_are_not_credential_writers(tmp_path, text):
    profile = _profile(tmp_path, text + "OPENAI_API_KEY=fixture\n")
    assert profile.values == {KEY: "fixture"}
    assert profile.issues == ()
    assert len(profile.assignments) == 1
    assert cleanup_shell_profile(profile, {KEY: "fixture"}).after == text.encode()


def test_ordinary_include_never_follows_unknown_script(tmp_path):
    included = tmp_path / "outside"
    included.write_text("export OPENAI_API_KEY=not-scanned\n")
    profile = _profile(tmp_path, f'. "{included}"\n')
    assert profile.values == {}
    assert profile.assignments == ()
    assert profile.issues == ()
    assert cleanup_shell_profile(profile, {KEY: "not-scanned"}).after == profile.before


def test_matching_duplicates_are_removable_but_differing_duplicates_are_ambiguous(tmp_path):
    profile = _profile(tmp_path, "OPENAI_API_KEY=fixture\nexport OPENAI_API_KEY='fixture'\n")
    assert profile.values == {KEY: "fixture"}
    assert len(profile.assignments) == 2
    assert cleanup_shell_profile(profile, {KEY: "fixture"}).after == b""
    profile = _profile(tmp_path, "OPENAI_API_KEY=fixture\nOPENAI_API_KEY=different\n")
    assert profile.values == {}
    assert len(profile.assignments) == 2
    issue, = profile.issues
    assert issue.names == (KEY,)
    assert issue.reason == "ambiguous_shell"
    assert issue.line == 2
    with pytest.raises(TakeoverStateError):
        cleanup_shell_profile(profile, {KEY: "fixture"})


def test_invalid_name_is_excluded_without_blocking_an_unrelated_name(tmp_path):
    content = (
        "OPENAI_API_KEY=fixture\n"
        'if true; then\nOPENAI_API_KEY="$DYNAMIC"\nfi\n'
        "ANTHROPIC_API_KEY=other\n"
    )
    profile = _profile(tmp_path, content)
    assert profile.values == {OTHER: "other"}
    assert cleanup_shell_profile(profile, {OTHER: "other"}).after == content.rsplit("ANTHROPIC", 1)[0].encode()
    with pytest.raises(TakeoverStateError):
        cleanup_shell_profile(profile, {KEY: "fixture"})


def test_multiple_heredocs_do_not_change_outer_shell_structure(tmp_path):
    prefix = (
        "cat <<'ONE' <<-TWO\n"
        "if this were code; then {\n"
        "ONE\n"
        "\techo '}'\n"
        "\tTWO\n"
    )
    profile = _profile(tmp_path, prefix + "OPENAI_API_KEY=fixture\n")
    assert profile.values == {KEY: "fixture"}
    assert profile.issues == ()
    assert cleanup_shell_profile(profile, {KEY: "fixture"}).after == prefix.encode()


def test_cleanup_round_trips_through_journal_without_reformatting(tmp_path):
    before = (
        "# 注释\r\nexport OPENAI_API_KEY='fixture-密钥' # consented\r\n"
        "\r\nexport PATH=\"$HOME/bin:$PATH\"\nANTHROPIC_API_KEY=keep\n"
        "export OPENAI_API_KEY=fixture-密钥\n# 尾部"
    ).encode()
    after = (
        "# 注释\r\n\r\nexport PATH=\"$HOME/bin:$PATH\"\n"
        "ANTHROPIC_API_KEY=keep\n# 尾部"
    ).encode()
    profile = _profile(tmp_path, before)
    edit = cleanup_shell_profile(profile, {KEY: "fixture-密钥"})
    assert edit.before == before
    assert edit.after == after
    assert profile.path.read_bytes() == before
    restored = NativeFileEdit.from_payload(edit.to_payload())
    assert restored == edit
    restored.check()
    restored.apply()
    restored.apply()
    assert profile.path.read_bytes() == after
    restored.check(applied=True)
    restored.apply(reverse=True)
    restored.apply(reverse=True)
    assert profile.path.read_bytes() == before


def test_noop_guards_and_selected_value_mismatch(tmp_path):
    profile = _profile(tmp_path, "OPENAI_API_KEY=fixture\n")
    for selected in ({}, {OTHER: "absent"}):
        edit = cleanup_shell_profile(profile, selected)
        assert edit.before == edit.after == profile.before
        edit.check()
        edit.apply()
    with pytest.raises(TakeoverStateError, match="value changed") as error:
        cleanup_shell_profile(profile, {KEY: "mismatched-secret"})
    assert "mismatched-secret" not in str(error.value)
    assert "fixture" not in str(error.value)


def test_changed_or_new_file_fails_compare_before_write(tmp_path):
    profile = _profile(tmp_path, "OPENAI_API_KEY=fixture\n")
    edit = cleanup_shell_profile(profile, {KEY: "fixture"})
    profile.path.write_bytes(b"concurrent edit\n")
    with pytest.raises(TakeoverStateError, match="changed"):
        edit.apply()
    assert profile.path.read_bytes() == b"concurrent edit\n"
    absent, = [profile for profile in read_shell_profiles(tmp_path, NAMES) if profile.path.name == ".profile"]
    guard = cleanup_shell_profile(absent, {})
    assert guard.before is guard.after is None
    absent.path.write_bytes(b"OPENAI_API_KEY=new\n")
    with pytest.raises(TakeoverStateError, match="changed"):
        guard.check()


def test_all_eight_absent_paths_are_returned_in_order(tmp_path):
    profiles = read_shell_profiles(tmp_path, NAMES)
    assert tuple(profile.path.name for profile in profiles) == SHELL_PROFILE_NAMES
    assert len(profiles) == 8
    for profile in profiles:
        assert profile.path.is_absolute()
        assert profile.before is None
        assert profile.values == {}
        assert profile.assignments == profile.issues == ()
        edit = cleanup_shell_profile(profile, {})
        assert edit.before is edit.after is None
        edit.apply()
        assert not profile.path.exists()


def test_none_home_and_ambient_credentials_are_hermetic(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv(KEY, "fixture-runtime-must-not-be-read")
    (tmp_path / ".profile").write_bytes(b"OPENAI_API_KEY=stored\n")
    profiles = read_shell_profiles(None, NAMES)
    assert profiles[0].values == {KEY: "stored"}
    assert all(profile.path.parent == tmp_path for profile in profiles)


@pytest.mark.parametrize("kind", ["symlink", "dangling", "directory", "unreadable", "encoding"])
def test_unreadable_and_nonregular_files_have_sanitized_issues(monkeypatch, tmp_path, kind):
    target = tmp_path / ".profile"
    if kind == "symlink":
        outside = tmp_path / "outside"
        outside.write_bytes(b"OPENAI_API_KEY=must-not-read\n")
        target.symlink_to(outside)
    elif kind == "dangling":
        target.symlink_to(tmp_path / "absent")
    elif kind == "directory":
        target.mkdir()
    elif kind == "encoding":
        target.write_bytes(b"\xffOPENAI_API_KEY=must-not-read\n")
    else:
        target.write_bytes(b"OPENAI_API_KEY=must-not-read\n")
    real_read = Path.read_bytes
    def read(path):
        if path == target and kind in {"symlink", "dangling", "directory"}:
            pytest.fail("nonregular profile was followed")
        if path == target and kind == "unreadable":
            raise PermissionError("sensitive exception content")
        return real_read(path)
    monkeypatch.setattr(Path, "read_bytes", read)
    profile = read_shell_profiles(tmp_path, NAMES)[0]
    assert profile.before is None
    assert profile.assignments == ()
    assert profile.values == {}
    issue, = profile.issues
    assert issue.names == tuple(sorted(NAMES))
    assert issue.reason == "unreadable"
    assert issue.line is None
    assert "sensitive" not in repr(profile) + repr(issue)
    with pytest.raises(TakeoverStateError):
        cleanup_shell_profile(profile, {KEY: "must-not-read"})


def test_snapshots_are_immutable_and_reprs_do_not_expose_contents(tmp_path):
    profile = _profile(tmp_path, "OPENAI_API_KEY=fixture-private-value\n")
    assignment, = profile.assignments
    issue = ShellIssue((KEY,), "dynamic_shell", 1)
    for value, field in [(profile, "before"), (assignment, "value"), (issue, "reason")]:
        with pytest.raises(FrozenInstanceError):
            setattr(value, field, "replacement")
    assert "fixture-private-value" not in repr(profile) + repr(assignment) + repr(issue)
    assert "fixture-private-value" not in repr(cleanup_shell_profile(profile, profile.values))
    detached = profile.values
    detached[KEY] = "replacement"
    assert profile.values == {KEY: "fixture-private-value"}
    assert isinstance(assignment, ShellAssignment)


def test_parser_never_executes_profile_commands(monkeypatch, tmp_path):
    import os
    import subprocess

    def forbidden(*args, **kwargs):
        pytest.fail("the shell parser attempted to execute a process")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(os, "system", forbidden)
    marker = tmp_path / "executed"
    profile = _profile(tmp_path, f"OPENAI_API_KEY=$(touch {marker})\n")
    assert profile.values == {}
    assert profile.issues[0].reason == "dynamic_shell"
    assert not marker.exists()
