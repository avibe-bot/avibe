#!/usr/bin/env bash
# Avibe Installation Script
# Usage: bash -o pipefail -c 'curl -fsSL https://avibe.bot/install.sh | bash -s -- --launch'
# Uninstall, keeping your data:
#   bash -o pipefail -c 'curl -fsSL https://avibe.bot/install.sh | bash -s -- --uninstall'
# Add --purge to also delete your data; without a terminal to confirm on, a purge also needs --yes.
# The uninstaller never deletes through a link; anything behind a link is reported for you to remove.
#
# Prerequisites: None! uv will be installed automatically and manages Python for you.

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
REPO="avibe-bot/avibe"
PACKAGE_NAME="avibe-os"
NODE_MINIMUM_REQUIREMENT="20.19+ or 22.12+"
VIBE_BIN_PATH=""
VIBE_TOOL_BIN_DIR=""
# A root install has one supported stable launcher, whatever PATH order the
# shell happens to have. Upgrades keep every other managed launcher in step.
ROOT_TOOL_BIN_DIR="/usr/local/bin"
# The fixed stable-launcher locations this installer chooses from, in order.
# Launcher discovery checks them beside PATH and uv's configured tool bin.
# Keep in step with INSTALLER_LAUNCHER_DIRS in vibe/upgrade.py.
INSTALLER_LAUNCHER_DIRS=("$HOME/.local/bin" "$HOME/bin" "/usr/local/bin" "/opt/homebrew/bin")
PUBLIC_INSTALL_SCRIPT_URL="https://avibe.bot/install.sh"
VIBE_CANDIDATE_BIN_PATH=""
# uv 0.10.8 and later fetch managed Python from Astral's CDN and fall back to
# GitHub; earlier uv fetches it from GitHub only. uv_python_install_mirror
# gives earlier uv the CDN for the install step unless the user chose a source.
ASTRAL_PYTHON_INSTALL_MIRROR="https://releases.astral.sh/github/python-build-standalone/releases/download"
UV_INSTALL_PYTHON_MIRROR=""
LAUNCH_AFTER_INSTALL=""
PURGE_USER_DATA=""
ASSUME_YES=""
AVIBE_LAUNCHED=""
ORIGINAL_PATH="$PATH"
if [ -n "${AVIBE_HOME:-}" ]; then
    AVIBE_RUNTIME_HOME="${AVIBE_HOME/#\~/$HOME}"
    case "$AVIBE_RUNTIME_HOME" in
        /*) ;;
        *) AVIBE_RUNTIME_HOME="$PWD/$AVIBE_RUNTIME_HOME" ;;
    esac
elif [ -e "$HOME/.avibe" ] || [ -L "$HOME/.avibe" ]; then
    AVIBE_RUNTIME_HOME="$HOME/.avibe"
elif [ -e "$HOME/.vibe_remote" ] || [ -L "$HOME/.vibe_remote" ]; then
    AVIBE_RUNTIME_HOME="$HOME/.vibe_remote"
else
    AVIBE_RUNTIME_HOME="$HOME/.avibe"
fi
# A trailing separator makes a link read as the directory it names.
while [ "${#AVIBE_RUNTIME_HOME}" -gt 1 ] && [ "${AVIBE_RUNTIME_HOME%/}" != "$AVIBE_RUNTIME_HOME" ]; do
    AVIBE_RUNTIME_HOME="${AVIBE_RUNTIME_HOME%/}"
done
REMOTE_ACCESS_PAIRING_KEY=""
REMOTE_ACCESS_PAIRED=""

print_banner() {
    echo -e "${BLUE}"
    cat << 'EOF'
    ___          _ __
   /   | _   __ (_) /_  ___
  / /| || | / // / __ \/ _ \
 / ___ || |/ // / /_/ /  __/
/_/  |_||___//_/_.___/\___/
EOF
    echo -e "${NC}"
    echo -e "${GREEN}The local-first Agent OS for Web and chat${NC}"
    echo ""
}

info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

success() {
    echo -e "${GREEN}[OK]${NC} $1"
}

warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

error() {
    echo -e "${RED}[ERROR]${NC} $1"
    exit 1
}

# Detect OS
detect_os() {
    case "$(uname -s)" in
        Linux*)     OS="linux";;
        Darwin*)    OS="macos";;
        CYGWIN*|MINGW*|MSYS*) OS="windows";;
        *)          OS="unknown";;
    esac
    echo "$OS"
}

path_contains_dir() {
    local path_value="$1"
    local target_dir="$2"

    case ":$path_value:" in
        *":$target_dir:"*) return 0 ;;
        *) return 1 ;;
    esac
}

ensure_writable_dir() {
    local dir="$1"

    if [ -z "$dir" ]; then
        return 1
    fi

    if [ ! -d "$dir" ]; then
        mkdir -p "$dir" 2>/dev/null || return 1
    fi

    [ -d "$dir" ] && [ -w "$dir" ]
}

is_absolute_dir() {
    case "$1" in
        /*) return 0 ;;
        *) return 1 ;;
    esac
}

is_sbin_dir() {
    case "$1" in
        */sbin) return 0 ;;
        *) return 1 ;;
    esac
}

