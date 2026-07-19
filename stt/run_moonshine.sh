#!/bin/bash
# Moonshine STT cell launcher (self-installing, CARAVAN command-cell ready).
# Usage: run_moonshine.sh [port] [language] [--install-only]
#        language: en | es | zh | ja | ko | vi | uk | ar   (NO Russian)
# CPU-only — safe to run on a box whose GPUs are busy with LLMs.
#     bash run_moonshine.sh 8025 en
set -e
PORT="${1:-8025}"
LANG_="${2:-en}"
VENV="${VENV:-$HOME/moonshine-venv}"

if [ ! -x "$VENV/bin/python" ]; then
  echo "moonshine: creating venv $VENV …"
  python3 -m venv "$VENV"
  "$VENV/bin/pip" -q install -U pip
  "$VENV/bin/pip" install moonshine-voice
fi

if [ "${3:-}" = "--install-only" ]; then
  # prewarm: also fetch the model so the first cell start is instant
  "$VENV/bin/python" - <<PY
from moonshine_voice import get_model_for_language
print(get_model_for_language("${LANG_}")[0])
PY
  echo "moonshine: venv ready at $VENV (install-only)"
  exit 0
fi

HERE="$(cd "$(dirname "$0")" && pwd)"
SERVER="$HERE/moonshine_server.py"
[ -f "$SERVER" ] || SERVER="$HOME/moonshine_server.py"
exec "$VENV/bin/python" "$SERVER" "$PORT" "$LANG_"
