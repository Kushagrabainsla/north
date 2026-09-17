---
name: interactive-human-wizard
description: "Use when guiding a human through manual steps the agent cannot execute directly (e.g., credential provisioning, OAuth authorization, hardware pairing, or one-off cutovers) via an interactive bash wizard."
domains:
  - engineering
  - general
---
# Interactive Human Wizard

> **When a task requires human-only actions (credentials, 2FA, OAuth, physical devices), generate an interactive, self-verifying shell wizard rather than dumping long text instructions.**

## Use this when
- Provisioning API keys, OAuth tokens, or third-party web credentials.
- Setting up physical hardware, local IoT smart-home devices, or OS permissions.
- Guiding the user through complex one-off migrations or environment setups.

## Do NOT use for
- Automated tasks that North agents or CLI commands can execute directly (use `adding-a-north-tool` or direct tool calls).
- Simple single-command environment setups (use regular scripts or documentation).

---

## Core Characteristics of a Great Wizard

1. **Stage-by-Stage Progression**: Displays clear stage counters (e.g., `[Stage 2/5]`).
2. **Automated URL Opening**: Opens browser pages directly to the exact setup or credentials portal.
3. **Secret Masking**: Uses silent/masked input (`read -s` or `read -rp`) for sensitive API tokens.
4. **Idempotent Updates**: Safely updates or appends to `.env` files without duplicate keys or corrupting existing values.
5. **Immediate Verification Gates**: Checks that each entered secret or setting actually works (e.g., pinging an API endpoint) *before* advancing to the next stage.
6. **Graceful Cancellation & Resumption**: Safe to exit (`Ctrl+C`) and re-run without losing progress.

---

## Procedure

### 1. Scope Human-Only Prerequisites
Analyze configuration requirements:
- Inspect `.env.example`, API schemas, and service documentation.
- List all manual values the human must produce (keys, webhook URLs, tokens, OAuth client IDs).

### 2. Design the Verification Checks
For each required secret, identify a lightweight verification probe:
- OpenRouter / OpenAI: `curl https://openrouter.ai/api/v1/models -H "Authorization: Bearer $KEY"`
- GitHub token: `gh auth status` or `curl https://api.github.com/user -H "Authorization: token $KEY"`
- Database connection: `psql "$DB_URL" -c "\q"` or python connection ping.

### 3. Generate the Interactive Wizard Script
Create a structured bash script (typically under `scripts/setup_<service>.sh` or a temporary wizard path):

```bash
#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="${1:-$HOME/.north/.env}"
mkdir -p "$(dirname "$ENV_FILE")"
touch "$ENV_FILE"

bold="\033[1m"; green="\033[1;32m"; yellow="\033[1;33m"; red="\033[1;31m"; reset="\033[0m"

info()    { echo -e "  ${bold}$*${reset}"; }
success() { echo -e "  ${green}✓${reset}  $*"; }
warn()    { echo -e "  ${yellow}!${reset}  $*"; }
fail()    { echo -e "  ${red}✗${reset}  $*"; exit 1; }

upsert_env() {
    local key="$1" val="$2"
    if grep -q "^${key}=" "$ENV_FILE" 2>/dev/null; then
        # Update existing key
        sed -i.bak "s|^${key}=.*|${key}=${val}|" "$ENV_FILE" && rm -f "${ENV_FILE}.bak"
    else
        echo "${key}=${val}" >> "$ENV_FILE"
    fi
}

echo -e "\n${bold}★ North Setup Wizard — Service Configuration${reset}\n"

# Stage 1: API Key Entry & Verification
info "[Stage 1/2] Service Authentication"
open_url="https://provider.example.com/api-keys"
echo "Opening browser to create your key: $open_url"
python3 -m webbrowser "$open_url" 2>/dev/null || open "$open_url" 2>/dev/null || true

read -rsp "  Paste your API key: " api_key </dev/tty
echo ""

if [[ -z "$api_key" ]]; then
    fail "API key cannot be empty."
fi

# Verification Gate
info "Verifying API key..."
if curl -sf -H "Authorization: Bearer $api_key" https://provider.example.com/v1/ping >/dev/null; then
    upsert_env "SERVICE_API_KEY" "$api_key"
    success "API key verified and saved to $ENV_FILE"
else
    fail "API key verification failed. Please check the key and try again."
fi

success "Wizard complete! North is ready to use this service."
```

### 4. Provide Execution Instructions
Instruct the user clearly on how to run the wizard:
```bash
chmod +x scripts/setup_<service>.sh
./scripts/setup_<service>.sh
```

### 5. Confirm System Readiness
After wizard completion, run North's health checks or test suite to verify full end-to-end integration.

---

## Red Flags
- Dumping raw secrets in command arguments where they leak into shell history (`history | grep ...`).
- Overwriting `.env` files destructively instead of idempotent upserts.
- Moving to stage N+1 when stage N's credentials failed verification.
- Leaving confusing error messages without troubleshooting links.

## Verification Checklist
- [ ] Wizard prompts human inputs securely
- [ ] Automatically opens required browser portals
- [ ] Each stage includes an active verification probe
- [ ] Configuration is written idempotently to `.env`
