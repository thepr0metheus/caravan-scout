"""Env-driven constants, controller-contract placeholders, config defaults."""
from __future__ import annotations

import json
import os
import re
import signal
import shlex
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SERVER_CELLS_DIR = Path(os.environ.get("LAMA_CARAVAN_SERVER_CELLS_DIR", str(PROJECT_ROOT / "var/server-cells"))).expanduser()

# Variant 2: the controller (lama-caravan) is the single command builder. It
# sends payload["args"] with these placeholders for host-local paths; we swap
# them for the files we actually downloaded. Must match the controller's
# LLAMA_PATH_PLACEHOLDER_* constants in app.py.
LLAMA_PATH_PLACEHOLDER_MODEL = "{{MODEL_PATH}}"
LLAMA_PATH_PLACEHOLDER_MMPROJ = "{{MMPROJ_PATH}}"
LLAMA_PATH_PLACEHOLDER_SPEC = "{{SPEC_PATH}}"


DEFAULT_CONFIG = {
    "hostId": socket.gethostname().split(".")[0],
    "displayName": socket.gethostname().split(".")[0],
    "listenHost": "0.0.0.0",
    "listenPort": 8092,
    # The LAMA CARAVAN admin URL; empty = heartbeat stays off until configured.
    "controllerUrl": "",
    "heartbeatIntervalSeconds": 60,
    # Fleet registry (single source of truth for agent identity). When set, this host's
    # VM/docker agents are derived from <registryUrl>/api/agents instead of being hand-listed
    # in "agents" below. Empty string = legacy behaviour (static "agents" list only).
    "registryUrl": "",
    "agents": [],
    "applyCommand": "",
    "openclawConfigPath": "",
    "openclawAgentId": "openclaw",
    # llama-node: run a local llama-server on this host's GPU
    "llamaServerBin": "",      # path to llama-server binary
    "modelsBasePath": "",      # local cache dir for downloaded models (~/.llama-model-cache)
    "llamaNodeDefaultPort": 8180,
    "cleanOldModels": False,   # delete previously cached models when starting a new one
}


