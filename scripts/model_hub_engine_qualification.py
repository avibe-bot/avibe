#!/usr/bin/env python3
"""Qualify a Model Hub engine candidate, and check that qualification later.

A candidate is a reviewed record, separate from the engine Avibe packages:
``patches/model-hub-engine-candidates/<asset_release_tag>/`` holds the exact
``model-hub-engine-manifest.json`` to publish and a ``candidate.json`` naming
the minimum Avibe version that may run it. Reviewing the record does not
schedule the engine for any Avibe release.

Qualification runs in ``.github/workflows/model-hub-engine-qualification.yml``
on the default branch, with no secret. Each job writes one evidence document;
the last job joins them into a receipt. A receipt is complete only when every
evidence class below is present for these exact candidate bytes:

- ``build``: the pinned source rebuilt to the reviewed digests;
- ``platform_smoke``: each of the six archives started and passed Avibe's
  health check on its own platform;
- ``hosted_wire``: the hosted passthrough compared with every baseline engine;
- ``released_avibe_e2e``: the full hermetic Model Hub E2E driven through the
  released wheel of the minimum and of the current stable Avibe;
- ``backward_state``: state written by the candidate read back by every
  baseline engine;
- ``lifecycle``: activation, rollback and recovery of the candidate.

The last two have no producing job yet; they arrive with the engine lifecycle
work. They are required all the same, so no complete receipt can exist until
they do.

Evidence counts only under the job that produces its class
(``EVIDENCE_PRODUCERS``). ``receipt`` reads each class from the artifact
named for its job, refuses any document that job does not produce, and
records the job on what it accepts. A class with no producing job is
therefore accepted from nowhere, whatever a job uploads, both when a receipt
is made and when the finalizer checks one.

That is an allowlist by artifact name and document shape. It is not proof of
which job uploaded an artifact: the receipt does not authenticate the
uploading job. Several jobs execute the candidate, as the user that writes
their evidence and beside the run's artifact credential. A candidate that
attacks its runner can make such a job say anything about itself and could
replace an artifact another job uploaded. Containing candidate execution is
unfinished qualification work, owed before every evidence class has a
producer (docs/plans/model-hub-engine-channel.md).

The finalizer does not take a receipt on trust. ``verify_receipt`` re-reads
the producing run from GitHub: the workflow file, the default-branch commit,
the attempt, and every job's conclusion. It then requires the receipt to name
the reviewed candidate, the harness as it is reviewed now, the Avibe wheels as
GitHub serves them now, and every baseline the index lists now.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from xml.etree import ElementTree

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core import model_hub_engine_channel as channel  # noqa: E402
from core.model_hub_engine_manifest import (  # noqa: E402
    EXPECTED_PLATFORMS,
    OWNED_RELEASE_ROOT,
    SHA256_RE,
    EngineManifestError,
    ReleaseSpec,
    parse_release_manifest,
)

REPOSITORY = channel.CHANNEL_REPOSITORY
DEFAULT_BRANCH = "master"
CANDIDATES_DIR = REPO_ROOT / "patches" / "model-hub-engine-candidates"
CANDIDATE_FILE = "candidate.json"
MANIFEST_FILE = "model-hub-engine-manifest.json"
PACKAGED_MANIFEST = "vibe/model_hub_runtime/cliproxyapi_manifest.json"
WHEEL_SERVICE_ENTRY = "vibe/service_main.py"
QUALIFICATION_WORKFLOW_PATH = ".github/workflows/model-hub-engine-qualification.yml"
MIRROR_RELEASE_ROOT = "https://dl.avibe.bot/releases"
RECEIPT_ARTIFACT = "model-hub-engine-qualification-receipt"
ASSETS_ARTIFACT = "model-hub-engine-candidate-assets"
AVIBE_ROLES = ("minimum", "current_stable")
E2E_SELECTION = ["-m", "e2e_model_hub", "tests/e2e"]
#: What a released-Avibe run records about itself: the commit its tests came
#: from, the digest of that tests tree, the digest of its resolved dependency
#: set, and its toolchain.
E2E_PROVENANCE_FIELDS = ("tests_commit", "tests_sha256", "dependencies_sha256", "python", "node", "agent_clis")
RELEASED_E2E_ACTION = ".github/actions/model-hub-released-e2e/action.yml"
ORIGINS_MODULE = "scripts/model_hub_engine_qualification_origins.py"
EVIDENCE_ARTIFACT_PREFIX = "qualification-evidence-"


@dataclass(frozen=True)
class EvidenceProducer:
    """The one job that may supply an evidence class, and the artifact it uploads."""

    #: The job, by the name GitHub reports.
    job: str
    artifact: str
    evidence_class: str
    #: The field that tells one document of this class from another: the
    #: platform, the role, or the baseline. ``None`` when the class has one document.
    subject: str | None = None
    #: The one value of that field this job writes. ``None`` when the job
    #: writes one document for each value, as hosted-wire does per baseline.
    value: str | None = None

    @property
    def single(self) -> bool:
        return self.subject is None or self.value is not None

    def produces(self, document: Mapping[str, Any]) -> bool:
        """Whether ``document`` is of this job's class and about what this job is about."""

        if document.get("class") != self.evidence_class:
            return False
        if self.subject is None:
            return True
        about = document.get(self.subject)
        return isinstance(about, str) and bool(about) and (self.value is None or about == self.value)


#: Who may say what. ``backward_state`` and ``lifecycle`` are absent: no job
#: produces them yet, so no document of either class is accepted from anywhere.
EVIDENCE_PRODUCERS: tuple[EvidenceProducer, ...] = (
    EvidenceProducer("build", f"{EVIDENCE_ARTIFACT_PREFIX}build", "build"),
    *(
        EvidenceProducer(
            f"platform-smoke ({platform})",
            f"{EVIDENCE_ARTIFACT_PREFIX}platform-smoke-{platform}",
            "platform_smoke",
            "platform",
            platform,
        )
        for platform in sorted(EXPECTED_PLATFORMS)
    ),
    *(
        EvidenceProducer(
            f"released-avibe-e2e ({role})",
            f"{EVIDENCE_ARTIFACT_PREFIX}released-avibe-e2e-{role}",
            "released_avibe_e2e",
            "role",
            role,
        )
        for role in AVIBE_ROLES
    ),
    EvidenceProducer("hosted-wire", f"{EVIDENCE_ARTIFACT_PREFIX}hosted-wire", "hosted_wire", "baseline_tag"),
)
#: Every job of the qualification workflow, by the name GitHub reports. All
#: must have succeeded in the receipt's run attempt; none may be skipped.
REQUIRED_JOBS = (*(producer.job for producer in EVIDENCE_PRODUCERS), "receipt")
#: The code that decides whether a candidate qualifies. A receipt binds the
#: digest of these files, so evidence from an older harness does not qualify
#: under a newer one. Everything else a run used is bound by the commit and the
#: release tag the receipt records.
HARNESS_FILES = (
    QUALIFICATION_WORKFLOW_PATH,
    RELEASED_E2E_ACTION,
    "core/model_hub_engine_channel.py",
    "core/model_hub_engine_manifest.py",
    "scripts/build_model_hub_engine.py",
    "scripts/ci_avault_verifier.py",
    "scripts/ci_loopback_only.sh",
    "scripts/model_hub_engine_channel.py",
    "scripts/model_hub_engine_qualification.py",
    "scripts/model_hub_engine_qualification_origins.py",
    "scripts/model_hub_engine_release_guard.py",
    "scripts/model_hub_hosted_wire_gate.py",
    "scripts/release_asset_download.py",
    "tests/e2e/test_model_hub_engine_hosted_passthrough.py",
)
#: Packages a released wheel provides. A harness directory must hold none of
#: them, so a test that imports one gets the wheel's, not a checkout's.
PRODUCT_PACKAGES = ("vibe", "core", "config", "modules", "storage")

