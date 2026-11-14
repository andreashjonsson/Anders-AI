"""Hands-free voice input for the "Anders" assistant (OpenJarvis `chat --voice`).

Wake phrases:
  "Hej Anders" -> Swedish conversation      "Hey Anders" -> English conversation
"hej" and "hey" sound almost identical, so the language is decided by Whisper's
language identification of what you say (greeting + command). In tests a bare
"Hey Anders" was identified as English and "Hej Anders" as Swedish.

Flow:
  * Idle: every utterance in the room is checked by a tiny Whisper model
    (first 2.5 s only, ~0.3 s CPU) for "hej/hey Anders". Anything else is ignored.
  * Wake: "Hej Anders, tänd lamporna" works in one breath; or say only
    "Hej Anders", wait for the beep, then speak.
  * Follow-up: after a spoken answer it listens a few seconds without the wake phrase.
  * Typing works at any time (type + Enter). Ctrl+C exits.

Env:
  JARVIS_HANDSFREE=1          enable (the launcher sets it)
  ANDERS_WAKE_NAMES           regex for the name (default: anders)
  JARVIS_FOLLOWUP_SECONDS=5   follow-up window (0 = off)
  JARVIS_SILENCE_SECONDS=1.0  pause that ends a sentence
  JARVIS_BEEP=1               beep when woken
"""

from __future__ import annotations

import io
import os
import re
import select
import sys
import threading
import wave
from typing import Any

_SR = 16000
_BLOCK = 1024

_state: dict[str, Any] = {"last_lang": None, "models": None}
_models_lock = threading.Lock()

_NAME = os.environ.get("ANDERS_WAKE_NAMES", r"and(?:e|é)?r?s")
_GREETING = r"(?:hej|hey|hei|hai|hi|hallå|hallo|hejsan|okej|ok)"
# "Tjenare" (or "Tjenare Anders") also wakes Anders, and is always Swedish. Whisper writes it in a few ways.
_TJENA = r"(?:tj[eäé]n(?:a|are|ar|aste|ixen|is|asåmycke)|ch[eäé]nare|kj[eäé]nare|sj[eä]nare|trenare)"
_WAKE = re.compile(rf"^\W*(?:\w+\W+){{0,2}}?(?:{_GREETING}\W+(?:{_NAME})|{_TJENA}(?:\W+(?:{_NAME}))?"
                   rf"|(?:senare|tjänare)\W+(?:{_NAME}))\b\W*", re.I)
_WAKE_SV = re.compile(rf"^\W*(?:\w+\W+){{0,2}}?(?:{_TJENA}\b|(?:senare|tjänare)\W+(?:{_NAME})\b)"
                      r"|^\W*(?:senare|tjänare)\W*$", re.I)
# A lone "Tjenare!" is sometimes heard as "senare"/"tjänare": accept those only as the whole utterance.
_WAKE_ALONE = re.compile(r"^\W*(?:senare|tjänare)\W*$", re.I)

_MSG = {
    "sv": {"listen": "Lyssnar… (prata nu)", "trans": "Transkriberar…", "nothing": "Hörde inget — försök igen.",
           "follow": "Lyssnar efter följdfråga…"},
    "en": {"listen": "Listening… (speak now)", "trans": "Transcribing…", "nothing": "Didn't catch that — try again.",
           "follow": "Listening for a follow-up…"},
}


# ---------------------------------------------------------------------------
# Models (loaded once, in the background as soon as the chat starts)
# ---------------------------------------------------------------------------
def _load_models() -> dict[str, Any]:
    with _models_lock:
        if _state["models"] is None:
            from faster_whisper import WhisperModel

            from openjarvis.core.config import load_config

            sv_model = load_config().speech.model or "KBLab/kb-whisper-small"
            _state["models"] = {
                "wake": WhisperModel("KBLab/kb-whisper-tiny", device="cpu", compute_type="int8"),
                "sv": WhisperModel(sv_model, device="cpu", compute_type="int8"),  # Swedish-tuned
                "en": WhisperModel("small", device="cpu", compute_type="int8"),  # standard Whisper
            }
        return _state["models"]


def _preload() -> None:
    threading.Thread(target=_load_models, daemon=True).start()


# ---------------------------------------------------------------------------
# Audio helpers
# ---------------------------------------------------------------------------
def _beep() -> None:
    if os.environ.get("JARVIS_BEEP", "1") != "1":
        return
    try:
        import numpy as np
        import sounddevice as sd

        t = np.arange(int(0.12 * 24000)) / 24000
        tone = 0.15 * np.sin(2 * np.pi * 880 * t) * np.hanning(t.size)
        sd.play(tone.astype("float32"), 24000)
        sd.wait()
    except Exception:
        pass


def _typed_line() -> str | None:
    try:
        ready, _, _ = select.select([sys.stdin], [], [], 0)
    except (OSError, ValueError):
        return None
    if ready:
        line = sys.stdin.readline()
        return "/quit" if line == "" else line.strip()
    return None


