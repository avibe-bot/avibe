from __future__ import annotations

import argparse
import json
import sys

from .constants import STAGES
from .errors import HarnessError, StageNotImplementedError
from .environment import checked_workspace_root
from .reports import load_report
from .sanity import run_sanity
from .identifiers import validate_run_id
from .paths import ensure_owner_directory, runtime_root


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
        if args.command == "run":
            return _run_stage(args.stage, args.run_id)
        if args.command == "report":
            run_id = validate_run_id(args.run_id)
            workspace = checked_workspace_root()
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
            print(json.dumps(load_report(path), ensure_ascii=True, indent=2))
            return 0
        raise HarnessError("unknown_command")
    except HarnessError as exc:
        print(f"memory-poc: {exc}", file=sys.stderr)
        return 2


def _run_stage(stage: str, run_id: str) -> int:
    if stage != "sanity":
        raise StageNotImplementedError(f"stage_not_implemented:{stage}")
    report_path = run_sanity(run_id=run_id)
    print(json.dumps({"run_id": run_id, "report": str(report_path)}, ensure_ascii=True))
    return 0
