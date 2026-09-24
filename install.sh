#!/usr/bin/env bash
# install.sh — put caravan-scout on this machine: the sidecar that lends the
# machine's GPU or CPU to a LAMA CARAVAN controller.
#
#   ./install.sh [--skip-llama] [--skip-whisper] [--llama-tag <tag>]
#
# Run it on the machine you are adding, from a clone of this repository. It
# installs what the machine needs — llama.cpp built with CUDA where there is
# an NVIDIA card, the whisper speech server there too — starts the scout as a
# service, waits until it answers and prints the address to enter on the
# controller: Model servers → ＋ Add scout. The controller pairs the scout
# itself and hands it its fleet token: there is nothing to edit here.
#
# Idempotent — safe to re-run. uninstall.sh takes the scout off again.

set -euo pipefail

LLAMA_TAG="${LLAMA_TAG:-}"          # empty = the latest llama.cpp release
LLAMA_DIR="${HOME}/llama.cpp"
SKIP_LLAMA=0
SKIP_WHISPER=0
# The scout runs where it was cloned: this directory is the service's home.
INSTALL_DIR="$(cd "$(dirname "$0")" && pwd)"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BOLD='\033[1m'; NC='\033[0m'
info()  { echo -e "${GREEN}[install]${NC} $*"; }
warn()  { echo -e "${YELLOW}[warn]${NC}   $*"; }
err()   { echo -e "${RED}[error]${NC}  $*" >&2; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-llama)   SKIP_LLAMA=1 ;;
    --skip-whisper) SKIP_WHISPER=1 ;;
    --llama-tag)    LLAMA_TAG="$2"; shift ;;
    -h|--help)      sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) err "unknown arg: $1 (see ./install.sh --help)"; exit 1 ;;
  esac
  shift
done

have()       { command -v "$1" &>/dev/null; }
on_linux()   { [[ "$(uname -s)" == "Linux" ]]; }
on_macos()   { [[ "$(uname -s)" == "Darwin" ]]; }
nproc_safe() { nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 4; }

PORT="$(python3 -c "import json,sys
try: print(int(json.load(open(sys.argv[1])).get('listenPort') or 8092))
except Exception: print(8092)" "$INSTALL_DIR/config.json" 2>/dev/null || echo 8092)"

# ── 1. python3 ────────────────────────────────────────────────────────────────
info "Checking Python 3..."
if ! have python3; then
  if on_linux; then
    sudo apt-get update -qq && sudo apt-get install -y python3
  else
    err "python3 not found — install it first"; exit 1
  fi
fi
python3 --version

# ── 2. llama.cpp (Linux + NVIDIA) ─────────────────────────────────────────────
# Build + config wiring live in scripts/install-llama.sh, which can also be
# re-run on its own. It writes llamaServerBin and modelsBasePath into
# config.json; the scout needs nothing else there until a controller pairs it.
if [[ $SKIP_LLAMA -eq 1 ]]; then
  warn "Skipping llama.cpp (--skip-llama)"
elif on_macos; then
  warn "macOS: llama.cpp comes from Homebrew — brew install llama.cpp — and its"
  warn "path goes into config.json as llamaServerBin. Command cells need nothing."
elif on_linux; then
  # shellcheck source=scripts/install-llama.sh
  source "$INSTALL_DIR/scripts/install-llama.sh"
  install_llama "$INSTALL_DIR" "$LLAMA_DIR" "$LLAMA_TAG" 0
fi

# ── 3. whisper (faster-whisper ASR server) ────────────────────────────────────
# So the machine can run a whisper command cell with no setup of its own.
# GPU-gated inside the script.
if [[ $SKIP_WHISPER -eq 1 ]]; then
  warn "Skipping whisper (--skip-whisper)"
elif on_linux; then
  # Run, never source: install-whisper.sh is a script of its own that exits
  # early on a host with no NVIDIA GPU. Sourced, that `exit 0` ended this
  # installer before its summary; on a GPU host the function called after it,
  # install_whisper, never existed, and `set -e` failed the install at the end.
  VENV="${HOME}/wsr" bash "$INSTALL_DIR/scripts/install-whisper.sh" \
    || warn "whisper provisioning failed — see the output above"
fi