def _record_utterance(startup_seconds: float | None) -> tuple[str, Any]:
    """Record one utterance. startup_seconds=None waits forever for speech.

    Returns ("voice", float32 array) | ("typed", text) | ("timeout", None).
    """
    import numpy as np
    import sounddevice as sd
    from jarvis_bilingual_tts import _EndOfSpeechDetector

    silence = float(os.environ.get("JARVIS_SILENCE_SECONDS", "1.0"))
    while True:
        det = _EndOfSpeechDetector(silence, startup_seconds or 3600.0, 30.0)
        frames: list[Any] = []
        pre: list[Any] = []  # short pre-roll so the first syllable isn't cut
        with sd.RawInputStream(samplerate=_SR, channels=1, dtype="int16", blocksize=_BLOCK) as stream:
            while True:
                typed = _typed_line()
                if typed:
                    return "typed", typed
                raw, _ = stream.read(_BLOCK)
                chunk = np.frombuffer(bytes(raw), dtype=np.int16).copy()
                ended = det.update(chunk)
                if det.started:
                    if pre:
                        frames.extend(pre)
                        pre = []
                    frames.append(chunk)
                else:
                    pre = (pre + [chunk])[-5:]  # ~320 ms
                if ended:
                    break
        if det.started:
            return "voice", np.concatenate(frames).astype("float32") / 32768.0
        if startup_seconds is not None:
            return "timeout", None
        # waiting forever: keep listening


def _transcribe(models: dict[str, Any], audio: Any, lang: str) -> str:
    from jarvis_bilingual_tts import _HALLUCINATIONS

    segs, _ = models[lang].transcribe(
        audio, language=lang, beam_size=5, vad_filter=True, condition_on_previous_text=False
    )
    kept = [s.text.strip() for s in segs
            if s.text.strip() and not _HALLUCINATIONS.search(s.text)
            and not (s.no_speech_prob > 0.6 and s.avg_logprob < -0.7)]
    return " ".join(kept).strip()


def _detect_lang(models: dict[str, Any], audio: Any) -> str:
    try:
        _, _, probs = models["en"].detect_language(audio)
        p = dict(probs)
        return "en" if p.get("en", 0.0) > p.get("sv", 0.0) else "sv"
    except Exception:
        return "sv"


def _is_wake(models: dict[str, Any], audio: Any) -> bool:
    segs, _ = models["wake"].transcribe(
        audio[: int(2.5 * _SR)], language="sv", beam_size=1,
        condition_on_previous_text=False, without_timestamps=True,
    )
    head = "".join(s.text for s in segs).strip()
    _state["wake_head"] = head  # lets the app see which wake phrase was used ("Tjenare" = Swedish)
    return bool(_WAKE.search(head) or _WAKE_ALONE.match(head))


# ---------------------------------------------------------------------------
# Replacement for openjarvis.cli._voice_chat.read_voice_input
# ---------------------------------------------------------------------------
def read_voice_input(console: Any, session: Any) -> Any:
    from openjarvis.cli._voice_chat import VOICE_EXIT, _terminal_safe_text

    _preload()
    follow_secs = float(os.environ.get("JARVIS_FOLLOWUP_SECONDS", "5"))
    follow_lang = _state["last_lang"] if follow_secs > 0 else None
    _state["last_lang"] = None
    try:
        while True:
            if follow_lang:
                console.print(f"[dim cyan]{_MSG[follow_lang]['follow']}[/dim cyan]")
                kind, audio = _record_utterance(follow_secs)
            else:
                console.print('[dim]Säg [bold]"Hej Anders"[/bold] (svenska) eller [bold]"Hey Anders"[/bold] '
                              "(English) … eller skriv och tryck Enter. Ctrl+C avslutar.[/dim]")
                kind, audio = _record_utterance(None)
            was_follow, follow_lang = bool(follow_lang), None

            if kind == "typed":
                return audio
            if kind == "timeout":
                continue
            models = _load_models()
            if not was_follow and not _is_wake(models, audio):
                continue  # someone talking, not to Anders

            lang = _detect_lang(models, audio)
            console.print(f"[dim]{_MSG[lang]['trans']}[/dim]")
            text = _transcribe(models, audio, lang)
            if not was_follow:
                text = _WAKE.sub("", text, count=1).strip()
            if not text or len(re.sub(r"\W", "", text)) < 2:
                # Only the wake phrase: beep and listen for the actual command.
                _beep()
                console.print(f"[dim cyan]{_MSG[lang]['listen']}[/dim cyan]")
                kind, audio = _record_utterance(6.0)
                if kind == "typed":
                    return audio
                if kind == "timeout":
                    console.print(f"[dim]{_MSG[lang]['nothing']}[/dim]")
                    continue
                lang = _detect_lang(models, audio)
                text = _transcribe(models, audio, lang)
            if not text:
                console.print(f"[dim]{_MSG[lang]['nothing']}[/dim]")
                continue
            console.print(f"[bold]Du (röst, {lang}):[/bold] {_terminal_safe_text(text)}")
            _state["last_lang"] = lang
            return text
    except KeyboardInterrupt:
        return VOICE_EXIT
    except Exception as exc:
        console.print(f"[red]Mic error: {_terminal_safe_text(exc)}[/red]")
        return VOICE_EXIT


def install() -> None:
    import openjarvis.cli._voice_chat as vc

    vc.read_voice_input = read_voice_input
    chat_cmd = sys.modules.get("openjarvis.cli.chat_cmd")
    if chat_cmd is not None:
        chat_cmd.read_voice_input = read_voice_input


if os.environ.get("JARVIS_HANDSFREE") == "1" and os.path.basename(sys.argv[0] if sys.argv else "") == "jarvis":
    try:
        install()
    except Exception as exc:  # pragma: no cover
        sys.stderr.write(f"hands-free not enabled: {exc!r}\n")
