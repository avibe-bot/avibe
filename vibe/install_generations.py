"""Best-effort collection of positively owned Avibe install generations.

An activation receipt is ownership evidence, not a lease or a rollback plan.
Unreceipted directories include released installs and concurrently staged
candidates, so age and directory names never authorize their removal.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import shlex
import shutil
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

import psutil

from config.atomic_io import write_atomic

if TYPE_CHECKING:
    from vibe.upgrade import AtomicActivation

logger = logging.getLogger(__name__)
RECEIPT = ".avibe-install.json"
INSTALLER_PID = ".avibe-installing"
_PATH_OPTIONS = ("--candidate", "--launcher", "--source-generation")


@dataclass(frozen=True)
class _OwnerIdentity:
    uid: int | None = None
    name: str | None = None
    sid: str | None = None


class _ProcessInspectionUnavailable(RuntimeError):
    """Process ownership or path provenance was not positively established."""


def _receipt_launchers(generation: Path) -> set[Path] | None:
    try:
        payload = json.loads((generation / RECEIPT).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ValueError(f"unknown install receipt in {generation}")
    launchers = payload.get("launchers")
    if (
        not isinstance(launchers, list)
        or not launchers
        or any(
            not isinstance(value, str)
            or not Path(value).is_absolute()
            or Path(value).name.lower() not in {"vibe", "vibe.exe"}
            for value in launchers
        )
    ):
        raise ValueError(f"invalid install receipt in {generation}")
    return {Path(value) for value in launchers}


def _launcher_receipt_path(launcher: Path) -> Path:
    return launcher.expanduser().absolute().parent.resolve() / launcher.name


def _write_receipt(generation: Path, launchers: set[Path]) -> None:
    write_atomic(
        generation / RECEIPT,
        json.dumps({"version": 1, "launchers": sorted(map(str, launchers))}),
    )


def reserve_activation_reference(launcher: Path, target: Path) -> bool:
    """Reserve a new alias before publishing it.

    Fresh candidates have no receipt yet and are recorded only after activation.
    An already-owned generation must publish the alias in its existing receipt
    first, so a receipt write failure leaves the stable launcher unchanged.
    """

    from vibe import upgrade

    root = upgrade.atomic_uv_install_root().expanduser().resolve()
    generation = upgrade._generation_for_path(target, root)
    if generation is None:
        return False
    launchers = _receipt_launchers(generation)
    if launchers is None:
        return False
    launcher_path = _launcher_receipt_path(launcher)
    if launcher_path not in launchers:
        launchers.add(launcher_path)
        _write_receipt(generation, launchers)
    return True


def record_activation(
    launcher: Path,
    target: Path,
    *,
    receipt_reserved: bool = False,
    allow_new_receipt: bool = True,
) -> None:
    """Record only a committed activation; failure must never discard its target."""
    from vibe import upgrade

    if receipt_reserved:
        return
    try:
        root = upgrade.atomic_uv_install_root().expanduser().resolve()
        generation = upgrade._generation_for_path(target, root)
        if generation is None:
            return
        launchers = _receipt_launchers(generation)
        if launchers is None:
            if not allow_new_receipt:
                return
            launchers = set()
        # Resolve the directory, not the entrypoint: the latter is replaced.
        launchers.add(_launcher_receipt_path(launcher))
        _write_receipt(generation, launchers)
    except Exception:
        # This is an explicit post-commit boundary: no bookkeeping error may
        # escape to callers that discard candidates on activation failure.
        # Without durable positive ownership, later collection cannot distinguish
        # this generation from pre-protocol history. It must remain unowned.
        logger.warning(
            "Install activation succeeded; retention receipt unavailable, "
            "unowned generation will be retained",
            exc_info=True,
        )


def _installer_is_live(generation: Path) -> bool:
    marker = generation / INSTALLER_PID
    try:
        pid = int(marker.read_text(encoding="utf-8-sig").strip())
        written_at = marker.stat().st_mtime
    except FileNotFoundError:
        return False
    if pid <= 0:
        raise ValueError(f"invalid installer owner in {generation}")
    try:
        process = psutil.Process(pid)
        # A reused PID is not the installer. Round for filesystems with coarse
        # timestamps; uncertainty retains data instead of deleting it.
        return process.status() != psutil.STATUS_ZOMBIE and int(process.create_time()) <= int(written_at)
    except psutil.NoSuchProcess:
        return False


def _windows_sid_to_text(sid: object) -> str:
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    convert_sid = advapi32.ConvertSidToStringSidW
    convert_sid.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)]
    convert_sid.restype = wintypes.BOOL
    text = ctypes.c_wchar_p()
    if not convert_sid(sid, ctypes.byref(text)):
        raise OSError(ctypes.get_last_error(), "ConvertSidToStringSidW failed")
    try:
        return text.value
    finally:
        local_free = ctypes.WinDLL("kernel32", use_last_error=True).LocalFree
        local_free.argtypes = [ctypes.c_void_p]
        local_free.restype = ctypes.c_void_p
        local_free(text)


def _windows_sid_to_account_name(sid: object) -> str:
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    lookup_account_sid = advapi32.LookupAccountSidW
    lookup_account_sid.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_void_p,
        ctypes.c_wchar_p,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_wchar_p,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
    ]
    lookup_account_sid.restype = wintypes.BOOL
    name_size = wintypes.DWORD()
    domain_size = wintypes.DWORD()
    sid_type = wintypes.DWORD()
    lookup_account_sid(
        None,
        sid,
        None,
        ctypes.byref(name_size),
        None,
        ctypes.byref(domain_size),
        ctypes.byref(sid_type),
    )
    if ctypes.get_last_error() != 122:  # ERROR_INSUFFICIENT_BUFFER
        raise OSError(ctypes.get_last_error(), "LookupAccountSidW size failed")
    name = ctypes.create_unicode_buffer(name_size.value)
    domain = ctypes.create_unicode_buffer(domain_size.value)
    if not lookup_account_sid(
        None,
        sid,
        name,
        ctypes.byref(name_size),
        domain,
        ctypes.byref(domain_size),
        ctypes.byref(sid_type),
    ):
        raise OSError(ctypes.get_last_error(), "LookupAccountSidW failed")
    return f"{domain.value}\\{name.value}" if domain.value else name.value


def _windows_filesystem_owner(path: Path) -> _OwnerIdentity:
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    get_named_security_info = advapi32.GetNamedSecurityInfoW
    get_named_security_info.restype = wintypes.DWORD
    security_descriptor = ctypes.c_void_p()
    owner_sid = ctypes.c_void_p()
    result = get_named_security_info(
        ctypes.c_wchar_p(str(path)),
        1,  # SE_FILE_OBJECT
        0x00000001,  # OWNER_SECURITY_INFORMATION
        ctypes.byref(owner_sid),
        None,
        None,
        None,
        ctypes.byref(security_descriptor),
    )
    if result:
        raise OSError(result, f"GetNamedSecurityInfoW failed for {path}")
    try:
        sid = _windows_sid_to_text(owner_sid)
        try:
            name = _windows_sid_to_account_name(owner_sid)
        except OSError:
            name = None
        return _OwnerIdentity(sid=sid, name=name)
    finally:
        local_free = ctypes.WinDLL("kernel32", use_last_error=True).LocalFree
        local_free.argtypes = [ctypes.c_void_p]
        local_free.restype = ctypes.c_void_p
        local_free(security_descriptor)


def _windows_process_owner(process: psutil.Process) -> _OwnerIdentity:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    open_process.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    open_process_token = advapi32.OpenProcessToken
    open_process_token.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    open_process_token.restype = wintypes.BOOL
    get_token_information = advapi32.GetTokenInformation
    get_token_information.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    get_token_information.restype = wintypes.BOOL
    process_handle = open_process(
        0x1000,
        False,
        process.pid,
    )  # PROCESS_QUERY_LIMITED_INFORMATION
    if not process_handle:
        error_code = ctypes.get_last_error()
        if error_code == 87:  # ERROR_INVALID_PARAMETER: snapshot PID no longer exists
            try:
                if not process.is_running():
                    raise psutil.NoSuchProcess(process.pid)
            except psutil.NoSuchProcess:
                raise
            except psutil.Error:
                pass
        raise OSError(error_code, f"OpenProcess failed for pid {process.pid}")
    token_handle = ctypes.c_void_p()
    try:
        if not open_process_token(process_handle, 0x0008, ctypes.byref(token_handle)):
            raise OSError(ctypes.get_last_error(), f"OpenProcessToken failed for pid {process.pid}")
        try:
            size = wintypes.DWORD()
            get_token_information(token_handle, 1, None, 0, ctypes.byref(size))
            if not size.value:
                raise OSError(ctypes.get_last_error(), f"GetTokenInformation size failed for pid {process.pid}")
            buffer = ctypes.create_string_buffer(size.value)
            if not get_token_information(
                token_handle,
                1,  # TokenUser
                buffer,
                size,
                ctypes.byref(size),
            ):
                raise OSError(ctypes.get_last_error(), f"GetTokenInformation failed for pid {process.pid}")
            owner_sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p)).contents
            sid = _windows_sid_to_text(owner_sid)
            try:
                name = _windows_sid_to_account_name(owner_sid)
            except OSError:
                name = None
            return _OwnerIdentity(sid=sid, name=name)
        finally:
            close_handle(token_handle)
    finally:
        close_handle(process_handle)


def _filesystem_owner(root: Path) -> _OwnerIdentity:
    if os.name == "nt":
        return _windows_filesystem_owner(root)
    try:
        metadata = root.stat()
    except OSError as exc:
        raise _ProcessInspectionUnavailable(f"cannot inspect install root owner: {root}") from exc
    name = None
    try:
        import pwd

        name = pwd.getpwuid(metadata.st_uid).pw_name
    except (ImportError, KeyError, OSError):
        pass
    return _OwnerIdentity(uid=metadata.st_uid, name=name)


def _process_owner(process: psutil.Process) -> _OwnerIdentity:
    if os.name == "nt":
        try:
            return _windows_process_owner(process)
        except psutil.NoSuchProcess:
            raise
        except (OSError, RuntimeError) as exc:
            # OpenProcess can race a process that disappeared after
            # process_iter() captured it.  Preserve that normal snapshot race
            # as NoSuchProcess so the caller skips only that process; a live
            # or unclassifiable process still defers collection.
            try:
                if not process.is_running():
                    raise psutil.NoSuchProcess(process.pid)
            except psutil.NoSuchProcess:
                raise
            except psutil.Error:
                pass
            try:
                name = process.username()
            except psutil.NoSuchProcess:
                raise
            except (OSError, psutil.Error, AttributeError) as owner_exc:
                raise _ProcessInspectionUnavailable(
                    f"cannot inspect process owner for pid {process.pid}"
                ) from owner_exc
            if name:
                return _OwnerIdentity(name=name)
            raise _ProcessInspectionUnavailable(
                f"cannot inspect process owner for pid {process.pid}"
            ) from exc
    try:
        uids = process.uids()
    except AttributeError:
        uids = None
    except psutil.NoSuchProcess:
        raise
    except psutil.Error as exc:
        raise _ProcessInspectionUnavailable(
            f"cannot inspect process owner for pid {process.pid}"
        ) from exc
    if uids is not None:
        return _OwnerIdentity(uid=uids.real)
    try:
        return _OwnerIdentity(name=process.username())
    except psutil.NoSuchProcess:
        raise
    except (OSError, psutil.Error, AttributeError) as exc:
        raise _ProcessInspectionUnavailable(
            f"cannot inspect process owner for pid {process.pid}"
        ) from exc


def _owners_match_any(
    process_owner: _OwnerIdentity,
    install_owners: list[_OwnerIdentity],
) -> bool:
    comparable = False
    incomparable = False
    for install_owner in install_owners:
        if process_owner.sid is not None and install_owner.sid is not None:
            comparable = True
            if process_owner.sid == install_owner.sid:
                return True
        elif process_owner.uid is not None and install_owner.uid is not None:
            comparable = True
            if process_owner.uid == install_owner.uid:
                return True
        elif process_owner.name is not None and install_owner.name is not None:
            comparable = True
            if os.path.normcase(process_owner.name) == os.path.normcase(install_owner.name):
                return True
        else:
            incomparable = True
    if incomparable or not comparable:
        raise _ProcessInspectionUnavailable("process and install owner identities are incomparable")
    return False


def _normalize_process_argument(value: object) -> str:
    text = str(value)
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


def _shell_tokens_with_quote_provenance(
    command: str,
) -> list[tuple[str, tuple[bool, ...]]]:
    """Parse a POSIX fallback command while retaining quoting evidence."""

    tokens: list[tuple[str, tuple[bool, ...]]] = []
    current: list[tuple[str, bool]] = []
    token_started = False
    quote: str | None = None
    index = 0
    while index < len(command):
        value = command[index]
        if quote is not None:
            if value == quote:
                quote = None
            elif value == "\\" and quote == '"' and index + 1 < len(command):
                index += 1
                current.append((command[index], True))
            else:
                current.append((value, True))
            token_started = True
        elif value in "\"'":
            quote = value
            token_started = True
        elif value == "\\":
            if index + 1 >= len(command):
                raise ValueError("unterminated escape in fallback command")
            index += 1
            current.append((command[index], True))
            token_started = True
        elif value.isspace():
            if token_started:
                tokens.append((
                    "".join(value for value, _ in current),
                    tuple(quoted for _, quoted in current),
                ))
                current = []
                token_started = False
        else:
            current.append((value, False))
            token_started = True
        index += 1
    if quote is not None:
        raise ValueError("unterminated quote in fallback command")
    if token_started:
        tokens.append((
            "".join(value for value, _ in current),
            tuple(quoted for _, quoted in current),
        ))
    return tokens


def _fallback_path_boundaries_are_proven(
    command: str,
    arguments: list[str],
) -> bool:
    if os.name == "nt":
        return True
    try:
        tokens = _shell_tokens_with_quote_provenance(command)
    except ValueError:
        return False
    if [value for value, _ in tokens] != arguments:
        return False
    if arguments and not all(tokens[0][1]):
        return False
    for index, argument in enumerate(arguments):
        if argument in _PATH_OPTIONS:
            if index + 1 >= len(tokens) or not all(tokens[index + 1][1]):
                return False
        elif any(argument.startswith(f"{option}=") for option in _PATH_OPTIONS):
            option, operand = argument.split("=", 1)
            offset = len(option) + 1
            if not operand or not all(tokens[index][1][offset:]):
                return False
    return True


def _relative_path_argument(
    value: str,
    *,
    index: int,
    previous: str | None,
    cwd: Path,
    ignore_bare_interpreter: bool = False,
) -> Path | None:
    for option in _PATH_OPTIONS:
        prefix = f"{option}="
        if value.startswith(prefix):
            operand = value[len(prefix) :]
            if not operand:
                raise _ProcessInspectionUnavailable(f"empty path argument in {value}")
            return None if Path(operand).is_absolute() else Path(operand)
    if not value or value.startswith("-") or "://" in value:
        return None
    if Path(value).is_absolute():
        return None
    if previous in {"--candidate", "--launcher", "--source-generation"}:
        return Path(value)
    if (
        value in {".", ".."}
        or "/" in value
        or "\\" in value
        or Path(cwd / value).is_file()
    ):
        return Path(value)
    if not (value in {".", ".."} or value.startswith(("./", "../", ".\\", "..\\"))):
        if "/" not in value and "\\" not in value:
            if index != 0:
                return None
            name = Path(value).name.lower()
            if ignore_bare_interpreter and name.startswith("python"):
                return None
            if not name.startswith("python") and name not in {"vibe", "vibe.exe"}:
                return None
    path = Path(value)
    if index == 0:
        return path
    return None


def _process_arguments(process: psutil.Process) -> list[str]:
    try:
        arguments = process.cmdline()
    except psutil.AccessDenied:
        from vibe import runtime

        command = runtime.get_process_command(process.pid)
        if not command:
            raise _ProcessInspectionUnavailable(
                f"cannot inspect command line for pid {process.pid}"
            )
        try:
            arguments = shlex.split(command, posix=(os.name != "nt"))
        except ValueError as exc:
            raise _ProcessInspectionUnavailable(
                f"cannot parse command line for pid {process.pid}"
            ) from exc
        if not _fallback_path_boundaries_are_proven(command, arguments):
            raise _ProcessInspectionUnavailable(
                f"cannot prove command-line path boundaries for pid {process.pid}"
            )
    if not arguments:
        raise _ProcessInspectionUnavailable(f"cannot inspect command line for pid {process.pid}")
    return [_normalize_process_argument(value) for value in arguments]


def _process_cwd(process: psutil.Process) -> Path:
    try:
        cwd = process.cwd()
    except psutil.NoSuchProcess:
        raise
    except (psutil.Error, OSError, AttributeError) as exc:
        raise _ProcessInspectionUnavailable(f"cannot inspect cwd for pid {process.pid}") from exc
    if not cwd:
        raise _ProcessInspectionUnavailable(f"process {process.pid} has no inspectable cwd")
    path = Path(cwd).expanduser()
    if not path.is_absolute():
        raise _ProcessInspectionUnavailable(f"process {process.pid} has a relative cwd")
    return path


def _is_verifiable_kernel_thread(process: psutil.Process) -> bool:
    """Return true only for Linux tasks positively marked as kernel threads.

    An empty command line is not sufficient evidence: ordinary userspace
    processes can be unreadable or can race their own exit. Linux exposes the
    kernel-owned ``Kthread: 1`` marker in each task's proc status record; all
    other platforms and unreadable records remain in the normal fail-closed
    path.
    """

    if not sys.platform.startswith("linux"):
        return False
    try:
        status = (Path("/proc") / str(process.pid) / "status").read_text(
            encoding="utf-8",
        )
    except (OSError, UnicodeError):
        return False
    return any(
        line.partition(":")[0] == "Kthread"
        and line.partition(":")[2].strip() == "1"
        for line in status.splitlines()
    )


def _running_paths() -> set[Path]:
    """Keep logical argv, cwd, and image for every relevant process.

    The installation root owner is authoritative on POSIX and is read from the
    Windows filesystem security descriptor on Windows. Owners of the updater
    and recorded service/UI PIDs are additional authoritative identities for
    installations staged by an administrator. Relative argv paths are resolved
    against the current cwd for diagnostics, but their launch cwd is unknowable
    after a process changes directory, so collection defers instead of guessing.
    """

    from config import paths as config_paths
    from vibe import upgrade

    root = upgrade.atomic_uv_install_root().expanduser().resolve()
    try:
        install_owners = [_filesystem_owner(root)]
    except _ProcessInspectionUnavailable:
        if root.exists():
            raise
        install_owners = [_OwnerIdentity(uid=getattr(os, "getuid", lambda: None)())]
    paths = {Path(sys.executable), Path(sys.prefix), Path(__file__)}
    managed_pids = {os.getpid()}
    for pid_path in (config_paths.get_runtime_pid_path(), config_paths.get_runtime_ui_pid_path()):
        try:
            pid = int(pid_path.read_text(encoding="utf-8").strip())
            if pid > 0:
                managed_pids.add(pid)
        except FileNotFoundError:
            pass
    for pid in managed_pids:
        try:
            install_owners.append(_process_owner(psutil.Process(pid)))
        except psutil.NoSuchProcess:
            continue
    processes = list(psutil.process_iter())
    known_pids = {process.pid for process in processes}
    for pid in managed_pids - known_pids:
        try:
            processes.append(psutil.Process(pid))
        except psutil.NoSuchProcess:
            continue
    for process in processes:
        try:
            if process.pid <= 0:
                continue
            if process.pid not in managed_pids and not _owners_match_any(
                _process_owner(process),
                install_owners,
            ):
                continue
            if process.status() == psutil.STATUS_ZOMBIE:
                continue
            if _is_verifiable_kernel_thread(process):
                continue
            arguments = _process_arguments(process)
            cwd = _process_cwd(process)
            paths.add(cwd)
            relative = [
                path
                for index, value in enumerate(arguments)
                if (
                    path := _relative_path_argument(
                        value,
                        index=index,
                        previous=arguments[index - 1] if index else None,
                        cwd=cwd,
                        ignore_bare_interpreter=process.pid == os.getpid(),
                    )
                )
                is not None
            ]
            if relative:
                paths.update(cwd / path for path in relative)
                raise _ProcessInspectionUnavailable(
                    f"relative process paths have ambiguous launch provenance for pid {process.pid}"
                )
            values = [*arguments]
            values.extend(
                value.split("=", 1)[1]
                for value in arguments
                if any(value.startswith(f"{option}=") for option in _PATH_OPTIONS)
            )
            try:
                values.append(process.exe())
            except psutil.NoSuchProcess:
                raise
            except (psutil.Error, OSError, AttributeError) as exc:
                raise _ProcessInspectionUnavailable(
                    f"cannot inspect executable for pid {process.pid}"
                ) from exc
            paths.update(
                Path(value) for value in values
                if value and "\x00" not in value and Path(value).is_absolute()
            )
        except psutil.NoSuchProcess:
            continue
        # Ownership and path inspection failures propagate to the non-fatal
        # collection boundary. A partial keep set is never inferred.
    return paths


def collect_before_activation(activation: AtomicActivation) -> list[Path]:
    """Keep current/source/candidate + every live reference; remove only owned history.

    The caller holds the shared install lock. Collection precedes the launcher
    switch, so the previous selected generation survives this operation. With
    one launcher and no extra references, subsequent operations retain two
    receipt-owned generations regardless of whether shell, CLI, or API created
    them. Unowned history, including failed new receipts, is never adopted.
    """
    from vibe import upgrade

    try:
        root = upgrade.atomic_uv_install_root().expanduser().resolve()
        if not root.is_dir():
            return []
        generations = [
            child for child in root.iterdir()
            if not child.is_symlink() and child.is_dir() and child.resolve().parent == root
        ]
        owned: dict[Path, set[Path]] = {}
        for generation in generations:
            launchers = _receipt_launchers(generation)
            if launchers is not None:
                owned[generation] = launchers
        logger.info(
            "Install generation retention: %d owned, %d unowned history/staging retained",
            len(owned), len(generations) - len(owned),
        )
        if not owned:
            return []
        restart_path = upgrade.runtime_mod.get_restart_status_path()
        try:
            restart = json.loads(restart_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            pass
        else:
            try:
                state = upgrade.RestartState(restart.get("state")) if isinstance(restart, dict) else None
            except (TypeError, ValueError):
                state = None
            # The ordinary restart admission policy treats unknown states as
            # stale. Destructive collection needs positive terminal/pending
            # interpretation; a future writer's state cannot authorize deletion.
            if (
                state is None
                or state is upgrade.RestartState.UNKNOWN
                or upgrade.restart_record_is_pending(restart, restart_path)
            ):
                logger.info("Install generation collection deferred: restart ownership")
                return []
        candidate = upgrade._generation_for_path(activation.candidate_launcher, root)
        if any(generation != candidate and _installer_is_live(generation) for generation in generations):
            logger.info("Install generation collection deferred: another installer owns a handoff")
            return []

        paths = _running_paths()
        paths.add(activation.candidate_launcher)
        if activation.source_generation is not None:
            paths.add(activation.source_generation)
        kept = {
            generation for path in paths
            if (generation := upgrade._generation_for_path(path, root)) is not None
        }
        launchers = {activation.launcher}
        for references in owned.values():
            launchers.update(references)
        for launcher in launchers:
            launcher_generation = upgrade._launcher_generation(launcher, root)
            if launcher_generation is not None:
                kept.add(launcher_generation)
                # A symlink, hardlink, or marker-backed copy has a unique
                # launcher identity. Do not broaden that proof into a byte
                # match across every identical wheel copy, or repeated
                # cross-volume installs would retain all generations.
                continue
            try:
                launcher_bytes = launcher.read_bytes()
            except FileNotFoundError:
                continue
            # A copied launcher can outlive a failed/stale marker write.
            # Read fresh bytes: filecmp's stat-based cache cannot prove identity
            # after a same-size/same-mtime replacement.
            for generation in owned:
                target = generation / "bin" / launcher.name
                try:
                    target_bytes = target.read_bytes()
                except FileNotFoundError:
                    if any(reference.name == launcher.name for reference in owned[generation]):
                        kept.add(generation)  # damaged export: identity is unknown
                    continue
                if launcher_bytes == target_bytes:
                    kept.add(generation)
        removed = []
        for generation in owned.keys() - kept:
            try:
                shutil.rmtree(generation)
                removed.append(generation)
            except OSError:
                try:
                    if generation.is_dir():
                        _write_receipt(generation, owned[generation])
                except Exception:
                    logger.warning(
                        "Could not restore ownership receipt for %s after partial cleanup",
                        generation,
                        exc_info=True,
                    )
                logger.warning("Could not collect owned install generation %s", generation, exc_info=True)
        if removed:
            logger.info("Collected %d superseded owned install generations", len(removed))
        return removed
    except Exception:
        # Collection is optional. Unknown ownership/visibility means defer;
        # callers must still be able to install and activate a valid candidate.
        logger.warning("Install generation collection deferred; ownership could not be established", exc_info=True)
        return []
