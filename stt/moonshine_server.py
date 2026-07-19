#!/usr/bin/env python3
"""Moonshine v2 STT cell — a CPU-only recognizer server for LAMA CARAVAN.

Same HTTP contract as the whisper cell, so the voice app's LAN discovery finds it
and classifies it as an ASR (①) processor with a proper name:

    GET  /health                     200 {"status":"ok","model":"moonshine-<lang>"}
                                     | 503 {"status":"loading"} while warming
    POST /v1/audio/transcriptions    multipart: file=wav [, language]
                                     -> {"text": "..."}

No GPU needed — Moonshine runs on the CPU (medium-streaming-en beats Whisper
Large V3 WER at 250M params; ~0.7 s for a 6 s clip on a laptop core).
Languages: en es zh ja ko vi uk ar (NO Russian — keep whisper for RU).
Licensing: EN model is MIT; the others are Moonshine Community License
(free below $1M/yr revenue, registration required, «Powered by Moonshine AI»).

Usage: moonshine_server.py [port] [language]        # defaults: 8025 en
Setup: see run_moonshine.sh (creates ~/moonshine venv, installs the package).
"""
from __future__ import annotations

import io
import json
import re
import sys
import threading
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8025
LANG = (sys.argv[2] if len(sys.argv) > 2 else "en").lower()

_state = {"ready": False, "error": ""}
_lock = threading.Lock()
_transcriber = None


def _log(msg):
    sys.stderr.write(msg + "\n")
    sys.stderr.flush()


def _load():
    global _transcriber
    try:
        from moonshine_voice import get_model_for_language
        from moonshine_voice.transcriber import Transcriber
        path, arch = get_model_for_language(LANG)   # downloads on first run
        _transcriber = Transcriber(path, arch)
        _state["ready"] = True
        _log(f"moonshine[{LANG}] ready on :{PORT} ({path})")
    except Exception as exc:  # noqa: BLE001
        _state["error"] = str(exc)
        _log(f"moonshine[{LANG}]: load failed: {exc}")


def _wav_to_floats(data: bytes):
    """wav bytes -> (list[float] mono, sample_rate). 16-bit PCM expected."""
    wf = wave.open(io.BytesIO(data), "rb")
    sr = wf.getframerate()
    ch = wf.getnchannels()
    raw = wf.readframes(wf.getnframes())
    wf.close()
    import array
    a = array.array("h")
    a.frombytes(raw)
    if ch > 1:                                   # downmix to mono
        a = a[::ch]
    return [s / 32768.0 for s in a], sr


def _extract_file(body: bytes, ctype: str):
    m = re.search(r'boundary="?([^";,]+)"?', ctype or "")
    if not m:
        return None
    for part in body.split(b"--" + m.group(1).encode()):
        if b'name="file"' in part.split(b"\r\n\r\n", 1)[0]:
            payload = part.split(b"\r\n\r\n", 1)
            if len(payload) == 2:
                return payload[1].rstrip(b"\r\n-")
    return None


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if _state["ready"]:
            # the "model" field is what the voice app's discovery keys on (kind=asr)
            self._send(200, {"status": "ok", "model": f"moonshine-{LANG}"})
        elif _state["error"]:
            self._send(500, {"status": "error", "error": _state["error"]})
        else:
            self._send(503, {"status": "loading"})

    def do_POST(self):
        if not _state["ready"] or _transcriber is None:
            self._send(503, {"error": "model loading"})
            return
        ln = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(ln)
        wav = _extract_file(body, self.headers.get("Content-Type", ""))
        if not wav:
            self._send(400, {"error": "need multipart file=wav"})
            return
        try:
            audio, sr = _wav_to_floats(wav)
            with _lock:                          # one CPU inference at a time
                res = _transcriber.transcribe_without_streaming(audio, sr)
            text = " ".join(
                l.text for l in (getattr(res, "lines", None) or [])).strip()
            self._send(200, {"text": text})
        except Exception as e:  # noqa: BLE001
            _log(f"transcribe error: {e}")
            self._send(500, {"error": str(e)})


if __name__ == "__main__":
    threading.Thread(target=_load, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