is_transient_bin_dir() {
    local dir="$1"

    case "$dir" in
        */.venv/bin|*/venv/bin|*/env/bin|*/.pyenv/shims|*/.pyenv/versions/*/bin|*/.local/share/mise/installs/*/bin|*/.mise/installs/*/bin|*/uv/tools/*/bin)
            return 0
            ;;
    esac

    case "$dir" in
        "$AVIBE_RUNTIME_HOME"/runtime/install-generations/*) return 0 ;;
    esac

    if [ -n "${VIRTUAL_ENV:-}" ] && [ "$dir" = "${VIRTUAL_ENV%/}/bin" ]; then
        return 0
    fi

    if [ -n "${CONDA_PREFIX:-}" ] && [ "$dir" = "${CONDA_PREFIX%/}/bin" ]; then
        return 0
    fi

    if [ -n "${PYENV_ROOT:-}" ]; then
        case "$dir" in
            "${PYENV_ROOT%/}"/shims) return 0 ;;
            "${PYENV_ROOT%/}"/versions/*/bin) return 0 ;;
        esac
    fi

    if [ -n "${MISE_DATA_DIR:-}" ]; then
        case "$dir" in
            "${MISE_DATA_DIR%/}"/installs/*/bin) return 0 ;;
        esac
    fi

    return 1
}

is_avibe_launcher() {
    # pip and uv generate this console script from vibe.cli:main, including
    # legacy vibe-remote installs. Inspect it; never execute an unknown command
    # merely because it happens to be named vibe. Symlinks retain their logical
    # public directory while these reads follow the installed script.
    [ -f "$1" ] && [ -x "$1" ] &&
        LC_ALL=C grep -qE '^from vibe[.]cli import main[[:space:]]*$' "$1" &&
        LC_ALL=C grep -qE '^[[:space:]]*sys[.]exit\(main\(\)\)[[:space:]]*$' "$1"
}

launcher_destination_is_available() {
    { [ ! -e "$1/vibe" ] && [ ! -L "$1/vibe" ]; } || is_avibe_launcher "$1/vibe"
}

choose_tool_bin_dir() {
    local dir
    local fallback_sbin_dir=""

    if [ "$(id -u 2>/dev/null || echo 1)" = "0" ] && is_absolute_dir "$ROOT_TOOL_BIN_DIR" &&
        launcher_destination_is_available "$ROOT_TOOL_BIN_DIR" && ensure_writable_dir "$ROOT_TOOL_BIN_DIR"; then
        echo "$ROOT_TOOL_BIN_DIR"
        return 0
    fi

    local old_ifs="$IFS"
    IFS=":"
    # Reinstall through the established public entrypoint before choosing a new
    # writable directory. Do not resolve its symlink into a package generation:
    # the shared activation owner must keep switching this stable launcher.
    for dir in $ORIGINAL_PATH; do
        dir="${dir%/}"
        if [ -n "$dir" ] && is_absolute_dir "$dir" &&
            ! is_transient_bin_dir "$dir" && ! is_sbin_dir "$dir" &&
            [ -w "$dir" ] && is_avibe_launcher "$dir/vibe"; then
            IFS="$old_ifs"
            echo "$dir"
            return 0
        fi
    done

    for dir in $ORIGINAL_PATH; do
        if [ -n "$dir" ] && is_absolute_dir "$dir" && ! is_transient_bin_dir "$dir" &&
            launcher_destination_is_available "$dir" && ensure_writable_dir "$dir"; then
            if is_sbin_dir "$dir"; then
                if [ -z "$fallback_sbin_dir" ]; then
                    fallback_sbin_dir="$dir"
                fi
                continue
            fi
            IFS="$old_ifs"
            echo "$dir"
            return 0
        fi
    done
    IFS="$old_ifs"

    for dir in "${INSTALLER_LAUNCHER_DIRS[@]}"; do
        if is_absolute_dir "$dir" && launcher_destination_is_available "$dir" && ensure_writable_dir "$dir"; then
            echo "$dir"
            return 0
        fi
    done

    if [ -n "$fallback_sbin_dir" ]; then
        echo "$fallback_sbin_dir"
        return 0
    fi

    return 1
}

# Check if command exists
command_exists() {
    command -v "$1" >/dev/null 2>&1
}

is_apple_silicon_macos() {
    [ "$(detect_os)" = "macos" ] && [ "$(sysctl -n hw.optional.arm64 2>/dev/null || echo 0)" = "1" ]
}

resolve_binary_path() {
    local path="$1"
    local dir=""
    local target=""
    local depth=0

    if [ -z "$path" ]; then
        return 1
    fi

    while [ -L "$path" ] && [ "$depth" -lt 20 ] && command_exists readlink; do
        target="$(readlink "$path" 2>/dev/null || true)"
        if [ -z "$target" ]; then
            break
        fi
        case "$target" in
            /*) path="$target" ;;
            *)
                dir="$(dirname "$path")"
                path="$dir/$target"
                ;;
        esac
        depth=$((depth + 1))
    done

    printf '%s\n' "$path"
}

binary_architecture() {
    local path="$1"
    path="$(resolve_binary_path "$path" || true)"

    if [ -z "$path" ] || [ ! -e "$path" ] || ! command_exists file; then
        return 1
    fi

    file -b "$path" 2>/dev/null || true
}

is_arm64_binary() {
    local path="$1"
    binary_architecture "$path" | grep -Eq '(^|[^[:alnum:]_])(arm64e?|aarch64)([^[:alnum:]_]|$)'
}

is_x86_64_binary() {
    local path="$1"
    binary_architecture "$path" | grep -Eq '(^|[^[:alnum:]_])x86_64([^[:alnum:]_]|$)'
}

uv_binary_is_acceptable() {
    local uv_path="$1"

    if [ -z "$uv_path" ]; then
        return 1
    fi

    if is_apple_silicon_macos && is_x86_64_binary "$uv_path" && ! is_arm64_binary "$uv_path"; then
        return 1
    fi

    return 0
}

uv_is_native_for_host() {
    local uv_path

    uv_path="$(command -v uv 2>/dev/null || true)"
    if [ -z "$uv_path" ]; then
        return 1
    fi

    uv_binary_is_acceptable "$uv_path"
}

# Print the Python download mirror the install step should give uv, if any.
# Newer uv is left alone: an explicit mirror turns its GitHub fallback off.
uv_python_install_mirror() {
    if [ -n "${UV_PYTHON_INSTALL_MIRROR+set}" ] || [ -n "${UV_PYTHON_DOWNLOADS_JSON_URL+set}" ]; then
        return 0
    fi

    local version major minor patch_extra
    version="$(uv --version 2>/dev/null || true)"
    version="${version#uv }"
    IFS='.' read -r major minor patch_extra <<EOF
${version%% *}
EOF
    local patch="${patch_extra%%[^0-9]*}"
    case "$major.$minor.$patch" in
        *[!0-9.]*|.*|*..*|*.) return 0 ;;
    esac
    if [ "$major" -ne 0 ] || [ "$minor" -gt 10 ] || { [ "$minor" -eq 10 ] && [ "$patch" -ge 8 ]; }; then
        return 0
    fi

    # uv resolves its own config files, so a Python source the user chose there
    # is kept. A config uv cannot load keeps uv's default too.
    local settings
    settings="$(uv tool install --show-settings "$PACKAGE_NAME" 2>/dev/null)" || return 0
    case "$settings" in
        *"python_install_mirror: Some("*|*"python_downloads_json_url: Some("*) return 0 ;;
    esac
    printf '%s\n' "$ASTRAL_PYTHON_INSTALL_MIRROR"
}

node_version_parts() {
    local version=""
    version="$(node --version 2>/dev/null || true)"
    version="${version#v}"
    IFS='.' read -r major minor patch_extra <<EOF
$version
EOF
    local patch="${patch_extra%%[^0-9]*}"
    case "$major.$minor.$patch" in
        *[!0-9.]*|.*|*..*|*.) return 1 ;;
        *) printf '%s %s %s\n' "$major" "$minor" "$patch" ;;
    esac
}

node_is_acceptable() {
    local major minor patch
    read -r major minor patch <<EOF
$(node_version_parts || true)
EOF
    [ -n "${major:-}" ] || return 1
    if [ "$major" -eq 20 ]; then
        [ "$minor" -ge 19 ]
    elif [ "$major" -ge 22 ]; then
        if [ "$major" -gt 22 ]; then
            return 0
        fi
        [ "$minor" -ge 12 ]
    else
        return 1
    fi
}

node_platform_arch() {
    local os="$1"
    local machine=""
    machine="$(uname -m 2>/dev/null || true)"

    case "$machine" in
        arm64|aarch64) machine="arm64" ;;
        x86_64|amd64) machine="x64" ;;
        *) return 1 ;;
    esac

    case "$os" in
        macos) printf 'darwin-%s\n' "$machine" ;;
        linux) printf 'linux-%s\n' "$machine" ;;
        *) return 1 ;;
    esac
}

run_as_root() {
    if [ "$(id -u 2>/dev/null || echo 1)" = "0" ]; then
        "$@"
    elif command_exists sudo; then
        sudo "$@"
    else
        return 127
    fi
}

install_node() {
    if [ "${VIBE_INSTALL_SKIP_NODE:-}" = "1" ]; then
        warn "Skipping Node.js installation because VIBE_INSTALL_SKIP_NODE=1"
        return 0
    fi

    if command_exists node && node_is_acceptable; then
        success "Node.js is already installed"
        return 0
    fi

    local os
    os="$(detect_os)"

    info "Installing Node.js ${NODE_MINIMUM_REQUIREMENT} for Show Pages runtime..."
    case "$os" in
        macos)
            if command_exists brew; then
                brew install node || return 1
            else
                warn "Node.js ${NODE_MINIMUM_REQUIREMENT} is required for managed Show Pages. Install Homebrew or Node.js from https://nodejs.org/ if needed."
                return 1
            fi
            ;;
        linux)
            if command_exists apt-get; then
                curl -fsSL https://deb.nodesource.com/setup_22.x | run_as_root bash - || return 1
                run_as_root apt-get install -y nodejs || return 1
            elif command_exists dnf; then
                run_as_root dnf install -y nodejs npm || return 1
            elif command_exists yum; then
                run_as_root yum install -y nodejs npm || return 1
            elif command_exists pacman; then
                run_as_root pacman -S --noconfirm nodejs npm || return 1
            else
                warn "Node.js ${NODE_MINIMUM_REQUIREMENT} is required for managed Show Pages. Please install Node.js globally with your system package manager if needed."
                return 1
            fi
            ;;
        *)
            warn "Node.js ${NODE_MINIMUM_REQUIREMENT} is required for managed Show Pages. Please install Node.js globally if needed."
            return 1
            ;;
    esac

    if command_exists node && node_is_acceptable; then
        success "Node.js installed successfully"
        return 0
    fi

    warn "Node.js installation completed but Node.js ${NODE_MINIMUM_REQUIREMENT} is not available in PATH"
    return 1
}

install_node_optional() {
    set +e
    install_node
    local node_status=$?
    set -e

    if [ "$node_status" -eq 0 ]; then
        return 0
    fi

    warn "Node.js ${NODE_MINIMUM_REQUIREMENT} is not available, so managed Show Pages may install/start later when first used."
    warn "Continuing with Avibe installation; install Node.js manually if Show Pages runtime reports it missing."
    return 0
}

uv_tool_install() {
    # uv writes a tool environment in place.  Give every attempt its own
    # durable generation and activate its launcher only after uv exits
    # successfully, so an interrupted copy cannot damage the active install.
    local generation_root="$AVIBE_RUNTIME_HOME/runtime/install-generations/$(date +%s)-$$-${RANDOM}"
    # 3.0.13 identifies uv-managed installs from the executable's /uv/tools/
    # path. Keep that released contract in every generation so the old binary
    # can hand the first upgrade to uv instead of falling through to pip.
    local generation_tools="$generation_root/uv/tools"
    local generation_bin="$generation_root/bin"
    local stable_bin_dir="${VIBE_TOOL_BIN_DIR:-$HOME/.local/bin}"
    local previous_target=""
    local source_snapshot=""
    if ! launcher_destination_is_available "$stable_bin_dir"; then
        warn "Refusing to replace an unrecognized vibe entrypoint in $stable_bin_dir"
        return 1
    fi
    mkdir -p "$generation_tools" "$generation_bin" "$stable_bin_dir"
    # Publish before the source snapshot. Collection in another activation
    # retains this installer's entire handoff while the shell is still alive.
    # The marker protects staging even after uv has finished its tool receipt.
    local installer_marker="$generation_root/.avibe-installing"
    local installer_marker_temporary="$installer_marker.new"
    if ! printf '%s\n' "$$" > "$installer_marker_temporary" ||
        ! mv -f -- "$installer_marker_temporary" "$installer_marker"; then
        warn "Could not protect the installer handoff"
        rm -rf -- "$generation_root"
        return 1
    fi
    if [ -e "$stable_bin_dir/vibe" ] || [ -L "$stable_bin_dir/vibe" ]; then
        previous_target="$(resolve_binary_path "$stable_bin_dir/vibe" || true)"
        local current_protocol=""
        current_protocol="$(
            cd "$AVIBE_RUNTIME_HOME" || exit 1
            env -u PYTHONPATH -u PYTHONHOME AVIBE_HOME="$AVIBE_RUNTIME_HOME" "$stable_bin_dir/vibe" \
                __activate-install --protocol-version 2>/dev/null || true
        )"
        if [ "${current_protocol:-0}" -ge 2 ] 2>/dev/null; then
            source_snapshot="$(
                cd "$AVIBE_RUNTIME_HOME" || exit 1
                env -u PYTHONPATH -u PYTHONHOME AVIBE_HOME="$AVIBE_RUNTIME_HOME" "$stable_bin_dir/vibe" \
                    __activate-install --snapshot --launcher "$stable_bin_dir/vibe" 2>/dev/null || true
            )"
        else
            source_snapshot="$previous_target"
        fi
    fi

    # Suppress package-manager progress only, not activation/retention diagnostics.
    if env ${UV_INSTALL_PYTHON_MIRROR:+"UV_PYTHON_INSTALL_MIRROR=$UV_INSTALL_PYTHON_MIRROR"} \
        UV_TOOL_DIR="$generation_tools" UV_TOOL_BIN_DIR="$generation_bin" uv tool install "$@" 2>/dev/null; then
        VIBE_CANDIDATE_BIN_PATH="$generation_bin/vibe"
        if [ ! -x "$VIBE_CANDIDATE_BIN_PATH" ]; then
            warn "uv completed but the candidate vibe launcher was not created"
            rm -rf -- "$generation_root"
            return 1
        fi
        # The candidate's shared Python activation owner canonicalizes this
        # snapshot against the generation root. Keep path identity out of the
        # bootstrap script so aliases cannot produce a second interpretation.
        local activation_args=(
            __activate-install
            --launcher "$stable_bin_dir/vibe"
            --candidate "$VIBE_CANDIDATE_BIN_PATH"
        )
        if [ -n "$source_snapshot" ]; then
            activation_args+=(--source-generation "$source_snapshot")
        fi
        local activation_owner=""
        local owner=""
        local owner_protocol=""
        for owner in "$VIBE_CANDIDATE_BIN_PATH" "$previous_target"; do
            if [ -z "$owner" ] || [ ! -x "$owner" ]; then
                continue
            fi
            owner_protocol="$(
                cd "$AVIBE_RUNTIME_HOME" || exit 1
                env -u PYTHONPATH -u PYTHONHOME AVIBE_HOME="$AVIBE_RUNTIME_HOME" "$owner" \
                    __activate-install --protocol-version 2>/dev/null || true
            )"
            if [ "${owner_protocol:-0}" -ge 1 ] 2>/dev/null; then
                activation_owner="$owner"
                break
            fi
        done
        if [ -n "$activation_owner" ]; then
            if ! (
                cd "$AVIBE_RUNTIME_HOME" || exit 1
                env -u PYTHONPATH -u PYTHONHOME AVIBE_HOME="$AVIBE_RUNTIME_HOME" "$activation_owner" \
                    "${activation_args[@]}"
            ); then
                warn "candidate Avibe environment could not be activated"
                rm -rf -- "$generation_root"
                return 1
            fi
        else
            # A legacy wheel can bootstrap a fresh machine, but replacing an
            # existing launcher requires a current wheel to own the shared
            # lock and source-snapshot checks above.
            if ! (
                cd "$AVIBE_RUNTIME_HOME" || exit 1
                env -u PYTHONPATH -u PYTHONHOME AVIBE_HOME="$AVIBE_RUNTIME_HOME" \
                    "$VIBE_CANDIDATE_BIN_PATH" --help >/dev/null 2>&1
            ); then
                warn "candidate vibe launcher failed its startup probe"
                rm -rf -- "$generation_root"
                return 1
            fi
            if [ -e "$stable_bin_dir/vibe" ] || [ -L "$stable_bin_dir/vibe" ]; then
                warn "legacy candidate cannot safely replace an existing Avibe installation"
                rm -rf -- "$generation_root"
                return 1
            fi
            if ! ln -s "$VIBE_CANDIDATE_BIN_PATH" "$stable_bin_dir/vibe"; then
                warn "legacy candidate Avibe launcher could not be activated"
                rm -rf -- "$generation_root"
                return 1
            fi
        fi
        VIBE_TOOL_BIN_DIR="$stable_bin_dir"
        VIBE_BIN_PATH="$stable_bin_dir/vibe"
        rm -f -- "$generation_root/.avibe-installing" || warn "Could not remove the completed installer marker"
        return 0
    fi
    rm -rf -- "$generation_root"
    return 1
}

install_package_candidate() {
    local package_spec="$1"
    shift

    if [ "$package_spec" = "$PACKAGE_NAME" ]; then
        uv_tool_install "$package_spec" --force --refresh "$@"
    else
        uv_tool_install "$package_spec" --force "$@"
    fi
}

resolve_vibe_on_original_path() {
    PATH="$ORIGINAL_PATH" command -v vibe 2>/dev/null || true
}

is_vibe_immediately_available() {
    local resolved_vibe

    if [ -z "$VIBE_BIN_PATH" ]; then
        return 1
    fi

    resolved_vibe="$(resolve_vibe_on_original_path)"
    [ -n "$resolved_vibe" ] && [ "$resolved_vibe" = "$VIBE_BIN_PATH" ]
}

# Install uv if not present
install_uv() {
    local existing_uv=""
    existing_uv="$(command -v uv 2>/dev/null || true)"

    if [ -n "$existing_uv" ] && uv_is_native_for_host; then
        success "uv is already installed"
        return 0
    fi

    if [ -n "$existing_uv" ] && is_apple_silicon_macos && is_x86_64_binary "$existing_uv" && ! is_arm64_binary "$existing_uv"; then
        warn "Found x86_64 uv on Apple Silicon: $existing_uv"
        info "Installing native arm64 uv for this Mac..."
    fi
    
    info "Installing uv (will also manage Python automatically)..."
    
    local os
    os=$(detect_os)
    
    case "$os" in
        macos|linux)
            local curl_retry_all_errors=""
            if curl --help all 2>/dev/null | grep -q -- '--retry-all-errors'; then
                curl_retry_all_errors="--retry-all-errors"
            fi
            curl -LsSf --retry 2 $curl_retry_all_errors --retry-delay 1 --connect-timeout 10 --max-time 120 \
                https://astral.sh/uv/install.sh | sh
            # Add to PATH for current session
            export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
            ;;
        windows)
            powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
            ;;
        *)
            error "Unsupported operating system"
            ;;
    esac
    
    if command_exists uv && uv_is_native_for_host; then
        success "uv installed successfully"
    else
        # Try to find it in common locations
        if [ -f "$HOME/.local/bin/uv" ] && uv_binary_is_acceptable "$HOME/.local/bin/uv"; then
            export PATH="$HOME/.local/bin:$PATH"
            success "uv installed successfully"
        elif [ -f "$HOME/.cargo/bin/uv" ] && uv_binary_is_acceptable "$HOME/.cargo/bin/uv"; then
            export PATH="$HOME/.cargo/bin:$PATH"
            success "uv installed successfully"
        elif [ -n "$existing_uv" ] && command_exists uv; then
            error "uv is installed, but it does not match this Mac's native architecture. Please install native arm64 uv or remove the x86_64 uv from PATH."
        else
            error "Failed to install uv. Please install it manually: https://docs.astral.sh/uv/"
        fi
    fi
}

# Install avibe-os using uv (uv auto-downloads Python if needed)
install_vibe() {
    info "Installing avibe-os (Python will be downloaded automatically if needed)..."
    local install_package_spec="${AVIBE_INSTALL_PACKAGE_SPEC:-${VIBE_INSTALL_PACKAGE_SPEC:-}}"

    VIBE_TOOL_BIN_DIR="$(choose_tool_bin_dir || true)"
    if [ -n "$VIBE_TOOL_BIN_DIR" ]; then
        info "Installing vibe command into $VIBE_TOOL_BIN_DIR"
    else
        warn "Could not find a writable directory in PATH; you may need a new shell before 'vibe' is available"
    fi

    UV_INSTALL_PYTHON_MIRROR="$(uv_python_install_mirror)"
    if [ -n "$UV_INSTALL_PYTHON_MIRROR" ]; then
        info "This uv downloads Python from GitHub; using Astral's CDN for any Python download instead"
    fi

    if install_vibe_from_sources "$install_package_spec"; then
        return 0
    fi
    # The mirror replaces uv's GitHub source instead of adding one, so when no
    # package source worked through it, try them all once more without it.
    if [ -n "$UV_INSTALL_PYTHON_MIRROR" ]; then
        UV_INSTALL_PYTHON_MIRROR=""
        info "Retrying without Astral's CDN for the Python download..."
        if install_vibe_from_sources "$install_package_spec"; then
            return 0
        fi
    fi

    if [ -n "$install_package_spec" ]; then
        error "Failed to install avibe-os from custom package spec: $install_package_spec"
    fi
    error "Failed to install avibe-os from all sources"
}

install_vibe_from_sources() {
    local install_package_spec="$1"

    if [ -n "$install_package_spec" ]; then
        install_package_candidate "$install_package_spec" || return 1
        success "avibe-os installed successfully (from custom package spec)"
        return 0
    fi

    # uv tool install will auto-download Python if not available
    # --force: reinstall even if already installed
    # --refresh: refresh package cache to get latest version
    # Try in order: PyPI -> China mirror (tsinghua) -> GitHub
    if install_package_candidate "$PACKAGE_NAME"; then
        success "avibe-os installed successfully (from PyPI)"
    elif install_package_candidate "$PACKAGE_NAME" --index-url https://pypi.tuna.tsinghua.edu.cn/simple; then
        success "avibe-os installed successfully (from Tsinghua mirror)"
    elif install_package_candidate "git+https://github.com/${REPO}.git"; then
        success "avibe-os installed successfully (from GitHub)"
    else
        return 1
    fi
}

# Verify installation
verify_installation() {
    info "Verifying installation..."
    
    # Refresh PATH
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    if [ -n "$VIBE_TOOL_BIN_DIR" ]; then
        export PATH="$VIBE_TOOL_BIN_DIR:$PATH"
    fi
    
    if command_exists vibe; then
        VIBE_BIN_PATH="$(command -v vibe)"
        success "vibe command is available"
        echo ""
        "$VIBE_BIN_PATH" --help 2>/dev/null || true
        return 0
    fi
    
    # Check common install locations
    local vibe_locations=(
        "$HOME/.local/bin/vibe"
        "$HOME/.cargo/bin/vibe"
    )
    
    for loc in "${vibe_locations[@]}"; do
        if [ -f "$loc" ]; then
            VIBE_BIN_PATH="$loc"
            warn "vibe installed at $loc but not in PATH"
            echo ""
            echo -e "${YELLOW}Add this to your shell config (.bashrc, .zshrc, etc.):${NC}"
            echo -e "  export PATH=\"$(dirname "$loc"):\$PATH\""
            echo ""
            return 0
        fi
    done
    
    error "Installation verification failed. vibe command not found."
}

prepare_show_runtime() {
    if [ "${VIBE_INSTALL_SKIP_SHOW_RUNTIME:-}" = "1" ]; then
        warn "Skipping Show Runtime preparation because VIBE_INSTALL_SKIP_SHOW_RUNTIME=1"
        return 0
    fi

    local vibe_cmd="${VIBE_BIN_PATH:-}"
    if [ -z "$vibe_cmd" ] && command_exists vibe; then
        vibe_cmd="$(command -v vibe)"
    fi
    if [ -z "$vibe_cmd" ] || [ ! -x "$vibe_cmd" ]; then
        warn "Show Runtime was not prepared because the vibe command is not available yet"
        return 0
    fi

    info "Preparing Show Runtime for this platform..."
    if "$vibe_cmd" runtime prepare --strict; then
        success "Show Runtime is ready"
    else
        warn "Show Runtime preparation failed; Avibe installation is still complete"
        warn "Run 'vibe runtime prepare' after fixing Node.js or network access"
    fi
}

pair_remote_access() {
    local pairing_key="${REMOTE_ACCESS_PAIRING_KEY:-}"
    local backend_url="${AVIBE_PAIRING_BACKEND_URL:-https://avibe.bot}"
    local vibe_cmd="${VIBE_BIN_PATH:-}"

    if [ -z "$pairing_key" ]; then
        return 0
    fi

    if [ -z "$vibe_cmd" ] || [ ! -x "$vibe_cmd" ]; then
        error "Cannot pair remote access because the vibe command is not available."
    fi

    info "Pairing this Avibe with avibe.bot..."
    "$vibe_cmd" remote pair "$pairing_key" --backend-url "$backend_url"
    REMOTE_ACCESS_PAIRED="1"
    success "Remote access paired"

    info "Starting Avibe service..."
    "$vibe_cmd" start
    success "Avibe service started"
}

launch_vibe() {
    if [ "${LAUNCH_AFTER_INSTALL:-}" != "1" ] || [ "${REMOTE_ACCESS_PAIRED:-}" = "1" ]; then
        return 0
    fi

    local vibe_cmd="${VIBE_BIN_PATH:-}"
    if [ -z "$vibe_cmd" ] || [ ! -x "$vibe_cmd" ]; then
        error "Cannot launch Avibe because the verified vibe command is not available."
    fi

    info "Launching Avibe with $vibe_cmd..."
    "$vibe_cmd"
    AVIBE_LAUNCHED="1"
    success "Avibe launched"
}

# Print the one-line uninstall command for this runtime home, with any options
# appended. Like the install command, it fails when the download does, and it
# repeats AVIBE_HOME when that chose the home.
uninstall_command() {
    local home_prefix=""
    if [ -n "${AVIBE_HOME:-}" ]; then
        home_prefix="AVIBE_HOME=$(printf '%q' "$AVIBE_RUNTIME_HOME") "
    fi
    printf "%sbash -o pipefail -c 'curl -fsSL %s | bash -s -- --uninstall%s'\n" \
        "$home_prefix" "$PUBLIC_INSTALL_SCRIPT_URL" "${*:+ $*}"
}

print_uninstall_commands() {
    echo "  $(uninstall_command)"
    echo "  Add --purge to also delete your data in $AVIBE_RUNTIME_HOME. This cannot be undone."
}

# The installer owns uninstall. It runs outside Avibe, so nothing it deletes is
# in use by itself, and it cannot rely on the installed Python, which may come
# from any earlier release or be broken. It therefore carries its own copy of
# the launcher rule in vibe.upgrade.managed_stable_launchers; tests pin the
# two to the same cases.

install_generations_root() {
    printf '%s\n' "$AVIBE_RUNTIME_HOME/runtime/install-generations"
}

# Print an absolute path with its deepest existing ancestor made physical, so a
# path through symlinks or "..", even into something already deleted, compares
# by prefix.
physical_path() {
    local path="$1"
    local rest=""
    local dir=""

    while [ -n "$path" ] && [ "$path" != "/" ]; do
        path="${path%/}"
        if dir="$(cd -P -- "$path" 2>/dev/null && pwd -P)"; then
            printf '%s%s\n' "${dir%/}" "$rest"
            return 0
        fi
        rest="/${path##*/}$rest"
        path="${path%/*}"
    done
    printf '%s\n' "${rest:-/}"
}

