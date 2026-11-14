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

# "Kan du sjunga?", "sjung något", "can you sing", "sing me a song" ...
_ASK_TO_SING = re.compile(r"\bsjung(a|er)?\b|\bsing\b|\bsinging\b", re.I)


def pick_song(text: str) -> str | None:
    """A random song if *text* asks Anders to sing, else None."""
    if not _ASK_TO_SING.search(text or ""):
        return None
    return random.choice(SONGS)
