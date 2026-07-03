#!/usr/bin/env bash
# install.sh — install caravan-scout and optionally llama.cpp.
#
# Usage:
#   ./scripts/install.sh [options]
#
# Options:
#   --skip-llama          skip llama.cpp install/build
#   --skip-whisper        skip faster-whisper ASR server provisioning
#   --llama-tag <tag>     pin a specific llama.cpp release tag, e.g. b9101
#                         (default: latest release)
#   --admin-url <url>     Llama.cpp Easy Admin URL for model downloads
#                         (required unless ADMIN_URL is set in the env)
#
# The script is idempotent — safe to re-run.

set -euo pipefail

ADMIN_URL="${ADMIN_URL:-}"
LLAMA_TAG="${LLAMA_TAG:-}"          # empty = use latest release
LLAMA_DIR="${HOME}/llama.cpp"
SKIP_LLAMA=0
SKIP_WHISPER=0
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[install]${NC} $*"; }
warn()  { echo -e "${YELLOW}[warn]${NC}   $*"; }
err()   { echo -e "${RED}[error]${NC}  $*" >&2; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-llama)   SKIP_LLAMA=1 ;;
    --skip-whisper) SKIP_WHISPER=1 ;;
    --llama-tag)   LLAMA_TAG="$2"; shift ;;
    --admin-url)   ADMIN_URL="$2"; shift ;;
    *) err "unknown arg: $1"; exit 1 ;;
  esac
  shift
done

if [ -z "$ADMIN_URL" ]; then
  err "--admin-url is required (e.g. --admin-url http://<controller-ip>:8090)"
  exit 1
fi

have()       { command -v "$1" &>/dev/null; }
on_linux()   { [[ "$(uname -s)" == "Linux" ]]; }
on_macos()   { [[ "$(uname -s)" == "Darwin" ]]; }
is_nvidia()  { lspci 2>/dev/null | grep -qi "nvidia"; }
nproc_safe() { nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 4; }

# ── 1. python3 ────────────────────────────────────────────────────────────────
info "Checking Python 3..."
if ! have python3; then
  on_linux && sudo apt-get update -qq && sudo apt-get install -y python3 || \
    { err "python3 not found — install it first"; exit 1; }
fi
python3 --version

# ── 2. route-agent service ────────────────────────────────────────────────────
info "Setting up caravan-scout..."
INSTALL_DIR="${HOME}/projects/caravan-scout"

if [[ "$(realpath "$REPO_DIR")" != "$(realpath "$INSTALL_DIR" 2>/dev/null || echo __none__)" ]]; then
  if [[ -d "$INSTALL_DIR/.git" ]]; then
    info "  updating $INSTALL_DIR"
    git -C "$INSTALL_DIR" pull
  else
    info "  copying to $INSTALL_DIR"
    mkdir -p "$(dirname "$INSTALL_DIR")"
    cp -a "$REPO_DIR" "$INSTALL_DIR"
  fi
fi

# Initialise config.json from best-matching example if not present
if [[ ! -f "$INSTALL_DIR/config.json" ]]; then
  EXAMPLE="$INSTALL_DIR/examples/config.$(hostname -s).json"
  if [[ -f "$EXAMPLE" ]]; then
    cp "$EXAMPLE" "$INSTALL_DIR/config.json"
    info "  created config.json from $EXAMPLE"
  else
    cp "$INSTALL_DIR/examples/config.example.json" "$INSTALL_DIR/config.json"
    warn "  no host-specific example found — copied config.example.json as template"
    warn "  edit $INSTALL_DIR/config.json before starting"
  fi
fi

# Patch adminUrl into config.json
python3 - "$INSTALL_DIR/config.json" "$ADMIN_URL" <<'PYEOF'
import sys, json
path, url = sys.argv[1], sys.argv[2]
c = json.load(open(path))
changed = False
if c.get("controllerUrl", "").rstrip("/") != url.rstrip("/"):
    c["controllerUrl"] = url.rstrip("/")
    changed = True
if changed:
    json.dump(c, open(path, "w"), indent=2, ensure_ascii=False)
    print(f"  set controllerUrl={url}")
PYEOF

# systemd user service (Linux)
if on_linux; then
  info "Installing systemd user service..."
  mkdir -p "${HOME}/.config/systemd/user"
  sed "s|%h/projects/caravan-scout|${INSTALL_DIR}|g" \
    "$INSTALL_DIR/systemd/caravan-scout.service" \
    > "${HOME}/.config/systemd/user/caravan-scout.service"
  systemctl --user daemon-reload
  systemctl --user enable caravan-scout.service
  info "  systemd service enabled"
fi

# launchd agent (macOS)
if on_macos; then
  info "Installing launchd agent..."
  PLIST="${HOME}/Library/LaunchAgents/com.caravan-scout.plist"
  sed "s|/Users/[a-z]*/projects|${HOME}/projects|g" \
    "$INSTALL_DIR/launchd/com.caravan-scout.plist" > "$PLIST"
  launchctl unload "$PLIST" 2>/dev/null || true
  launchctl load "$PLIST"
  info "  launchd agent loaded"
fi

# ── 3. llama.cpp ──────────────────────────────────────────────────────────────
# Build + config wiring lives in install-llama.sh so it can also be re-run
# standalone on an already-provisioned agent.
if [[ $SKIP_LLAMA -eq 1 ]]; then
  warn "Skipping llama.cpp (--skip-llama)"
elif on_macos; then
  warn "macOS: install via Homebrew:  brew install llama.cpp"
  warn "Then set llamaServerBin in config.json and skip NFS (models on local disk)."
elif on_linux; then
  # shellcheck source=scripts/install-llama.sh
  source "$INSTALL_DIR/scripts/install-llama.sh"
  install_llama "$INSTALL_DIR" "$LLAMA_DIR" "$LLAMA_TAG" 0
fi  # on_linux

# ── 3b. whisper (faster-whisper ASR server) ───────────────────────────────────
# Provisioned via install-whisper.sh so this host can run a whisper "command"
# cell straight from CARAVAN with no manual setup. GPU-gated inside the function.
if [[ $SKIP_WHISPER -eq 1 ]]; then
  warn "Skipping whisper (--skip-whisper)"
elif on_linux; then
  # shellcheck source=scripts/install-whisper.sh
  source "$INSTALL_DIR/scripts/install-whisper.sh"
  install_whisper "$INSTALL_DIR" "${HOME}/wsr"
fi

# ── 4. done ───────────────────────────────────────────────────────────────────
echo ""
info "━━━ Install complete ━━━"
echo "  Agent dir   : $INSTALL_DIR"
echo "  Config      : $INSTALL_DIR/config.json"
echo "  Admin URL   : $ADMIN_URL"
[[ -f "${LLAMA_DIR}/build/bin/llama-server" ]] && \
  echo "  llama-server: ${LLAMA_DIR}/build/bin/llama-server"
[[ -f "${HOME}/run_whisper.sh" ]] && \
  echo "  whisper     : bash ~/run_whisper.sh \$PORT large-v3  (CARAVAN command cell)"
echo ""
echo "  Start the agent:"
if on_linux; then
  echo "    systemctl --user start caravan-scout.service"
  echo "    systemctl --user status caravan-scout.service"
else
  echo "    launchctl start com.caravan-scout"
fi
echo ""
echo "  Once running, open Llama.cpp Easy Admin → Topology"
echo "  This host will appear automatically and you can launch"
echo "  llama-server on its GPU from the UI."
