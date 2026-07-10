#!/bin/bash
set -e

cleanup() {
    python -c "
from vibe import runtime
runtime.stop_service()
runtime.stop_ui()
runtime.write_status('stopped', 'container shutdown')
"
}

trap cleanup EXIT INT TERM

start_ui_process() {
    local runtime_dir="$1"
    local ui_stdout="$runtime_dir/ui_stdout.log"
    local ui_stderr="$runtime_dir/ui_stderr.log"

    echo "Starting UI server on 0.0.0.0:${VIBE_UI_PORT:-5123}..." >&2
    python -c "
from vibe.ui_server import run_ui_server
run_ui_server('0.0.0.0', ${VIBE_UI_PORT:-5123})
" > >(python -m vibe.log_sink "$ui_stdout") 2> >(python -m vibe.log_sink "$ui_stderr") &

    local ui_pid=$!
    echo "$ui_pid" > "$runtime_dir/vibe-ui.pid"
    echo "$ui_pid"
}

resolve_runtime_dir() {
    python -c "
from config.paths import get_runtime_dir
print(get_runtime_dir())
"
}

write_runtime_status() {
    local state="$1"
    local detail="$2"
    local service_pid="${3:-None}"
    local ui_pid="${4:-None}"

    python -c "
from vibe import runtime
runtime.write_status('${state}', '${detail}', ${service_pid}, ${ui_pid})
"
}

read_runtime_service_pid() {
    local runtime_dir="$1"
    local pid_file="$runtime_dir/vibe.pid"

    if [ ! -f "$pid_file" ]; then
        return 1
    fi

    local runtime_pid
    runtime_pid="$(tr -d '[:space:]' < "$pid_file")"
    if [[ ! "$runtime_pid" =~ ^[0-9]+$ ]]; then
        return 1
    fi

    echo "$runtime_pid"
}

read_runtime_state() {
    local runtime_dir="$1"
    local status_file="$runtime_dir/status.json"

    if [ ! -f "$status_file" ]; then
        return 1
    fi

    python - "$status_file" <<'PY'
import json
import sys
from pathlib import Path

status_path = Path(sys.argv[1])
try:
    payload = json.loads(status_path.read_text(encoding="utf-8"))
except Exception:
    sys.exit(1)

state = payload.get("state")
if isinstance(state, str):
    print(state)
PY
}

wait_for_replacement_service_pid() {
    local runtime_dir="$1"
    local previous_pid="$2"
    local attempts=50

    while [ "$attempts" -gt 0 ]; do
        local runtime_pid=""
        runtime_pid="$(read_runtime_service_pid "$runtime_dir" 2>/dev/null || true)"
        if [ -n "$runtime_pid" ] && [ "$runtime_pid" != "$previous_pid" ] && kill -0 "$runtime_pid" 2>/dev/null; then
            echo "$runtime_pid"
            return 0
        fi
        sleep 0.1
        attempts=$((attempts - 1))
    done

    return 1
}

is_intentional_service_stop_state() {
    case "$1" in
        stopped|stopping|restarting|starting)
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

ensure_service_pid() {
    local runtime_dir="$1"
    local service_pid="$2"
    local ui_pid="$3"

    local current_runtime_pid=""
    current_runtime_pid="$(read_runtime_service_pid "$runtime_dir" 2>/dev/null || true)"
    if [ -n "$current_runtime_pid" ] && [ "$current_runtime_pid" != "$service_pid" ] && kill -0 "$current_runtime_pid" 2>/dev/null; then
        echo "$current_runtime_pid"
        return 0
    fi

    if kill -0 "$service_pid" 2>/dev/null; then
        echo "$service_pid"
        return 0
    fi

    local replacement_pid=""
    replacement_pid="$(wait_for_replacement_service_pid "$runtime_dir" "$service_pid" 2>/dev/null || true)"
    if [ -n "$replacement_pid" ]; then
        echo "Detected replacement service PID ${replacement_pid}, continuing supervisor loop." >&2
        write_runtime_status "running" "service restarted in container" "$replacement_pid" "$ui_pid"
        echo "$replacement_pid"
        return 0
    fi

    local runtime_state=""
    runtime_state="$(read_runtime_state "$runtime_dir" 2>/dev/null || true)"
    if is_intentional_service_stop_state "$runtime_state"; then
        return 0
    fi

    local service_exit_code=1
    local wait_status=0
    if wait "$service_pid" 2>/dev/null; then
        wait_status=0
    else
        wait_status=$?
    fi
    if [ "$wait_status" -ne 127 ]; then
        service_exit_code="$wait_status"
    fi
    echo "Service exited unexpectedly (code: ${service_exit_code}), stopping container..." >&2
    write_runtime_status "stopped" "service exited unexpectedly" "$service_pid" "$ui_pid"
    exit "$service_exit_code"
}