path_in_generation_root() {
    local root=""
    root="$(physical_path "$(install_generations_root)")"
    case "$(physical_path "$1")" in
        "$root"/?*) return 0 ;;
    esac
    return 1
}

# Whether deleting install-generations would break this launcher: it resolves
# into the root, or it is a hard link or copy of a generation's launcher.
# vibe.upgrade._launcher_generation recognizes the same shapes, but it moves a
# launcher only into a recognized installation, and a copy only with its
# marker. A launcher missing either would still break, so it goes too.
launcher_uses_install_generations() {
    local launcher="$1"
    local exported=""

    if path_in_generation_root "$(resolve_binary_path "$launcher")"; then
        return 0
    fi
    [ -f "$launcher" ] || return 1
    for exported in "$(install_generations_root)"/*/bin/vibe; do
        if [ -f "$exported" ] && { [ "$launcher" -ef "$exported" ] || cmp -s "$launcher" "$exported" 2>/dev/null; }; then
            return 0
        fi
    done
    return 1
}

# Print each distinct directory launcher discovery searches: PATH, uv's
# configured tool bin, then the installer's fixed locations.
launcher_search_dirs() {
    local dir=""
    local key=""
    local seen=":"
    local uv_tool_bin="${UV_TOOL_BIN_DIR:-}"
    local old_ifs="$IFS"
    local -a dirs=()

    IFS=":"
    for dir in $ORIGINAL_PATH; do
        dirs+=("$dir")
    done
    IFS="$old_ifs"
    dirs+=("${uv_tool_bin/#\~/$HOME}" "${INSTALLER_LAUNCHER_DIRS[@]}")

    for dir in "${dirs[@]}"; do
        dir="${dir%/}"
        is_absolute_dir "$dir" || continue
        key="$(physical_path "$dir")"
        case "$seen" in
            *":$key:"*) continue ;;
        esac
        seen="$seen$key:"
        printf '%s\n' "$dir"
    done
}

# Print each launcher this home's uninstall removes. Like the Python owner, it
# never counts one inside a uv tool environment or the generation root.
managed_launchers() {
    local dir=""
    local launcher=""

    while IFS= read -r dir; do
        launcher="$dir/vibe"
        case "$launcher" in
            */uv/tools/*) continue ;;
        esac
        if { [ -e "$launcher" ] || [ -L "$launcher" ]; } && ! path_in_generation_root "$launcher" &&
            launcher_uses_install_generations "$launcher"; then
            printf '%s\n' "$launcher"
        fi
    done < <(launcher_search_dirs)
}

# Print each launcher marker the uninstall leaves without a purpose: one beside
# a launcher it removes, or one naming a generation in the root it deletes.
stale_launcher_markers() {
    local dir=""
    local marker=""
    local marked=""
    local bom=$'\xef\xbb\xbf'

    while IFS= read -r dir; do
        marker="$dir/.vibe.avibe-generation"
        [ -f "$marker" ] || continue
        marked="$(cat "$marker" 2>/dev/null)"
        marked="${marked#"$bom"}"
        marked="${marked%$'\r'}"
        if printf '%s\n' "$@" | grep -Fqx -- "$dir/vibe" ||
            { is_absolute_dir "$marked" && path_in_generation_root "$marked"; }; then
            printf '%s\n' "$marker"
        fi
    done < <(launcher_search_dirs)
}

find_uv() {
    local candidate=""

    for candidate in "$(PATH="$ORIGINAL_PATH" command -v uv 2>/dev/null || true)" "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
        if [ -n "$candidate" ] && [ -x "$candidate" ]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

uv_tool_dir() {
    local uv="$1"
    local dir=""

    if [ -n "$uv" ]; then
        dir="$("$uv" tool dir 2>/dev/null || true)"
    fi
    printf '%s\n' "${dir:-${UV_TOOL_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/uv/tools}}"
}

# Whether uv's own uninstall of a tool removes only that tool's launchers. uv
# deletes every launcher path its receipt records, even one that another tool,
# such as a different `vibe`, has replaced since.
uv_tool_owns_its_launchers() {
    local environment="$1"
    local environment_root=""
    local path=""

    [ -f "$environment/uv-receipt.toml" ] || return 0
    environment_root="$(physical_path "$environment")"
    while IFS= read -r path; do
        [ -n "$path" ] || continue
        { [ -e "$path" ] || [ -L "$path" ]; } || continue
        case "$(physical_path "$(resolve_binary_path "$path")")" in
            "$environment_root"/*) continue ;;
        esac
        if cmp -s "$path" "$environment/bin/${path##*/}" 2>/dev/null; then
            continue
        fi
        return 1
    done < <(grep -oE "install-path = (\"[^\"]*\"|'[^']*')" "$environment/uv-receipt.toml" 2>/dev/null |
        sed -e "s/^install-path = [\"']//" -e "s/[\"']\$//")
    return 0
}

