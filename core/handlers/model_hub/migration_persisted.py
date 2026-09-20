"""One persisted inventory for credential references and consent snapshots."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from vibe.native_oauth_store import native_credentials_paths

from .migration_files import (
    _object,
    claude_settings_paths,
    codex_config_paths,
    native_config_references,
    opencode_auth_path,
    opencode_catalog_path,
    opencode_config_paths,
    tomllib,
)
from .migration_journal import NativeFileEdit, TakeoverStateError, _read_regular
from .migration_shell import ShellProfile, read_shell_profiles

BUILTIN_NAMES = frozenset({
    "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN", "OPENAI_API_KEY", "CODEX_API_KEY",
    "OPENAI_BASE_URL", "OPENROUTER_API_KEY",
})


@dataclass(frozen=True, repr=False)
class PersistedValue:
    value: str | None = None
    paths: tuple[str, ...] = ()
    reason: str | None = None


@dataclass(repr=False)
class PersistedInventory:
    profiles: tuple[ShellProfile, ...]
    guards: tuple[NativeFileEdit, ...]
    documents: dict[Path, dict | None]
    problems: dict[Path, str]
    backend_paths: dict[str, tuple[Path, ...]]
    credential_paths: dict[str, tuple[Path, ...]]
    references: dict[str, frozenset[str]]
    catalog_path: Path
    _values: dict[str, PersistedValue] = field(default_factory=dict)

    @classmethod
    def read(cls, home: Path | None, projects: tuple[Path, ...]) -> PersistedInventory:
        backend_paths = {
            "claude": claude_settings_paths(home, projects),
            "codex": codex_config_paths(home, projects),
            "opencode": opencode_config_paths(home, projects),
        }
        documents: dict[Path, dict | None] = {}
        problems: dict[Path, str] = {}
        guards: dict[Path, NativeFileEdit] = {}
        references: dict[str, set[str]] = {backend: set() for backend in backend_paths}
        for backend, paths in backend_paths.items():
            for path in paths:
                try:
                    before = _read_regular(path)
                    guards[path] = NativeFileEdit(path, before, before)
                except (OSError, TakeoverStateError):
                    problems[path] = "unreadable"
                    continue
                try:
                    payload = (
                        None if before is None else
                        tomllib.loads(before.decode()) if backend == "codex" else
                        _object(before, jsonc=backend == "opencode")
                    )
                    documents[path] = payload
                except (ValueError, UnicodeError, TakeoverStateError):
                    problems[path] = "config"
                    continue
                if not payload:
                    continue
                references[backend].update(native_config_references(backend, payload))
        # Native stores still own their read/permission policy. These snapshots
        # only bind file changes; no OS credential API is invoked here.
        credential_paths = {
            "claude": tuple(path.absolute() for path in native_credentials_paths("claude", home)),
            "codex": tuple(path.absolute() for path in native_credentials_paths("codex", home)),
            "opencode": (opencode_auth_path(home).absolute(),),
        }
        for path in dict.fromkeys(path for paths in credential_paths.values() for path in paths):
            path = path.absolute()
            try:
                before = _read_regular(path)
                guards[path] = NativeFileEdit(path, before, before)
            except (OSError, TakeoverStateError):
                problems[path] = "unreadable"
        catalog_path = opencode_catalog_path(home).absolute()
        try:
            before = _read_regular(catalog_path)
            guards[catalog_path] = NativeFileEdit(catalog_path, before, before)
            documents[catalog_path] = _object(before) if before else {}
        except (OSError, TakeoverStateError):
            # The optional catalog is not an authentication store. Its
            # existing loader degrades to no catalog when it is unreadable.
            documents[catalog_path] = {}
        names = BUILTIN_NAMES | frozenset().union(*references.values())
        return cls(
            read_shell_profiles(home, names), tuple(guards.values()), documents,
            problems, backend_paths, credential_paths,
            {backend: frozenset(names) for backend, names in references.items()},
            catalog_path,
        )

    def resolve(self, name: str) -> PersistedValue:
        if name in self._values:
            return self._values[name]
        paths: list[str] = []
        values: set[str] = set()
        reasons: list[str] = []
        for profile in self.profiles:
            relevant = [issue for issue in profile.issues if name in issue.names or not issue.names]
            if relevant or any(assignment.name == name for assignment in profile.assignments):
                paths.append(str(profile.path))
            reasons.extend(issue.reason for issue in relevant)
            value = profile.values.get(name)
            if value is not None:
                values.add(value)
        reason = next(
            (reason for reason in ("unreadable", "dynamic_shell", "ambiguous_shell") if reason in reasons),
            "ambiguous_shell" if len(values) > 1 else None,
        )
        result = PersistedValue(
            next(iter(values)) if len(values) == 1 and reason is None else None,
            tuple(dict.fromkeys(paths)), reason,
        )
        self._values[name] = result
        return result

    def snapshots(self, backend: str, *, shared: bool = False) -> tuple[NativeFileEdit, ...]:
        paths = {*self.backend_paths[backend], *self.credential_paths[backend]}
        if backend == "opencode":
            paths.add(self.catalog_path)
        guards = tuple(
            guard for guard in self.guards
            if shared or guard.path in paths
        )
        return (
            *guards,
            *(NativeFileEdit(profile.path, profile.before, profile.before) for profile in self.profiles
              if not any(issue.reason == "unreadable" for issue in profile.issues)),
        )

    @staticmethod
    def revision(guards: tuple[NativeFileEdit, ...]) -> str:
        digest = hashlib.sha256()
        for guard in guards:
            digest.update(str(guard.path).encode())
            digest.update(
                b"\0absent\0" if guard.before is None
                else b"\0present\0" + hashlib.sha256(guard.before).digest()
            )
        return digest.hexdigest()
