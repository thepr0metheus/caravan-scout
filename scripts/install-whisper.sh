#!/usr/bin/env bash
# install-whisper.sh — provision a faster-whisper ASR server on this host so the
# route-agent can start it as a CARAVAN "command" cell:
#
#     COMMAND:     bash ~/run_whisper.sh $PORT large-v3
#     HEALTH_PATH: /health
#
# It creates a dedicated venv (~/wsr) with faster-whisper + the bundled
# cuDNN/cuBLAS wheels, and drops ~/whisper_server.py + ~/run_whisper.sh in $HOME
# (run_whisper.sh puts those CUDA libs on LD_LIBRARY_PATH — CTranslate2 segfaults
# without them). The whisper model itself is auto-downloaded from HuggingFace on
# the first server start (no ggml file to fetch by hand).
#
# Can be run standalone on an already-provisioned agent:
#   ./scripts/install-whisper.sh [--venv DIR] [--install-dir DIR]
#
# Or sourced by install.sh, which then calls install_whisper().
#
# Idempotent: reuses the venv, upgrades deps and re-copies the server files.

# ── shared helpers (guard with declare -F; names like `info` collide with GNU
#    texinfo, so `command -v info` would wrongly succeed) ──────────────────────
__iw_RED='\033[0;31m'; __iw_GREEN='\033[0;32m'; __iw_YELLOW='\033[1;33m'; __iw_NC='\033[0m'
declare -F info       >/dev/null 2>&1 || info()       { echo -e "${__iw_GREEN}[install]${__iw_NC} $*"; }
declare -F warn       >/dev/null 2>&1 || warn()       { echo -e "${__iw_YELLOW}[warn]${__iw_NC}   $*"; }
declare -F err        >/dev/null 2>&1 || err()        { echo -e "${__iw_RED}[error]${__iw_NC}  $*" >&2; }
declare -F have       >/dev/null 2>&1 || have()       { command -v "$1" &>/dev/null; }
declare -F is_nvidia  >/dev/null 2>&1 || is_nvidia()  { lspci 2>/dev/null | grep -qi "nvidia"; }

# install_whisper [install_dir] [venv_dir]
install_whisper() {
  local install_dir="${1:-${HOME}/projects/caravan-scout}"
  local venv="${2:-${HOME}/wsr}"
  local src="${install_dir}/whisper"

  if [[ "$(uname -s)" == "Darwin" ]]; then
    warn "macOS: the faster-whisper GPU server is Linux/NVIDIA only — skipping whisper."
    return 0
  fi
  if ! is_nvidia; then
    warn "No NVIDIA GPU detected (lspci) — skipping whisper (faster-whisper) provisioning."
    return 0
  fi
  if [[ ! -f "${src}/whisper_server.py" || ! -f "${src}/run_whisper.sh" ]]; then
    err "bundled whisper files missing under ${src} — cannot provision."
    return 1
  fi

  info "NVIDIA GPU detected — provisioning faster-whisper ASR server (venv: ${venv})"

  have python3 || { err "python3 required"; return 1; }
  if ! python3 -c "import venv" 2>/dev/null; then
    info "Installing python3-venv..."
    sudo apt-get update -qq && sudo apt-get install -y python3-venv \
      || warn "could not install python3-venv — install it manually"
  fi

  # ── venv + deps ──────────────────────────────────────────────────────────────
  if [[ ! -x "${venv}/bin/python" ]]; then
    info "Creating venv at ${venv}..."
    python3 -m venv "$venv"
  fi
  "${venv}/bin/python" -m pip install -q --upgrade pip
  info "Installing faster-whisper + CUDA libs (cuDNN/cuBLAS) — a few hundred MB..."
  "${venv}/bin/python" -m pip install --upgrade \
      faster-whisper nvidia-cudnn-cu12 nvidia-cublas-cu12
  if ! "${venv}/bin/python" -c "import faster_whisper" 2>/dev/null; then
    err "faster-whisper failed to import in ${venv} — check pip output above."
    return 1
  fi

  # ── server files into $HOME (run_whisper.sh expects ~/whisper_server.py) ─────
  install -m 0644 "${src}/whisper_server.py" "${HOME}/whisper_server.py"
  install -m 0755 "${src}/run_whisper.sh"    "${HOME}/run_whisper.sh"
  info "  installed ~/whisper_server.py + ~/run_whisper.sh"

  # No ufw rule here on purpose: the 8001–8099 inference range is already open on
  # lab hosts, and firewall changes are managed separately.

  info "whisper ready. Test:   VENV=${venv} bash ~/run_whisper.sh 8004 large-v3"
  info "  CARAVAN command cell → COMMAND: bash ~/run_whisper.sh \$PORT large-v3   HEALTH_PATH: /health"
  info "  (the model ~large-v3 auto-downloads from HuggingFace on first start)"
  return 0   # explicit: this is sourced into install.sh under `set -e`
}

# ── standalone entrypoint ─────────────────────────────────────────────────────
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  set -euo pipefail
  IW_INSTALL_DIR="${HOME}/projects/caravan-scout"
  IW_VENV="${HOME}/wsr"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --install-dir) IW_INSTALL_DIR="$2"; shift ;;
      --venv)        IW_VENV="$2"; shift ;;
      -h|--help)     grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
      *) err "unknown arg: $1"; exit 1 ;;
    esac
    shift
  done
  install_whisper "$IW_INSTALL_DIR" "$IW_VENV"
fi