# Print "pid ppid command" for every process, or nothing when neither ps nor
# /proc can list them.
process_table() {
    local dir=""
    local key=""
    local value=""
    local ppid=""

    # Linux also names each executable, which a relative argv[0] hides.
    if [ -r /proc/self/cmdline ]; then
        for dir in /proc/[0-9]*; do
            [ -r "$dir/cmdline" ] || continue
            ppid=0
            while IFS=$'\t' read -r key value; do
                if [ "$key" = "PPid:" ]; then
                    ppid="$value"
                    break
                fi
            done 2>/dev/null < "$dir/status"
            value="$(readlink "$dir/exe" 2>/dev/null)"
            printf '%s %s %s%s\n' "${dir#/proc/}" "$ppid" "$(tr '\0' ' ' < "$dir/cmdline" 2>/dev/null)" \
                "${value:+ ($value)}"
        done
        return 0
    fi
    ps -eo pid=,ppid=,args= 2>/dev/null
}

# Print each live process that removal must wait for, as "pid command": one
# that runs from, or names on its command line, a path about to be deleted (the
# arguments). The uninstaller's own process tree never counts. Returns 2 when
# nothing can list processes.
blocking_processes() {
    local path=""
    local physical=""
    local paths=""
    local table=""

    for path in "$@"; do
        [ -n "$path" ] || continue
        paths="$paths$path"$'\n'
        physical="$(physical_path "$path")"
        if [ "$physical" != "$path" ]; then
            paths="$paths$physical"$'\n'
        fi
    done
    table="$(process_table)"
    if [ -z "$table" ]; then
        return 2
    fi
    printf '%s\n' "$table" | AVIBE_UNINSTALL_PATHS="$paths" AVIBE_UNINSTALL_SELF="$$" awk '
        BEGIN {
            count = split(ENVIRON["AVIBE_UNINSTALL_PATHS"], paths, "\n")
            self = ENVIRON["AVIBE_UNINSTALL_SELF"]
        }
        {
            pid = $1
            parent[pid] = $2
            $1 = ""; $2 = ""
            sub(/^ +/, "")
            command[pid] = $0
            order[++processes] = pid
        }
        END {
            for (pid = self; pid != "" && pid != "0" && !(pid in own); pid = parent[pid]) own[pid] = 1
            descendants[self] = 1
            do {
                grew = 0
                for (i = 1; i <= processes; i++) {
                    pid = order[i]
                    if (!(pid in descendants) && (parent[pid] in descendants)) { descendants[pid] = 1; grew = 1 }
                }
            } while (grew)
            for (i = 1; i <= processes; i++) {
                pid = order[i]
                if ((pid in own) || (pid in descendants)) continue
                text = command[pid] " "
                for (j = 1; j <= count; j++) {
                    if (paths[j] != "" && (index(text, paths[j] "/") || index(text, paths[j] " ") || index(text, paths[j] ")"))) {
                        print pid " " command[pid]
                        break
                    }
                }
            }
        }'
}

