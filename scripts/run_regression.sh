#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    PYTHON_BIN="python"
fi

mode="up"
target="${REGRESSION_TARGET:-master}"
incus_args=()
passthrough=()

usage() {
    "$PYTHON_BIN" "$REPO_ROOT/scripts/incus_regression.py" --help
}

while [ $# -gt 0 ]; do
    case "$1" in
        --master)
            target="master"
            shift
            ;;
        --worktree)
            target="worktree"
            shift
            ;;
        --down)
            mode="down"
            shift
            ;;
        --status)
            mode="status"
            shift
            ;;
        --logs)
            mode="logs"
            shift
            ;;
        --shell)
            mode="shell"
            shift
            ;;
        --delete)
            mode="delete"
            shift
            ;;
        --reset-config)
            incus_args+=(--reset-mode config)
            shift
            ;;
        --reset-all|--reset-state)
            incus_args+=(--reset-mode all)
            shift
            ;;
        --no-build)
            incus_args+=(--no-build-ui)
            shift
            ;;
        --env-file)
            if [ $# -lt 2 ]; then
                echo "--env-file requires a path argument" >&2
                exit 1
            fi
            incus_args+=(--env-file "$2")
            shift 2
            ;;
        --remote|--incus-remote)
            if [ $# -lt 2 ]; then
                echo "--remote requires a remote name" >&2
                exit 1
            fi
            incus_args+=(--remote "$2")
            shift 2
            ;;
        --allow-reset-paired-master|--dry-run|--clean|--force-deps|--no-build-ui|--force-ui|--yes)
            incus_args+=("$1")
            shift
            ;;
        --slug|--host-port|--ui-host|--ui-port|--worktree-port-start|--worktree-port-end|--image|--storage-pool|--network|--cpus|--memory|--disk|--processes)
            if [ $# -lt 2 ]; then
                echo "$1 requires an argument" >&2
                exit 1
            fi
            incus_args+=("$1" "$2")
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            passthrough+=("$1")
            shift
            ;;
    esac
done

if [ ${#passthrough[@]} -gt 0 ]; then
    echo "Unknown Incus regression argument(s): ${passthrough[*]}" >&2
    exit 1
fi

if [ ${#incus_args[@]} -gt 0 ]; then
    exec "$PYTHON_BIN" "$REPO_ROOT/scripts/incus_regression.py" "$mode" --target "$target" "${incus_args[@]}"
else
    exec "$PYTHON_BIN" "$REPO_ROOT/scripts/incus_regression.py" "$mode" --target "$target"
fi
