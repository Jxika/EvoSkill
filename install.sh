#!/usr/bin/env bash
# ────────────────────────────────────────────────────────────────
# EvoSkill — one-command installer
#
# Installs Python 3.12+, uv (if needed), project dependencies,
# and optionally agent harness CLIs.
#
# Usage (from a cloned repo):
#   ./install.sh
#   ./install.sh --agents claude,opencode
#   ./install.sh --all-agents
#
# Usage (one-liner, clones repo first):
#   curl -fsSL https://raw.githubusercontent.com/sentient-agi/EvoSkill/main/install.sh | bash
#   curl -fsSL ... | bash -s -- --all-agents
# ────────────────────────────────────────────────────────────────
set -euo pipefail

REPO_URL="${EVOSKILL_REPO_URL:-https://github.com/sentient-agi/EvoSkill.git}"
REPO_BRANCH="${EVOSKILL_BRANCH:-main}"
INSTALL_DIR="${EVOSKILL_INSTALL_DIR:-}"
MIN_PYTHON="3.12"
ALL_AGENTS=(claude opencode codex goose)

# ── helpers ───────────────────────────────────────────────────
bold()  { printf "\033[1m%s\033[0m" "$1"; }
green() { printf "\033[32m%s\033[0m" "$1"; }
yellow(){ printf "\033[33m%s\033[0m" "$1"; }
red()   { printf "\033[31m%s\033[0m" "$1"; }
dim()   { printf "\033[2m%s\033[0m" "$1"; }

info()  { printf "  $(green '✓') %s\n" "$1"; }
warn()  { printf "  $(yellow '!') %s\n" "$1"; }
fail()  { printf "  $(red '✗') %s\n" "$1"; exit 1; }

usage() {
    cat <<'EOF'
EvoSkill installer

Usage:
  ./install.sh [options]

Options:
  --agents LIST     Comma-separated agent CLIs to install
                    (claude, opencode, codex, goose)
  --all-agents      Install all supported agent CLIs
  --no-agents       Skip agent CLI installation (default)
  --pip             Use pip instead of uv
  --dir PATH        Clone/install into PATH (remote install only)
  --help            Show this help

Examples:
  ./install.sh
  ./install.sh --agents claude
  ./install.sh --all-agents
  curl -fsSL https://raw.githubusercontent.com/sentient-agi/EvoSkill/main/install.sh | bash
EOF
}

