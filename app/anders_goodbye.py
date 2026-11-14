"""Goodbyes: 'hejdå' gets a normal goodbye most of the time, and now and then a fun one ('Kryssa lugnt!').

The phrases live in goodbyes.txt next to this file (re-read automatically when it changes).
"""

from __future__ import annotations

import random
import re
from pathlib import Path

GOODBYE_FILE = Path(__file__).with_name("goodbyes.txt")
FUN_CHANCE = 0.35  # how often the goodbye is a fun one
MAX_WORDS = 7  # longer messages are real requests that happen to contain "hej då"

_BYE_SV = re.compile(r"\b(hej\s*då|hejdå|hejsan svejsan|ha det( så)? (bra|gött|fint)|vi (hörs|ses)|ses (sen|senare|imorgon)|"
                     r"adjö|ajöss|tjing|hej hej|över och ut|jag drar|jag sticker|jag går nu)\b", re.I)
_NIGHT_SV = re.compile(r"\bgod ?natt\b|\bsov gott\b|\bnatti\b", re.I)
_BYE_EN = re.compile(r"\b(bye|goodbye|good bye|see you|see ya|catch you later|take care|good night|gotta go|i'?m off|cya)\b"
                     r"|^\W*later\W*$", re.I)
# Questions and requests that merely mention a goodbye ("vad betyder hej då på engelska?") go to the assistant.
_NOT_BYE = re.compile(r"\?|\b(vad|hur|varför|vilken|betyder|kan du|säg|skriv|översätt|berätta|sätt|påminn|"
                      r"what|how|why|mean|can you|could you|say|write|translate|tell|need|set|remind)\b", re.I)

_cache: dict = {"mtime": None, "lines": {}}
_last: str | None = None


def _load() -> dict[str, list[str]]:
    try:
        mtime = GOODBYE_FILE.stat().st_mtime
    except OSError:
        return {}
    if mtime != _cache["mtime"]:
        lines: dict[str, list[str]] = {}
        kind = None
        for line in GOODBYE_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("[") and line.endswith("]"):
                kind = line[1:-1].strip().lower()
                lines.setdefault(kind, [])
            elif kind:
                lines[kind].append(line)
        _cache.update(mtime=mtime, lines=lines)
    return _cache["lines"]


def pick_goodbye(text: str, lang: str = "sv") -> str | None:
    """A goodbye if *text* is someone saying bye (short message), else None."""
    global _last
    text = (text or "").strip()
    if not text or len(re.findall(r"\w+", text)) > MAX_WORDS or _NOT_BYE.search(text):
        return None
    lines = _load()
    fun = random.random() < FUN_CHANCE
    if lang == "en":
        if not _BYE_EN.search(text):
            return None
        kinds = ["english_kul", "english"] if fun else ["english"]
    elif _NIGHT_SV.search(text):
        kinds = ["godnatt"]
    elif _BYE_SV.search(text):
        kinds = ["kul", "vanlig"] if fun else ["vanlig"]
    else:
        return None
    pool = next((lines[k] for k in kinds if lines.get(k)), None)
    if not pool:
        return None
    choice = random.choice([p for p in pool if p != _last] or pool)
    _last = choice
    return choice