# Ask an installed Avibe to stop its service; the first that succeeds is enough.
stop_avibe_service() {
    local launcher=""
    local output=""

    for launcher in "$@"; do
        [ -x "$launcher" ] || continue
        if output="$(
            cd "$AVIBE_RUNTIME_HOME" 2>/dev/null || cd /
            env -u PYTHONPATH -u PYTHONHOME AVIBE_HOME="$AVIBE_RUNTIME_HOME" "$launcher" stop 2>&1
        )"; then
            return 0
        fi
        warn "'$launcher stop' failed${output:+: $(printf '%s\n' "$output" | tail -n 3)}"
    done
    return 1
}

# Whether this uninstall is for the default home. Global uv tool installs and
# a legacy ~/.vibe_remote directory belong to no chosen AVIBE_HOME, so only the
# default home's uninstall removes them.
uninstalling_default_home() {
    [ -z "${AVIBE_HOME:-}" ] || [ "$AVIBE_RUNTIME_HOME" = "$HOME/.avibe" ]
}

# Print this home's data paths: for the default home both default names, and
# for an explicit AVIBE_HOME that home alone. Only a directory or a link is a
# home; anything else under those names is not Avibe's.
avibe_data_directories() {
    local path=""
    local -a paths=("$AVIBE_RUNTIME_HOME")

    if uninstalling_default_home; then
        paths=("$HOME/.avibe" "$HOME/.vibe_remote")
    fi
    for path in "${paths[@]}"; do
        if [ -L "$path" ] || [ -d "$path" ]; then
            printf '%s\n' "$path"
        fi
    done
}

