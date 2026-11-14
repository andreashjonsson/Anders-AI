"""Anders' memory: saved conversations, conversation summaries and long-term facts.

* Conversations are stored as JSON in ~/.openjarvis/anders/conversations/.
  The latest one is resumed when the app starts; older ones can be reopened.
* When a conversation is left, a 1–2 sentence summary is written (background).
  The most recent summaries are given to the model so Anders can answer
  "vad pratade vi om igår?".
* Durable facts ("Andreas gillar grönt", "har en värmepump") come from
  OpenJarvis' own memory service ([memory] in config.toml, facts in
  ~/.openjarvis/memory_facts.jsonl). Relevant facts are injected into every
  question with OpenJarvis' inject_context — same memory as `jarvis chat`.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

CONV_DIR = Path(os.environ.get("ANDERS_CONV_DIR") or Path.home() / ".openjarvis" / "anders" / "conversations")
MAX_SUMMARIES_IN_PROMPT = 8

_SUMMARY_PROMPT = (
    "Sammanfatta samtalet nedan i högst två korta meningar på svenska. "
    "Skriv vad användaren ville och vad som kom fram eller bestämdes. "
    "Skriv i tredje person om användaren (Andreas). Svara bara med sammanfattningen."
)


_FACT_PROMPT = (
    "Du plockar ut varaktiga fakta om användaren Andreas ur ett enskilt utbyte i ett samtal. "
    "Ett bra faktum är stabilt över tid och användbart i framtida samtal: preferenser, "
    "identitet, familj och husdjur, hemmet, mål, pågående projekt, begränsningar eller relationer. "
    "Ignorera engångsfrågor, småprat och allt som assistenten sa om sig själv eller föreslog.\n\n"
    "Skriv varje faktum på SVENSKA, i tredje person om Andreas, och behåll namn och ord exakt "
    "som han sa dem (t.ex. gädda ska förbli gädda).\n"
    "Exempel på svar: [\"Andreas har en hund som heter Bamse.\", \"Andreas bygger en altan hemma.\"]\n\n"
    "Svara ENDAST med en JSON-lista med korta fakta (var och en under 200 tecken). "
    "Om det inte finns något värt att komma ihåg, svara med []."
)


class _WithKwargs:
    """Engine wrapper adding e.g. think='low' for gpt-oss (extraction would otherwise reason at full depth)."""

    def __init__(self, engine: Any, kwargs: Callable[[], dict]) -> None:
        self._engine, self._kwargs = engine, kwargs

    def generate(self, messages, **kw):
        return self._engine.generate(messages, **{**self._kwargs(), **kw})

    def __getattr__(self, name):
        return getattr(self._engine, name)


class ConversationStore:
    def __init__(self, root: Path = CONV_DIR) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _path(self, cid: str) -> Path:
        safe = "".join(c for c in cid if c.isalnum() or c in "-_")
        return self.root / f"{safe}.json"

    def new(self) -> dict:
        now = time.time()
        return {"id": time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6], "title": "",
                "created": now, "updated": now, "summary": "", "summarized_at": 0, "messages": []}

    def save(self, conv: dict, touch: bool = True) -> None:
        if not conv["messages"]:
            return  # never store empty conversations
        if touch:
            conv["updated"] = time.time()
        if not conv["title"]:
            first = next((m["text"] for m in conv["messages"] if m["role"] == "user"), "")
            conv["title"] = (first[:60] + "…") if len(first) > 60 else first
        with self._lock:
            tmp = self._path(conv["id"]).with_suffix(".tmp")
            tmp.write_text(json.dumps(conv, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self._path(conv["id"]))

    def load(self, cid: str) -> dict | None:
        try:
            return json.loads(self._path(cid).read_text(encoding="utf-8"))
        except Exception:
            return None

    def delete(self, cid: str) -> None:
        self._path(cid).unlink(missing_ok=True)

    def all(self) -> list[dict]:
        out = []
        for p in self.root.glob("*.json"):
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except Exception:
                continue
        return sorted(out, key=lambda c: c.get("updated", 0), reverse=True)

    def latest(self) -> dict | None:
        convs = self.all()
        return convs[0] if convs else None


def _when(ts: float) -> str:
    """Swedish relative day label: 'idag kl 14:05', 'igår kl 09:30', 'i måndags', '3 nov'."""
    months = ["jan", "feb", "mars", "april", "maj", "juni", "juli", "aug", "sep", "okt", "nov", "dec"]
    days = ["måndags", "tisdags", "onsdags", "torsdags", "fredags", "lördags", "söndags"]
    t, now = time.localtime(ts), time.localtime()
    delta = (time.mktime(now[:3] + (0, 0, 0) + now[6:]) - time.mktime(t[:3] + (0, 0, 0) + t[6:])) / 86400
    hm = f"kl {t.tm_hour:02d}:{t.tm_min:02d}"
    if delta < 0.5:
        return f"idag {hm}"
    if delta < 1.5:
        return f"igår {hm}"
    if delta < 7:
        return f"i {days[t.tm_wday]} {hm}"
    return f"{t.tm_mday} {months[t.tm_mon - 1]}" + ("" if t.tm_year == now.tm_year else f" {t.tm_year}")


def summaries_block(store: ConversationStore, exclude_id: str = "") -> str:
    """Prompt section with the most recent conversation summaries."""
    lines = []
    for c in store.all():
        if c["id"] == exclude_id or not c.get("summary"):
            continue
        lines.append(f"- {_when(c.get('updated', 0))}: {c['summary']}")
        if len(lines) >= MAX_SUMMARIES_IN_PROMPT:
            break
    if not lines:
        return ""
    weekdays = ["måndag", "tisdag", "onsdag", "torsdag", "fredag", "lördag", "söndag"]
    now = time.localtime()
    return ("\n\n## Tidigare samtal med användaren (senaste först)\n"
            f"Just nu är det {weekdays[now.tm_wday]} kl {now.tm_hour:02d}:{now.tm_min:02d}. "
            "Använd tidsangivelserna exakt som de står (t.ex. \"idag\", \"igår\") när användaren frågar "
            "om tidigare samtal; nämn dem annars bara om det är relevant.\n" + "\n".join(lines))


class Memory:
    """Glue between the app, the conversation store and OpenJarvis' fact memory."""

    def __init__(self, config: Any, engine: Any, model_getter: Callable[[], str],
                 generate_kwargs: Callable[[], dict]) -> None:
        self.config = config
        self.engine = engine
        self.model_getter = model_getter
        self.generate_kwargs = generate_kwargs
        self.store = ConversationStore()
        self.service = None
        self.backend = None
        self.enabled = True
        if os.environ.get("ANDERS_FACTS_PATH"):  # tests: keep the real memory untouched
            config.memory.facts_path = os.environ["ANDERS_FACTS_PATH"]
        try:
            from openjarvis.memory import build_memory_service

            self.service = build_memory_service(config, engine, model_getter())
            if self.service is not None:
                # Keep facts in the user's own language: translating them back and forth
                # caused errors in testing ("fishing for pike" came back as "abborre").
                self.service._extractor._system_prompt = _FACT_PROMPT  # noqa: SLF001
                self.service._extractor._engine = _WithKwargs(engine, generate_kwargs)  # noqa: SLF001
                self.service.start()
        except Exception:
            self.service = None
        try:
            from openjarvis.cli.ask import _get_memory_backend

            self.backend = _get_memory_backend(config) if config.agent.context_from_memory else None
        except Exception:
            self.backend = None

    # -- facts -----------------------------------------------------------------
    def set_model(self, model: str) -> None:
        """Keep fact extraction on the active model (avoids loading a second model)."""
        try:
            if self.service is not None:
                self.service._extractor._model = model  # noqa: SLF001
        except Exception:
            pass

    def facts(self) -> list:
        if self.service is not None:
            return self.service.list_facts()
        try:
            from openjarvis.memory import load_configured_facts

            return load_configured_facts(self.config)
        except Exception:
            return []

    def remember_exchange(self, user_text: str, reply: str) -> None:
        if self.enabled and self.service is not None and user_text.strip():
            self.service.submit(user_text, reply)

    def forget_fact(self, index: int, expected_text: str) -> bool:
        store = getattr(self.service, "_store", None)
        if store is None or not hasattr(store, "_facts"):
            return False
        with store._lock:  # noqa: SLF001
            if 0 <= index < len(store._facts) and store._facts[index].text == expected_text:
                del store._facts[index]
                store._flush()
                return True
        return False

    def forget_all(self) -> int:
        return self.service.clear_facts() if self.service is not None else 0

    def with_context(self, query: str, messages: list) -> list:
        """OpenJarvis context injection: relevant facts (+ indexed documents)."""
        if not self.enabled:
            return messages
        try:
            from openjarvis.tools.storage.context import ContextConfig, inject_context

            m = self.config.memory
            cfg = ContextConfig(top_k=m.context_top_k, min_score=m.context_min_score,
                                max_context_tokens=m.context_max_tokens)
            return inject_context(query, messages, self.backend, config=cfg, facts=self.facts())
        except Exception:
            return messages

    # -- summaries ---------------------------------------------------------------
    def summarize(self, conv: dict) -> None:
        """Write/refresh the summary of a conversation (blocking; call from a thread)."""
        msgs = [m for m in conv.get("messages", []) if m["role"] in ("user", "assistant")]
        if len(msgs) < 2 or conv.get("summarized_at", 0) >= conv.get("updated", 0):
            return
        from openjarvis.core.types import Message, Role

        transcript = "\n".join(f"{'Andreas' if m['role'] == 'user' else 'Anders'}: {m['text'][:600]}" for m in msgs[-30:])
        try:
            res = self.engine.generate(
                [Message(role=Role.SYSTEM, content=_SUMMARY_PROMPT), Message(role=Role.USER, content=transcript)],
                model=self.model_getter(), max_tokens=400, temperature=0.2, **self.generate_kwargs(),
            )
            text = (res.get("content", "") if isinstance(res, dict) else str(res)).strip()
        except Exception:
            return
        if text:
            fresh = self.store.load(conv["id"]) or conv
            fresh["summary"] = text.split("\n")[0][:400]
            fresh["summarized_at"] = time.time()
            self.store.save(fresh, touch=False)  # keep its place in the history list

    def summarize_pending(self, exclude_id: str = "") -> None:
        for c in self.store.all():
            if c["id"] != exclude_id:
                self.summarize(c)
