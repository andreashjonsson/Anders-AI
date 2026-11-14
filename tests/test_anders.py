"""Quick checks for Anders' text logic (no microphone, GPU or language model needed).

Run:  ~/.openjarvis/.venv/bin/python tests/test_anders.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import anders_jokes  # noqa: E402
import anders_roasts  # noqa: E402
import anders_songs  # noqa: E402
from anders_text import _asked_to_schedule, _source_links, _speakable, _unsupported_names  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool) -> None:
    print(("ok    " if ok else "FAIL  ") + name)
    if not ok:
        FAILS.append(name)


# --- roasts: insults get a comeback, questions/commands/praise never do -----------------------------
for text in ["du är fan ful", "du är så jävla dum", "håll käften", "du suger", "roasta mig", "idiot",
             "din plåtburk", "jag hatar dig", "fuck you", "din mamma är tjock", "du är tråkig",
             "Värmland suger", "du är bög", "Anders är dum", "du är en tönt"]:
    check(f"roast: {text!r}", anders_roasts.pick_roast(text, "sv") is not None)
for text in ["vädret suger idag", "maten var äcklig", "du är fan bäst", "du är skitbra", "vad är klockan?",
             "kan du sätta en timer din idiot", "sätt på tyst läge", "min bror är gay", "det var dumt av mig",
             "kan du stänga av din högtalare", "Han är fan ful."]:
    check(f"no roast: {text!r}", anders_roasts.pick_roast(text, "sv") is None)
named = anders_roasts.pick_roast("roasta min kompis Lisa", "sv")
check("roast puts the name in", bool(named) and "Lisa" in named)
for text in ["you are ugly", "shut up", "you suck", "roast me"]:
    check(f"english roast: {text!r}", anders_roasts.pick_roast(text, "en") is not None)
check("no english roast for a question", anders_roasts.pick_roast("what is the weather", "en") is None)

# --- jokes ---------------------------------------------------------------------------------------------
for text in ["berätta ett skämt", "dra en vits", "säg något roligt", "berätta ett svart skämt"]:
    check(f"joke: {text!r}", anders_jokes.pick_joke(text, "sv") is not None)
check("joke: 'ett till' right after a joke", anders_jokes.pick_joke("ett till", "sv") is not None)
for text in ["skämtar du?", "det var ett dåligt skämt", "vad är klockan"]:
    check(f"no joke: {text!r}", anders_jokes.pick_joke(text, "sv") is None)
check("english joke", anders_jokes.pick_joke("tell me a joke", "en") is not None)
seen = {anders_jokes.pick_joke("berätta ett skämt", "sv") for _ in range(20)}
check("jokes don't repeat quickly", len(seen) == 20)

# --- songs ---------------------------------------------------------------------------------------------
check("song: 'sjung en låt'", anders_songs.pick_song("sjung en låt") is not None)
check("song: 'en till låt'", anders_songs.pick_song("en till låt") is not None)
check("no song for 'spela musik'", anders_songs.pick_song("spela musik") is None)

# --- timers/reminders only when asked --------------------------------------------------------------------
for text in ["påminn mig att ringa mamma kl 18", "sätt en timer på 10 minuter", "väck mig 06.30", "remind me in 5 minutes"]:
    check(f"schedule allowed: {text!r}", _asked_to_schedule([{"role": "user", "content": text}]))
for text in ["DU ÄR FAN FUL", "du är så jävla dum", "håll käften din skitdator"]:
    check(f"schedule blocked: {text!r}", not _asked_to_schedule([{"role": "user", "content": text}]))

# --- fact check: names that aren't in the sources --------------------------------------------------------
sources = ["### Julbord i Karlstad 2026 | Olssons Bazar\nSource: https://olssonsbazar.com/julbord/\n"
           "Summary: Fira julen på Olssons Bazar i Inre Hamn. Home Hotel Plaza har glöggmingel."]
good = "Här är ett förslag.\n- **Julbord**: Boka på **Olssons Bazar** i Inre Hamn.\n- Eller **Home Hotel Plaza**."
bad = good + "\n- Avsluta på **Caffè de Lune** eller Stadsgården.\n- Promenera i **Karlstads vattenterrass**."
check("fact check passes a correct answer", _unsupported_names(good, sources, "") == [])
missing = _unsupported_names(bad, sources, "")
check("fact check finds made-up places", {"Caffè de Lune", "Stadsgården", "Karlstads vattenterrass"} <= set(missing))
links = _source_links(sources, good)
check("source link for the page that was used", [x["site"] for x in links] == ["olssonsbazar.com"])

# --- speech text ----------------------------------------------------------------------------------------
check("markdown is not read aloud", _speakable("**Olssons Bazar** – [läs mer](https://x.se)") == "Olssons Bazar – läs mer")

print(f"\n{'Alla tester gick igenom' if not FAILS else f'{len(FAILS)} test(er) misslyckades'}.")
sys.exit(1 if FAILS else 0)