_STABLE_TAG_RE = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")


class QualificationError(RuntimeError):
    """Raised when a candidate, its evidence, or its receipt cannot be accepted."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> Any:
    try:
        return channel.strict_json(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise QualificationError(f"{path.name} cannot be read: {exc}") from exc


# ----- the reviewed candidate record -----------------------------------------


@dataclass(frozen=True)
class Candidate:
    asset_release_tag: str
    min_avibe_version: str
    manifest_bytes: bytes
    manifest_sha256: str
    spec: ReleaseSpec
    #: Owner-reviewed reasons for a hosted passthrough that differs, by baseline tag.
    hosted_wire_changes: Mapping[str, str]

    def identity(self) -> dict[str, Any]:
        return {
            "asset_release_tag": self.asset_release_tag,
            "min_avibe_version": self.min_avibe_version,
            "manifest_sha256": self.manifest_sha256,
            "assets": [
                {
                    "platform": archive.platform,
                    "sha256": archive.sha256,
                    "size_bytes": archive.size,
                    "binary_sha256": archive.binary_sha256,
                }
                for archive in self.spec.archives
            ],
        }


def load_candidate(directory: Path) -> Candidate:
    """Read one reviewed candidate record; reject anything that is not exactly one."""

    if directory.is_symlink() or not directory.is_dir():
        raise QualificationError("the candidate record is not a directory")
    entries = sorted(directory.iterdir())
    if any(path.is_symlink() or not path.is_file() for path in entries) or {path.name for path in entries} != {
        CANDIDATE_FILE,
        MANIFEST_FILE,
    }:
        raise QualificationError(f"a candidate record holds exactly {CANDIDATE_FILE} and {MANIFEST_FILE}")
    record = _read_json(directory / CANDIDATE_FILE)
    fields = {"schema_version", "asset_release_tag", "min_avibe_version", "manifest_sha256"}
    if not isinstance(record, dict) or not fields <= set(record) <= fields | {"hosted_wire_changes"}:
        raise QualificationError(f"{CANDIDATE_FILE} has the wrong fields")
    if type(record["schema_version"]) is not int or record["schema_version"] != 1:
        raise QualificationError(f"{CANDIDATE_FILE} schema_version must be 1")
    try:
        minimum = channel.stable_version(record["min_avibe_version"], "min_avibe_version")
    except ValueError as exc:
        raise QualificationError(str(exc)) from exc
    if minimum < channel.FIRST_CHANNEL_AVIBE_VERSION:
        first = ".".join(str(part) for part in channel.FIRST_CHANNEL_AVIBE_VERSION)
        raise QualificationError(f"no Avibe before {first} reads the channel; the minimum cannot be lower")

    manifest_bytes = (directory / MANIFEST_FILE).read_bytes()
    if len(manifest_bytes) > channel.MAX_MANIFEST_BYTES:
        raise QualificationError("the candidate manifest is larger than the channel carries")
    digest = hashlib.sha256(manifest_bytes).hexdigest()
    if record["manifest_sha256"] != digest:
        raise QualificationError("manifest_sha256 does not match the candidate manifest bytes")
    try:
        spec = parse_release_manifest(manifest_bytes)
    except EngineManifestError as exc:
        raise QualificationError(f"the candidate manifest is invalid: {exc}") from exc
    if not (spec.asset_release_tag == record["asset_release_tag"] == directory.name):
        raise QualificationError("the record's directory, tag and manifest name different releases")

    changes = record.get("hosted_wire_changes", {})
    if not isinstance(changes, dict) or any(
        not isinstance(tag, str) or not isinstance(reason, str) or not reason.strip() for tag, reason in changes.items()
    ):
        raise QualificationError("hosted_wire_changes maps a baseline release to its reviewed reason")
    return Candidate(
        asset_release_tag=spec.asset_release_tag,
        min_avibe_version=record["min_avibe_version"],
        manifest_bytes=manifest_bytes,
        manifest_sha256=digest,
        spec=spec,
        hosted_wire_changes=dict(changes),
    )


def harness_digest(root: Path = REPO_ROOT) -> str:
    """One digest over the files that decide whether a candidate qualifies."""

    digest = hashlib.sha256()
    for name in sorted(HARNESS_FILES):
        path = root / name
        if path.is_symlink() or not path.is_file():
            raise QualificationError(f"the qualification harness is missing {name}")
        digest.update(f"{name}\0{_sha256(path)}\n".encode())
    return digest.hexdigest()


# ----- released Avibe artifacts ----------------------------------------------


@dataclass(frozen=True)
class ReleasedAvibe:
    version: str
    tag: str
    wheel_name: str
    wheel_url: str
    wheel_sha256: str
    wheel_size: int

    def identity(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "tag": self.tag,
            "wheel": {"name": self.wheel_name, "sha256": self.wheel_sha256, "size_bytes": self.wheel_size},
        }


def current_stable_avibe(api: Callable[[str], Any]) -> str:
    """The highest published stable ``vX.Y.Z`` Avibe release."""

    releases = api(f"/repos/{REPOSITORY}/releases?per_page=100")
    versions = []
    for release in releases if isinstance(releases, list) else []:
        if not isinstance(release, dict) or release.get("draft") is not False or release.get("prerelease") is not False:
            continue
        match = _STABLE_TAG_RE.fullmatch(str(release.get("tag_name")))
        if match is not None:
            versions.append(tuple(int(part) for part in match.groups()))
    if not versions:
        raise QualificationError("GitHub lists no published stable Avibe release")
    return ".".join(str(part) for part in max(versions))


def resolve_released_avibe(version: str, api: Callable[[str], Any]) -> ReleasedAvibe:
    """Name the immutable wheel of a published stable Avibe release.

    A draft, a pre-release, a missing wheel, or a wheel GitHub gives no digest
    for is not a released artifact, and nothing else stands in for one.
    """

    try:
        channel.stable_version(version)
    except ValueError as exc:
        raise QualificationError(str(exc)) from exc
    tag = f"v{version}"
    release = api(f"/repos/{REPOSITORY}/releases/tags/{tag}")
    if not isinstance(release, dict) or release.get("tag_name") != tag:
        raise QualificationError(f"Avibe {version} has no published release")
    if release.get("draft") is not False or release.get("prerelease") is not False:
        raise QualificationError(f"Avibe {version} is not a published stable release")
    name = f"avibe_os-{version}-py3-none-any.whl"
    assets = [asset for asset in release.get("assets") or [] if isinstance(asset, dict) and asset.get("name") == name]
    if len(assets) != 1:
        raise QualificationError(f"the Avibe {version} release has no single {name}")
    asset = assets[0]
    digest, size, url = str(asset.get("digest") or ""), asset.get("size"), asset.get("browser_download_url")
    if not digest.startswith("sha256:") or SHA256_RE.fullmatch(digest.removeprefix("sha256:")) is None:
        raise QualificationError(f"GitHub records no SHA-256 for {name}")
    if type(size) is not int or size <= 0 or url != f"{OWNED_RELEASE_ROOT}/{tag}/{name}":
        raise QualificationError(f"{name} is not an asset of the Avibe-owned release")
    return ReleasedAvibe(
        version=version, tag=tag, wheel_name=name, wheel_url=url,
        wheel_sha256=digest.removeprefix("sha256:"), wheel_size=size,
    )


def bundled_engine_manifest(wheel: Path) -> bytes:
    """The engine manifest a released wheel packages: one qualification baseline."""

    try:
        with zipfile.ZipFile(wheel) as archive:
            return archive.read(PACKAGED_MANIFEST)
    except (OSError, KeyError, zipfile.BadZipFile) as exc:
        raise QualificationError(f"{wheel.name} packages no engine manifest") from exc


def prepare_harness(source: Path, wheel: Path, output: Path) -> None:
    """Lay out a released tag's tests where no product package can be imported from.

    Only the tests, the scripts they call and the pytest configuration are
    copied from the tag. The E2E harness starts the service as ``main.py`` in
    its working directory; that file is taken from the wheel, where it ships as
    ``vibe/service_main.py``. With no product package beside the tests,
    ``import vibe`` resolves to the installed wheel or fails.
    """

    import shutil

    if output.exists():
        raise QualificationError("the harness directory already exists")
    output.mkdir(parents=True)
    for name in ("tests", "scripts"):
        if not (source / name).is_dir():
            raise QualificationError(f"the released source has no {name} directory")
        shutil.copytree(source / name, output / name, symlinks=False)
    shutil.copy2(source / "pyproject.toml", output / "pyproject.toml")
    try:
        with zipfile.ZipFile(wheel) as archive:
            (output / "main.py").write_bytes(archive.read(WHEEL_SERVICE_ENTRY))
    except (OSError, KeyError, zipfile.BadZipFile) as exc:
        raise QualificationError(f"{wheel.name} ships no service entry script") from exc
    present = [name for name in PRODUCT_PACKAGES if (output / name).exists()]
    if present:
        raise QualificationError(f"the harness directory shadows product packages: {present}")


def tree_digest(root: Path) -> str:
    """One digest over every file under ``root``, by relative path and content."""

    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file() and "__pycache__" not in item.parts):
        digest.update(f"{path.relative_to(root).as_posix()}\0{_sha256(path)}\n".encode())
    return digest.hexdigest()


def install_origin_recorder(environment: Path, directory: Path) -> None:
    """Make every Python process in ``environment`` log where it imports Avibe from."""

    try:
        from scripts import model_hub_engine_qualification_origins as recorder
    except ModuleNotFoundError as exc:
        if exc.name != "scripts":
            raise
        import model_hub_engine_qualification_origins as recorder

    site_packages = sorted(environment.glob("lib/python*/site-packages"))
    if len(site_packages) != 1:
        raise QualificationError("the environment has no single site-packages directory")
    target = site_packages[0]
    (target / f"{recorder.MODULE_NAME}.py").write_bytes((REPO_ROOT / ORIGINS_MODULE).read_bytes())
    (target / recorder.CONFIG_NAME).write_text(json.dumps({"directory": str(directory.resolve())}), encoding="utf-8")
    (target / f"{recorder.MODULE_NAME}.pth").write_text(f"import {recorder.MODULE_NAME}\n", encoding="utf-8")
    directory.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class ProcessOrigins:
    """What one Python process reported: how it was started and what it imported."""

    command: tuple[str, ...]
    cwd: str
    prefix: str
    distribution_version: str | None
    #: Every origin recorded for each product module; more than one when it was found again elsewhere.
    modules: Mapping[str, tuple[Any, ...]]

    @property
    def script(self) -> Path | None:
        """The script this interpreter was started on, if it was started on one."""

        for argument in self.command[1:]:
            if argument in {"-c", "-m"}:
                return None
            if not argument.startswith("-"):
                return (Path(self.cwd) / argument).resolve()
        return None

    @property
    def is_test_runner(self) -> bool:
        if self.script is not None:
            return self.script.name == "pytest"
        return any(
            flag == "-m" and module == "pytest" for flag, module in zip(self.command[1:], self.command[2:])
        )

    @property
    def label(self) -> str:
        return " ".join(self.command[1:4]) or "a process"


def read_origin_logs(directory: Path) -> list[ProcessOrigins]:
    """Parse the per-process logs the origin recorder wrote.

    An unreadable log is an error. So is a log its process marked incomplete:
    what such a log holds is a prefix of what the process imported, not all of it.
    """

    incomplete = sorted(path.name for path in directory.glob("*.incomplete"))
    if incomplete:
        raise QualificationError(
            f"an import log is incomplete: {len(incomplete)} processes could not record every import, "
            f"such as {incomplete[0]}"
        )
    processes: list[ProcessOrigins] = []
    for log in sorted(directory.glob("*.jsonl")):
        header: dict[str, Any] | None = None
        modules: dict[str, tuple[Any, ...]] = {}
        try:
            for line in log.read_text(encoding="utf-8").splitlines():
                record = json.loads(line)
                if "process" in record:
                    # One log is one process. A second record would have its
                    # imports judged by the first one's environment.
                    if header is not None:
                        raise ValueError("more than one process record")
                    header = record["process"]
                elif header is None:
                    raise ValueError("no process record ahead of the imports")
                elif record["origin"] not in modules.setdefault(record["module"], ()):
                    modules[record["module"]] += (record["origin"],)
            if header is None:
                raise ValueError("no process record")
            processes.append(
                ProcessOrigins(
                    command=tuple(str(item) for item in header["command"]),
                    cwd=str(header["cwd"]),
                    prefix=str(header["prefix"]),
                    distribution_version=header["distribution_version"],
                    modules=modules,
                )
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise QualificationError(f"an import log cannot be read: {log.name}: {exc}") from exc
    return processes


def origin_problems(
    processes: Sequence[ProcessOrigins], *, environment: Path, version: str, harness: Path
) -> list[str]:
    """Why a run did not test the released wheel.

    Every product module any process imported must have been loaded from the
    wheel's environment. The test runner and the spawned service must each
    have reported, and the service must be the entry script in the harness,
    which is the wheel's own.
    """

    roots = {environment.absolute(), environment.resolve()}

    def inside(origin: Any) -> bool:
        origins = origin if isinstance(origin, list) else [origin]
        return bool(origins) and all(
            isinstance(item, str) and not roots.isdisjoint(Path(item).resolve().parents) for item in origins
        )

    problems: list[str] = []
    for process in processes:
        outside = sorted(
            ((name, origin) for name, origins in process.modules.items() for origin in origins if not inside(origin)),
            key=lambda found: found[0],
        )
        if outside:
            problems.append(
                f"{process.label} imported {len({name for name, _origin in outside})} product modules from outside "
                f"the released wheel's environment, such as {outside[0][0]} from {outside[0][1]!r}"
            )
        if process.modules and process.distribution_version != version:
            problems.append(f"{process.label} ran Avibe {process.distribution_version!r}, not {version}")
        # ``sys.prefix`` names the environment; the interpreter in it is a link to a base one elsewhere.
        if process.modules and Path(process.prefix).resolve() not in roots and Path(process.prefix) not in roots:
            problems.append(f"{process.label} ran in {process.prefix}, not the released wheel's environment")

    def covered(selected: list[ProcessOrigins], packages: Sequence[str]) -> bool:
        return any(all(name in process.modules for name in packages) for process in selected)

    if not covered([process for process in processes if process.is_test_runner], ("vibe",)):
        problems.append("the test runner reported no import of Avibe")
    entry = (harness / "main.py").resolve()
    if not covered([process for process in processes if process.script == entry], ("core", "vibe")):
        problems.append("no spawned service reported importing Avibe from the wheel's entry script")
    return problems


def origin_summary(processes: Sequence[ProcessOrigins], harness: Path) -> dict[str, int]:
    entry = (harness / "main.py").resolve()
    return {
        "processes": len(processes),
        "services": sum(1 for process in processes if process.script == entry),
        "product_modules": sum(len(process.modules) for process in processes),
    }


def junit_summary(report: Path) -> dict[str, int]:
    """Count a JUnit report. Strict ``xfail`` cases count as run, not as failures."""

    try:
        cases = list(ElementTree.parse(report).getroot().iter("testcase"))
    except (OSError, ElementTree.ParseError) as exc:
        raise QualificationError("the test run left no readable report") from exc
    failed = sum(1 for case in cases if case.find("failure") is not None or case.find("error") is not None)
    skipped = sum(
        1 for case in cases if (node := case.find("skipped")) is not None and node.get("type") != "pytest.xfail"
    )
    return {"tests": len(cases), "failed": failed, "skipped": skipped}


# ----- evidence and receipts -------------------------------------------------


def evidence_problems(
    candidate: Candidate,
    evidence: Sequence[Any],
    *,
    baselines: Mapping[str, str],
    avibe: Mapping[str, ReleasedAvibe],
) -> list[str]:
    """Everything that keeps ``evidence`` from qualifying ``candidate``.

    ``baselines`` maps each engine release the candidate must be compared with
    to its manifest digest. ``avibe`` maps each role to the released Avibe it
    must have been driven through.
    """

    by_class: dict[str, list[dict[str, Any]]] = {}
    problems: list[str] = []
    counted: set[tuple[str, Any]] = set()
    for item in evidence:
        kind = item.get("class") if isinstance(item, dict) else None
        if not isinstance(kind, str):
            problems.append("evidence: a document names no class")
            continue
        # A document counts only under the job that produces its class, and
        # once for what it is about. What it is about is the one field its
        # class is told apart by, never whichever field happens to be there.
        if not any(producer.evidence_class == kind for producer in EVIDENCE_PRODUCERS):
            problems.append(f"{kind}: no qualification job produces this evidence")
            continue
        producer = next(
            (
                producer
                for producer in EVIDENCE_PRODUCERS
                if producer.produces(item) and producer.job == item.get("producer")
            ),
            None,
        )
        if producer is None:
            problems.append(f"{kind}: a document is not recorded under the job that produces this evidence")
            continue
        about = item.get(producer.subject) if producer.subject is not None else None
        if (kind, about) in counted:
            problems.append(f"{kind}: more than one document about {about or 'the candidate'}")
            continue
        counted.add((kind, about))
        by_class.setdefault(kind, []).append(item)

    def bound(item: Mapping[str, Any]) -> bool:
        return item.get("manifest_sha256") == candidate.manifest_sha256

    builds = by_class.get("build", [])
    if len(builds) != 1 or not bound(builds[0]) or builds[0].get("reproduced") is not True or (
        builds[0].get("assets") != candidate.identity()["assets"]
    ):
        problems.append("build: no rebuild reproduced the reviewed manifest and its six assets")

    smokes = {item.get("platform"): item for item in by_class.get("platform_smoke", [])}
    for archive in candidate.spec.archives:
        item = smokes.get(archive.platform)
        if (
            item is None
            or not bound(item)
            or item.get("archive_sha256") != archive.sha256
            or item.get("binary_sha256") != archive.binary_sha256
            or item.get("reported_version") != candidate.spec.version
            or item.get("health") != "ok"
        ):
            problems.append(f"platform_smoke: {archive.platform} did not start and pass health from the reviewed archive")

    compared = {tag: digest for tag, digest in baselines.items() if tag != candidate.asset_release_tag}
    wires = {item.get("baseline_tag"): item for item in by_class.get("hosted_wire", [])}
    states = {item.get("baseline_tag"): item for item in by_class.get("backward_state", [])}
    for tag, digest in sorted(compared.items()):
        wire = wires.get(tag)
        if wire is None or not bound(wire) or wire.get("baseline_manifest_sha256") != digest or not wire.get("cases"):
            problems.append(f"hosted_wire: the candidate was not compared with {tag}")
        elif wire.get("differences") and tag not in candidate.hosted_wire_changes:
            problems.append(f"hosted_wire: the passthrough differs from {tag} and the record gives no reviewed reason")
        state = states.get(tag)
        if (
            state is None
            or not bound(state)
            or state.get("baseline_manifest_sha256") != digest
            or state.get("result") != "pass"
        ):
            problems.append(f"backward_state: state written by the candidate was not read back by {tag}")

    runs = {item.get("role"): item for item in by_class.get("released_avibe_e2e", [])}
    for role in AVIBE_ROLES:
        released, item = avibe.get(role), runs.get(role)
        if released is None:
            problems.append(f"released_avibe_e2e: the {role} Avibe release is unknown")
            continue
        junit = item.get("junit") if item is not None else None
        provenance = item.get("provenance") if item is not None else None
        if (
            item is None
            or not bound(item)
            or not isinstance(provenance, dict)
            or any(not isinstance(provenance.get(field), str) or not provenance[field] for field in E2E_PROVENANCE_FIELDS)
            or item.get("avibe") != released.identity()
            or item.get("imports_from_wheel") is not True
            or item.get("selection") != E2E_SELECTION
            or item.get("scenario_evidence") != "pass"
            or not isinstance(junit, dict)
            or not junit.get("tests")
            or junit.get("failed") != 0
        ):
            problems.append(
                f"released_avibe_e2e: the Model Hub E2E did not pass through the released Avibe {released.version} wheel"
            )
    if role_versions := {role: avibe[role].version for role in AVIBE_ROLES if role in avibe}:
        if role_versions.get("minimum") != candidate.min_avibe_version:
            problems.append("released_avibe_e2e: the minimum role is not the candidate's minimum Avibe")

    lifecycles = by_class.get("lifecycle", [])
    if len(lifecycles) != 1 or not bound(lifecycles[0]) or lifecycles[0].get("result") != "pass":
        problems.append("lifecycle: activation, rollback and recovery of the candidate were not exercised")
    return problems


def build_receipt(
    candidate: Candidate,
    evidence: Sequence[Any],
    *,
    baselines: Mapping[str, str],
    avibe: Mapping[str, ReleasedAvibe],
    run_id: int,
    run_attempt: int,
    head_sha: str,
    harness_sha256: str,
) -> dict[str, Any]:
    problems = evidence_problems(candidate, evidence, baselines=baselines, avibe=avibe)
    if problems:
        raise QualificationError("the candidate is not qualified: " + "; ".join(problems))
    return {
        "schema_version": 1,
        "repository": REPOSITORY,
        "workflow_path": QUALIFICATION_WORKFLOW_PATH,
        "run_id": run_id,
        "run_attempt": run_attempt,
        "head_sha": head_sha,
        "harness_sha256": harness_sha256,
        "candidate": candidate.identity(),
        "baselines": dict(sorted(baselines.items())),
        "avibe": {role: avibe[role].identity() for role in AVIBE_ROLES},
        "evidence": list(evidence),
    }


def run_identity_problems(receipt: Mapping[str, Any], *, api: Callable[[str], Any]) -> list[str]:
    """Why GitHub does not confirm that the receipt's run produced a qualification.

    Everything here is read from GitHub by run id. A receipt's own claims
    about its run are only compared with that, never believed.
    """

    run_id, attempt = receipt.get("run_id"), receipt.get("run_attempt")
    if type(run_id) is not int or type(attempt) is not int:
        return ["the receipt names no run"]
    run = api(f"/repos/{REPOSITORY}/actions/runs/{run_id}")
    if not isinstance(run, dict):
        return ["GitHub has no such qualification run"]
    head_sha = run.get("head_sha")
    expected = {
        "id": run_id,
        "run_attempt": attempt,
        "path": QUALIFICATION_WORKFLOW_PATH,
        "event": "workflow_dispatch",
        "head_branch": DEFAULT_BRANCH,
        "status": "completed",
        "conclusion": "success",
        "head_sha": receipt.get("head_sha"),
    }
    problems = [
        f"the run's {field} is {run.get(field)!r}, not {value!r}" for field, value in expected.items() if run.get(field) != value
    ]
    for field in ("repository", "head_repository"):
        if (run.get(field) or {}).get("full_name") != REPOSITORY:
            problems.append(f"the run's {field} is not {REPOSITORY}")
    if not isinstance(head_sha, str) or _COMMIT_RE.fullmatch(head_sha) is None:
        return problems + ["the run names no commit"]

    # A branch or tag may be named like the default branch; the commit decides.
    comparison = api(f"/repos/{REPOSITORY}/compare/{head_sha}...{DEFAULT_BRANCH}")
    if not isinstance(comparison, dict) or comparison.get("status") not in {"ahead", "identical"}:
        problems.append(f"the run's commit is not on {DEFAULT_BRANCH}")

    listing = api(f"/repos/{REPOSITORY}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100")
    jobs = listing.get("jobs") if isinstance(listing, dict) else None
    if not isinstance(jobs, list) or not isinstance(listing.get("total_count"), int) or listing["total_count"] != len(jobs):
        return problems + ["the run's jobs cannot be read completely"]
    conclusions = {job.get("name"): job.get("conclusion") for job in jobs if isinstance(job, dict)}
    for name in REQUIRED_JOBS:
        if conclusions.get(name) != "success":
            problems.append(f"job {name!r} concluded {conclusions.get(name)!r}, not 'success'")
    for name, conclusion in sorted((str(name), conclusion) for name, conclusion in conclusions.items()):
        if name not in REQUIRED_JOBS and conclusion != "success":
            problems.append(f"job {name!r} concluded {conclusion!r}")
    return problems


def verify_receipt(
    receipt: Any,
    *,
    candidate: Candidate,
    api: Callable[[str], Any],
    harness_sha256: str,
    baselines: Mapping[str, str],
) -> None:
    """Stop unless ``receipt`` qualifies ``candidate`` for promotion right now.

    ``baselines`` is what the index lists now, plus what the Avibe releases
    bundle. The Avibe releases themselves are resolved again from GitHub, so a
    receipt made against a wheel that has since changed, or before a newer
    stable release, no longer qualifies.
    """

    if not isinstance(receipt, dict) or receipt.get("schema_version") != 1:
        raise QualificationError("the receipt has an unknown shape")
    problems = run_identity_problems(receipt, api=api)
    if receipt.get("repository") != REPOSITORY or receipt.get("workflow_path") != QUALIFICATION_WORKFLOW_PATH:
        problems.append("the receipt is for another repository or workflow")
    if receipt.get("candidate") != candidate.identity():
        problems.append("the receipt is for other candidate bytes than the reviewed record")
    if receipt.get("harness_sha256") != harness_sha256:
        problems.append("the receipt was produced by a different qualification harness than the reviewed one")
    avibe = {
        "minimum": resolve_released_avibe(candidate.min_avibe_version, api),
        "current_stable": resolve_released_avibe(current_stable_avibe(api), api),
    }
    evidence = receipt.get("evidence")
    problems += evidence_problems(
        candidate, evidence if isinstance(evidence, list) else [], baselines=baselines, avibe=avibe
    )
    if problems:
        raise QualificationError("the receipt does not qualify the candidate: " + "; ".join(problems))


# ----- evidence producers ----------------------------------------------------


def _guard():
    try:
        from scripts import model_hub_engine_release_guard as guard
    except ModuleNotFoundError as exc:
        if exc.name != "scripts":
            raise
        import model_hub_engine_release_guard as guard
    return guard


def verified_assets(candidate_dir: Path, asset_dir: Path) -> Candidate:
    """Require ``asset_dir`` to be exactly the candidate's publishable asset set."""

    candidate = load_candidate(candidate_dir)
    guard = _guard()
    try:
        guard.verify_release_assets(candidate_dir / MANIFEST_FILE, asset_dir)
    except guard.ReleaseGuardError as exc:
        raise QualificationError(f"the assets are not the reviewed candidate: {exc}") from exc
    return candidate


