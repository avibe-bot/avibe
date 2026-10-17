#!/usr/bin/env python3
"""Fail an engine change that alters how the engine passes hosted tools through.

Native hosted tools are assumed rather than probed, so a passthrough regression
in an engine release would otherwise reach users unnoticed. When a change
replaces the engine, the hosted passthrough cases run against the base and the
changed engine and are compared byte for byte. Any difference fails, unless the
pull request carries the label that says the change is intended. PR CI takes
both engines from their published releases. Before a candidate is published,
``--head-assets`` takes it from the local build output instead. See
docs/plans/model-hub-cpa-dependency.md.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import urllib.request
from xml.etree import ElementTree

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import model_hub_engine_release_guard as release_guard  # noqa: E402

MANIFEST = "vibe/model_hub_runtime/cliproxyapi_manifest.json"
CASES = "tests/e2e/test_model_hub_engine_hosted_passthrough.py"
# Runs a command where only loopback answers; Linux CI only (needs passwordless sudo).
LOOPBACK_ONLY = "scripts/ci_loopback_only.sh"
EVIDENCE_DIR_ENV = "AVIBE_HOSTED_WIRE_EVIDENCE_DIR"
CHANGE_LABEL = "engine-hosted-wire-change"
# What each case records, in the order a difference is reported.
WIRE_PARTS = (
    ("upstream", "path", "upstream request path"),
    ("upstream", "query", "upstream request query"),
    ("upstream", "headers", "upstream header set"),
    ("upstream", "body", "upstream request bytes"),
    ("downstream", "status", "downstream status"),
    ("downstream", "body", "downstream bytes"),
)


class GateError(RuntimeError):
    """Raised when the gate cannot reach a verdict."""


def wire_differences(base_cases: Mapping[str, dict], head_cases: Mapping[str, dict]) -> dict[str, list[str]]:
    """Map every case whose two recordings differ to the wire parts that differ."""
    differences = {}
    for case in sorted(base_cases.keys() | head_cases.keys()):
        if case not in base_cases or case not in head_cases:
            differences[case] = ["recorded against only one engine"]
            continue
        parts = [label for direction, part, label in WIRE_PARTS
                 if base_cases[case][direction][part] != head_cases[case][direction][part]]
        if parts:
            differences[case] = parts
    return differences


def wire_change_violation(differences: Mapping[str, list[str]], labels: frozenset[str] | None) -> str | None:
    """Return why a hosted passthrough difference blocks the change, or None.

    ``labels`` are the pull request's labels, or None when there is no pull
    request or its labels could not be read; both count as unlabeled.
    """
    if not differences or (labels is not None and CHANGE_LABEL in labels):
        return None
    summary = "; ".join(f"{case}: {', '.join(parts)}" for case, parts in differences.items())
    return (f"the engine changes the hosted passthrough ({summary}). If that is intended, label the pull "
            f"request {CHANGE_LABEL} and say in its body what changed and why")


def pull_request_labels(repository: str, number: str, token: str | None) -> frozenset[str] | None:
    """The pull request's labels as they are now, or None when they cannot be read."""
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "avibe-model-hub-hosted-wire-gate/1"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(f"https://api.github.com/repos/{repository}/pulls/{number}", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return frozenset(label["name"] for label in json.load(response)["labels"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"::warning::hosted_wire gate: cannot read the pull request's labels ({type(exc).__name__}); "
              "judging it as unlabeled")
        return None


def _git_show(revision: str, path: str) -> bytes:
    try:
        return subprocess.run(
            ["git", "-C", str(REPO_ROOT), "show", f"{revision}:{path}"],
            check=True, capture_output=True, timeout=60,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise GateError(f"cannot read {path} at the base revision {revision}") from exc


def _met_its_expectation(testcase: ElementTree.Element) -> bool:
    """A case passed, or failed as its strict ``xfail`` marker says it must; an unexpected pass fails."""
    if testcase.find("failure") is not None or testcase.find("error") is not None:
        return False
    skipped = testcase.find("skipped")
    return skipped is None or skipped.get("type") == "pytest.xfail"


def _run_cases(
    side: str, manifest_bytes: bytes, platform: str, work: Path, asset_dir: Path | None = None,
    *, must_pass: bool, loopback_only: bool = False,
) -> dict[str, dict]:
    """Run the case set against one side's engine; return each case's recorded wire.

    Each case records the exchange before asserting on it. The PR engine must pass
    every case. The base engine only has to record every case: one that fails its
    assertions there has a different wire, which the comparison reports.
    """
    work.mkdir(parents=True)
    pinned = work / "cliproxyapi_manifest.json"
    pinned.write_bytes(manifest_bytes)
    try:
        spec = release_guard.load_release_spec(pinned)
    except release_guard.ReleaseGuardError as exc:
        raise GateError(f"the {side} manifest is invalid: {exc}") from exc
    archive = next((item for item in spec.archives if item.platform == platform), None)
    if archive is None:
        raise GateError(f"the {side} manifest has no {platform} asset")
    try:
        if asset_dir is None:
            local_archive = work / archive.name
            release_guard._download(archive.owned_url, local_archive, max_bytes=archive.size)
            release_guard._verify_archive(local_archive, archive)
        else:
            # Exactly what publication would upload: every asset, sidecar and the manifest copy.
            release_guard.verify_release_assets(pinned, asset_dir)
            local_archive = asset_dir.resolve() / archive.name
    except release_guard.ReleaseGuardError as exc:
        raise GateError(f"the {side} engine is not the asset its manifest pins: {exc}") from exc
    # The E2E harness installs offline from a file URL; the archive was verified above.
    manifest = json.loads(manifest_bytes)
    for asset in manifest["assets"]:
        if asset["platform"] == platform:
            asset["url"] = local_archive.as_uri()
    offline_manifest = work / "offline-manifest.json"
    offline_manifest.write_text(json.dumps(manifest), encoding="utf-8")

    evidence, report = work / "evidence", work / "cases.xml"
    evidence.mkdir()
    print(f"hosted_wire gate: running the hosted passthrough cases against the {side} engine "
          f"{manifest['asset_release_tag']} ({platform})", flush=True)
    command = [sys.executable, "-m", "pytest", "-m", "e2e_model_hub", "-p", "no:cacheprovider",
               "-q", "-ra", f"--junitxml={report}", CASES]
    if loopback_only:
        # The engine was downloaded above; the cases and the engine they start need only loopback,
        # so one that ignores its loopback Source fails at once instead of reaching the network.
        command = ["bash", LOOPBACK_ONLY, *command]
    result = subprocess.run(
        command,
        cwd=REPO_ROOT,
        env={**os.environ, "VIBE_MODEL_HUB_ENGINE_MANIFEST_PATH": str(offline_manifest),
             EVIDENCE_DIR_ENV: str(evidence)},
        check=False,
    )
    try:
        testcases = list(ElementTree.parse(report).getroot().iter("testcase"))
    except (OSError, ElementTree.ParseError) as exc:
        raise GateError(f"the {side} engine's case run left no report") from exc
    names = {testcase.get("name") for testcase in testcases}
    passed = all(_met_its_expectation(testcase) for testcase in testcases)
    if not testcases or (must_pass and (result.returncode != 0 or not passed)):
        raise GateError(f"the hosted passthrough cases did not all pass against the {side} engine")
    cases = {path.stem: json.loads(path.read_text(encoding="utf-8")) for path in evidence.glob("*.json")}
    if set(cases) != names:
        raise GateError(f"the {side} engine's run recorded {sorted(cases)}, expected {sorted(names)}")
    return cases


def hosted_wire_differences(
    base_bytes: bytes, head_bytes: bytes, work: Path, head_assets: Path | None = None,
    *, loopback_only: bool = False,
) -> dict[str, list[str]]:
    from vibe.model_hub_runtime.installer import EngineRuntimeManager

    platform = EngineRuntimeManager(runtime_dir=work / "probe", offline=True).host_platform()
    base_cases = _run_cases("base", base_bytes, platform, work / "base",
                            must_pass=False, loopback_only=loopback_only)
    head_cases = _run_cases("PR", head_bytes, platform, work / "head", head_assets,
                            must_pass=True, loopback_only=loopback_only)
    return wire_differences(base_cases, head_cases)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="revision holding the base manifest")
    parser.add_argument(
        "--head-assets", type=Path,
        help="a local candidate's build output directory, instead of its published release",
    )
    parser.add_argument("--pull-request", help="number of the pull request whose labels may accept a difference")
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", "avibe-bot/avibe"))
    parser.add_argument("--work-dir", type=Path, help="keep downloads and per-case evidence here")
    parser.add_argument(
        "--loopback-only", action="store_true",
        help=f"run the cases in a network namespace with only loopback ({LOOPBACK_ONLY}; Linux CI)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        base_bytes = _git_show(args.base, MANIFEST)
        head_bytes = (REPO_ROOT / MANIFEST).read_bytes()
        try:
            base_tag = json.loads(base_bytes)["asset_release_tag"]
            head_tag = json.loads(head_bytes)["asset_release_tag"]
        except (ValueError, KeyError, TypeError) as exc:
            raise GateError(f"cannot read either manifest's asset_release_tag ({type(exc).__name__})") from exc
        if base_tag == head_tag:
            print(f"hosted_wire gate: the engine is unchanged ({head_tag}); nothing to compare")
            return 0
        with tempfile.TemporaryDirectory(prefix="hosted-wire-") as scratch:
            differences = hosted_wire_differences(base_bytes, head_bytes, args.work_dir or Path(scratch),
                                                  args.head_assets, loopback_only=args.loopback_only)
    except GateError as exc:
        print(f"::error::hosted_wire gate: {exc}")
        return 1
    if not differences:
        print(f"hosted_wire gate: {base_tag} -> {head_tag}: hosted passthrough byte-identical")
        return 0
    print(f"hosted_wire gate: {base_tag} -> {head_tag}: hosted passthrough differs")
    for case, parts in differences.items():
        print(f"  {case}: {', '.join(parts)}")
    labels = None
    if args.pull_request:
        labels = pull_request_labels(args.repository, args.pull_request, os.environ.get("GH_TOKEN"))
    else:
        print("hosted_wire gate: no pull request, so no label can accept the difference")
    violation = wire_change_violation(differences, labels)
    if violation:
        print(f"::error file={MANIFEST}::hosted_wire gate: {violation}")
        return 1
    print(f"hosted_wire gate: the pull request is labeled {CHANGE_LABEL}, so the difference is intended")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
