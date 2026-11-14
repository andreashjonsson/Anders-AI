"""Local Chatterbox Multilingual TTS server for Jarvis.

Runs in its own venv (~/.openjarvis/chatterbox/.venv, torch 2.6) because
Chatterbox pins dependencies that conflict with OpenJarvis.

  GET  /health                     -> {"ready": true}
  POST /tts  {"text": "...", "lang": "sv"|"en", ...}  -> audio/wav

Listens on 127.0.0.1 only. Env:
  JARVIS_TTS_PORT       (default 8765)
  JARVIS_VOICE_REF      optional reference clip (wav, ~10 s) to clone a voice
  JARVIS_TTS_DEVICE     cuda (default) or cpu
"""

from __future__ import annotations

import io
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import warnings

warnings.filterwarnings("ignore")

import soundfile as sf  # noqa: E402
import torch  # noqa: E402
from chatterbox.mtl_tts import ChatterboxMultilingualTTS  # noqa: E402

PORT = int(os.environ.get("JARVIS_TTS_PORT", "8765"))
DEVICE = os.environ.get("JARVIS_TTS_DEVICE", "cuda" if torch.cuda.is_available() else "cpu")
VOICE_REF = os.environ.get("JARVIS_VOICE_REF", "").strip() or None

_lock = threading.Lock()
print(f"[chatterbox] loading on {DEVICE} ...", flush=True)
MODEL = ChatterboxMultilingualTTS.from_pretrained(device=DEVICE)
if VOICE_REF:
    MODEL.prepare_conditionals(VOICE_REF)
    print(f"[chatterbox] voice reference: {VOICE_REF}", flush=True)
with _lock:
    MODEL.generate("Hej.", language_id="sv")  # warm-up
print(f"[chatterbox] ready on 127.0.0.1:{PORT}", flush=True)


def _post_process(y, *, warmth_db: float = 0.0, pitch_st: float = 0.0):
    """Optional voice shaping after synthesis.

    warmth_db: low-shelf boost at 180 Hz (+ a gentle -warmth/2 dB dip at 3.5 kHz)
               -> fuller, "deeper"-sounding voice without changing pitch. No artifacts.
    pitch_st:  real pitch shift in semitones via the WORLD vocoder (formants kept).
               Lowers pitch precisely but adds some buzz (est. quality 3.7 -> 2.4).
    """
    import numpy as np
    import torchaudio.functional as AF

    sr = MODEL.sr
    if abs(pitch_st) > 1e-3:
        import pyworld as pw

        y64 = y.astype("float64")
        f0, sp, ap = pw.wav2world(y64, sr)
        y = pw.synthesize(f0 * 2 ** (pitch_st / 12), sp, ap, sr).astype("float32")
    if abs(warmth_db) > 1e-3:
        w = torch.from_numpy(y.astype("float32")).unsqueeze(0)
        w = AF.bass_biquad(w, sr, gain=warmth_db, central_freq=180.0, Q=0.7)
        w = AF.equalizer_biquad(w, sr, 3500.0, -warmth_db / 2, 1.0)
        y = w.squeeze(0).numpy()
    peak = float(np.abs(y).max()) if y.size else 0.0
    return y / (peak / 0.95) if peak > 0.95 else y


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # quiet
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._send(200, b'{"ready": true}', "application/json")
        else:
            self._send(404, b"{}", "application/json")

    def do_POST(self):
        if self.path != "/tts":
            return self._send(404, b"{}", "application/json")
        try:
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            text = str(req["text"])[:1000]
            lang = req.get("lang", "sv")
            kwargs = {
                "exaggeration": float(req.get("exaggeration", 0.5)),
                "cfg_weight": float(req.get("cfg_weight", 0.5)),
                "temperature": float(req.get("temperature", 0.8)),
            }
            with _lock, torch.inference_mode():
                wav = MODEL.generate(text, language_id=lang, **kwargs)
            audio = _post_process(
                wav.squeeze(0).cpu().numpy(),
                warmth_db=float(req.get("warmth", 0.0)),
                pitch_st=float(req.get("pitch", 0.0)),
            )
            buf = io.BytesIO()
            sf.write(buf, audio, MODEL.sr, format="WAV", subtype="PCM_16")
            self._send(200, buf.getvalue(), "audio/wav")
        except Exception as exc:  # noqa: BLE001
            self._send(500, json.dumps({"error": repr(exc)}).encode(), "application/json")


if __name__ == "__main__":
    try:
        ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)