def require_published(candidate_dir: Path, work: Path) -> None:
    """Require the release and its mirror to serve exactly the candidate's asset set.

    Clients try the mirror first and fall back to the release, checking the
    same pinned digests either way, so both must hold every byte before an
    index may name the release.
    """

    candidate = load_candidate(candidate_dir)
    guard = _guard()
    manifest = candidate_dir / MANIFEST_FILE
    sizes = {archive.name: archive.size for archive in candidate.spec.archives}
    try:
        guard.fetch_release_assets(manifest, work / "release")
        mirror = work / "mirror"
        mirror.mkdir(parents=True)
        for name in sorted(candidate.spec.expected_asset_names):
            guard._download(
                f"{MIRROR_RELEASE_ROOT}/{candidate.asset_release_tag}/{name}",
                mirror / name,
                max_bytes=sizes.get(name, channel.MAX_MANIFEST_BYTES),
            )
        guard.verify_release_assets(manifest, mirror)
    except guard.ReleaseGuardError as exc:
        raise QualificationError(f"the release is not fully published and mirrored: {exc}") from exc


def build_evidence(candidate_dir: Path, asset_dir: Path) -> dict[str, Any]:
    candidate = verified_assets(candidate_dir, asset_dir)
    return {
        "class": "build",
        "manifest_sha256": candidate.manifest_sha256,
        "assets": candidate.identity()["assets"],
        "reproduced": True,
    }


