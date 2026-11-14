"""Bilingual (Swedish/English) TTS backend for OpenJarvis.

Local add-on, kept outside the OpenJarvis source tree so updates don't wipe it.
Loaded at Python startup via ``jarvis_bilingual.pth`` in the venv's site-packages.

Each reply is cleaned of Markdown, its language is guessed from the text, and it
is spoken by:
  * Swedish -> Piper (``sv_SE-<voice>-medium``), CPU, ~0.2 s per sentence
  * English -> Kokoro (``speech.voice_id``, e.g. bm_george)

Config (``~/.openjarvis/config.toml``):
  [speech]
  tts_backend = "bilingual"
  voice_id = "bm_george"            # English Kokoro voice
Env overrides:
  JARVIS_SV_VOICE   Piper Swedish voice: nst (male), alma, lisa   (default: nst)
  JARVIS_TTS_LANG   force "sv" or "en" instead of auto-detecting
"""

from __future__ import annotations

import io
import os
import re
import threading
import wave
from pathlib import Path
from typing import Any, List

from openjarvis.core.registry import TTSRegistry
from openjarvis.speech.tts import TTSBackend, TTSResult

PIPER_DIR = Path(os.environ.get("OPENJARVIS_HOME", Path.home() / ".openjarvis")) / "voices" / "piper"
DEFAULT_SV_VOICE = "nst"
DEFAULT_EN_VOICE = "bm_george"

_SV_WORDS = set(
    "och är att det jag inte på för med som en ett du vi har kan vill ska den de "
    "om men så hur vad här där nu också eller från till av mig dig sig min din "
    "hej tack ja nej kanske bara mycket inga något några skulle kommer gör".split()
)
_EN_WORDS = set(
    "the and is are to of you it that this for with on as be have can will would "
    "what how here there now also or from my your me not just very some hello "
    "thanks yes no maybe do does i".split()
)


def detect_language(text: str) -> str:
    """Return "sv" or "en" for *text* (Swedish wins ties — the user is Swedish)."""
    forced = os.environ.get("JARVIS_TTS_LANG", "").strip().lower()
    if forced in ("sv", "en"):
        return forced
    lowered = text.lower()
    words = re.findall(r"[a-zåäöé']+", lowered)
    sv = sum(w in _SV_WORDS for w in words) + 2 * len(re.findall(r"[åäö]", lowered))
    en = sum(w in _EN_WORDS for w in words)
    return "en" if en > sv else "sv"


_UNITS = {
    "sv": [(r"°\s*C\b", " grader"), (r"°", " grader"), (r"(\d)\s*%", r"\1 procent"), (r"\bm/s\b", "meter per sekund"),
           (r"\bkm/h\b", "kilometer i timmen"), (r"\bmm\b", "millimeter"), (r"\bkr\b", "kronor"), (r"\bkl\.?\s", "klockan "),
           (r"\bca\.?\s", "cirka "), (r"\bt\.ex\.?", "till exempel"), (r"\bbl\.a\.?", "bland annat")],
    "en": [(r"°\s*C\b", " degrees"), (r"°", " degrees"), (r"(\d)\s*%", r"\1 percent"), (r"\bm/s\b", "meters per second"),
           (r"\bkm/h\b", "kilometers per hour"), (r"\bmm\b", "millimeters")],
}


def _sv_number(tok: str) -> str:
    from num2words import num2words

    neg = tok.startswith(("-", "−"))
    tok = tok.lstrip("-−")
    if len(tok) > 1 and tok[0] == "0" and tok.isdigit():  # phone numbers etc: digit by digit
        return " ".join(num2words(int(d), lang="sv") for d in tok)
    if "," in tok or "." in tok:
        whole, frac = re.split(r"[,.]", tok, maxsplit=1)
        words = f"{num2words(int(whole or 0), lang='sv')} komma {' '.join(num2words(int(d), lang='sv') for d in frac)}"
    else:
        words = num2words(int(tok), lang="sv")
    return ("minus " if neg else "") + words


