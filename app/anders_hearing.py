"""Speech-to-text with sanity checks, so Anders doesn't answer background noise or the TV.

hear() picks Swedish or English (transcribing both when the language is unclear and keeping the one
Whisper is more sure about) and rejects transcripts Whisper itself isn't confident in.

Whisper's avg_logprob, measured on Andreas' voice: clear speech in the right language about -0.2..-0.35,
the same speech forced into the wrong language -0.45..-0.8, garbage from noise/TV usually below -0.9.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

MIN_CONFIDENCE = -0.95  # after a wake phrase / push-to-talk: the user is clearly talking to Anders
MIN_CONFIDENCE_FOLLOWUP = -0.75  # follow-up without wake phrase: stricter, the TV must not trigger replies
SURE_LANG = 0.80  # language-detection probability needed to skip transcribing in both languages
SWEDISH_BONUS = 0.08  # Anders lives in Värmland: on a tie, Swedish wins

# Whisper's classic hallucinations on silence/noise (YouTube outros, subtitle credits ...)
_HALLUCINATIONS = re.compile(
    r"(textning\.nu|undertext|svensktextning|tack för att du (har )?tittat|tack för att ni tittade|prenumerera|"
    r"thanks for watching|thank you for watching|subtitles by|amara\.org|like and subscribe|"
    r"see you (guys )?in the next|vi ses i nästa|^\W*(tack|thank you|you|bye)\W*$)", re.I)


@dataclass
class Heard:
    text: str
    lang: str
    confidence: float  # weighted avg_logprob; closer to 0 = surer
    no_speech: float
    reason: str = ""  # why it was rejected ("" = accepted)

    @property
    def ok(self) -> bool:
        return not self.reason


def _transcribe(models: dict[str, Any], audio: Any, lang: str) -> Heard:
    segs, _ = models[lang].transcribe(audio, language=lang, beam_size=5, vad_filter=True,
                                      condition_on_previous_text=False)
    segs = [s for s in segs if s.text.strip()]
    if not segs:
        return Heard("", lang, -9.0, 1.0, "tyst")
    weight = [max(1, len(s.text.strip())) for s in segs]
    conf = sum(s.avg_logprob * w for s, w in zip(segs, weight)) / sum(weight)
    no_speech = max(s.no_speech_prob for s in segs)
    text = " ".join(s.text.strip() for s in segs if not _HALLUCINATIONS.search(s.text.strip())).strip()
    heard = Heard(text, lang, conf, no_speech)
    if not text:
        heard.reason = "hallucination"
    elif max(s.compression_ratio for s in segs) > 2.4:
        heard.reason = "upprepning"  # Whisper looping on noise
    return heard


def _language_probs(models: dict[str, Any], audio: Any) -> tuple[float, float]:
    try:
        _, _, probs = models["en"].detect_language(audio)
        p = dict(probs)
        return p.get("sv", 0.0), p.get("en", 0.0)
    except Exception:
        return 1.0, 0.0


def hear(models: dict[str, Any], audio: Any, *, followup: bool = False) -> Heard:
    """Transcribe *audio* (16 kHz float32). Check .ok before using .text."""
    p_sv, p_en = _language_probs(models, audio)
    if p_sv >= SURE_LANG:
        candidates = [_transcribe(models, audio, "sv")]
    elif p_en >= SURE_LANG:
        candidates = [_transcribe(models, audio, "en")]
    else:  # unclear (noisy room, short phrase): try both and keep the one Whisper is surer about
        candidates = [_transcribe(models, audio, "sv"), _transcribe(models, audio, "en")]
    best = max(candidates, key=lambda h: h.confidence + (SWEDISH_BONUS if h.lang == "sv" else 0.0)
               - (5.0 if h.reason else 0.0))
    if best.ok:
        limit = MIN_CONFIDENCE_FOLLOWUP if followup else MIN_CONFIDENCE
        if best.confidence < limit:
            best.reason = f"osäker ({best.confidence:.2f})"
        elif best.no_speech > 0.8 and best.confidence < -0.5:
            best.reason = "troligen inte tal"
        elif len(re.sub(r"\W", "", best.text)) < 2:
            best.reason = "för kort"
    return best