def write_local_manifest(manifest_path: Path, asset_dir: Path, platform: str, output: Path) -> ReleaseSpec:
    """Write a copy of a manifest whose ``platform`` asset is read from ``asset_dir``.

    The directory must be exactly the manifest's publishable asset set, so the
    local file is the pinned archive. Only the URL changes; the size and
    digests the installer checks stay the manifest's.
    """

    guard = _guard()
    try:
        spec = guard.verify_release_assets(manifest_path, asset_dir)
    except guard.ReleaseGuardError as exc:
        raise QualificationError(f"the assets are not the manifest's: {exc}") from exc
    archive = spec.archive_for(platform)
    if archive is None:
        raise QualificationError(f"the manifest has no {platform} asset")
    manifest = json.loads(spec.manifest_bytes)
    for asset in manifest["assets"]:
        if asset["platform"] == platform:
            asset["url"] = (asset_dir.resolve() / archive.name).as_uri()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest), encoding="utf-8")
    return spec


def platform_smoke_evidence(candidate_dir: Path, asset_dir: Path, work: Path) -> dict[str, Any]:
    """Install this host's archive from disk, start it, and require Avibe's health check.

    The engine runs from an isolated runtime and state directory on loopback,
    with no source configured and no credential. Installing verifies the
    archive and binary digests and that the binary reports the manifest's
    version; the supervisor's start is the health check Avibe applies.
    """

    candidate = verified_assets(candidate_dir, asset_dir)
    from vibe.model_hub_runtime.installer import EngineRuntimeManager
    from vibe.model_hub_runtime.state import EngineStateStore
    from vibe.model_hub_runtime.supervisor import EngineSupervisor

    work.mkdir(parents=True, exist_ok=True)
    probe = EngineRuntimeManager(runtime_dir=work / "probe", offline=True)
    archive = candidate.spec.archive_for(probe.host_platform())
    if archive is None:
        raise QualificationError("the candidate has no asset for this host")
    # The one archive this host installs, read from the verified directory.
    local_manifest = work / "local-manifest.json"
    write_local_manifest(candidate_dir / MANIFEST_FILE, asset_dir, archive.platform, local_manifest)

    manager = EngineRuntimeManager(runtime_dir=work / "engine", manifest_path=local_manifest, offline=False)
    installed = manager.ensure()
    if not installed.get("ok") or installed.get("platform") != archive.platform:
        raise QualificationError(f"the candidate did not install: {installed.get('reason')}")
    supervisor = EngineSupervisor(installer=manager, state_store=EngineStateStore(work / "state"))
    try:
        supervisor.ensure_running()
    except Exception as exc:  # noqa: BLE001 - any start failure is a failed smoke
        raise QualificationError(f"the candidate did not start and pass health: {exc}") from exc
    finally:
        supervisor.stop()
    return {
        "class": "platform_smoke",
        "manifest_sha256": candidate.manifest_sha256,
        "platform": archive.platform,
        "archive_sha256": archive.sha256,
        "binary_sha256": archive.binary_sha256,
        "reported_version": installed.get("version"),
        "health": "ok",
    }


