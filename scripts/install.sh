#!/usr/bin/env bash
set -euo pipefail

REPO="https://github.com/Kushagrabainsla/north"
NORTH_HOME="${NORTH_HOME:-$HOME/.north}"

# ── Colours ───────────────────────────────────────────────────────────────────

bold="\033[1m"
green="\033[1;32m"
yellow="\033[1;33m"
red="\033[1;31m"
reset="\033[0m"

info()    { echo -e "  ${bold}$*${reset}"; }
success() { echo -e "  ${green}✓${reset}  $*"; }
warn()    { echo -e "  ${yellow}!${reset}  $*"; }
fail()    { echo -e "  ${red}✗${reset}  $*"; exit 1; }

echo ""
echo -e "${bold}★ north - Personal Life Operating System${reset}"
echo ""

# ── 1. Check for Docker (optional - only needed for --docker server mode) ─────

if command -v docker &>/dev/null; then
    success "Docker found ($(docker --version | cut -d' ' -f3 | tr -d ',')) - available for --docker mode"
else
    info "Docker not found - north will run in local mode (recommended for personal use)"
fi

# ── 2. Install uv if needed ───────────────────────────────────────────────────

if ! command -v uv &>/dev/null; then
    info "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    success "uv installed"
else
    success "uv found ($(uv --version))"
fi

# ── 3. Install north CLI ──────────────────────────────────────────────────────

info "Installing north CLI from GitHub..."
uv tool install "git+$REPO" --force-reinstall -q
success "north CLI installed"

# Make sure the uv tool bin is on PATH for the rest of this session
UV_TOOL_BIN="$(uv tool dir)/bin"
if [[ ":$PATH:" != *":$UV_TOOL_BIN:"* ]]; then
    export PATH="$UV_TOOL_BIN:$PATH"
fi

# ── 4. Set up ~/.north/ ───────────────────────────────────────────────────────

mkdir -p "$NORTH_HOME"

# ── 5. OpenRouter API key ─────────────────────────────────────────────────────

ENV_FILE="$NORTH_HOME/.env"

if grep -q "NORTH_OPENROUTER_API_KEY" "$ENV_FILE" 2>/dev/null; then
    success "OpenRouter API key already configured"
else
    echo ""
    warn "You need an OpenRouter API key for LLM inference and voice."
    warn "Get one free at: https://openrouter.ai/keys"
    echo ""
    read -rp "  Enter your OpenRouter API key (or press Enter to skip): " api_key </dev/tty
    if [[ -n "$api_key" ]]; then
        echo "NORTH_OPENROUTER_API_KEY=$api_key" >> "$ENV_FILE"
        success "API key saved to $ENV_FILE"
    else
        warn "Skipped - add it later: echo 'NORTH_OPENROUTER_API_KEY=sk-or-...' >> $ENV_FILE"
    fi
fi

# ── 6. Shell PATH reminder ────────────────────────────────────────────────────

PROFILE_FILES=("$HOME/.bashrc" "$HOME/.zshrc" "$HOME/.profile")
PATH_LINE='export PATH="$HOME/.local/bin:$PATH"'
PATH_CONFIGURED=false

for f in "${PROFILE_FILES[@]}"; do
    if [[ -f "$f" ]] && grep -q '.local/bin' "$f" 2>/dev/null; then
        PATH_CONFIGURED=true
        break
    fi
done

# ── Done ──────────────────────────────────────────────────────────────────────

echo ""
echo -e "${green}${bold}  north is ready.${reset}"
echo ""

if ! command -v north &>/dev/null && [[ "$PATH_CONFIGURED" == false ]]; then
    warn "Add uv's bin directory to your PATH, then run north start:"
    echo ""
    echo "    export PATH=\"\$HOME/.local/bin:\$PATH\"  # add to your shell profile"
    echo "    north start"
else
    echo "    north start"
fi

echo ""