def speakable_numbers(text: str, lang: str) -> str:
    """'21°C', '448,5', 'kl 10:30', '10.00–16.30' -> words the voice can read correctly."""
    for pat, rep in _UNITS.get(lang, []):
        text = re.sub(pat, rep, text)
    if lang != "sv":
        return text
    try:
        def clock(m):
            h, mi = int(m.group(1)), int(m.group(2))
            return _sv_number(str(h)) + ("" if mi == 0 else " och " + _sv_number(str(mi)))
        # phone numbers (start with 0, groups of digits): digit by digit
        text = re.sub(r"(?<!\d)0\d{1,3}(?:[\s‑-]?\d{2,3}){2,4}(?!\d)",
                      lambda m: " ".join(_sv_number(d) for d in re.sub(r"\D", "", m.group(0))), text)
        text = re.sub(r"(?<=\d)\s*[-–‑]\s*(?=\d)", " till ", text)                         # ranges 10–16
        text = re.sub(r"(?<=[a-zåäö])\s*[–‑]\s*(?=[a-zåäö])", " till ", text)              # måndag–fredag
        text = re.sub(r"\b([01]?\d|2[0-3])[:.]([0-5]\d)\b(?!\d)", clock, text)            # times
        text = re.sub(r"(?<!\w)(\d{1,3}(?: \d{3})+)(?!\d)", lambda m: m.group(1).replace(" ", ""), text)  # 10 000
        text = re.sub(r"(?<![\w,.])[-−]?\d+(?:[,.]\d+)?(?![\w])", lambda m: _sv_number(m.group(0)), text)
    except Exception:
        pass
    return text


def clean_for_speech(text: str, lang: str | None = None) -> str:
    """Strip Markdown and other things that sound bad when read aloud."""
    lang = lang or detect_language(text)
    code_note = " (koden visas på skärmen) " if lang == "sv" else " (the code is shown on screen) "
    text = re.sub(r"```.*?(```|$)", code_note, text, flags=re.S)  # fenced code
    text = re.sub(r"<think>.*?</think>", " ", text, flags=re.S)  # reasoning traces
    text = re.sub(r"`([^`]*)`", r"\1", text)  # inline code
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)  # images
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)  # links -> label
    text = re.sub(r"https?://\S+", " ", text)  # bare URLs
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.M)  # headings
    text = re.sub(r"^\s*([-*+•]|\d+[.)])\s+", "", text, flags=re.M)  # list markers
    text = re.sub(r"^\s*\|?[-:| ]{3,}\|?\s*$", " ", text, flags=re.M)  # table rules
    text = text.replace("|", ", ")
    text = re.sub(r"(\*\*|__|\*|_|~~)(?=\S)(.+?)(?<=\S)\1", r"\2", text)  # emphasis
    text = re.sub(r"[*#>]", " ", text)
    text = re.sub(r"[\U0001F300-\U0001FAFF☀-➿]", "", text)  # emoji
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"(?<![.!?:;,])\n+", ". ", text)  # line breaks -> sentence pauses
    text = re.sub(r"\n+", " ", text)
    text = speakable_numbers(text, lang)
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# Chatterbox Multilingual (natural voice) via the local server in
# ~/.openjarvis/chatterbox/server.py. Sentences are synthesized one at a time
# and played while the next one is generated.
# ---------------------------------------------------------------------------
_CB_URL = f"http://127.0.0.1:{os.environ.get('JARVIS_TTS_PORT', '8765')}"


def _chatterbox_ready(wait_seconds: float = 0.0) -> bool:
    import time
    import urllib.request

    if os.environ.get("JARVIS_TTS_ENGINE", "chatterbox") != "chatterbox":
        return False
    deadline = time.time() + wait_seconds
    while True:
        try:
            with urllib.request.urlopen(_CB_URL + "/health", timeout=0.5) as r:
                return r.status == 200
        except Exception:
            if time.time() >= deadline:
                return False
            time.sleep(0.5)


def _split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?…])\s+", text)
    out: list[str] = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if out and len(out[-1]) < 25:  # merge very short fragments
            out[-1] = f"{out[-1]} {p}"
        else:
            out.append(p)
    return out