def _require_provenance(provenance: Any) -> dict[str, str]:
    if not isinstance(provenance, dict) or any(
        not isinstance(provenance.get(field), str) or not provenance[field] for field in E2E_PROVENANCE_FIELDS
    ):
        raise QualificationError(f"the run must record {', '.join(E2E_PROVENANCE_FIELDS)}")
    return {field: provenance[field] for field in E2E_PROVENANCE_FIELDS}


def _run_facts(
    *, version: str, wheel: Path, wheel_sha256: str, environment: Path, harness: Path, origins_dir: Path,
    provenance: Any,
) -> tuple[dict[str, str], list[ProcessOrigins], list[str]]:
    """What a released-wheel run was made of, and why it does not count if it does not."""

    recorded = _require_provenance(provenance)
    problems = []
    if not wheel.is_file() or _sha256(wheel) != wheel_sha256:
        problems.append(f"{wheel.name} is not the published wheel")
    try:
        with zipfile.ZipFile(wheel) as archive:
            entry = archive.read(WHEEL_SERVICE_ENTRY)
    except (OSError, KeyError, zipfile.BadZipFile):
        entry = None
    if entry is None or not (harness / "main.py").is_file() or (harness / "main.py").read_bytes() != entry:
        problems.append("the harness does not start the service from the wheel's own entry script")
    if recorded["tests_sha256"] != tree_digest(harness / "tests"):
        problems.append("the tests that ran are not the tests that were recorded")
    processes = read_origin_logs(origins_dir)
    problems += origin_problems(processes, environment=environment, version=version, harness=harness)
    return recorded, processes, problems


