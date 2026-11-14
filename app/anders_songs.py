"""Songs Anders 'sings' (reads aloud) when someone asks him to sing.

Picked at random. Add more songs to the list (each one a triple-quoted string). Each line ends with punctuation so the voice reads it line by line.
"""

from __future__ import annotations

import random
import re

SONGS = [
    """Ack, Värmland, du sköna, du härliga land,
du krona bland Svea rikes länder!
Och komme jag än mitt i det förlovade land,
till Värmland jag ändå återvänder.
Ja, där vill jag leva, ja, där vill jag dö.
Om en gång ifrån Värmland jag tager mig en mö,
så vet jag att aldrig jag mig ångrar.""",
]

# "Kan du sjunga?", "sjung något", "en till låt", "can you sing", "sing me a song" ...
_ASK_TO_SING = re.compile(
    r"\bsjung(a|er)?\b|\bsing\b|\bsinging\b"
    r"|\b(en|ett)\s+(till|annan|annat|ny)\s+(låt|sång|visa)\b|\b(låt|sång|visa)\s+till\b|\banother song\b", re.I)
_unsung: list[str] = []  # songs left before any song is repeated


def pick_song(text: str, force: bool = False) -> str | None:
    """A song if *text* asks Anders to sing (or *force*, e.g. 'en till' right after a song), else None.

    Every song is sung once before any of them comes back, and never the same one twice in a row.
    """
    if not SONGS or not (force or _ASK_TO_SING.search(text or "")):
        return None
    global _last
    if not _unsung:
        _unsung.extend(SONGS)
        random.shuffle(_unsung)
        if len(_unsung) > 1 and _unsung[-1] == _last:
            _unsung.insert(0, _unsung.pop())
    _last = _unsung.pop()
    return _last


_last: str | None = None