# Print a data path for the user, naming what a link points to.
describe_data_path() {
    if [ -L "$1" ]; then
        printf '%s -> %s\n' "$1" "$(readlink "$1" 2>/dev/null)"
    else
        printf '%s\n' "$1"
    fi
}

# Print the physical path of <base>[/<rel>] for deleting it, when that cannot
# go through a link: <base> itself and every step below it are real
# directories, whatever its ancestors are. Every directory the uninstaller
# deletes goes through here. Returns 2 with the resolved path when the spelled
# path goes through a link, and 1 when it names no directory.
verified_directory() {
    local base="$1"
    local rel="${2:-}"
    local logical=""
    local parent=""
    local actual=""

    logical="$(CDPATH= cd -L -- "$base" 2>/dev/null && pwd -L)" || return 1
    parent="$(CDPATH= cd -P -- "$(dirname -- "$logical")" 2>/dev/null && pwd -P)" || return 1
    actual="$(CDPATH= cd -P -- "$base${rel:+/$rel}" 2>/dev/null && pwd -P)" || return 1
    printf '%s\n' "$actual"
    [ "$actual" = "${parent%/}/$(basename -- "$logical")${rel:+/$rel}" ] || return 2
}

# Whether an explicit AVIBE_HOME shows it is an Avibe home. The installer and
# Avibe both create its runtime directory, and uninstall keeps it until a purge.
looks_like_avibe_home() {
    [ -d "$1/runtime" ] && [ ! -L "$1/runtime" ]
}