def released_e2e_evidence(
    candidate: Candidate,
    *,
    role: str,
    released: ReleasedAvibe,
    wheel: Path,
    environment: Path,
    harness: Path,
    origins_dir: Path,
    junit: Path,
    scenario_evidence_passed: bool,
    provenance: Any,
) -> dict[str, Any]:
    if role not in AVIBE_ROLES:
        raise QualificationError(f"unknown Avibe role {role}")
    if not wheel.is_file() or wheel.stat().st_size != released.wheel_size:
        raise QualificationError(f"{wheel.name} is not the wheel GitHub published for Avibe {released.version}")
    recorded, processes, problems = _run_facts(
        version=released.version, wheel=wheel, wheel_sha256=released.wheel_sha256, environment=environment,
        harness=harness, origins_dir=origins_dir, provenance=provenance,
    )
    if problems:
        raise QualificationError("the run did not test the released wheel: " + "; ".join(problems))
    return {
        "class": "released_avibe_e2e",
        "manifest_sha256": candidate.manifest_sha256,
        "role": role,
        "avibe": released.identity(),
        "imports_from_wheel": True,
        "origins": origin_summary(processes, harness),
        "provenance": recorded,
        "selection": E2E_SELECTION,
        "junit": junit_summary(junit),
        "scenario_evidence": "pass" if scenario_evidence_passed else "fail",
    }


