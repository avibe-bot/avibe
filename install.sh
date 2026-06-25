#!/usr/bin/env bash
# Avibe Installation Script
# Usage: curl -fsSL https://avibe.bot/install.sh | bash
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
ORIGINAL_PATH="$PATH"
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
        */.venv/bin|*/venv/bin|*/env/bin|*/.pyenv/shims|*/.pyenv/versions/*/bin|*/.local/share/mise/installs/*/bin|*/.mise/installs/*/bin)
            return 0
            ;;
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

choose_tool_bin_dir() {
    local dir
    local fallback_sbin_dir=""

    local old_ifs="$IFS"
    IFS=":"
    for dir in $ORIGINAL_PATH; do
        if [ -n "$dir" ] && is_absolute_dir "$dir" && ! is_transient_bin_dir "$dir" && ensure_writable_dir "$dir"; then
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

    local preferred_dirs=(
        "$HOME/.local/bin"
        "$HOME/bin"
        "/usr/local/bin"
        "/opt/homebrew/bin"
    )

    for dir in "${preferred_dirs[@]}"; do
        if is_absolute_dir "$dir" && ensure_writable_dir "$dir"; then
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
    if [ -n "$VIBE_TOOL_BIN_DIR" ]; then
        UV_TOOL_BIN_DIR="$VIBE_TOOL_BIN_DIR" uv tool install "$@"
    else
        uv tool install "$@"
    fi
}

install_package_candidate() {
    local package_spec="$1"
    shift

    if [ "$package_spec" = "$PACKAGE_NAME" ]; then
        uv_tool_install "$package_spec" --force --refresh "$@" 2>/dev/null
    else
        uv_tool_install "$package_spec" --force "$@" 2>/dev/null
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
            curl -LsSf https://astral.sh/uv/install.sh | sh
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

    if [ -n "$install_package_spec" ]; then
        if install_package_candidate "$install_package_spec"; then
            success "avibe-os installed successfully (from custom package spec)"
            return 0
        fi

        error "Failed to install avibe-os from custom package spec: $install_package_spec"
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
        error "Failed to install avibe-os from all sources"
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
        echo "  uv tool uninstall avibe-os       # current uv install"
        echo "  uv tool uninstall vibe-remote    # legacy uv install"
        echo "  pip uninstall avibe-os vibe-remote"
        echo "  rm -rf ~/.avibe ~/.vibe_remote   # remove config and data"
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

    if is_vibe_immediately_available; then
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
    echo "  uv tool uninstall avibe-os       # current uv install"
    echo "  uv tool uninstall vibe-remote    # legacy uv install"
    echo "  pip uninstall avibe-os vibe-remote"
    echo "  rm -rf ~/.avibe ~/.vibe_remote   # remove config and data"
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
    
    # Done
    print_next_steps
}

# Run main
main "$@"