# Whether deleting a directory would delete the user's home or the filesystem.
path_holds_home() {
    local target=""

    target="$(physical_path "$1")"
    [ "$target" = "/" ] && return 0
    case "$(physical_path "$HOME")/" in
        "$target"/*) return 0 ;;
    esac
    return 1
}

confirm_purge() {
    local answer=""

    if [ "$ASSUME_YES" = "1" ]; then
        return 0
    fi
    # Read the terminal itself: under curl | bash, standard input is the script.
    if ! { true < /dev/tty; } 2>/dev/null; then
        warn "A purge needs confirmation, and no terminal is available. Nothing was removed."
        echo "  To confirm without a terminal, run:"
        echo "  $(uninstall_command --purge --yes)"
        return 1
    fi
    printf 'Delete all of this permanently? [y/N] ' > /dev/tty
    read -r answer < /dev/tty || answer=""
    case "$answer" in
        y|Y|yes|YES|Yes) return 0 ;;
    esac
    info "Purge cancelled. Nothing was removed."
    return 1
}

# Name a vibe left on PATH, other than a managed launcher (the arguments) that
# could not be removed, which is reported as such. This installer never uses
# pip, so an Avibe console script there came from a pip install, which only
# that pip can remove.
report_remaining_vibe() {
    local remaining=""
    local interpreter=""
    local launcher=""

    remaining="$(PATH="$ORIGINAL_PATH" command -v vibe 2>/dev/null || true)"
    if [ -z "$remaining" ] || { [ ! -e "$remaining" ] && [ ! -L "$remaining" ]; }; then
        return 0
    fi
    for launcher in "$@"; do
        if [ "$launcher" = "$remaining" ]; then
            return 0
        fi
    done
    if ! is_avibe_launcher "$remaining"; then
        info "Another vibe command remains at $remaining. This installer did not install it, so it was left in place."
        return 0
    fi
    interpreter="$(head -n 1 "$remaining" 2>/dev/null)"
    interpreter="${interpreter#\#!}"
    case "$interpreter" in
        /*" "*|"") interpreter="python3" ;;
        /*) ;;
        *) interpreter="python3" ;;
    esac
    info "An Avibe install that this installer did not make remains at $remaining, such as a pip install."
    echo "  Remove it with the Python that runs it: $interpreter -m pip uninstall avibe-os vibe-remote"
}

print_kept_data() {
    local item=""

    [ "$#" -gt 0 ] || return 0
    echo ""
    echo "Your data was kept in:"
    for item in "$@"; do
        echo "  $(describe_data_path "$item")"
    done
    echo "To delete it too (this cannot be undone), run:"
    echo "  $(uninstall_command --purge)"
}

uninstall_avibe() {
    # Every step reports its own failure; one failure must not hide the rest.
    set +e
    local root=""
    local item=""
    local uv=""
    local tool_dir=""
    local package=""
    local failed=0
    local -a launchers=()
    local -a markers=()
    local -a uv_tools=()
    local -a data=()
    local target=""
    local -a doomed_data=()
    local -a doomed_physical=()
    local -a unlinked_data=()
    local -a running=()

    root="$(install_generations_root)"
    while IFS= read -r item; do launchers+=("$item"); done < <(managed_launchers)
    while IFS= read -r item; do markers+=("$item"); done < <(stale_launcher_markers "${launchers[@]}")
    local -a other_uv_tools=()
    uv="$(find_uv || true)"
    tool_dir="$(uv_tool_dir "$uv")"
    for package in "$PACKAGE_NAME" vibe-remote; do
        if [ ! -d "$tool_dir/$package" ]; then
            continue
        elif uninstalling_default_home; then
            uv_tools+=("$package")
        else
            other_uv_tools+=("$package")
        fi
    done
    while IFS= read -r item; do data+=("$item"); done < <(avibe_data_directories)

    info "Uninstalling Avibe for $AVIBE_RUNTIME_HOME"
    for package in "${other_uv_tools[@]}"; do
        info "Leaving the uv tool install $package in place: it belongs to no AVIBE_HOME, so only an uninstall of the default home removes it"
    done
    if [ "${#launchers[@]}" -eq 0 ] && [ "${#markers[@]}" -eq 0 ] && [ "${#uv_tools[@]}" -eq 0 ] &&
        [ ! -e "$root" ] && [ ! -L "$root" ] && { [ "$PURGE_USER_DATA" != "1" ] || [ "${#data[@]}" -eq 0 ]; }; then
        info "No Avibe installation was found, so nothing was removed."
        report_remaining_vibe
        print_kept_data "${data[@]}"
        return 0
    fi
    if [ "$PURGE_USER_DATA" = "1" ]; then
        # A purge never deletes through a link. It deletes real directories
        # and removes a link itself, keeping whatever the link points to.
        for item in "${data[@]}"; do
            if [ -L "$item" ]; then
                unlinked_data+=("$item")
            elif ! uninstalling_default_home && ! looks_like_avibe_home "$item"; then
                warn "Refusing to purge $item: it has no runtime directory, so it does not look like an Avibe home. Nothing was removed."
                return 1
            elif path_holds_home "$item"; then
                warn "Refusing to purge $item: it holds your home directory. Nothing was removed."
                return 1
            elif ! target="$(verified_directory "$item")"; then
                warn "Refusing to purge $item: it resolves through a link to ${target:-nothing}. Nothing was removed."
                echo "  Name the home without the link, or delete what it names by hand if it is yours."
                return 1
            else
                doomed_data+=("$item")
                doomed_physical+=("$target")
            fi
        done
        local -a listing=("${launchers[@]}" "${markers[@]}")
        if [ -e "$root" ] || [ -L "$root" ]; then listing+=("$root"); fi
        for item in "${uv_tools[@]}"; do listing+=("$tool_dir/$item (uv tool $item)"); done
        for item in "${doomed_data[@]}"; do listing+=("$item    (your Avibe data)"); done
        echo ""
        if [ "${#listing[@]}" -gt 0 ]; then
            echo -e "${YELLOW}This permanently deletes:${NC}"
            for item in "${listing[@]}"; do echo "  $item"; done
        fi
        if [ "${#unlinked_data[@]}" -gt 0 ]; then
            echo -e "${YELLOW}It removes these links, not what they point to:${NC}"
            for item in "${unlinked_data[@]}"; do echo "  $(describe_data_path "$item")"; done
        fi
        echo ""
        confirm_purge || return 1
    fi

    # Nothing is removed while Avibe could still be using it: first ask it to
    # stop, then check that no process runs from or names what goes next.
    local stopped=0
    local -a stoppers=("${launchers[@]}")
    local -a doomed=("${launchers[@]}" "$root")
    for package in "${uv_tools[@]}"; do
        stoppers+=("$tool_dir/$package/bin/vibe")
        doomed+=("$tool_dir/$package")
    done
    doomed+=("${doomed_data[@]}")
    if stop_avibe_service "${stoppers[@]}"; then
        stopped=1
        success "Stopped the Avibe service"
    else
        # Without a confirmed stop, anything still using the home counts too.
        doomed+=("$AVIBE_RUNTIME_HOME")
    fi
    # Children of a stopped service can take a moment to exit.
    local attempt=""
    local blocking=""
    local listed=1
    for attempt in 1 2 3 4 5 6; do
        running=()
        blocking="$(blocking_processes "${doomed[@]}")" || listed=0
        while IFS= read -r item; do
            if [ -n "$item" ]; then running+=("$item"); fi
        done <<< "$blocking"
        if [ "${#running[@]}" -eq 0 ] || [ "$attempt" = 6 ]; then
            break
        fi
        sleep 1
    done
    if [ "${#running[@]}" -gt 0 ]; then
        warn "These processes still use what the uninstall would delete, so nothing was removed:"
        for item in "${running[@]}"; do
            echo "  pid ${item:0:200}"
        done
        echo "  Stop them, then run the uninstall again."
        return 1
    fi
    if [ "$listed" != "1" ]; then
        warn "No process list is available here, so the uninstall cannot confirm that nothing still uses what it deletes. Nothing was removed."
        return 1
    fi
    if [ "$stopped" != "1" ]; then
        if [ "${#stoppers[@]}" -gt 0 ]; then
            warn "The installed vibe could not stop the service. No process runs from or names what the uninstall deletes or this home."
        else
            info "No installed vibe could be asked to stop the service. No process runs from or names what the uninstall deletes or this home."
        fi
        echo "  Processes outside those paths, such as a managed OpenCode server, could not be confirmed stopped."
        echo "  To check, run: ps -eo pid,args | grep -E 'opencode serve|cloudflared'"
    fi

    for item in "${launchers[@]}" "${markers[@]}"; do
        if rm -f -- "$item" 2>/dev/null && [ ! -e "$item" ] && [ ! -L "$item" ]; then
            success "Removed $item"
        else
            warn "Could not remove $item"
            failed=1
        fi
    done
    if [ -e "$root" ] || [ -L "$root" ]; then
        # The root goes only when the home and every step below it are real
        # directories: never through a link, never any other kind of entry.
        # Nor while a managed launcher it serves is still there to dangle.
        local step=""
        local left=""
        for step in "$AVIBE_RUNTIME_HOME" "$AVIBE_RUNTIME_HOME/runtime" "$root"; do
            if [ -L "$step" ]; then
                left="$step is a link to $(readlink "$step" 2>/dev/null), and the uninstaller never deletes through a link"
                break
            elif [ ! -d "$step" ]; then
                left="$step is not a directory, and the uninstaller deletes only real directories"
                break
            fi
        done
        local physical_root=""
        if [ -z "$left" ] && ! physical_root="$(verified_directory "$AVIBE_RUNTIME_HOME" runtime/install-generations)"; then
            left="$root resolves through a link to ${physical_root:-nothing}, and the uninstaller never deletes through a link"
        fi
        for step in "${launchers[@]}"; do
            if [ -z "$left" ] && { [ -e "$step" ] || [ -L "$step" ]; }; then
                left="$step could not be removed, and removing the root would leave it dangling"
            fi
        done
        if [ -n "$left" ]; then
            warn "Left $root in place: $left."
            echo "  If it is yours, remove it with: rm -rf -- $(printf '%q' "$(physical_path "$root")")"
            failed=1
        elif rm -rf -- "$physical_root" 2>/dev/null && [ ! -e "$physical_root" ] && [ ! -L "$physical_root" ]; then
            success "Removed $root"
        else
            warn "Could not remove $root"
            failed=1
        fi
    fi
    for package in "${uv_tools[@]}"; do
        if [ -z "$uv" ]; then
            warn "uv was not found, so the uv tool install $package at $tool_dir/$package was left in place"
            failed=1
        elif ! uv_tool_owns_its_launchers "$tool_dir/$package"; then
            warn "Left the uv tool install $package in place: a launcher it records now belongs to another program, which 'uv tool uninstall $package' would delete"
            failed=1
        elif "$uv" tool uninstall "$package" >/dev/null 2>&1 || [ ! -d "$tool_dir/$package" ]; then
            success "Removed the uv tool install $package"
        else
            warn "Could not remove the uv tool install $package; run 'uv tool uninstall $package'"
            failed=1
        fi
    done

    if [ "$PURGE_USER_DATA" = "1" ]; then
        local -a kept_targets=()
        for item in "${unlinked_data[@]}"; do
            target="$(readlink "$item" 2>/dev/null)"
            case "$target" in
                /*) ;;
                *) target="$(dirname -- "$item")/$target" ;;
            esac
            kept_targets+=("$item -> $target")
            if rm -f -- "$item" 2>/dev/null && [ ! -e "$item" ] && [ ! -L "$item" ]; then
                success "Removed the link $item"
            else
                warn "Could not remove the link $item"
                failed=1
            fi
        done
        local index=0
        for index in "${!doomed_data[@]}"; do
            item="${doomed_physical[$index]}"
            if rm -rf -- "$item" 2>/dev/null && [ ! -e "$item" ] && [ ! -L "$item" ]; then
                success "Deleted ${doomed_data[$index]}"
            else
                warn "Could not delete ${doomed_data[$index]}"
                failed=1
            fi
        done
        for item in "${kept_targets[@]}"; do
            if [ -e "${item#* -> }" ] || [ -L "${item#* -> }" ]; then
                info "Kept ${item#* -> }, which ${item%% -> *} pointed to. Delete it by hand if it is yours."
            fi
        done
    fi

    report_remaining_vibe "${launchers[@]}"

    echo ""
    if [ "$failed" -ne 0 ]; then
        warn "Avibe was not completely removed. See the warnings above."
    elif [ "${#doomed_data[@]}" -gt 0 ]; then
        success "Avibe and its data were removed."
    else
        success "Avibe was removed."
    fi
    if [ "$PURGE_USER_DATA" != "1" ]; then
        print_kept_data "${data[@]}"
    fi
    return "$failed"
}

# Print next steps
print_next_steps() {
    local vibe_dir
    vibe_dir="$(dirname "${VIBE_BIN_PATH:-$HOME/.local/bin/vibe}")"

    echo ""
    echo -e "${GREEN}Installation complete!${NC}"
    echo ""
    echo -e "${BLUE}Next steps:${NC}"
    if [ "${REMOTE_ACCESS_PAIRED:-}" = "1" ]; then
        if is_vibe_immediately_available; then
            echo "  1. Open your avibe.bot URL"
            echo "  2. Sign in with the same avibe.bot account to continue"
            echo "  3. Optional: run 'vibe status' to check the local service"
        else
            echo "  1. Run 'export PATH=\"${vibe_dir}:\$PATH\"' in your shell"
            echo "  2. Open your avibe.bot URL"
            echo "  3. Sign in with the same avibe.bot account to continue"
            echo "  4. Optional: run 'vibe status' to check the local service"
        fi
        echo ""
        echo -e "${BLUE}Quick commands:${NC}"
        echo "  vibe          - Start Avibe (service + web UI)"
        echo "  vibe status   - Check service status"
        echo "  vibe remote   - Manage remote Web UI access"
        echo "  vibe stop     - Stop all services"
        echo "  vibe doctor   - Run diagnostics"
        echo ""
        echo -e "${BLUE}Uninstall:${NC}"
        print_uninstall_commands
        if ! is_vibe_immediately_available; then
            echo ""
            echo -e "${BLUE}If 'vibe' is not found in a new shell:${NC}"
            echo "  ${VIBE_BIN_PATH:-$HOME/.local/bin/vibe}"
        fi
        echo ""
        echo -e "${BLUE}Documentation:${NC}"
        echo "  https://github.com/${REPO}#readme"
        echo ""
        return
    fi

    if [ "${AVIBE_LAUNCHED:-}" = "1" ]; then
        if is_vibe_immediately_available; then
            echo "  1. Finish setup in the browser"
            echo "  2. Choose your chat platform and agent backend"
            echo "  3. Enable a channel or DM and send your first task"
            echo "  4. Optional: run 'vibe remote' to open the Web UI from another device"
        else
            echo "  1. Finish setup in the browser"
            echo "  2. Run 'export PATH=\"${vibe_dir}:\$PATH\"' for future terminal commands"
            echo "  3. Choose your chat platform and agent backend"
            echo "  4. Enable a channel or DM and send your first task"
            echo "  5. Optional: run 'vibe remote' to open the Web UI from another device"
        fi
    elif is_vibe_immediately_available; then
        echo "  1. Run 'vibe' to open the setup wizard"
        echo "  2. Choose your chat platform and agent backend"
        echo "  3. Enable a channel or DM and send your first task"
        echo "  4. Optional: run 'vibe remote' to open the Web UI from another device"
    else
        echo "  1. Run 'export PATH=\"${vibe_dir}:\$PATH\"' in your shell"
        echo "  2. Run 'vibe' to open the setup wizard"
        echo "  3. Choose your chat platform and agent backend"
        echo "  4. Enable a channel or DM and send your first task"
        echo "  5. Optional: run 'vibe remote' to open the Web UI from another device"
    fi
    echo ""
    echo -e "${BLUE}Quick commands:${NC}"
    echo "  vibe          - Start Avibe (service + web UI)"
    echo "  vibe remote   - Set up remote Web UI access"
    echo "  vibe status   - Check service status"
    echo "  vibe stop     - Stop all services"
    echo "  vibe doctor   - Run diagnostics"
    echo ""
    echo -e "${BLUE}Uninstall:${NC}"
    print_uninstall_commands
    echo ""
    echo -e "${BLUE}If 'vibe' is still not found:${NC}"
    echo "  ${VIBE_BIN_PATH:-$HOME/.local/bin/vibe}"
    echo ""
    echo -e "${BLUE}Documentation:${NC}"
    echo "  https://github.com/${REPO}#readme"
    echo ""
}

# Main installation flow
main() {
    print_banner
    REMOTE_ACCESS_PAIRING_KEY="${AVIBE_PAIRING_KEY:-}"
    unset AVIBE_PAIRING_KEY

    local arg
    local uninstall=""
    for arg in "$@"; do
        case "$arg" in
            --launch) LAUNCH_AFTER_INSTALL="1" ;;
            --uninstall) uninstall="1" ;;
            --purge) PURGE_USER_DATA="1" ;;
            --yes) ASSUME_YES="1" ;;
            *) error "Unsupported installer option: $arg" ;;
        esac
    done
    # Each option means one thing, so a purge is never implied by another.
    if [ "$uninstall" = "1" ] && [ "$LAUNCH_AFTER_INSTALL" = "1" ]; then
        error "--launch cannot be combined with --uninstall"
    fi
    if [ "$PURGE_USER_DATA" = "1" ] && [ "$uninstall" != "1" ]; then
        error "--purge deletes your data during an uninstall; use it with --uninstall"
    fi
    if [ "$ASSUME_YES" = "1" ] && [ "$PURGE_USER_DATA" != "1" ]; then
        error "--yes confirms a purge; use it with --uninstall --purge"
    fi
    if [ "$uninstall" = "1" ]; then
        uninstall_avibe
        exit $?
    fi

    local os
    os=$(detect_os)
    info "Detected OS: $os"
    
    # Install uv (which manages Python automatically)
    install_uv

    VIBE_TOOL_BIN_DIR="$(choose_tool_bin_dir || true)"
    if [ -n "$VIBE_TOOL_BIN_DIR" ]; then
        info "Using tool bin directory $VIBE_TOOL_BIN_DIR"
    else
        warn "Could not find a writable directory in PATH; falling back to ~/.local/bin where possible"
    fi

    # Node.js only powers the optional managed Show Page runtime. Never let it
    # block installation of the main avibe CLI/service.
    install_node_optional

    # Install avibe-os
    install_vibe
    
    # Verify
    verify_installation

    # Pre-download the current platform Show Runtime when possible. This is
    # intentionally warning-only so Node/network issues never break avibe.
    prepare_show_runtime

    # Optional avibe.bot one-step install + pair flow. This runs through the
    # verified absolute vibe path, not PATH, so it works even when the installer
    # put the command into a fallback tool bin directory.
    pair_remote_access

    # The public pipe-to-shell flow cannot update its parent shell's PATH.
    # Launch through the verified absolute path while this process still owns it.
    launch_vibe
    
    # Done
    print_next_steps
}

# Run main
main "$@"