detect_repo_root() {
    local script_dir=""
    if [[ -n "${BASH_SOURCE[0]:-}" && "${BASH_SOURCE[0]}" != bash && "${BASH_SOURCE[0]}" != /dev/fd/* ]]; then
        script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
        if [[ -f "$script_dir/pyproject.toml" ]] && grep -q 'name = "evoskill"' "$script_dir/pyproject.toml" 2>/dev/null; then
            echo "$script_dir"
            return 0
        fi
    fi
    if [[ -f "pyproject.toml" ]] && grep -q 'name = "evoskill"' pyproject.toml 2>/dev/null; then
        pwd
        return 0
    fi
    return 1
}

ensure_repo() {
    if root="$(detect_repo_root)"; then
        echo "$root"
        return 0
    fi

    local target="${INSTALL_DIR:-$(pwd)/EvoSkill}"
    printf "\n  %s\n\n" "$(bold 'Cloning EvoSkill repository...')"

    if [[ -d "$target/.git" ]]; then
        info "Repository already exists at $target"
        git -C "$target" fetch --depth 1 origin "$REPO_BRANCH"
        git -C "$target" checkout "$REPO_BRANCH"
        git -C "$target" pull --ff-only origin "$REPO_BRANCH" || true
    else
        git clone --depth 1 --branch "$REPO_BRANCH" "$REPO_URL" "$target"
    fi

    echo "$target"
}

python_version_is_312() {
    local py="$1"
    "$py" - <<'PY' 2>/dev/null
import sys
raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)
PY
}

find_python_312() {
    local candidate
    for candidate in "${EVOSKILL_PYTHON:-}" python3.12; do
        [[ -n "$candidate" ]] || continue
        if command -v "$candidate" &>/dev/null && python_version_is_312 "$candidate"; then
            echo "$candidate"
            return 0
        fi
    done
    return 1
}

try_install_python_312() {
    printf "\n  %s\n" "$(bold 'Attempting to install Python 3.12...')"

    if [[ "$(uname -s)" == "Darwin" ]] && command -v brew &>/dev/null; then
        if brew install python@3.12; then
            if [[ -x "$(brew --prefix python@3.12)/bin/python3.12" ]]; then
                info "Installed Python via Homebrew"
                echo "$(brew --prefix python@3.12)/bin/python3.12"
                return 0
            fi
        else
            warn "Homebrew install of python@3.12 failed — skipped"
        fi
    fi

    if command -v apt-get &>/dev/null; then
        warn "python3.12 is not in default Ubuntu repos (deadsnakes PPA is often required)"
        if sudo apt-get update && sudo apt-get install -y python3.12 python3.12-venv python3-pip; then
            if command -v python3.12 &>/dev/null; then
                info "Installed Python via apt"
                echo "python3.12"
                return 0
            fi
        else
            warn "apt install of python3.12 failed — skipped"
        fi
    fi

    return 1
}

ensure_python_for_uv() {
    ensure_uv
    printf "\n  %s\n" "$(bold 'Installing Python 3.12 via uv...')"
    uv python install "$MIN_PYTHON"
    uv python pin "$MIN_PYTHON"
    local py
    py="$(uv python find "$MIN_PYTHON")"
    info "Python pinned to 3.12: $($py --version 2>&1)"
}

ensure_python_for_pip() {
    local py=""
    if py="$(find_python_312)"; then
        info "Python found: $($py --version 2>&1)"
        echo "$py"
        return 0
    fi

    if py="$(try_install_python_312)" && python_version_is_312 "$py"; then
        info "Python ready: $($py --version 2>&1)"
        echo "$py"
        return 0
    fi

    fail "Python 3.12 not found for --pip mode. Omit --pip to use uv (recommended), or install Python 3.12 manually: https://www.python.org/downloads/"
}

ensure_uv() {
    if command -v uv &>/dev/null; then
        info "uv found: $(uv --version)"
        return 0
    fi

    printf "\n  %s\n" "$(bold 'Installing uv...')"
    curl -LsSf https://astral.sh/uv/install.sh | sh

    if [[ -f "${HOME}/.local/bin/uv" ]]; then
        export PATH="${HOME}/.local/bin:${PATH}"
    elif [[ -f "${HOME}/.cargo/bin/uv" ]]; then
        export PATH="${HOME}/.cargo/bin:${PATH}"
    fi

    command -v uv &>/dev/null || fail "uv installation failed"
    info "uv installed: $(uv --version)"
}

ensure_pip_venv() {
    local repo_root="$1"
    local py="$2"
    local venv="$repo_root/.venv"

    if [[ ! -x "$venv/bin/python" ]]; then
        "$py" -m venv "$venv"
        info "Created virtualenv at $venv"
    fi

    echo "$venv/bin/python"
}

install_project() {
    local repo_root="$1"
    local use_pip="$2"

    cd "$repo_root"
    printf "\n  %s\n" "$(bold 'Installing EvoSkill dependencies...')"

    if [[ "$use_pip" == "1" ]]; then
        local py venv_py
        py="$(ensure_python_for_pip)"
        venv_py="$(ensure_pip_venv "$repo_root" "$py")"
        "$venv_py" -m pip install --upgrade pip
        "$venv_py" -m pip install -e .
        info "Installed with pip into .venv"
    else
        ensure_python_for_uv
        uv sync --python "$MIN_PYTHON"
        info "Installed with uv sync (Python 3.12)"
    fi

    if command -v evoskill &>/dev/null; then
        info "evoskill CLI ready: $(command -v evoskill)"
    elif [[ -x "$repo_root/.venv/bin/evoskill" ]]; then
        info "evoskill CLI ready: $repo_root/.venv/bin/evoskill"
        warn "Add the virtualenv to your PATH:"
        printf "    %s\n" "$(dim "export PATH=\"$repo_root/.venv/bin:\$PATH\"")"
    elif [[ -d "$repo_root/.venv/bin" ]]; then
        warn "Activate the virtualenv to use evoskill:"
        printf "    %s\n" "$(dim "source $repo_root/.venv/bin/activate")"
    fi
}

install_agent_brew() {
    local agent="$1"
    case "$agent" in
        claude)
            brew install --cask claude-code
            ;;
        opencode)
            brew install opencode
            ;;
        codex)
            brew install --cask codex
            ;;
        goose)
            brew install block-goose-cli
            ;;
        *)
            warn "Unknown agent: $agent (supported: ${ALL_AGENTS[*]})"
            return 1
            ;;
    esac
}

agent_binary() {
    case "$1" in
        claude) echo "claude" ;;
        opencode) echo "opencode" ;;
        codex) echo "codex" ;;
        goose) echo "goose" ;;
        *) echo "" ;;
    esac
}

install_agents() {
    local agents_csv="$1"
    [[ -n "$agents_csv" ]] || return 0

    printf "\n  %s\n" "$(bold 'Installing agent CLIs...')"

    local agent
    IFS=',' read -r -a selected <<< "$agents_csv"
    for agent in "${selected[@]}"; do
        agent="${agent// /}"
        [[ -n "$agent" ]] || continue

        local bin
        bin="$(agent_binary "$agent")"
        if [[ -n "$bin" ]] && command -v "$bin" &>/dev/null; then
            info "$agent already installed ($bin)"
            continue
        fi

        if [[ "$(uname -s)" == "Darwin" ]] && command -v brew &>/dev/null; then
            if install_agent_brew "$agent"; then
                info "Installed $agent"
            else
                warn "Failed to install $agent via Homebrew — continuing with remaining agents"
            fi
        else
            warn "Skipping $agent — auto-install is macOS/Homebrew only."
            case "$agent" in
                claude)  printf "    %s\n" "$(dim 'https://docs.anthropic.com/en/docs/claude-code/setup')" ;;
                opencode) printf "    %s\n" "$(dim 'https://opencode.ai/docs/installation')" ;;
                codex)   printf "    %s\n" "$(dim 'https://github.com/openai/codex')" ;;
                goose)   printf "    %s\n" "$(dim 'https://block.github.io/goose/docs/getting-started/installation')" ;;
            esac
        fi
    done
}

print_next_steps() {
    printf "\n  %s\n\n" "$(bold 'Setup complete!')"
    cat <<'EOF'
  Next steps:

    1. Set an API key for your chosen harness:
         export ANTHROPIC_API_KEY=your-key-here      # Claude Code
         export OPENAI_API_KEY=your-key-here         # Codex CLI
         export OPENROUTER_API_KEY=your-key-here     # OpenCode / Goose / OpenHands

    2. Initialize a project inside any git repository:
         evoskill init

    3. Run the self-improvement loop:
         evoskill run

  Harbor benchmarks are included with the Python install.
  For Harbor task execution, Docker must be installed locally.

  Docs: README.md
EOF
}

# ── parse args ────────────────────────────────────────────────
AGENTS=""
USE_PIP=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --agents)
            [[ $# -ge 2 ]] || fail "--agents requires a value"
            AGENTS="$2"
            shift 2
            ;;
        --all-agents)
            AGENTS="$(IFS=,; echo "${ALL_AGENTS[*]}")"
            shift
            ;;
        --no-agents)
            AGENTS=""
            shift
            ;;
        --pip)
            USE_PIP=1
            shift
            ;;
        --dir)
            [[ $# -ge 2 ]] || fail "--dir requires a value"
            INSTALL_DIR="$2"
            shift 2
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            fail "Unknown option: $1 (try --help)"
            ;;
    esac
done

# ── main ──────────────────────────────────────────────────────
printf "\n%s\n" "$(bold 'EvoSkill Installer')"

REPO_ROOT="$(ensure_repo)"
install_project "$REPO_ROOT" "$USE_PIP"
install_agents "$AGENTS"
print_next_steps