# Ensure runtime directories exist and seed default config if missing
python -c "
from config.paths import ensure_data_dirs, get_config_path
ensure_data_dirs()
config_path = get_config_path()
if not config_path.exists():
    from vibe.runtime import default_config
    default_config().save(config_path)
"

MODE="${1:-ui}"

case "$MODE" in
    ui)
        # Start only the UI server (foreground) - for API E2E testing
        echo "Starting UI server on 0.0.0.0:${VIBE_UI_PORT:-5123}..."
        exec python -c "
from vibe.ui_server import run_ui_server
run_ui_server('0.0.0.0', ${VIBE_UI_PORT:-5123})
"
        ;;
    full)
        # Start service + UI server under a lightweight supervisor loop
        echo "Starting service..."
        python main.py &
        SERVICE_PID=$!

        # Write PID for runtime tracking
        RUNTIME_DIR="$(resolve_runtime_dir)"
        mkdir -p "$RUNTIME_DIR"
        echo "$SERVICE_PID" > "$RUNTIME_DIR/vibe.pid"

        UI_PID="$(start_ui_process "$RUNTIME_DIR")"

        write_runtime_status "running" "started" "$SERVICE_PID" "$UI_PID"

        while true; do
            CURRENT_UI_PID=""
            if [ -f "$RUNTIME_DIR/vibe-ui.pid" ]; then
                CURRENT_UI_PID="$(cat "$RUNTIME_DIR/vibe-ui.pid")"
            fi

            SERVICE_PID="$(ensure_service_pid "$RUNTIME_DIR" "${SERVICE_PID:-}" "${CURRENT_UI_PID:-$UI_PID}")"

            if [ -z "$CURRENT_UI_PID" ]; then
                UI_PID="$(start_ui_process "$RUNTIME_DIR")"
                CURRENT_STATE="$(read_runtime_state "$RUNTIME_DIR" 2>/dev/null || true)"
                if [ -z "$CURRENT_STATE" ]; then
                    if [ -n "${SERVICE_PID:-}" ]; then
                        CURRENT_STATE="running"
                    else
                        CURRENT_STATE="stopped"
                    fi
                fi
                write_runtime_status "$CURRENT_STATE" "ui restarted" "${SERVICE_PID:-None}" "$UI_PID"
            elif ! kill -0 "$CURRENT_UI_PID" 2>/dev/null; then
                UI_EXIT_CODE=0
                wait "$CURRENT_UI_PID" 2>/dev/null || UI_EXIT_CODE=$?
                echo "UI server exited unexpectedly (code: ${UI_EXIT_CODE:-unknown}), restarting..."
                UI_PID="$(start_ui_process "$RUNTIME_DIR")"
                CURRENT_STATE="$(read_runtime_state "$RUNTIME_DIR" 2>/dev/null || true)"
                if [ -z "$CURRENT_STATE" ]; then
                    if [ -n "${SERVICE_PID:-}" ]; then
                        CURRENT_STATE="running"
                    else
                        CURRENT_STATE="stopped"
                    fi
                fi
                write_runtime_status "$CURRENT_STATE" "ui restarted after crash" "${SERVICE_PID:-None}" "$UI_PID"
            fi

            sleep 1
        done
        ;;
    cli)
        # Run a vibe CLI command (e.g., docker run ... cli doctor)
        shift
        exec vibe "$@"
        ;;
    exec)
        # Run arbitrary command (for debugging)
        shift
        exec "$@"
        ;;
    *)
        echo "Usage: docker-entrypoint.sh {ui|full|cli|exec}"
        echo "  ui   - Start UI server only (default)"
        echo "  full - Start service + UI server"
        echo "  cli  - Run vibe CLI command"
        echo "  exec - Run arbitrary command"
        exit 1
        ;;
esac