def _chatterbox_wav(sentence: str, lang: str) -> bytes:
    import json
    import urllib.request

    body = json.dumps({
        "text": sentence,
        "lang": lang,
        "cfg_weight": float(os.environ.get("JARVIS_TTS_CFG", "0.5")),
        "exaggeration": float(os.environ.get("JARVIS_TTS_EXAGGERATION", "0.5")),
        "warmth": float(os.environ.get("JARVIS_TTS_WARMTH", "0")),
        "pitch": float(os.environ.get("JARVIS_TTS_PITCH", "0")),
    }).encode()
    req = urllib.request.Request(_CB_URL + "/tts", data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def _chatterbox_speak(text: str, lang: str) -> None:
    import queue

    import sounddevice as sd
    import soundfile as sf

    q: "queue.Queue[bytes | None]" = queue.Queue()
    errors: list[BaseException] = []

    def producer() -> None:
        try:
            for s in _split_sentences(text):
                q.put(_chatterbox_wav(s, lang))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            q.put(None)

    threading.Thread(target=producer, daemon=True).start()
    played = 0
    while (wav := q.get()) is not None:
        data, sr = sf.read(io.BytesIO(wav), dtype="float32")
        sd.play(data, sr)
        sd.wait()
        played += 1
    if errors and played == 0:
        raise errors[0]


@TTSRegistry.register("bilingual")
class BilingualTTSBackend(TTSBackend):
    """Routes Swedish text to Piper and English text to Kokoro."""

    backend_id = "bilingual"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._piper: dict[str, Any] = {}
        self._kokoro: Any = None
        self._cb_checked = False

    # -- engines -----------------------------------------------------------
    def _piper_voice(self, name: str) -> Any:
        with self._lock:
            if name not in self._piper:
                from piper import PiperVoice

                path = PIPER_DIR / f"sv_SE-{name}-medium.onnx"
                if not path.exists():
                    raise RuntimeError(f"Piper voice not found: {path}")
                self._piper[name] = PiperVoice.load(str(path))
            return self._piper[name]

    def _kokoro_backend(self) -> Any:
        with self._lock:
            if self._kokoro is None:
                from openjarvis.speech.kokoro_tts import KokoroTTSBackend

                # CPU on purpose: the GPU is reserved for the LLM (gpt-oss
                # already fills ~7.8 of 8 GB). Override with JARVIS_TTS_DEVICE.
                device = os.environ.get("JARVIS_TTS_DEVICE", "cpu")
                self._kokoro = KokoroTTSBackend(device=device)
            return self._kokoro

    # -- TTSBackend API ----------------------------------------------------
    def synthesize(
        self,
        text: str,
        *,
        voice_id: str = "",
        speed: float = 1.0,
        output_format: str = "wav",
    ) -> TTSResult:
        clean = clean_for_speech(text)
        if not clean:
            return TTSResult(audio=b"", format="wav")
        lang = detect_language(clean)
        # First call may arrive while the launcher is still starting the server:
        # wait for it once (JARVIS_TTS_WAIT seconds), never again afterwards.
        first_wait = 0.0 if self._cb_checked else float(os.environ.get("JARVIS_TTS_WAIT", "0"))
        self._cb_checked = True
        if _chatterbox_ready(first_wait):
            try:
                _chatterbox_speak(clean, lang)
                # Audio was already played sentence-by-sentence; empty audio
                # tells OpenJarvis's speak() there is nothing left to play.
                return TTSResult(audio=b"", format="wav", voice_id="chatterbox", metadata={"language": lang})
            except Exception:
                pass  # fall through to the local Piper/Kokoro voices
        if lang == "en":
            en_voice = voice_id if voice_id and voice_id[:1] in "ab" else DEFAULT_EN_VOICE
            result = self._kokoro_backend().synthesize(
                clean, voice_id=en_voice, speed=speed, output_format="wav"
            )
            result.metadata = {**(result.metadata or {}), "language": "en"}
            return result

        sv_voice = os.environ.get("JARVIS_SV_VOICE", DEFAULT_SV_VOICE).strip() or DEFAULT_SV_VOICE
        voice = self._piper_voice(sv_voice)
        syn_config = None
        if speed and abs(speed - 1.0) > 1e-3:
            from piper import SynthesisConfig

            syn_config = SynthesisConfig(length_scale=1.0 / speed)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wav_file:
            voice.synthesize_wav(clean, wav_file, syn_config=syn_config)
        sr = voice.config.sample_rate
        audio = buf.getvalue()
        frames = max(0, (len(audio) - 44) // 2)
        return TTSResult(
            audio=audio,
            format="wav",
            duration_seconds=frames / sr if sr else 0.0,
            voice_id=f"sv_SE-{sv_voice}",
            sample_rate=sr,
            metadata={"language": "sv"},
        )

    def available_voices(self) -> List[str]:
        sv = sorted(p.name.split("-")[1] for p in PIPER_DIR.glob("sv_SE-*-medium.onnx"))
        return [f"sv:{v}" for v in sv] + ["en:bm_george", "en:bm_lewis", "en:bf_emma", "en:am_adam"]

    def health(self) -> bool:
        try:
            import piper  # noqa: F401
        except ImportError:
            return False
        return any(PIPER_DIR.glob("sv_SE-*-medium.onnx"))


# ---------------------------------------------------------------------------
# Speech-to-text: KB-Whisper with hallucination guards
# ---------------------------------------------------------------------------
from openjarvis.core.registry import SpeechRegistry  # noqa: E402

# Phrases Whisper models (esp. subtitle-trained ones) invent from silence/noise.
_HALLUCINATIONS = re.compile(
    r"(textning\.nu|undertext|svensktextning|tack för att du (har )?tittat|"
    r"prenumerera|thanks for watching|subtitles by|amara\.org)",
    re.I,
)


@SpeechRegistry.register("kb-whisper")
class KBWhisperBackend:
    """faster-whisper with VAD + no-speech filtering (config: speech.backend = "kb-whisper")."""

    backend_id = "kb-whisper"

    def __init__(self) -> None:
        from openjarvis.core.config import load_config

        sp = load_config().speech
        self._model_id = sp.model or "KBLab/kb-whisper-small"
        self._device = sp.device if sp.device not in ("", "auto") else "cpu"
        self._compute_type = sp.compute_type or "int8"
        self._model: Any = None
        self._lock = threading.Lock()

    def _ensure_model(self) -> Any:
        with self._lock:
            if self._model is None:
                from faster_whisper import WhisperModel

                self._model = WhisperModel(
                    self._model_id, device=self._device, compute_type=self._compute_type
                )
            return self._model

    def transcribe(self, audio: bytes, *, format: str = "wav", language: str | None = None) -> Any:
        from openjarvis.speech._stubs import Segment, TranscriptionResult

        if len(audio) < 44 + 2 * 1600:  # < 0.1 s of audio: nothing was said
            return TranscriptionResult(text="", language=None, confidence=None, duration_seconds=0.0, segments=[])
        model = self._ensure_model()
        segments_iter, info = model.transcribe(
            io.BytesIO(audio),
            language=language or None,
            beam_size=5,
            vad_filter=True,  # drop non-speech (key clicks, hum, silence)
            vad_parameters={"min_silence_duration_ms": 500, "speech_pad_ms": 200},
            condition_on_previous_text=False,
            no_speech_threshold=0.5,
        )
        kept = []
        for seg in segments_iter:
            text = seg.text.strip()
            if not text or _HALLUCINATIONS.search(text):
                continue
            if seg.no_speech_prob > 0.6 and seg.avg_logprob < -0.7:
                continue  # model itself thinks this is not speech
            kept.append(seg)
        text = " ".join(s.text.strip() for s in kept).strip()
        return TranscriptionResult(
            text=text,
            language=getattr(info, "language", None),
            confidence=getattr(info, "language_probability", None),
            duration_seconds=getattr(info, "duration", 0.0),
            segments=[Segment(text=s.text.strip(), start=s.start, end=s.end, confidence=None) for s in kept],
        )

    def supported_formats(self) -> List[str]:
        return ["wav", "mp3", "flac", "ogg", "m4a", "webm"]

    def health(self) -> bool:
        try:
            import faster_whisper  # noqa: F401
        except ImportError:
            return False
        return True


# ---------------------------------------------------------------------------
# Microphone: stop on *speech* ending (Silero VAD), not on a fixed loudness
# ---------------------------------------------------------------------------
# OpenJarvis's recorder treats RMS > 500 as speech; this mic's noise floor is
# ~960, so it never detected silence and ran to the 30 s cap.

_SR = 16000
_CHUNK = 1024  # 64 ms


class _EndOfSpeechDetector:
    """Feed 16 kHz int16 chunks; ``update`` returns True when the utterance ended."""

    def __init__(self, silence_seconds: float, startup_seconds: float, max_seconds: float) -> None:
        self.silence_chunks = max(1, round(silence_seconds * _SR / _CHUNK))
        self.startup_chunks = max(1, round(startup_seconds * _SR / _CHUNK))
        self.max_chunks = max(1, round(max_seconds * _SR / _CHUNK))
        self.n = 0
        self.speech_run = 0
        self.silence_run = 0
        self.started = False
        self._prev = None
        self._noise: list[float] = []
        self._vad = None
        try:
            from faster_whisper.vad import get_vad_model

            self._vad = get_vad_model()
        except Exception:
            self._vad = None  # fall back to adaptive energy threshold

    def _is_speech(self, chunk) -> bool:
        import numpy as np

        x = chunk.astype("float32") / 32768.0
        if self._vad is not None:
            ctx = self._prev if self._prev is not None else np.zeros_like(x)
            self._prev = x
            probs = np.asarray(self._vad(np.concatenate([ctx, x]))).reshape(-1)
            p = float(probs[-2:].max())
            return p > (0.5 if not self.started else 0.35)
        # Energy fallback: threshold adapts to the room's noise floor.
        rms = float(np.sqrt((x**2).mean())) * 32768.0
        floor = float(np.percentile(self._noise, 30)) if self._noise else rms
        speech = rms > max(500.0, floor * (3.0 if not self.started else 2.0))
        if not speech:
            self._noise = (self._noise + [rms])[-60:]
        return speech

    def update(self, chunk) -> bool:
        self.n += 1
        if self._is_speech(chunk):
            self.speech_run += 1
            self.silence_run = 0
            if self.speech_run >= 2:  # ~130 ms of speech
                self.started = True
        else:
            self.speech_run = 0
            if self.started:
                self.silence_run += 1
        if self.n >= self.max_chunks:
            return True
        if not self.started:
            return self.n >= self.startup_chunks  # nobody spoke
        return self.silence_run >= self.silence_chunks


def record_until_silence(
    *,
    sample_rate: int = _SR,
    silence_threshold: int = 500,  # unused; kept for API compatibility
    silence_seconds: float | None = None,
    startup_silence_seconds: float = 6.0,
    max_seconds: float = 30.0,
) -> bytes:
    """Drop-in replacement for openjarvis.speech.voice_io.record_until_silence."""
    import numpy as np
    import sounddevice as sd

    if silence_seconds is None:
        silence_seconds = float(os.environ.get("JARVIS_SILENCE_SECONDS", "1.0"))
    det = _EndOfSpeechDetector(silence_seconds, startup_silence_seconds, max_seconds)
    frames: list[bytes] = []
    with sd.RawInputStream(samplerate=_SR, channels=1, dtype="int16", blocksize=_CHUNK) as stream:
        while True:
            raw, _ = stream.read(_CHUNK)
            data = bytes(raw)
            frames.append(data)
            if det.update(np.frombuffer(data, dtype=np.int16)):
                break
    if not det.started:
        frames = []  # only noise: hand Whisper nothing to hallucinate from
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(_SR)
        wf.writeframes(b"".join(frames))
    return buf.getvalue()


try:
    import openjarvis.speech.voice_io as _voice_io

    _voice_io.record_until_silence = record_until_silence
except Exception:  # pragma: no cover
    pass


__all__ = [
    "BilingualTTSBackend",
    "KBWhisperBackend",
    "clean_for_speech",
    "detect_language",
    "record_until_silence",
]
