"""Pure text helpers for Anders (no audio, no models), so they can be tested on their own."""

from __future__ import annotations

import json
import re

_MORE = re.compile(r"^\s*(ett|en|kör en|ta en)\s+(till|annan|annat)\s*[!.?]*\s*$|^\s*(mer|fler|another( one)?|one more)\s*[!.?]*\s*$", re.I)


_KNOWN_NAMES = {"karlstad", "värmland", "sverige", "sweden", "värmlandstrafik", "stockholm", "göteborg", "anders",
                "andreas", "google", "spotify", "swish", "sj", "jul", "julafton", "nyår", "nyårsafton", "lucia", "påsk",
                "midsommar", "måndag", "tisdag", "onsdag", "torsdag", "fredag", "lördag", "söndag"}


def _unsupported_names(answer: str, sources: list[str], question: str = "") -> list[str]:
    """Proper names in *answer* (capitalised words mid-sentence) that appear nowhere in the sources."""
    blob = " ".join(sources).lower().replace("’", "'")
    q = question.lower()
    found: list[str] = []
    for line in answer.splitlines():
        # drop the list marker and a leading bold label ("- **Kväll**: ..."), then look mid-sentence only
        body = re.sub(r"^\s*(?:[-*•]|\d+[.)]|#{1,6})\s*", "", line)
        body = re.sub(r"^\*\*[^*]{1,40}\*\*\s*[:–-]\s*", "", body)
        for m in re.finditer(r"\*\*([A-ZÅÄÖ][^*]{2,60})\*\*", body):  # bold names, checked as a whole
            name = m.group(1).strip().rstrip(":.")
            key = name.lower().replace("’", "'")
            if m.start() == 0 or key in _KNOWN_NAMES or key in q:
                continue
            if key not in blob and re.sub(r"^(\w+?)s\b", r"\1", key) not in blob and name not in found:
                found.append(name)
        plain = body.replace("**", "")
        for m in re.finditer(r"[A-ZÅÄÖ][\wåäöéü'’&-]+(?:\s+[A-ZÅÄÖ][\wåäöéü'’&-]*)*", plain):
            start = m.start()
            if start == 0 or re.search(r"[.!?:]\s*[\"“”]?$", plain[:start]):
                continue  # first word of a sentence, not necessarily a name
            name = m.group(0).rstrip(".’'").strip()
            key = name.lower().replace("’", "'")
            if len(key) < 3 or key in _KNOWN_NAMES or key in q:
                continue
            stems = {key, key[:-1] if key.endswith("s") else key}
            if not any(st in blob for st in stems) and name not in found:
                found.append(name)
    return found


def _source_links(results: list[str], answer: str, limit: int = 4) -> list[dict]:
    """The web pages an answer actually used: search hits/pages that mention names from the answer."""
    from urllib.parse import urlparse

    items: list[dict] = []
    for res in results:
        try:
            page = json.loads(res)
            if isinstance(page, dict) and page.get("url"):
                items.append({"title": page.get("titel") or "", "url": page["url"], "text": page.get("text", ""),
                              "read": True})
                continue
        except (ValueError, TypeError):
            pass
        for m in re.finditer(r"###\s*(.+?)\n\s*Source:\s*(\S+)(.*?)(?=\n###|\Z)", res, re.S):
            items.append({"title": m.group(1).strip(), "url": m.group(2).strip(), "text": m.group(3), "read": False})
    names = {n.strip().lower() for n in re.findall(r"\*\*([^*]{3,60})\*\*", answer)}
    names |= {w.lower() for w in re.findall(r"\b[A-ZÅÄÖ][\wåäö&'-]{3,}(?:\s+[A-ZÅÄÖ][\wåäö&'-]+)*", answer)}
    out, seen = [], set()
    for it in items:
        host = urlparse(it["url"]).netloc.removeprefix("www.")
        if not host or it["url"] in seen:
            continue
        blob = (it["title"] + " " + it["text"]).lower()
        score = sum(1 for n in names if len(n) > 3 and n in blob) + (2 if it["read"] else 0)
        if score:
            seen.add(it["url"])
            out.append({"title": (it["title"] or host)[:90], "url": it["url"], "site": host, "score": score})
    out.sort(key=lambda x: -x["score"])
    return [{k: v for k, v in x.items() if k != "score"} for x in out[:limit]]


def _speakable(text: str) -> str:
    """Drop Markdown symbols so the voice doesn't read them."""
    text = re.sub(r"[*_`#]+", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)  # [text](link) -> text
    return re.sub(r"\s+", " ", text).strip()


_SCHEDULE_TOOLS = {"set_timer", "set_alarm", "set_reminder"}
_SCHEDULE_WORDS = re.compile(
    r"påminn|påminnelse|remind|timer|larm|alarm|väck|wake|nedräkning|countdown|"
    r"minut|minute|sekund|second|timm|hour|klockan|o'?clock|\bkl\b|\d{1,2}[:.]\d{2}|"
    r"imorgon|i morgon|tomorrow|ikväll|i kväll|tonight|\bom \d|\bin \d", re.I)


def _asked_to_schedule(msgs: list) -> bool:
    """True if the user's latest message actually asks for a timer, alarm or reminder."""
    for m in reversed(msgs):
        if m.get("role") == "user":
            return bool(_SCHEDULE_WORDS.search(m.get("content") or ""))
    return True
