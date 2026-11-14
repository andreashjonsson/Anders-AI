"""Hand-written comebacks for when someone insults Anders (the language model isn't funny in Swedish).

The lines live in roasts.txt next to this file (re-read automatically when it changes).
pick_roast() returns a comeback, or None so the language model answers as usual. Anders is an
assistant first: anything that looks like a question, request or praise is never roasted.
"""

from __future__ import annotations

import random
import re
from pathlib import Path

ROAST_FILE = Path(__file__).with_name("roasts.txt")
MAX_WORDS = 15  # longer messages are probably real questions with a swear word in them

_YOU = re.compile(r"\b(du|dig|dej|din|ditt|dina|anders|you|your|ur|u)\b", re.I)
# Requests and questions go to the assistant, even with an insult in them ("kan du, din idiot, sätta en timer").
_REQUEST = re.compile(
    r"\b(kan du|kunde du|skulle du|vill du|orkar du|hjälp|berätta|säg|sätt|ställ|starta|stoppa|spela|visa|"
    r"öppna|stäng|påminn|lägg till|sök|hur|när|vilken|vilket|vilka|vad blir|vad är klockan|hur mycket)\b", re.I)
_PRAISE = re.compile(
    r"\b(bra|bäst|bästa|grym|grymt|kul|rolig|roligt|snäll|tack|älskar|gillar|fin|fint|smart|duktig|härlig|"
    r"najs|cool|snygg|skitbra|skitkul|skitsnygg|love|great|awesome|nice|thanks)\b", re.I)
_NOT_PRAISE = re.compile(r"\binte (så |ett dugg |särskilt )?(bra|rolig|roligt|kul|smart|snäll|snygg)\b", re.I)
_ROAST_NAME = re.compile(
    r"\broast(?:a)?\s+(?:(?:min|mitt|mina|vår)\s+)?(?:(?:kompis|vän|polare|bror|brorsa|syster|syrra|flickvän|pojkvän|"
    r"tjej|kille|sambo|fru|man|kollega|chef|granne)\s+)?([A-ZÅÄÖ][\wåäö-]+)")
_NOT_A_NAME = {"Mig", "Mej", "Me", "Oss", "Honom", "Henne", "Dem", "Dom", "Lite", "Nu", "Igen", "Någon", "Anders"}

# (category, pattern, must be aimed at Anders) - checked in order, first match wins.
_KINDS = [
    ("värmland", re.compile(
        r"\b(värmland|karlstad|värmlänning\w*)\b.{0,40}\b(suger|skit\w*|tråkig\w*|kass|sämst|ful\w*|dålig\w*|äcklig\w*|hålan?)\b"
        r"|\b(skit|kass|ful|tråkig|äcklig)\w*\s+(värmland|karlstad)\b", re.I), False),
    ("roasta", re.compile(r"\broast(a)?\s+(mig|mej|me|oss|honom|henne|dem|dom)\b|\bge mig en roast\b", re.I), False),
    ("mamma", re.compile(r"\bdin (mamma|morsa|mor|farsa|pappa)\b|\byour (mom|mum|mother|mama)\b", re.I), False),
    ("fuckoff", re.compile(r"\bfuck (you|off|dig|u)\b|\bdra åt (helvete|skogen|fanders|h-e)\b|\bdit pepparn växer\b"
                           r"|\bfan ta dig\b|\bhelvete ta dig\b|\bstick och brinn\b", re.I), False),
    ("hata", re.compile(r"\bhatar (dig|dej|anders)\b|\bi hate (you|u)\b", re.I), False),
    ("tyst", re.compile(r"\bhåll (käften|truten|klaffen|mun)\b|\bshut up\b|\bkäften\b", re.I), False),
    ("robot", re.compile(r"\b(plåtburk|plåthög|skitburk|skitrobot|skitdator|skitmaskin|skrothög|elektronikskrot|"
                         r"brödrost|miniräknare|kaffebryggare)\b"
                         r"|\bdin (dumma |jävla |fula |sega )?(robot|burk|maskin|bot|högtalare)\b", re.I), True),
    ("allmän", re.compile(r"\bbög\w*|\bhomo\b|\bhomse\b|\bfjoll\w*|\bgay\b|\bfag\w*", re.I), True),
    ("ful", re.compile(r"\bful(a|ing|ingen)?\b|\bäcklig\w*|\bvidrig\w*|\bugly\b", re.I), True),
    ("dum", re.compile(r"\bdum(ma|t|skalle|huvud)?\b|\bkorkad\w*|\bidiot\w*|\bpucko\b|\btrög\w*|\bdumbom\b|\bmuppen?\b|"
                       r"\bhjärndöd\b|\bstupid\b|\bdumb\b|\bmoron\b", re.I), True),
    ("långsam", re.compile(r"\bseg(a)?\b|\blångsam\w*|\bslow\b|tar (en |så )?evighet|tar så (jävla |förbannat )?lång tid", re.I), True),
    ("tråkig", re.compile(r"\btråkig\w*|\bboring\b|\binte (ett dugg |särskilt |så )?rolig\b|\bostrolig\b|\bsömnig\b", re.I), True),
    ("kass", re.compile(r"värdelös\w*|\bsuger\b|\bskit\b|\bkass\b|\bsämst\b|\bdålig\b|\bpinsam\b|\bpatetisk\b|"
                        r"\buseless\b|\bsucks?\b|\btrash\b", re.I), True),
    ("allmän", re.compile(r"\btönt\w*|\bnolla\b|\bloser\b|\bclown\w*|\bfjant\w*|\bmes\b|\bnörd\b|\btomte\b|"
                          r"\bjävel\b|\bgubbjävel\b|\bwannabe\b", re.I), True),
]

_cache: dict = {"mtime": None, "roasts": {}}
_last: dict[str, str] = {}


def _load() -> dict[str, list[str]]:
    """Read roasts.txt (cached until the file changes)."""
    try:
        mtime = ROAST_FILE.stat().st_mtime
    except OSError:
        return {}
    if mtime != _cache["mtime"]:
        roasts: dict[str, list[str]] = {}
        kind = None
        for line in ROAST_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("[") and line.endswith("]"):
                kind = line[1:-1].strip().lower()
                roasts.setdefault(kind, [])
            elif kind:
                roasts[kind].append(line)
        _cache.update(mtime=mtime, roasts=roasts)
    return _cache["roasts"]


def _choose(kind: str) -> str | None:
    options = _load().get(kind) or []
    if not options:
        return None
    fresh = [r for r in options if r != _last.get(kind)] or options
    _last[kind] = random.choice(fresh)
    return _last[kind]


def pick_roast(text: str, lang: str = "sv") -> str | None:
    """A comeback if *text* insults Anders or asks for a roast (Swedish), else None."""
    text = (text or "").strip()
    words = re.findall(r"\w+", text)
    if lang != "sv" or not words or len(words) > MAX_WORDS:
        return None

    m = _ROAST_NAME.search(text)
    if m and m.group(1) not in _NOT_A_NAME:
        line = _choose("roasta_namn")
        return line.replace("{namn}", m.group(1)) if line else None

    praise = _PRAISE.search(text) and not _NOT_PRAISE.search(text)
    request = _REQUEST.search(text)
    aimed = bool(_YOU.search(text)) or len(words) == 1
    for kind, pattern, needs_aim in _KINDS:
        if not pattern.search(text):
            continue
        if kind not in ("roasta", "värmland") and (praise or request):
            return None  # assistant first
        if needs_aim and not aimed:
            continue
        return _choose(kind)
    return None
