#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BUILD_UI=1

usage() {
    cat <<'EOF'
Usage: ./scripts/run_current_avibe.sh [--no-build-ui] [vibe arguments...]

Run Avibe directly from the current checkout instead of the installed package.
The UI is rebuilt by default so frontend changes are included.

Examples:
  ./scripts/run_current_avibe.sh
  ./scripts/run_current_avibe.sh --no-build-ui
  ./scripts/run_current_avibe.sh status
  AVIBE_HOME=/tmp/avibe-dev ./scripts/run_current_avibe.sh
EOF
}

args=()
while [ "$#" -gt 0 ]; do
    case "$1" in
        --no-build-ui)
            BUILD_UI=0
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        --)
            shift
            args+=("$@")
            break
            ;;
        *)
            args+=("$1")
            shift
            ;;
    esac
done

if ! command -v uv >/dev/null 2>&1; then
    echo "error: uv is required (https://docs.astral.sh/uv/)" >&2
    exit 1
fi

if [ "$BUILD_UI" -eq 1 ]; then
    if ! command -v npm >/dev/null 2>&1; then
        echo "error: npm is required to build the Web UI; pass --no-build-ui to use the existing ui/dist" >&2
        exit 1
    fi
    npm --prefix "$REPO_ROOT/ui" ci
    npm --prefix "$REPO_ROOT/ui" run build
elif [ ! -f "$REPO_ROOT/ui/dist/index.html" ]; then
    echo "error: ui/dist is missing; run without --no-build-ui first" >&2
    exit 1
fi

export PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}"
cd "$REPO_ROOT"

exec uv run python -c 'from vibe.cli import main; main()' "${args[@]}"
