"""Hand-written jokes for when someone asks Anders for one (the language model isn't funny in Swedish).

The jokes live in jokes.txt next to this file (re-read automatically when it changes).
pick_joke() returns a joke, or None so the language model answers as usual.
"""

from __future__ import annotations

import random
import re
import time
from pathlib import Path

JOKE_FILE = Path(__file__).with_name("jokes.txt")
FOLLOWUP_SECONDS = 180  # "ett till" counts as asking for another joke this long after the last one

_JOKE = r"(skämt|skämtet|vits|vitsar|vitsen|ordvits|pappaskämt|roligt|roliga historia|rolig historia)"
_ASK = re.compile(
    r"\b(berätta|dra|kör|ta|säg|hör|ge mig|har du|kan du|vill höra|kom med|hitta på)\b.{0,30}\b" + _JOKE + r"\b"
    r"|\b(ett|en|nåt|något|några|fler|ditt bästa|ditt sämsta)\s+(\w+\s+){0,2}(skämt|vits|vitsar|ordvits|pappaskämt)\b"
    r"|\bfå mig att skratta\b|\bsäg något roligt\b|\bsäg nåt roligt\b", re.I)
_MORE = re.compile(r"^\s*(ett till|en till|ett annat|en annan|mer|fler|kör en till|ta en till|ett skämt till|en vits till)"
                   r"\s*[!.?]*\s*$", re.I)
_ASK_EN = re.compile(r"\b(tell|give|know|got|say|hear)\b.{0,25}\bjokes?\b|\bmake me laugh\b|\bsay something funny\b"
                     r"|\b(a|another|one more)\s+(\w+\s+)?joke\b", re.I)
_MORE_EN = re.compile(r"^\s*(another( one)?|one more|more)\s*[!.?]*\s*$", re.I)
# Reactions to a joke ("det var ett dåligt skämt") are not requests for a new one.
_REACTION = re.compile(r"\b(det|den|detta|det där|den där)\s+(var|är)\b|\bvilket\b.{0,15}\b(skämt|vits)", re.I)
_TOPICS = [
    ("svart", re.compile(r"\b(svart|svarta|mörk|mörkt|sjukt|sjuka|grov|grovt|elak|elakt|död|döden)\w*", re.I)),
    ("dator", re.compile(r"\b(dator\w*|data|ai|robot\w*|teknik|program\w*|it|internet|wifi)\b", re.I)),
    ("värmland", re.compile(r"\b(värmland\w*|värmlänning\w*|karlstad|stockholm\w*|göteborg\w*)\b", re.I)),
    ("vardag", re.compile(r"\b(svensk\w*|sverige|vardag\w*|jobb\w*|fika)\b", re.I)),
    ("ordvits", re.compile(r"\b(ordvits\w*|pappaskämt|dåligt|dåliga|töntigt|töntiga|barn\w*)\b", re.I)),
]

_cache: dict = {"mtime": None, "jokes": {}}
_used: set[str] = set()
_last_joke_at = 0.0


def _load() -> dict[str, list[str]]:
    """Read jokes.txt (cached until the file changes)."""
    try:
        mtime = JOKE_FILE.stat().st_mtime
    except OSError:
        return {}
    if mtime != _cache["mtime"]:
        jokes: dict[str, list[str]] = {}
        kind = None
        for line in JOKE_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("[") and line.endswith("]"):
                kind = line[1:-1].strip().lower()
                jokes.setdefault(kind, [])
            elif kind:
                jokes[kind].append(line)
        _cache.update(mtime=mtime, jokes=jokes)
    return _cache["jokes"]


def pick_joke(text: str, lang: str = "sv") -> str | None:
    """A joke if *text* asks for one (or 'ett till' right after one), else None. Swedish only."""
    global _last_joke_at
    text = (text or "").strip()
    if not text:
        return None
    recent = time.time() - _last_joke_at < FOLLOWUP_SECONDS
    jokes = _load()
    if lang == "en":
        asked = bool(_ASK_EN.search(text)) and not re.search(r"\b(that|this) (was|is)\b", text, re.I)
        if not (asked or (_MORE_EN.match(text) and recent)):
            return None
        kind = "english"
    else:
        asked = bool(_ASK.search(text)) and not _REACTION.search(text)
        if not (asked or (_MORE.match(text) and recent)):
            return None
        kind = next((k for k, p in _TOPICS if p.search(text)), None) if asked else _cache.get("last_kind")
    pool = jokes.get(kind) if kind in jokes else [j for k, js in jokes.items() if k != "english" for j in js]
    if not pool:
        return None
    fresh = [j for j in pool if j not in _used]
    if not fresh:  # every joke in this pool has been told: start over
        _used.difference_update(pool)
        fresh = pool
    joke = random.choice(fresh)
    _used.add(joke)
    _last_joke_at = time.time()
    _cache["last_kind"] = kind
    return joke