def rehearsal_record(
    *,
    tag: str,
    tag_commit: str,
    version: str,
    wheel: Path,
    wheel_sha256: str,
    environment: Path,
    harness: Path,
    origins_dir: Path,
    junit: Path,
    provenance: Any,
) -> tuple[dict[str, Any], list[str]]:
    """Describe a rehearsal of the released-wheel harness, and what is wrong with it.

    A rehearsal exercises the harness against some published wheel, a release
    candidate included. Its record is not evidence: it has its own class, which
    no receipt reads, so nothing rehearsed can qualify an engine.
    """

    recorded, processes, problems = _run_facts(
        version=version, wheel=wheel, wheel_sha256=wheel_sha256, environment=environment, harness=harness,
        origins_dir=origins_dir, provenance=provenance,
    )
    summary = junit_summary(junit)
    record = {
        "class": "rehearsal",
        "tag": tag,
        "tag_commit": tag_commit,
        "wheel": {"name": wheel.name, "sha256": wheel_sha256},
        "harness_sha256": harness_digest(),
        "provenance": recorded,
        "origins": origin_summary(processes, harness),
        "selection": E2E_SELECTION,
        "junit": summary,
        "failing": junit_failures(junit),
        "harness_problems": problems,
    }
    return record, problems


def junit_failures(report: Path) -> list[str]:
    """The cases a JUnit report records as failed or errored."""

    try:
        cases = list(ElementTree.parse(report).getroot().iter("testcase"))
    except (OSError, ElementTree.ParseError) as exc:
        raise QualificationError("the test run left no readable report") from exc
    return sorted(
        f"{case.get('classname')}::{case.get('name')}"
        for case in cases
        if case.find("failure") is not None or case.find("error") is not None
    )


# ----- command line ----------------------------------------------------------


def _api(token: str | None) -> Callable[[str], Any]:
    try:
        from scripts.model_hub_engine_channel import ProducerError, github_api
    except ModuleNotFoundError as exc:
        if exc.name != "scripts":
            raise
        from model_hub_engine_channel import ProducerError, github_api

    def call(path: str) -> Any:
        try:
            return github_api(path, token=token)
        except ProducerError as exc:
            raise QualificationError(str(exc)) from exc

    return call


def _load_released(path: Path) -> ReleasedAvibe:
    record = _read_json(path)
    try:
        return ReleasedAvibe(**record)
    except TypeError as exc:
        raise QualificationError(f"{path.name} is not a released Avibe record") from exc


