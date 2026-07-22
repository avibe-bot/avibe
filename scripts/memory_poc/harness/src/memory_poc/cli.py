from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .constants import STAGES
from .errors import ConfigurationError, HarnessError
from .environment import checked_workspace_root, discover_provider_settings, verify_harness_interpreter
from .paths import ensure_owner_directory, read_private_text, runtime_root
from .reports import load_report
from .sanity import load_sanity_fixture, run_sanity
from .stage2 import run_stage2
from .identifiers import validate_run_id


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m memory_poc")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run")
    run.add_argument("--stage", required=True, choices=STAGES)
    run.add_argument("--run-id", required=True)
    report = subparsers.add_parser("report")
    report.add_argument("--run-id", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        workspace = checked_workspace_root()
        verify_harness_interpreter(workspace)
        if args.command == "run":
            return _run_stage(args.stage, args.run_id)
        if args.command == "report":
            run_id = validate_run_id(args.run_id)
            state = runtime_root(workspace)
            if not state.is_dir():
                raise HarnessError("report_not_found")
            ensure_owner_directory(state, anchor=workspace)
            runs = state / "runs"
            run_directory = runs / run_id
            if not runs.is_dir() or not run_directory.is_dir():
                raise HarnessError("report_not_found")
            ensure_owner_directory(runs, anchor=state)
            ensure_owner_directory(run_directory, anchor=state)
            path = run_directory / "report.json"
            fixture_texts = _fixture_texts_for_report(run_directory)
            print(
                json.dumps(
                    load_report(path, fixture_texts=fixture_texts, secret_values=_report_secret_values(workspace)),
                    ensure_ascii=True,
                    indent=2,
                )
            )
            return 0
        raise HarnessError("unknown_command")
    except HarnessError as exc:
        print(f"memory-poc: {exc}", file=sys.stderr)
        return 2


def _run_stage(stage: str, run_id: str) -> int:
    if stage == "sanity":
        report_path = run_sanity(run_id=run_id)
    else:
        report_path = run_stage2(stage=stage, run_id=run_id)
    print(json.dumps({"run_id": run_id, "report": str(report_path)}, ensure_ascii=True))
    return 0


def _fixture_texts_for_report(run_directory: Path) -> tuple[str, ...]:
    """Select report redaction data from trusted, per-run metadata only."""
    try:
        metadata = json.loads(read_private_text(run_directory / "run.json"))
    except (HarnessError, OSError, ValueError) as exc:
        raise HarnessError("report_fixture_source_unknown") from exc
    if metadata == {"stage": "sanity", "fixture_set": "stage1-mini"}:
        return tuple(message["content"] for message in load_sanity_fixture().messages)
    if (
        isinstance(metadata, dict)
        and metadata.get("stage") == "stage2"
        and isinstance(metadata.get("corpus_revision"), str)
        and isinstance(metadata.get("completed_stages"), list)
        and all(isinstance(item, str) for item in metadata["completed_stages"])
    ):
        try:
            workspace = run_directory.parents[3]
            from .corpus import load_corpus

            corpus = load_corpus(workspace)
        except HarnessError as exc:
            raise HarnessError("report_fixture_source_unknown") from exc
        if metadata["corpus_revision"] == corpus.revision:
            return tuple(message.text for message in corpus.messages)
    raise HarnessError("report_fixture_source_unknown")


def _report_secret_values(workspace: Path) -> tuple[str, ...]:
    """Use live configuration for report redaction when it is safely available."""
    try:
        settings = discover_provider_settings(workspace)
    except ConfigurationError as exc:
        if str(exc).split(":", 1)[0] in {"provider_configuration_missing", "provider_configuration_incomplete"}:
            return ()
        raise
    return (settings.llm_api_key, settings.embedding_api_key)