# ── 4. the service: installed, started, and kept after logout ────────────────
if on_linux; then
  info "Installing the systemd user service..."
  mkdir -p "${HOME}/.config/systemd/user"
  sed "s|%h/projects/caravan-scout|${INSTALL_DIR}|g" \
    "$INSTALL_DIR/systemd/caravan-scout.service" \
    > "${HOME}/.config/systemd/user/caravan-scout.service"
  systemctl --user daemon-reload
  systemctl --user enable caravan-scout.service >/dev/null
  systemctl --user restart caravan-scout.service
  # Without lingering, a user service stops at logout and does not start at
  # boot — a machine lent to the fleet would drop off it overnight.
  if [[ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != "yes" ]]; then
    loginctl enable-linger "$USER" 2>/dev/null \
      || sudo loginctl enable-linger "$USER" \
      || warn "could not keep the scout running after logout — run: sudo loginctl enable-linger $USER"
  fi
elif on_macos; then
  info "Installing the launchd agent..."
  PLIST="${HOME}/Library/LaunchAgents/com.caravan-scout.plist"
  mkdir -p "$(dirname "$PLIST")"
  sed "s|/Users/user/projects/caravan-scout|${INSTALL_DIR}|g" \
    "$INSTALL_DIR/launchd/com.caravan-scout.plist" > "$PLIST"
  launchctl unload "$PLIST" 2>/dev/null || true
  launchctl load "$PLIST"
fi

# ── 5. the firewall lets the controller in ───────────────────────────────────
# The controller reaches the scout on its port and each cell on its own port.
# The scout's port is opened here. The cells' ports are the controller's to
# pick (its cell range, 22001–22999 unless changed there); a whole range is
# left to the operator, who can open it to the controller's address alone.
CELL_RANGE="22001:22999"
CELL_HINT="the cells need the controller's cell ports (22001–22999 unless changed there): sudo ufw allow from <controller-ip> to any port ${CELL_RANGE} proto tcp"
if on_linux && have ufw && sudo -n true 2>/dev/null \
   && sudo ufw status 2>/dev/null | grep -q "Status: active"; then
  if ! sudo ufw status 2>/dev/null | grep -qE "^${PORT}(/tcp)?\b"; then
    info "Opening port ${PORT}/tcp in ufw for the controller..."
    sudo ufw allow "${PORT}/tcp" comment 'caravan-scout' >/dev/null \
      || warn "could not open ${PORT}/tcp — run: sudo ufw allow ${PORT}/tcp"
  fi
  sudo ufw status 2>/dev/null | grep -qE "^${CELL_RANGE%%:*}:" || warn "ufw is active here, and ${CELL_HINT}"
elif on_linux && have ufw; then
  warn "If ufw is active here, the controller needs port ${PORT}: sudo ufw allow ${PORT}/tcp"
  warn "and ${CELL_HINT}"
fi

# ── 6. wait until the scout answers, then say where it is ────────────────────
info "Waiting for the scout to answer on :${PORT}..."
ANSWER=""
for _ in $(seq 1 30); do
  ANSWER="$(curl -fsS "http://127.0.0.1:${PORT}/api/pairing" 2>/dev/null || true)"
  [[ -n "$ANSWER" ]] && break
  sleep 1
done
if [[ -z "$ANSWER" ]]; then
  err "the scout did not answer on :${PORT} within 30 s"
  if on_linux; then
    err "see: journalctl --user -u caravan-scout.service -n 50"
  else
    err "see: ${INSTALL_DIR}/caravan-scout.err.log"
  fi
  exit 1
fi
read -r ADDR PAIRED < <(python3 -c "import json,sys
d = json.loads(sys.argv[1])
print(d.get('ip') or '?', 'yes' if d.get('controllerUrl') else 'no')" "$ANSWER")

echo ""
info "━━━ caravan-scout is running ━━━"
echo -e "  Address : ${BOLD}${ADDR}${NC}"
echo -e "  Port    : ${BOLD}${PORT}${NC}"
[[ -f "${LLAMA_DIR}/build/bin/llama-server" ]] && \
  echo "  llama-server: ${LLAMA_DIR}/build/bin/llama-server"
echo ""
if [[ "$PAIRED" == "yes" ]]; then
  echo "  Already paired with a controller — it keeps reporting there."
else
  echo "  Now open the LAMA CARAVAN board: Model servers → ＋ Add scout,"
  echo -e "  and enter ${BOLD}${ADDR}${NC} with port ${BOLD}${PORT}${NC}. That is all."
fi
echo ""