def collect_evidence(directory: Path) -> list[dict[str, Any]]:
    """Read each producing job's evidence from the artifact that job uploaded.

    ``directory`` holds one subdirectory per downloaded artifact, named after
    it. Anything that is not a producer's artifact, and any document that the
    artifact's job does not produce, stops the receipt: a run that holds
    evidence for a class with no job, or one job's evidence in another job's
    artifact, is not a run to believe. Each accepted document is recorded
    with the job its artifact is named for. The name is all that ties an
    artifact to a job; who uploaded it is not checked.
    """

    by_artifact = {producer.artifact: producer for producer in EVIDENCE_PRODUCERS}
    entries = sorted(directory.iterdir()) if directory.is_dir() else []
    evidence: list[dict[str, Any]] = []
    for entry in entries:
        producer = by_artifact.get(entry.name)
        if producer is None or entry.is_symlink() or not entry.is_dir():
            raise QualificationError(f"{entry.name} is not the evidence of any qualification job")
        files = sorted(entry.iterdir())
        if any(path.is_symlink() or not path.is_file() or path.suffix != ".json" for path in files):
            raise QualificationError(f"{producer.job} uploaded something that is not an evidence document")
        documents = [_read_json(path) for path in files]
        for document in documents:
            if not isinstance(document, dict) or "producer" in document or not producer.produces(document):
                raise QualificationError(f"{producer.job} uploaded evidence that it does not produce")
        if producer.single and len(documents) != 1:
            raise QualificationError(f"{producer.job} uploaded {len(documents)} evidence documents, not one")
        about = [document[producer.subject] for document in documents] if producer.subject is not None else []
        if len(set(about)) != len(about):
            raise QualificationError(f"{producer.job} uploaded more than one document about the same {producer.subject}")
        evidence += [{**document, "producer": producer.job} for document in documents]
    return evidence


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    def command(name: str, help_text: str, *, candidate: bool = True) -> argparse.ArgumentParser:
        sub = commands.add_parser(name, help=help_text)
        if candidate:
            sub.add_argument("--candidate-dir", type=Path, required=True)
        return sub

    command("candidate", "Validate a reviewed candidate record.")
    command("harness-digest", "Print the digest of the qualification harness.", candidate=False)

    build = command("evidence-build", "Record that a rebuild reproduced the reviewed assets.")
    build.add_argument("--asset-dir", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)

    smoke = command("evidence-smoke", "Start this host's archive and record its health.")
    smoke.add_argument("--asset-dir", type=Path, required=True)
    smoke.add_argument("--work-dir", type=Path, required=True)
    smoke.add_argument("--output", type=Path, required=True)

    released = command("released-avibe", "Resolve a released Avibe wheel for one role.")
    released.add_argument("--role", required=True, choices=AVIBE_ROLES)
    released.add_argument("--output", type=Path, required=True)

    bundled = command("bundled-manifest", "Extract the engine manifest a released wheel packages.", candidate=False)
    bundled.add_argument("--wheel", type=Path, required=True)
    bundled.add_argument("--output", type=Path, required=True)

    harness = command("prepare-harness", "Lay out a released tag's tests without product packages.", candidate=False)
    harness.add_argument("--source", type=Path, required=True)
    harness.add_argument("--wheel", type=Path, required=True)
    harness.add_argument("--output", type=Path, required=True)

    e2e = command("evidence-released-e2e", "Record an E2E run driven through a released wheel.")
    e2e.add_argument("--role", required=True, choices=AVIBE_ROLES)
    e2e.add_argument("--released", type=Path, required=True)
    e2e.add_argument("--wheel", type=Path, required=True)
    e2e.add_argument("--environment", type=Path, required=True)
    e2e.add_argument("--harness", type=Path, required=True)
    e2e.add_argument("--origins-dir", type=Path, required=True)
    e2e.add_argument("--junit", type=Path, required=True)
    e2e.add_argument("--provenance", type=Path, required=True)
    e2e.add_argument("--scenario-evidence", required=True, choices=["pass", "fail"])
    e2e.add_argument("--output", type=Path, required=True)

    recorder = command("install-origin-recorder", "Log every process's Avibe imports in an environment.", candidate=False)
    recorder.add_argument("--environment", type=Path, required=True)
    recorder.add_argument("--output-dir", type=Path, required=True)

    local = command("local-manifest", "Point one platform's asset of a manifest at a verified local file.", candidate=False)
    local.add_argument("--manifest", type=Path, required=True)
    local.add_argument("--asset-dir", type=Path, required=True)
    local.add_argument("--platform", default="linux-amd64")
    local.add_argument("--output", type=Path, required=True)

    digest = command("tree-digest", "Print the digest of a directory tree.", candidate=False)
    digest.add_argument("--root", type=Path, required=True)

    rehearsal = command("rehearsal-record", "Describe a harness rehearsal. It is not evidence.", candidate=False)
    rehearsal.add_argument("--tag", required=True)
    rehearsal.add_argument("--tag-commit", required=True)
    rehearsal.add_argument("--version", required=True)
    rehearsal.add_argument("--wheel", type=Path, required=True)
    rehearsal.add_argument("--wheel-sha256", required=True)
    rehearsal.add_argument("--environment", type=Path, required=True)
    rehearsal.add_argument("--harness", type=Path, required=True)
    rehearsal.add_argument("--origins-dir", type=Path, required=True)
    rehearsal.add_argument("--junit", type=Path, required=True)
    rehearsal.add_argument("--provenance", type=Path, required=True)
    rehearsal.add_argument("--require-pass", action="store_true", help="also fail when a test failed")
    rehearsal.add_argument("--output", type=Path, required=True)

    available = command("availability", "Require the release and its mirror to serve the candidate.")
    available.add_argument("--work-dir", type=Path, required=True)

    for name, help_text in (
        ("receipt", "Join a run's evidence into a receipt."),
        ("verify-receipt", "Check a receipt against GitHub and the reviewed record."),
    ):
        sub = command(name, help_text)
        sub.add_argument("--baselines", type=Path, required=True, help="JSON object: release tag to manifest SHA-256")
        if name == "receipt":
            sub.add_argument("--evidence-dir", type=Path, required=True)
            sub.add_argument("--released-dir", type=Path, required=True)
            sub.add_argument("--run-id", type=int, required=True)
            sub.add_argument("--run-attempt", type=int, required=True)
            sub.add_argument("--head-sha", required=True)
            sub.add_argument("--output", type=Path, required=True)
        else:
            sub.add_argument("--receipt", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    try:
        if args.command == "harness-digest":
            print(harness_digest())
            return 0
        if args.command == "bundled-manifest":
            args.output.write_bytes(bundled_engine_manifest(args.wheel))
            return 0
        if args.command == "prepare-harness":
            prepare_harness(args.source, args.wheel, args.output)
            return 0
        if args.command == "install-origin-recorder":
            install_origin_recorder(args.environment, args.output_dir)
            return 0
        if args.command == "tree-digest":
            print(tree_digest(args.root))
            return 0
        if args.command == "local-manifest":
            write_local_manifest(args.manifest, args.asset_dir, args.platform, args.output)
            return 0
        if args.command == "rehearsal-record":
            record, problems = rehearsal_record(
                tag=args.tag, tag_commit=args.tag_commit, version=args.version, wheel=args.wheel,
                wheel_sha256=args.wheel_sha256, environment=args.environment, harness=args.harness,
                origins_dir=args.origins_dir, junit=args.junit, provenance=_read_json(args.provenance),
            )
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            print(json.dumps({key: record[key] for key in ("class", "tag", "junit", "origins", "harness_problems")}))
            if problems:
                raise QualificationError("the rehearsal did not test the released wheel: " + "; ".join(problems))
            if args.require_pass and (record["junit"]["failed"] or not record["junit"]["tests"]):
                raise QualificationError(f"{record['junit']['failed']} rehearsed cases failed: {record['failing'][:10]}")
            return 0

        candidate = load_candidate(args.candidate_dir)
        result: Any = candidate.identity()
        if args.command == "evidence-build":
            result = build_evidence(args.candidate_dir, args.asset_dir)
        elif args.command == "availability":
            require_published(args.candidate_dir, args.work_dir)
        elif args.command == "evidence-smoke":
            result = platform_smoke_evidence(args.candidate_dir, args.asset_dir, args.work_dir)
        elif args.command == "released-avibe":
            api = _api(token)
            version = candidate.min_avibe_version if args.role == "minimum" else current_stable_avibe(api)
            result = resolve_released_avibe(version, api).__dict__
        elif args.command == "evidence-released-e2e":
            result = released_e2e_evidence(
                candidate,
                role=args.role,
                released=_load_released(args.released),
                wheel=args.wheel,
                environment=args.environment,
                harness=args.harness,
                origins_dir=args.origins_dir,
                junit=args.junit,
                scenario_evidence_passed=args.scenario_evidence == "pass",
                provenance=_read_json(args.provenance),
            )
        elif args.command in {"receipt", "verify-receipt"}:
            baselines = _read_json(args.baselines)
            if not isinstance(baselines, dict) or not all(
                isinstance(tag, str) and isinstance(digest, str) for tag, digest in baselines.items()
            ):
                raise QualificationError("baselines maps a release tag to a manifest SHA-256")
            if args.command == "receipt":
                result = build_receipt(
                    candidate,
                    collect_evidence(args.evidence_dir),
                    baselines=baselines,
                    avibe={role: _load_released(args.released_dir / f"{role}.json") for role in AVIBE_ROLES},
                    run_id=args.run_id,
                    run_attempt=args.run_attempt,
                    head_sha=args.head_sha,
                    harness_sha256=harness_digest(),
                )
            else:
                verify_receipt(
                    _read_json(args.receipt),
                    candidate=candidate,
                    api=_api(token),
                    harness_sha256=harness_digest(),
                    baselines=baselines,
                )
                result = {"ok": True, **candidate.identity()}
        if getattr(args, "output", None) is not None:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        else:
            print(json.dumps(result, sort_keys=True))
        return 0
    except QualificationError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
