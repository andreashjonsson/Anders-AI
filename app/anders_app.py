"""Anders desktop app — backend.

Runs in the OpenJarvis venv. Serves the UI (static/index.html) on
http://127.0.0.1:8770 and talks to it over a WebSocket (/ws).

Pipeline:  mic -> wake phrase "Hej/Hey Anders" -> Whisper (sv/en)
           -> gpt-oss via OpenJarvis' engine (streamed, with SOUL/USER persona)
           -> sentences spoken by the Chatterbox server as they arrive.

Reuses the add-ons in ~/.openjarvis/plugins (wake/STT helpers, TTS client).
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import queue
import uuid
from collections import OrderedDict
import subprocess
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path.home() / ".openjarvis" / "plugins"))

import numpy as np  # noqa: E402
import sounddevice as sd  # noqa: E402
import soundfile as sf  # noqa: E402
import uvicorn  # noqa: E402
from fastapi import FastAPI, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.responses import FileResponse, HTMLResponse  # noqa: E402

import jarvis_bilingual_tts as tts  # noqa: E402
import jarvis_handsfree as hf  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))
from anders_memory import Memory, summaries_block  # noqa: E402
import anders_tools  # noqa: E402
from anders_alarms import Scheduler, fmt_duration  # noqa: E402
from anders_system import SystemControl  # noqa: E402
import anders_songs  # noqa: E402
import anders_roasts  # noqa: E402
import anders_jokes  # noqa: E402
import anders_hearing  # noqa: E402
from anders_text import (_MORE, _SCHEDULE_TOOLS, _asked_to_schedule, _source_links,  # noqa: E402
                         _speakable, _unsupported_names)

import faulthandler  # noqa: E402
import signal  # noqa: E402

faulthandler.register(signal.SIGUSR1, all_threads=True)  # `kill -USR1 <pid>` dumps all threads to the log

HERE = Path(__file__).parent
PORT = int(os.environ.get("ANDERS_APP_PORT", "8770"))
SETTINGS_FILE = Path(os.environ.get("ANDERS_SETTINGS_FILE") or Path.home() / ".openjarvis" / "anders-settings.json")
SR = 16000
BLOCK = 1024

# ---------------------------------------------------------------------------
# Settings (persisted; applied live through the env vars the TTS client reads)
# ---------------------------------------------------------------------------
DEFAULTS = {
    "handsfree": True,
    "followup_seconds": float(os.environ.get("JARVIS_FOLLOWUP_SECONDS", "5")),
    "silence_seconds": float(os.environ.get("JARVIS_SILENCE_SECONDS", "1.0")),
    "warmth": float(os.environ.get("JARVIS_TTS_WARMTH", "4")),
    "exaggeration": float(os.environ.get("JARVIS_TTS_EXAGGERATION", "0.3")),
    "pitch": float(os.environ.get("JARVIS_TTS_PITCH", "0")),
    "speak_replies": True,
    "speak_typed": True,  # also speak replies to typed messages (voice questions always get spoken replies)
    "model": "",  # "" = OpenJarvis default_model
    "reasoning": "low",  # gpt-oss reasoning effort: low | medium | high
    "memory": True,  # remember facts + earlier conversations
    "computer_control": True,  # volume, brightness, apps, media, shut down (with confirmation) …
    "tools": True,  # internet, weather, time, location, calculator
    "home_location": "",  # e.g. "Karlstad"; empty = approximate location from the IP address
    "roasts": True,  # hand-written comebacks when someone insults Anders (roasts.txt)
    "jokes": True,  # hand-written jokes when someone asks for one (jokes.txt)
    "fact_check": True,  # check longer web-based answers against the sources before showing them
}
_ENV_MAP = {
    "warmth": "JARVIS_TTS_WARMTH",
    "exaggeration": "JARVIS_TTS_EXAGGERATION",
    "pitch": "JARVIS_TTS_PITCH",
    "followup_seconds": "JARVIS_FOLLOWUP_SECONDS",
    "silence_seconds": "JARVIS_SILENCE_SECONDS",
}


def load_settings() -> dict:
    s = dict(DEFAULTS)
    try:
        s.update({k: v for k, v in json.loads(SETTINGS_FILE.read_text()).items() if k in DEFAULTS})
    except Exception:
        pass
    return s


def apply_settings(s: dict) -> None:
    for k, env in _ENV_MAP.items():
        os.environ[env] = str(s[k])


SETTINGS = load_settings()
apply_settings(SETTINGS)


# ---------------------------------------------------------------------------
# Event bus: worker threads -> all connected UIs
# ---------------------------------------------------------------------------
class Bus:
    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self.clients: set[WebSocket] = set()
        self.last_state = {"type": "state", "state": "starting", "detail": "Startar…"}

    def emit(self, msg: dict) -> None:
        if msg.get("type") == "state":
            self.last_state = msg
        if self.loop is not None:
            self.loop.call_soon_threadsafe(asyncio.ensure_future, self._send(msg))

    async def _send(self, msg: dict) -> None:
        data = json.dumps(msg)
        for ws in list(self.clients):
            try:
                await ws.send_text(data)
            except Exception:
                self.clients.discard(ws)


BUS = Bus()


_T0 = time.time()


def _setup_log():
    """~/.openjarvis/.state/anders.log: what Anders heard, did and what went wrong (max 3 x 1 MB)."""
    import logging
    import logging.handlers

    logger = logging.getLogger("anders")
    if not logger.handlers:
        path = Path.home() / ".openjarvis" / ".state" / "anders.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        h = logging.handlers.RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S"))
        logger.addHandler(h)
        logger.setLevel(logging.DEBUG if os.environ.get("ANDERS_DEBUG") else logging.INFO)

        def hook(args):  # crashes in background threads end up in the log instead of vanishing
            logger.error("crash in thread %s", getattr(args.thread, "name", "?"),
                         exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
        threading.excepthook = hook
    return logger


log = _setup_log()


def _dbg(msg: str) -> None:
    log.debug(msg)
    if os.environ.get("ANDERS_DEBUG"):
        print(f"[{time.time() - _T0:7.2f}] {msg}", file=sys.stderr, flush=True)


def state(name: str, detail: str = "") -> None:
    BUS.emit({"type": "state", "state": name, "detail": detail})


# ---------------------------------------------------------------------------
# Assistant core
# ---------------------------------------------------------------------------
class Anders:
    def __init__(self) -> None:
        self.inbox: "queue.Queue[tuple[str, Any]]" = queue.Queue()  # ("text"|"voice", payload)
        self.stop_speaking = threading.Event()
        self.cancel_reply = threading.Event()
        self.ptt = threading.Event()  # "Prata" button: listen once without wake phrase
        from collections import deque
        self.turn_sources: list[dict] = []  # web pages the current answer is based on (shown under it)
        self.last_canned: tuple[str, float] | None = None  # ("song" | "joke" | "roast", when)
        self.voice_levels: deque = deque(maxlen=20)  # loudness of recent wake-phrase utterances (the user's voice)
        self.muted = not SETTINGS["handsfree"]
        self.busy = threading.Event()  # thinking/speaking: mic is closed (no echo)
        self.followup_lang: str | None = None
        self.voice_engine = "…"
        self.shutdown = threading.Event()
        self._init_llm()

    # -- LLM ---------------------------------------------------------------
    def _init_llm(self) -> None:
        from openjarvis.core.config import load_config
        from openjarvis.core.types import Message, Role
        from openjarvis.engine import get_engine
        from openjarvis.prompt.builder import SystemPromptBuilder

        self.Message, self.Role = Message, Role
        self.config = load_config()
        self.model = (os.environ.get("ANDERS_MODEL") or SETTINGS.get("model")
                      or self.config.intelligence.default_model or "gpt-oss-coder")
        if ":" not in self.model:
            self.model += ":latest"  # match Ollama's tag names
        self.switching = threading.Lock()
        resolved = get_engine(self.config, self.config.engine.default or None)
        self.engine = resolved[1] if isinstance(resolved, tuple) else resolved
        builder = SystemPromptBuilder(
            agent_template=self.config.agent.default_system_prompt or "",
            memory_files_config=self.config.memory_files,
            system_prompt_config=self.config.system_prompt,
        )
        self.system_prompt = builder.build()
        self.memory = Memory(self.config, self.engine, lambda: self.model, self._gen_kwargs)
        self.memory.enabled = SETTINGS.get("memory", True)
        self.alarms = Scheduler(on_fire=self._on_alarm,
                                on_change=lambda items: BUS.emit({"type": "timers", "items": items}))
        self.ring_stop = threading.Event()
        self.ringing: dict | None = None
        self.snooze_requested = False
        self.pending: dict | None = None  # action waiting for the user's own confirmation
        self.countdown_cancel = threading.Event()
        self.counting_down: dict | None = None
        self._after_reply: list = []
        self.system = SystemControl(request_confirm=self._request_confirm, after_reply=self._after_reply.append)
        self.tools = anders_tools.Tools(lambda: SETTINGS.get("home_location", ""), self._remember_note, self.alarms,
                                        self.system)
        threading.Thread(target=self._warm_location, daemon=True).start()
        latest = self.memory.store.latest() if self.memory.enabled else None
        self._open(latest or self.memory.store.new())

    _pending_facts: list = []
    last_activity = 0.0

    def fact_worker(self) -> None:
        while not self.shutdown.is_set():
            time.sleep(1.0)
            if (self._pending_facts and not self.busy.is_set() and self.inbox.empty()
                    and time.time() - self.last_activity > 8 and not self.switching.locked()):
                text, reply = self._pending_facts.pop(0)
                self.memory.remember_exchange(text, reply)

    # -- computer control: confirmation & countdown ------------------------------------
    YES = re.compile(r"^\W*(ja|japp|jajamän|absolut|kör|gör det|bekräfta|bekräftar|visst|yes|yeah|do it|confirm)\b", re.I)
    NO = re.compile(r"^\W*(nej|nä|nej tack|avbryt|stopp|stoppa|ångra|vänta|no|cancel|stop)\b", re.I)

    def _request_confirm(self, action: dict) -> None:
        self.pending = {**action, "id": uuid.uuid4().hex[:6], "expires": time.time() + 45}
        BUS.emit({"type": "confirm", "id": self.pending["id"], "label": self.pending["label"]})

    def _clear_pending(self, emit: bool = True) -> None:
        if self.pending is not None:
            self.pending = None
            if emit:
                BUS.emit({"type": "confirm_off"})

    def pending_active(self) -> bool:
        if self.pending is not None and time.time() > self.pending["expires"]:
            self._clear_pending()
        return self.pending is not None

    def confirm_pending(self, lang: str = "sv", via: str = "voice") -> None:
        """Runs ONLY from the user's own 'ja' or the Bekräfta button (never from the language model)."""
        action = self.pending
        self._clear_pending()
        if action is None:
            return
        if action.get("countdown"):
            threading.Thread(target=self._countdown, args=(action, lang, via), daemon=True).start()
        else:
            try:
                action["run"]()
            except Exception as exc:
                BUS.emit({"type": "info", "text": f"Det gick inte: {exc}"})
            self._say("Okej, klart." if lang == "sv" else "Okay, done.", lang, via)

    def _countdown(self, action: dict, lang: str, via: str = "voice") -> None:
        n = int(action["countdown"])
        self.countdown_cancel.clear()
        self.counting_down = action
        self._say(f"Okej. {action['countdown_text']} om {n} sekunder. Säg avbryt om du ångrar dig.", "sv", via)
        try:
            for left in range(n, 0, -1):
                BUS.emit({"type": "countdown", "label": action["countdown_text"], "seconds": left})
                if self.countdown_cancel.wait(1.0):
                    BUS.emit({"type": "countdown_off", "cancelled": True})
                    self._say("Avbrutet." if lang == "sv" else "Cancelled.", lang, via)
                    return
            BUS.emit({"type": "countdown_off", "cancelled": False})
            action["run"]()
        finally:
            self.counting_down = None

    def cancel_countdown(self) -> bool:
        if self.counting_down is None:
            return False
        self.countdown_cancel.set()
        return True

    def _usual_level(self) -> float:
        levels = sorted(self.voice_levels)
        return levels[len(levels) // 2] if levels else 0.0

    def _loud_enough(self, level: float) -> bool:
        """Follow-ups without wake phrase must be roughly as loud as the user's own voice (not the TV)."""
        if len(self.voice_levels) < 3:
            return True  # still learning the user's level
        return level >= 0.35 * self._usual_level()

    def _say(self, text: str, lang: str, via: str = "voice") -> None:
        if via == "text" and not (SETTINGS["speak_replies"] and SETTINGS.get("speak_typed", True)):
            BUS.emit({"type": "info", "text": text})
        else:
            self.inbox.put(("replay", {"text": text, "lang": lang, "id": ""}))
            BUS.emit({"type": "info", "text": text})

    def intercept(self, text: str, lang: str, via: str = "voice") -> bool:
        """Handle yes/no for a pending action and 'avbryt' during a countdown. True = consumed."""
        if self.counting_down is not None and self.NO.match(text):
            self.cancel_countdown()
            return True
        if self.pending_active():
            if self.YES.match(text):
                self.confirm_pending(lang, via)
                return True
            self._clear_pending()
            if self.NO.match(text) and len(text.split()) <= 3:
                self._say("Okej, jag låter bli." if lang == "sv" else "Okay, I won't.", lang, via)
                return True
        return False

    def _run_after_reply(self) -> None:
        actions, self._after_reply[:] = list(self._after_reply), []
        for fn in actions:
            try:
                fn()
            except Exception as exc:
                BUS.emit({"type": "info", "text": f"Det gick inte: {exc}"})

    # -- timers & alarms ringing ---------------------------------------------------
    _chime_cache = None

    @classmethod
    def _chime(cls):
        """A soft three-note chime (generated, no sound files needed)."""
        if cls._chime_cache is None:
            sr = 24000
            out = np.zeros(int(sr * 1.6), dtype=np.float32)
            for f, start in [(659.25, 0.0), (783.99, 0.22), (1046.5, 0.44)]:
                t = np.arange(int(sr * 1.1)) / sr
                tone = (np.sin(2 * np.pi * f * t) + 0.3 * np.sin(2 * np.pi * 2 * f * t)) * np.exp(-3.2 * t)
                i = int(start * sr)
                out[i:i + t.size] += (0.22 * tone[: out.size - i]).astype(np.float32)
            cls._chime_cache = (out, sr)
        return cls._chime_cache

    @staticmethod
    def alarm_message(item: dict) -> str:
        label = item.get("label") or ""
        if item["kind"] == "timer":
            return f"Timern för {label} är klar!" if label else f"Timern på {fmt_duration(item.get('duration', 0))} är klar!"
        if item["kind"] == "alarm":
            now = time.strftime("%H:%M")
            return f"Larm: {label}! Klockan är {now}." if label else f"God morgon! Det är dags att vakna, klockan är {now}."
        return f"Påminnelse: {label}."

    def _on_alarm(self, item: dict) -> None:
        threading.Thread(target=self._ring, args=(item,), daemon=True).start()

    def _ring(self, item: dict) -> None:
        while self.ringing is not None and not self.shutdown.is_set():  # one at a time
            time.sleep(0.3)
        self.ringing, msg = item, self.alarm_message(item)
        self.ring_stop.clear()
        self.snooze_requested = False
        BUS.emit({"type": "alarm", "item": item, "message": msg})
        try:
            subprocess.Popen(["notify-send", "-u", "critical", "-a", "Anders", "Anders", msg],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
        speech = None
        try:
            for _cycle in range(6):  # about a minute, unless stopped
                if self.ring_stop.is_set() or self.shutdown.is_set():
                    break
                while self.busy.is_set() and not self.ring_stop.is_set():  # don't talk over an answer
                    time.sleep(0.2)
                chime, sr = self._chime()
                self._play_interruptible(chime, sr)
                if self.ring_stop.is_set():
                    break
                if speech is None:
                    try:
                        speech = self._synth(msg, "sv")
                    except Exception:
                        speech = b""
                if speech:
                    data, ssr = sf.read(io.BytesIO(speech), dtype="float32")
                    self._play_interruptible(data if data.ndim == 1 else data.mean(axis=1), ssr)
                for _ in range(40):  # ~8 s pause between repeats
                    if self.ring_stop.is_set():
                        break
                    time.sleep(0.2)
        finally:
            if self.snooze_requested:
                self.alarms.snooze(item, 9)
            self.ringing = None
            BUS.emit({"type": "alarm_off", "snoozed": self.snooze_requested})

    def _play_interruptible(self, data, sr: int) -> None:
        block = int(sr * 0.05)
        with sd.OutputStream(samplerate=sr, channels=1, dtype="float32", blocksize=block) as out:
            for i in range(0, len(data), block):
                if self.ring_stop.is_set():
                    break
                chunk = np.asarray(data[i:i + block], dtype=np.float32)
                BUS.emit({"type": "level", "v": float(min(1.0, np.sqrt((chunk ** 2).mean()) * 6))})
                out.write(chunk.reshape(-1, 1))

    def stop_ringing(self, snooze: bool = False) -> bool:
        if self.ringing is None:
            return False
        self.snooze_requested = snooze
        self.ring_stop.set()
        return True

    # -- tools -----------------------------------------------------------------
    def _remember_note(self, note: str) -> None:
        store = getattr(self.memory.service, "_store", None)
        if store is not None:
            n = time.localtime()
            stamp = f"(sparat {anders_tools.WEEKDAYS[n.tm_wday]} {n.tm_mday} {anders_tools.MONTHS[n.tm_mon - 1]} {n.tm_year})"
            store.add_with_trust(f"{note.strip()} {stamp}", source="user", trust="trusted")

    def _warm_location(self) -> None:
        try:
            self.tools.loc.current()
        except Exception:
            pass

    def _context_note(self) -> str:
        """Time + place for the system prompt (answers simple questions without a tool call)."""
        note = f"\n\n## Just nu\nDatum och tid: {anders_tools.now_text()}."
        try:
            loc = self.tools.loc.current()
            if loc:
                note += f" Användarens plats: {', '.join(x for x in (loc['name'], loc['region']) if x)} ({loc['source']})."
        except Exception:
            pass
        if SETTINGS.get("tools", True):
            note += ("\n\n## Verktyg\nDu har verktyg för väder (yr.no), webbsökning, att läsa webbsidor, plats, tid, "
                     "kalkylator, anteckningar, timers, larm och påminnelser, listor (t.ex. inköpslistan), elpris, "
                     "svenska kalenderdagar (namnsdag, helgdagar), SVT-nyheter, Wikipedia, valutaomräkning och att "
                     "öppna webbsidor. Använd dem när frågan kräver aktuell, lokal eller exakt information eller en "
                     "handling (timer, larm, lista) – gissa aldrig och påstå aldrig att något är gjort utan verktyget. "
                     "Bekräfta timers och larm kort med tiden. "
                     + ("Du kan också styra datorn (volym, ljusstyrka, musik, öppna/stänga program, mörkt läge, stör ej, "
                        "status, lås, vila, stäng av, starta om, logga ut). Anropa ALLTID datorverktyget direkt när användaren ber "
                        "om något sådant – fråga aldrig själv först, verktyget sköter bekräftelsen. Om verktyget svarar "
                        "'väntar_på_bekräftelse': be användaren kort säga ja eller trycka Bekräfta, och påstå aldrig att "
                        "det redan är gjort. " if SETTINGS.get("computer_control", True) else "")
                     + 
                     "Vid frågor om detaljer som öppettider, priser, datum eller adresser: läs källsidan med fetch_webpage "
                     "innan du svarar – sökresultatens korta utdrag är ofta ofullständiga eller gamla. "
                     "Svara sedan kort och naturligt med det viktigaste. Läs aldrig upp webbadresser; "
                     "nämn källan med namn om det är relevant (t.ex. 'enligt SMHI'). "
                     "Väderdata gäller användarens plats om inget annat sägs."
                     "\n\n## Riktiga ställen\nNär användaren vill ha förslag på restauranger, kaféer, barer, butiker, "
                     "evenemang, aktiviteter eller sevärdheter på en ort (t.ex. planera en dejt, vad kan man göra i X): "
                     "gör FLERA sökningar med web_search, en per del av svaret (t.ex. 'restauranger Karlstad', "
                     "'julbord Karlstad 2026', 'kafé Karlstad centrum', 'evenemang Karlstad december'). "
                     "Varje namn på ett ställe, evenemang eller en scen som du skriver MÅSTE stå ordagrant i ett sökresultat "
                     "eller på en sida du läst med fetch_webpage. Skriv aldrig ett namn du inte har sett i resultaten, inte ens "
                     "om du tror att stället finns. Skriv datum, tider och scener exakt som i källan. Föreslå inte "
                     "medlems- eller privata aktiviteter. Saknas förslag för någon del, skriv det och hänvisa till var man "
                     "kan kolla (t.ex. karlstad.se). "
                     "Kollektivtrafiken i Värmland heter Värmlandstrafik.")
        note += ("\n\n## Formatering\nKorta svar (fakta, väder, timer, småprat): 1-3 meningar utan Markdown. "
                 "Längre svar (planer, förslag på flera ställen, listor, guider, recept, jämförelser, steg-för-steg) "
                 "visas på skärmen och ska formateras snyggt i Markdown, som ChatGPT: börja med EN kort pratig mening "
                 "i vanlig text, utan fetstil eller rubrik (bara den läses upp), sedan korta avsnitt med ### rubriker, punktlistor med -, "
                 "**fetstil** för namn på ställen och tider, och korta rader. Inga tabeller och inga emojis. "
                 "Håll det under ungefär 20 rader och avsluta med en kort mening.")
        return note

    def _chat(self, messages: list, on_token, on_tool, max_rounds: int = 5) -> str:
        """Stream a reply from Ollama, running tool calls in between (tool calling + streaming)."""
        import httpx

        def to_dict(m):
            return m if isinstance(m, dict) else {"role": getattr(m.role, "value", str(m.role)), "content": m.content}

        msgs = [to_dict(m) for m in messages]
        use_tools = SETTINGS.get("tools", True)
        think = (SETTINGS.get("reasoning") or "low") if "gpt-oss" in self.model else False
        final = ""
        sources: list[str] = []  # web results this answer is based on (for the fact check)
        for _round in range(max_rounds + 1):
            # After a web search the answer is held back until it has been checked against the sources.
            hold = bool(sources) and SETTINGS.get("fact_check", True)
            payload = {"model": self.model, "messages": msgs, "stream": True, "think": think,
                       "keep_alive": "10m", "options": {"num_predict": 1024, "num_ctx": _num_ctx()}}
            if use_tools and _round < max_rounds:
                payload["tools"] = [t for t in anders_tools.TOOL_SPECS if SETTINGS.get("computer_control", True)
                                    or not t["function"]["name"].startswith("computer_")]
            content, calls = [], []
            with httpx.stream("POST", "http://127.0.0.1:11434/api/chat", json=payload, timeout=httpx.Timeout(300, connect=10)) as r:
                if r.status_code == 400 and "tools" in payload:  # model without tool support
                    use_tools = False
                    continue
                r.raise_for_status()
                for line in r.iter_lines():
                    if self.cancel_reply.is_set():
                        return "".join(content)
                    if not line:
                        continue
                    chunk = json.loads(line)
                    msg = chunk.get("message") or {}
                    tok = msg.get("content") or ""
                    if tok:
                        content.append(tok)
                        if not hold:
                            on_token(tok)
                    calls.extend(msg.get("tool_calls") or [])
                    if chunk.get("done"):
                        break
            final = "".join(content)
            _dbg(f"round {_round}: {len(calls)} tool call(s), {len(final)} chars")
            if not calls:
                if hold:
                    final = self._fact_check(final, sources, msgs)
                    on_token(final)
                self.turn_sources = _source_links(sources, final)
                return final
            msgs.append({"role": "assistant", "content": final, "tool_calls": calls})
            for c in calls:
                fn = c.get("function") or {}
                name, args = fn.get("name", ""), fn.get("arguments") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = {}
                if name in _SCHEDULE_TOOLS and not _asked_to_schedule(msgs):
                    # gpt-oss sometimes answers insults/banter with a reminder or timer; block that.
                    _dbg(f"blocked {name}({args}): the user didn't ask for a timer/alarm/reminder")
                    msgs.append({"role": "tool", "tool_name": name, "content": json.dumps({
                        "ok": False, "fel": "Användaren bad inte om någon timer, larm eller påminnelse. "
                        "Skapa inget. Svara i stället direkt på det användaren sa, i din vanliga stil."},
                        ensure_ascii=False)})
                    continue
                on_tool(name, args)
                result = self.tools.run(name, args)
                log.info("tool %s(%s) -> %d chars", name, json.dumps(args, ensure_ascii=False)[:200], len(result))
                if name in ("web_search", "fetch_webpage"):
                    sources.append(result)
                _dbg(f"tool {name}({args}) -> {result[:120]!r}")
                msgs.append({"role": "tool", "content": result, "tool_name": name})
        return final

    def _fact_check(self, draft: str, sources: list[str], msgs: list) -> str:
        """Second pass for longer web-based answers: drop names and details the sources don't support."""
        if len(draft) < 350 and "\n" not in draft.strip():
            return draft  # short factual answer: nothing to gain
        if self.cancel_reply.is_set():
            return draft
        question = next((m.get("content", "") for m in reversed(msgs) if m.get("role") == "user"), "")
        state("thinking", "Faktakollar…")
        missing = _unsupported_names(draft, sources, question)
        log.info("fact check: names not in the sources: %s", missing or "none")
        if not missing:
            return draft  # every name in the answer is in the sources: no slow rewrite needed
        src = "\n\n---\n\n".join(sources)[-14000:]
        prompt = (
            "Du är faktagranskare. Nedan finns en fråga, KÄLLOR från webbsökningar och ett UTKAST till svar.\n"
            "Skriv om utkastet så att det bara innehåller sådant som stöds av källorna:\n"
            "- Varje namn på ett ställe, en restaurang, ett kafé, en butik, ett evenemang, en artist, en scen, en gata "
            "eller en webbsida måste stå i källorna. Står det inte där: ta bort det.\n"
            "- Rätta stavningen av namn så att den stämmer exakt med källorna.\n"
            "- Datum, tider, priser och adresser: behåll bara om de står i källorna, annars ta bort dem.\n"
            "- Ta bort sådant som enligt källorna bara är för medlemmar eller privata grupper.\n"
            "- Allmänna tips utan namn (klä er varmt, boka i förväg, ta en promenad) får stå kvar. Men om ett allmänt "
            "tips nämner en namngiven plats, gata, park eller byggnad som inte står i källorna: skriv om tipset utan namnet.\n"
            "- Lägg inte till något nytt. Behåll Markdown-formateringen, tonen och den första vanliga meningen.\n"
            "- Om en hel del blir tom, ta bort den rubriken.\n"
            "Svara ENDAST med det färdiga svaret, utan kommentarer om granskningen.\n"
            f"En automatisk kontroll hittade INTE följande namn i källorna: {', '.join(missing)}. "
            "Ta bort dem (eller hela punkten), om du inte ser att de ändå står i källorna med annan stavning.\n\n"
            f"FRÅGA:\n{question}\n\nKÄLLOR:\n{src}\n\nUTKAST:\n{draft}")
        try:
            import httpx
            r = httpx.post("http://127.0.0.1:11434/api/chat", timeout=httpx.Timeout(300, connect=10), json={
                "model": self.model, "stream": False, "keep_alive": "10m",
                "think": "low" if "gpt-oss" in self.model else False,
                "options": {"num_predict": 1500, "num_ctx": _num_ctx(), "temperature": 0.2},
                "messages": [{"role": "user", "content": prompt}]})
            r.raise_for_status()
            checked = ((r.json().get("message") or {}).get("content") or "").strip()
        except Exception as exc:
            _dbg(f"fact check failed: {exc}")
            return draft
        _dbg(f"fact check: {len(draft)} -> {len(checked)} chars")
        return checked if len(checked) > 40 else draft

    # -- conversations -------------------------------------------------------
    def _gen_kwargs(self) -> dict:
        return {"think": SETTINGS.get("reasoning") or "low"} if "gpt-oss" in self.model else {}

    def _open(self, conv: dict) -> None:
        """Make *conv* the active conversation and rebuild the model history from it."""
        self.conv = conv
        system = self.system_prompt or ""
        if self.memory.enabled:
            system += summaries_block(self.memory.store, exclude_id=conv["id"])
        self.history = [self.Message(role=self.Role.SYSTEM, content=system)] if system else []
        for m in conv["messages"]:
            role = self.Role.USER if m["role"] == "user" else self.Role.ASSISTANT
            self.history.append(self.Message(role=role, content=m["text"]))

    def _summarize_later(self, conv: dict) -> None:
        if self.memory.enabled and len(conv.get("messages", [])) >= 2:
            threading.Thread(target=self.memory.summarize, args=(conv,), daemon=True).start()

    def reset(self) -> None:
        """Start a new conversation (the old one is kept and summarized)."""
        if getattr(self, "conv", None) is not None:
            self._summarize_later(self.conv)
        self._open(self.memory.store.new())

    def open_conversation(self, cid: str) -> bool:
        if cid == self.conv["id"]:
            return True
        conv = self.memory.store.load(cid)
        if conv is None:
            return False
        self._summarize_later(self.conv)
        self._open(conv)
        return True

    def conversation_event(self) -> dict:
        return {"type": "conversation", "id": self.conv["id"], "title": self.conv.get("title", ""),
                "messages": self.conv["messages"]}

    def conversations_event(self) -> dict:
        items = [{"id": c["id"], "title": c.get("title") or "(utan titel)", "updated": c.get("updated", 0),
                  "summary": c.get("summary", ""), "count": len(c.get("messages", []))}
                 for c in self.memory.store.all()]
        return {"type": "conversations", "items": items, "current": self.conv["id"]}

    def facts_event(self) -> dict:
        facts = [{"i": i, "text": f.text, "trust": f.trust or "", "created": f.created_at}
                 for i, f in enumerate(self.memory.facts())]
        return {"type": "facts", "items": facts, "enabled": self.memory.enabled,
                "service": self.memory.service is not None}

    def warm_up(self, label: str = "Laddar språkmodellen…") -> None:
        """Load the LLM into memory (after the voice server took its GPU share)."""
        try:
            state("starting", label)
            asyncio.run(self._collect([self.Message(role=self.Role.USER, content="Hej")], max_tokens=1))
        except Exception as exc:
            BUS.emit({"type": "info", "text": f"Kunde inte ladda språkmodellen: {exc}"})

    # -- models --------------------------------------------------------------
    @staticmethod
    def _ollama(path: str, body: dict | None = None, timeout: float = 5.0) -> dict:
        import urllib.request

        host = "http://127.0.0.1:11434"
        req = urllib.request.Request(host + path, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")

    def list_models(self) -> list[dict]:
        """Chat models available in Ollama (embedding-only models are left out)."""
        try:
            tags = self._ollama("/api/tags").get("models", [])
        except Exception:
            return [{"name": self.model, "size_gb": None, "params": "", "family": ""}]
        out = []
        for m in tags:
            d = m.get("details") or {}
            if "embed" in (d.get("family") or "") or "embed" in m["name"]:
                continue
            out.append({"name": m["name"], "size_gb": round(m.get("size", 0) / 1e9, 1),
                        "params": d.get("parameter_size", ""), "family": d.get("family", "")})
        return sorted(out, key=lambda x: x["name"])

    def set_model(self, name: str) -> None:
        """Switch model mid-conversation (history is kept). Runs in a worker thread."""
        if name == self.model:
            return
        with self.switching:
            old, self.model = self.model, name
            self.memory.set_model(name)
            SETTINGS["model"] = name
            SETTINGS_FILE.write_text(json.dumps(SETTINGS, indent=2))
            BUS.emit({"type": "settings", "settings": SETTINGS, "model": name})
            was = BUS.last_state
            label = f"Byter till {name.replace(':latest', '')}…"
            state("starting", label)
            try:  # free the old model's memory first so the new one gets the GPU
                self._ollama("/api/generate", {"model": old, "keep_alive": 0}, timeout=15)
            except Exception:
                pass
            t = time.time()
            self.warm_up(label)
            BUS.emit({"type": "info", "text": f"Bytte modell till {name.replace(':latest', '')} "
                                              f"(laddades på {time.time() - t:.0f} s). Konversationen fortsätter."})
            if was.get("state") not in ("starting", "thinking", "speaking"):
                BUS.emit(was)

    async def _collect(self, messages, max_tokens: int = 1024, on_token=None) -> str:
        out = []
        kwargs = self._gen_kwargs()  # gpt-oss reasoning effort: low = much faster first word
        async for tok in self.engine.stream(messages, model=self.model, max_tokens=max_tokens, **kwargs):
            if self.cancel_reply.is_set():
                break
            out.append(tok)
            if on_token:
                on_token(tok)
        return "".join(out)

    # -- speaking ----------------------------------------------------------
    def _play(self, wav: bytes) -> None:
        data, sr = sf.read(io.BytesIO(wav), dtype="float32")
        if data.ndim > 1:
            data = data.mean(axis=1)
        block = int(sr * 0.05)
        with sd.OutputStream(samplerate=sr, channels=1, dtype="float32", blocksize=block) as out:
            for i in range(0, len(data), block):
                if self.stop_speaking.is_set():
                    break
                chunk = data[i:i + block]
                BUS.emit({"type": "level", "v": float(min(1.0, np.sqrt((chunk ** 2).mean()) * 6))})
                out.write(chunk.reshape(-1, 1))
        BUS.emit({"type": "level", "v": 0.0})

    def _set_voice_engine(self, engine: str) -> None:
        if engine != self.voice_engine:
            self.voice_engine = engine
            BUS.emit({"type": "voice", "engine": engine})

    def _synth(self, sentence: str, lang: str) -> bytes | None:
        """Your voice (Chatterbox) when the server answers; the local backup voice only for this sentence otherwise."""
        clean = tts.clean_for_speech(sentence, lang)
        if not clean:
            return None
        key = (clean, lang, SETTINGS["warmth"], SETTINGS["exaggeration"], SETTINGS["pitch"],
               os.environ.get("JARVIS_VOICE_REF", ""))
        if key in self._tts_cache:
            self._tts_cache.move_to_end(key)
            return self._tts_cache[key]
        try:
            wav = tts._chatterbox_wav(clean, lang)
            self._set_voice_engine("chatterbox")
            self._tts_cache[key] = wav
            while len(self._tts_cache) > 64:  # keep the last 64 sentences
                self._tts_cache.popitem(last=False)
            return wav
        except Exception:
            self._set_voice_engine("piper/kokoro")
            if not self._voice_repair.is_set():  # try to bring your voice back in the background
                threading.Thread(target=self.ensure_voice, daemon=True).start()
        return self._backup_voice(clean, lang)

    _fallback = None
    _tts_cache: "OrderedDict[tuple, bytes]" = OrderedDict()

    def replay(self, text: str, lang: str, mid: str) -> None:
        """Read an earlier answer aloud again (the speaker button next to a chat bubble)."""
        self.busy.set()
        self.stop_speaking.clear()
        BUS.emit({"type": "replay", "id": mid, "playing": True})
        try:
            q: "queue.Queue[str | None]" = queue.Queue()
            for sentence in tts._split_sentences(re.sub(r"\s*\n+\s*", " ", text)):
                q.put(sentence)
            q.put(None)
            self._speaker(q, lang or tts.detect_language(text))
        finally:
            BUS.emit({"type": "replay", "id": mid, "playing": False})
            self.busy.clear()
            state("idle")

    def _backup_voice(self, clean: str, lang: str) -> bytes | None:
        """Piper (Swedish) / Kokoro (English), without touching the global TTS settings."""
        if self._fallback is None:
            self._fallback = tts.BilingualTTSBackend()
        b = self._fallback
        if lang == "en":
            return b._kokoro_backend().synthesize(clean, voice_id=tts.DEFAULT_EN_VOICE, output_format="wav").audio
        import wave as _wave
        voice = b._piper_voice(os.environ.get("JARVIS_SV_VOICE", tts.DEFAULT_SV_VOICE) or tts.DEFAULT_SV_VOICE)
        buf = io.BytesIO()
        with _wave.open(buf, "wb") as wf:
            voice.synthesize_wav(clean, wf)
        return buf.getvalue()

    # -- voice server (self-repair) ----------------------------------------------
    _voice_proc = None
    _voice_repair = threading.Event()

    def ensure_voice(self, timeout: float = 150.0) -> bool:
        """Start the Chatterbox server if it is not running. It needs ~3.3 GB of GPU memory,
        so the language model is unloaded first (it reloads on the next question)."""
        if tts._chatterbox_ready(1.0):
            self._set_voice_engine("chatterbox")
            return True
        if self._voice_repair.is_set():
            return False
        self._voice_repair.set()
        try:
            while self.busy.is_set() and not self.shutdown.is_set():  # never unload the LLM mid-reply
                time.sleep(0.5)
            py = Path.home() / ".openjarvis/chatterbox/.venv/bin/python"
            server = Path.home() / ".openjarvis/chatterbox/server.py"
            if not py.exists():
                return False
            BUS.emit({"type": "info", "text": "Din röst var inte igång – startar den igen (tar ungefär en halv minut)…"})
            for m in self._loaded_models():
                try:
                    self._ollama("/api/generate", {"model": m, "keep_alive": 0}, timeout=15)
                except Exception:
                    pass
            _wait_for_free_gpu(need_mb=3800, timeout=30)
            log = open(Path.home() / ".openjarvis/.state/chatterbox.log", "ab")
            self._voice_proc = subprocess.Popen([str(py), "-I", str(server)], stdout=log, stderr=log,
                                                env=dict(os.environ), start_new_session=True)
            ok = tts._chatterbox_ready(timeout)
            self._set_voice_engine("chatterbox" if ok else "piper/kokoro")
            BUS.emit({"type": "info", "text": "Din röst är tillbaka." if ok else
                      "Kunde inte starta din röst – använder reservrösten. Se ~/.openjarvis/.state/chatterbox.log"})
            return ok
        finally:
            self._voice_repair.clear()

    def keep_model_loaded(self) -> None:
        """Ollama unloads idle models after 5 min; keep ours loaded while the app is open."""
        while not self.shutdown.is_set():
            time.sleep(120)
            if self.busy.is_set() or self.switching.locked() or self._voice_repair.is_set():
                continue
            try:
                self._ollama("/api/generate", {"model": self.model, "keep_alive": "10m",
                                               "options": {"num_ctx": _num_ctx()}}, timeout=120)
            except Exception:
                pass

    def _loaded_models(self) -> list[str]:
        try:
            return [m["name"] for m in self._ollama("/api/ps").get("models", [])]
        except Exception:
            return []

    def stop_voice_proc(self) -> None:
        if self._voice_proc is not None and self._voice_proc.poll() is None:
            self._voice_proc.terminate()

    def _speaker(self, sentences: "queue.Queue[str | None]", lang: str) -> None:
        """Synthesize + play sentences as they arrive (next one is prepared while one plays)."""
        audio_q: "queue.Queue[bytes | None]" = queue.Queue(maxsize=2)

        def synth_worker():
            while (s := sentences.get()) is not None:
                if self.stop_speaking.is_set():
                    continue
                try:
                    _dbg(f"synth start: {s[:40]!r}")
                    wav = self._synth(s, lang)
                    _dbg("synth done")
                    if wav:
                        audio_q.put(wav)
                except Exception as exc:
                    BUS.emit({"type": "info", "text": f"Röstfel: {exc}"})
            audio_q.put(None)

        threading.Thread(target=synth_worker, daemon=True).start()
        first = True
        while (wav := audio_q.get()) is not None:
            if self.stop_speaking.is_set():
                continue
            if first:
                state("speaking", "Anders pratar")
                first = False
            self._play(wav)

    # -- one conversational turn ---------------------------------------------
    def handle(self, text: str, lang: str, via: str) -> None:
        self.busy.set()
        self.stop_speaking.clear()
        self.cancel_reply.clear()
        log.info("user (%s, %s): %s", via, lang, text[:300])
        self.turn_sources = []
        t_start = time.time()
        BUS.emit({"type": "user", "text": text, "lang": lang, "via": via})
        state("thinking", "Tänker…" if lang == "sv" else "Thinking…")
        self.history.append(self.Message(role=self.Role.USER, content=text))
        self.conv["messages"].append({"role": "user", "text": text, "lang": lang, "via": via, "ts": time.time()})
        conv = self.conv  # the conversation this turn belongs to (even if the user switches mid-reply)
        # Refresh the summaries of earlier conversations (they are written in the background).
        if self.history and self.history[0].role == self.Role.SYSTEM:
            self.history[0] = self.Message(role=self.Role.SYSTEM, content=(self.system_prompt or "")
                                           + (summaries_block(self.memory.store, exclude_id=conv["id"]) if self.memory.enabled else "")
                                           + self._context_note())
        # Long-term memory: relevant facts (and indexed documents) for this question.
        messages = self.memory.with_context(text, self.history)

        sentences: "queue.Queue[str | None]" = queue.Queue()
        speak = SETTINGS["speak_replies"] and (via == "voice" or SETTINGS.get("speak_typed", True))
        speaker = threading.Thread(target=self._speaker, args=(sentences, lang), daemon=True) if speak else None
        if speaker:
            speaker.start()
        buf = {"text": "", "pending": "", "chunks": 0, "screen_only": False}
        md_start = re.compile(r"(^|\n)[ \t]*(#{1,6}\s|[-*•]\s|\d+[.)]\s|\*\*|\|)")
        sentence_end = re.compile(r"(.{12,}?[.!?…])\s+(?=\S)", re.S)
        clause_end = re.compile(r"(.{25,}?[,;:–])\s+(?=\S)", re.S)  # first chunk only: speak sooner

        def on_token(tok: str) -> None:
            buf["text"] += tok
            BUS.emit({"type": "token", "text": tok})
            if not speak or buf["screen_only"]:
                return
            buf["pending"] += tok
            # Formatted answers (lists, headings): only the intro is read aloud, the rest is for the screen.
            md = md_start.search(buf["pending"])
            if md:
                buf["pending"] = buf["pending"][:md.start()]
                buf["screen_only"] = True
                if buf["pending"].strip():
                    sentences.put(_speakable(buf["pending"]))
                    buf["chunks"] += 1
                buf["pending"] = ""
                return
            # hand text to the speaker as soon as a sentence (or, at the start, a clause) is complete
            while True:
                m = sentence_end.match(buf["pending"])
                if not m and buf["chunks"] == 0:
                    m = clause_end.match(buf["pending"])
                if not m:
                    break
                sentences.put(_speakable(m.group(1)))
                buf["chunks"] += 1
                _dbg(f"queued: {m.group(1).strip()[:40]!r}")
                buf["pending"] = buf["pending"][m.end():]

        try:
            def on_tool(name: str, args: dict) -> None:
                label = anders_tools.STATUS.get(name, ("Använder verktyg…", "Using a tool…"))[0 if lang == "sv" else 1]
                state("thinking", label)
                BUS.emit({"type": "tool", "name": name, "args": args, "label": label})

            # Songs, jokes and comebacks are hand-written: the language model isn't funny in Swedish.
            canned, kind = None, None
            again = _MORE.match(text) and self.last_canned and time.time() - self.last_canned[1] < 180
            if again and self.last_canned[0] == "song":  # "en till" right after a song: another song
                canned, kind = anders_songs.pick_song(text, force=True), "song"
            if canned is None and (song := anders_songs.pick_song(text)):
                canned, kind = song, "song"
            if canned is None and SETTINGS.get("jokes", True) and (joke := anders_jokes.pick_joke(text, lang)):
                canned, kind = joke, "joke"
            if canned is None and SETTINGS.get("roasts", True) and (roast := anders_roasts.pick_roast(text, lang)):
                canned, kind = roast, "roast"
            if kind:
                self.last_canned = (kind, time.time())
                log.info("canned %s", kind)
            if canned is not None:
                on_token(canned)
                reply = canned
            else:
                reply = self._chat(messages, on_token, on_tool)
        except Exception as exc:
            reply = buf["text"]
            log.exception("reply failed")
            if not reply.strip():
                down = "connect" in str(exc).lower() or "refused" in str(exc).lower()
                reply = (("Jag får ingen kontakt med min hjärna, Ollama, just nu. Prova igen om en stund."
                          if down else "Något gick snett när jag skulle svara. Prova att fråga igen.")
                         if lang == "sv" else
                         ("I can't reach my language model right now. Try again in a moment."
                          if down else "Something went wrong while I was answering. Please try again."))
                on_token(reply)
            BUS.emit({"type": "info", "text": f"Språkmodellen svarade inte: {exc}"})
        if not reply.strip() and not self.cancel_reply.is_set():
            # Never just go quiet: the model sometimes returns nothing at all.
            log.warning("empty reply to %r", text[:200])
            reply = "Jag hörde dig, men kom inte på något svar. Kan du säga det på ett annat sätt?" if lang == "sv" \
                else "I heard you, but I couldn't come up with an answer. Could you put it another way?"
            on_token(reply)
        if speak and buf["pending"].strip() and not buf["screen_only"]:
            sentences.put(_speakable(buf["pending"]))
        if speak and buf["screen_only"]:
            sentences.put("Resten står på skärmen." if lang == "sv" else "The rest is on the screen.")
        if speak:
            sentences.put(None)
        if conv is self.conv:
            self.history.append(self.Message(role=self.Role.ASSISTANT, content=reply))
        if self.turn_sources:
            BUS.emit({"type": "sources", "items": self.turn_sources})
        if reply.strip():
            conv["messages"].append({"role": "assistant", "text": reply, "lang": lang, "ts": time.time(),
                                     **({"sources": self.turn_sources} if self.turn_sources else {})})
            self.memory.store.save(conv)
        log.info("reply (%.1f s): %s", time.time() - t_start, reply[:300].replace("\n", " "))
        BUS.emit({"type": "reply_done", "text": reply})
        if speaker:
            speaker.join()
        self._run_after_reply()  # e.g. lock the screen / suspend, now that Anders has finished talking
        # Fact extraction waits until Anders is idle so it never delays the next answer.
        if reply.strip():
            self._pending_facts.append((text, reply))
        self.last_activity = time.time()
        self.followup_lang = lang if (via == "voice" and SETTINGS["followup_seconds"] > 0) else None
        self.busy.clear()
        state("idle")

    def regenerate(self) -> None:
        """'Nytt svar': forget the last answer and ask the same question again."""
        msgs = self.conv["messages"]
        while msgs and msgs[-1]["role"] == "assistant":
            msgs.pop()
        if not msgs or msgs[-1]["role"] != "user":
            return
        last = msgs.pop()
        while self.history and getattr(self.history[-1].role, "value", "") in ("assistant", "user") \
                and self.history[-1].role != self.Role.SYSTEM:
            role = self.history.pop().role
            if role == self.Role.USER:
                break
        self.last_canned = None
        log.info("regenerate: %r", last["text"][:200])
        self.handle(last["text"], last.get("lang") or "sv", last.get("via") or "text")

    def worker(self) -> None:
        while not self.shutdown.is_set():
            try:
                kind, payload = self.inbox.get(timeout=0.2)
            except queue.Empty:
                continue
            if kind == "text":
                text = payload.strip()
                lang = tts.detect_language(text) if text else "sv"
                if text and not self.intercept(text, lang, "text"):
                    self.handle(text, lang, "text")
            elif kind == "voice":
                self.handle(payload["text"], payload["lang"], "voice")
            elif kind == "regenerate":
                self.regenerate()
            elif kind == "replay":
                self.replay(payload["text"], payload.get("lang", ""), payload.get("id", ""))

    # -- listening -------------------------------------------------------------
    def _record(self, startup_seconds: float | None) -> Any:
        """Record one utterance (levels streamed to the UI). None = nothing/aborted."""
        det = tts._EndOfSpeechDetector(SETTINGS["silence_seconds"], startup_seconds or 3600.0, 30.0)
        frames, pre, last_emit = [], [], 0.0
        with sd.RawInputStream(samplerate=SR, channels=1, dtype="int16", blocksize=BLOCK) as stream:
            while not self.shutdown.is_set():
                if self.busy.is_set() or (self.muted and not self.ptt.is_set()):
                    return None
                if startup_seconds is None and self.ptt.is_set():
                    return "PTT"  # restart in push-to-talk mode
                raw, _ = stream.read(BLOCK)
                chunk = np.frombuffer(bytes(raw), dtype=np.int16).copy()
                now = time.time()
                if now - last_emit > 0.06:
                    lvl = float(np.sqrt((chunk.astype(np.float32) ** 2).mean()) / 32768.0)
                    BUS.emit({"type": "level", "v": min(1.0, lvl * 12)})
                    last_emit = now
                ended = det.update(chunk)
                if det.started:
                    if pre:
                        frames.extend(pre)
                        pre = []
                    frames.append(chunk)
                    if startup_seconds is None and len(frames) == 3:
                        state("hearing", "")
                else:
                    pre = (pre + [chunk])[-5:]
                if ended:
                    break
        if not det.started:
            return None
        return np.concatenate(frames).astype("float32") / 32768.0

    def listener(self) -> None:
        hf._load_models()  # Whisper models (CPU)
        while not self.shutdown.is_set():
            if self.busy.is_set():
                time.sleep(0.05)
                continue
            ptt = self.ptt.is_set()
            awaiting_yes = self.pending_active() and time.time() < self.pending["expires"] - 30  # first 15 s
            follow = self.followup_lang or ("sv" if (awaiting_yes or self.counting_down) else None)
            if self.muted and not ptt:
                state("muted", "Mikrofonen är av")
                while self.muted and not self.ptt.is_set() and not self.shutdown.is_set():
                    time.sleep(0.1)
                continue
            if ptt or follow:
                lang_hint = follow or "sv"
                state("listening", ("Lyssnar…" if lang_hint == "sv" else "Listening…")
                      + ("" if ptt else (" (följdfråga)" if lang_hint == "sv" else " (follow-up)")))
                timeout = 6.0 if ptt else SETTINGS["followup_seconds"]
            else:
                state("idle", 'Säg "Hej Anders" eller "Hey Anders"')
                timeout = None
            try:
                audio = self._record(timeout)
            except Exception as exc:
                BUS.emit({"type": "info", "text": f"Mikrofonfel: {exc}"})
                time.sleep(2)
                continue
            self.followup_lang = None
            if isinstance(audio, str) and audio == "PTT":
                continue
            self.ptt.clear()
            if audio is None:
                continue
            models = hf._load_models()
            woke = not (ptt or follow)
            if woke and not hf._is_wake(models, audio):
                continue
            level = _speech_level(audio)
            if follow and not ptt and not self._loud_enough(level):
                log.info("ignored follow-up: too quiet (%.4f vs your usual %.4f) - probably TV/background",
                         level, self._usual_level())
                continue
            state("transcribing", "Transkriberar…")
            heard = anders_hearing.hear(models, audio, followup=bool(follow and not ptt))
            lang = heard.lang
            text = hf._WAKE.sub("", heard.text, count=1).strip() if woke else heard.text
            log.info("heard %r (%s, confidence %.2f, level %.4f)%s", heard.text, lang, heard.confidence, level,
                     f" -> ignored: {heard.reason}" if not heard.ok else "")
            if woke and heard.text:
                self.voice_levels.append(level)  # learn how loud the user usually is
            if not heard.ok and woke and heard.text and text and len(re.sub(r"\W", "", text)) >= 2:
                # Woken on purpose but the rest was unclear: say so instead of guessing or staying silent.
                self._say("Jag hörde inte riktigt, kan du ta det igen?" if lang == "sv"
                                else "Sorry, I didn't catch that. Could you say it again?", lang)
                self.followup_lang = lang
                continue
            if not heard.ok and not woke:
                if ptt:
                    BUS.emit({"type": "info", "text": "Jag hörde inget tydligt. Prova igen, lite närmare mikrofonen."})
                continue
            if not text or len(re.sub(r"\W", "", text)) < 2:
                if woke:  # only the wake phrase: listen for the command
                    hf._beep()
                    self.followup_lang = lang
                continue
            if self.intercept(text, lang):
                continue
            if self.ringing is not None:
                low = text.lower()
                snooze = bool(re.search(r"snooz|snus|vänta lite|snooze", low))
                self.stop_ringing(snooze=snooze)
                if snooze or re.fullmatch(r"\W*(stäng av|stopp|stoppa|sluta|tyst|ok(ej)?|tack|jag är vaken|stop|turn it off|be quiet)\W*(tack)?\W*", low):
                    continue  # it was just "turn it off": nothing to ask the language model
            self.inbox.put(("voice", {"text": text, "lang": lang}))
            while self.inbox.qsize() or not self.busy.is_set():  # wait until the turn starts
                time.sleep(0.02)
                if self.busy.is_set() or self.shutdown.is_set():
                    break


def _speech_level(audio: Any) -> float:
    """Loudness of the voiced part of an utterance (RMS of the loudest 30 % of 20 ms frames)."""
    try:
        a = np.asarray(audio, dtype=np.float32)
        n = len(a) // 320
        if n < 3:
            return float(np.sqrt(np.mean(a ** 2))) if len(a) else 0.0
        rms = np.sqrt((a[: n * 320].reshape(n, 320) ** 2).mean(axis=1))
        top = np.sort(rms)[-max(1, int(n * 0.3)):]
        return float(top.mean())
    except Exception:
        return 1.0


def _num_ctx() -> int:
    """Context size used by OpenJarvis' own Ollama calls. Every request must use the same
    value: Ollama reloads the whole model (10-20 s) whenever num_ctx changes between calls."""
    try:
        from openjarvis.engine.ollama import _default_num_ctx

        return _default_num_ctx()
    except Exception:
        return 16384


def _wait_for_free_gpu(need_mb: int, timeout: float) -> bool:
    """Ollama confirms an unload before the GPU memory is actually released: wait for it."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            out = subprocess.run(["nvidia-smi", "--query-gpu=memory.total,memory.used", "--format=csv,noheader,nounits"],
                                 capture_output=True, text=True, timeout=5).stdout.split(",")
            if int(out[0]) - int(out[1]) >= need_mb:
                return True
        except Exception:
            return True  # no nvidia-smi: nothing to wait for
        time.sleep(0.5)
    return False


ANDERS: Anders | None = None

# ---------------------------------------------------------------------------
# Web app
# ---------------------------------------------------------------------------
app = FastAPI()


@app.get("/")
def index() -> HTMLResponse:
    return HTMLResponse((HERE / "static" / "index.html").read_text(encoding="utf-8"))


@app.get("/logo")
def logo() -> FileResponse:
    icons = sorted((Path.home() / ".local/share/icons").glob("anders-*.png"), key=lambda p: p.stat().st_mtime)
    path = icons[-1] if icons else Path.home() / ".local/share/icons/openjarvis.png"
    return FileResponse(path, headers={"Cache-Control": "no-cache"})


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    BUS.clients.add(ws)
    await ws.send_text(json.dumps({"type": "hello", "build": int((HERE / "static" / "index.html").stat().st_mtime)}))
    await ws.send_text(json.dumps({"type": "settings", "settings": SETTINGS, "model": ANDERS.model if ANDERS else ""}))
    await ws.send_text(json.dumps(BUS.last_state))
    if ANDERS:
        await ws.send_text(json.dumps(ANDERS.conversation_event()))
        await ws.send_text(json.dumps({"type": "timers", "items": ANDERS.alarms.public()}))
        if ANDERS.ringing is not None:
            await ws.send_text(json.dumps({"type": "alarm", "item": ANDERS.ringing,
                                           "message": ANDERS.alarm_message(ANDERS.ringing)}))
        if ANDERS.voice_engine != "…":
            await ws.send_text(json.dumps({"type": "voice", "engine": ANDERS.voice_engine}))
        await ws.send_text(json.dumps({"type": "models", "models": await asyncio.to_thread(ANDERS.list_models),
                                       "current": ANDERS.model}))
    try:
        while True:
            msg = json.loads(await ws.receive_text())
            t = msg.get("type")
            if ANDERS is None:
                continue
            if t == "text" and msg.get("text", "").strip():
                ANDERS.ptt.clear()
                ANDERS.inbox.put(("text", msg["text"]))
            elif t == "ptt":
                ANDERS.followup_lang = None
                ANDERS.ptt.set()
            elif t == "replay" and str(msg.get("text", "")).strip():
                ANDERS.stop_speaking.set()  # stop whatever is playing first
                ANDERS.inbox.put(("replay", {"text": str(msg["text"])[:4000], "lang": msg.get("lang", ""),
                                             "id": str(msg.get("id", ""))}))
            elif t == "open_url":
                url = str(msg.get("url", ""))
                if re.match(r"^https?://[^\s\"']+$", url):
                    subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            elif t == "regenerate" and not ANDERS.busy.is_set():
                ANDERS.inbox.put(("regenerate", None))
            elif t == "confirm_action":
                if ANDERS.pending_active() and msg.get("id") == ANDERS.pending["id"]:
                    ANDERS.confirm_pending()
            elif t == "cancel_action":
                if not ANDERS.cancel_countdown():
                    ANDERS._clear_pending()
            elif t == "alarm_stop":
                ANDERS.stop_ringing(snooze=False)
            elif t == "alarm_snooze":
                ANDERS.stop_ringing(snooze=True)
            elif t == "cancel_timer":
                ANDERS.alarms.cancel(str(msg.get("id", "")))
            elif t == "stop":
                ANDERS.cancel_countdown()
                ANDERS.stop_ringing(snooze=False)
                ANDERS.stop_speaking.set()
                ANDERS.cancel_reply.set()
                ANDERS.ptt.clear()
            elif t == "mute":
                ANDERS.muted = bool(msg.get("value"))
                SETTINGS["handsfree"] = not ANDERS.muted
                SETTINGS_FILE.write_text(json.dumps(SETTINGS, indent=2))
                BUS.emit({"type": "settings", "settings": SETTINGS, "model": ANDERS.model})
            elif t == "models":
                await ws.send_text(json.dumps({"type": "models", "models": await asyncio.to_thread(ANDERS.list_models),
                                               "current": ANDERS.model}))
            elif t == "set_model":
                name = str(msg.get("model", ""))
                if name in {m["name"] for m in await asyncio.to_thread(ANDERS.list_models)}:
                    threading.Thread(target=ANDERS.set_model, args=(name,), daemon=True).start()
            elif t == "reset":
                ANDERS.stop_speaking.set()
                ANDERS.cancel_reply.set()
                ANDERS.reset()
                BUS.emit({"type": "reset"})
                BUS.emit(ANDERS.conversation_event())
            elif t == "conversations":
                await ws.send_text(json.dumps(await asyncio.to_thread(ANDERS.conversations_event)))
            elif t == "open_conversation":
                ANDERS.stop_speaking.set()
                ANDERS.cancel_reply.set()
                if ANDERS.open_conversation(str(msg.get("id", ""))):
                    BUS.emit(ANDERS.conversation_event())
            elif t == "delete_conversation":
                cid = str(msg.get("id", ""))
                if cid == ANDERS.conv["id"]:
                    ANDERS.reset()
                    BUS.emit(ANDERS.conversation_event())
                ANDERS.memory.store.delete(cid)
                await ws.send_text(json.dumps(await asyncio.to_thread(ANDERS.conversations_event)))
            elif t == "facts":
                await ws.send_text(json.dumps(await asyncio.to_thread(ANDERS.facts_event)))
            elif t == "forget_fact":
                ANDERS.memory.forget_fact(int(msg.get("i", -1)), str(msg.get("text", "")))
                await ws.send_text(json.dumps(await asyncio.to_thread(ANDERS.facts_event)))
            elif t == "forget_all":
                ANDERS.memory.forget_all()
                await ws.send_text(json.dumps(await asyncio.to_thread(ANDERS.facts_event)))
            elif t == "settings":
                for k, v in (msg.get("settings") or {}).items():
                    if k in DEFAULTS:
                        SETTINGS[k] = type(DEFAULTS[k])(v)
                apply_settings(SETTINGS)
                ANDERS.memory.enabled = SETTINGS.get("memory", True)
                SETTINGS_FILE.write_text(json.dumps(SETTINGS, indent=2))
                BUS.emit({"type": "settings", "settings": SETTINGS, "model": ANDERS.model})
    except WebSocketDisconnect:
        pass
    finally:
        BUS.clients.discard(ws)
        if not BUS.clients:
            asyncio.ensure_future(_exit_if_abandoned())


async def _exit_if_abandoned(grace: float = 20.0) -> None:
    """The window was closed (no UI connected): shut down so nothing keeps the mic open."""
    await asyncio.sleep(grace)
    if not BUS.clients and os.environ.get("ANDERS_STAY_ALIVE") != "1":
        if ANDERS:
            ANDERS.stop_voice_proc()
            ANDERS.shutdown.set()
            ANDERS.stop_speaking.set()
        os._exit(0)


@app.on_event("startup")
async def _startup() -> None:
    BUS.loop = asyncio.get_running_loop()

    def boot() -> None:
        global ANDERS
        state("starting", "Startar Anders…")
        ANDERS = Anders()
        state("starting", "Startar rösten…")
        wait = float(os.environ.get("JARVIS_TTS_WAIT", "0"))
        voice_ok = tts._chatterbox_ready(wait) or ANDERS.ensure_voice()
        ANDERS._set_voice_engine("chatterbox" if voice_ok else "piper/kokoro")
        ANDERS.warm_up()
        state("starting", "Laddar taligenkänning…")
        hf._load_models()
        threading.Thread(target=ANDERS.worker, daemon=True).start()
        threading.Thread(target=ANDERS.memory.summarize_pending, args=(ANDERS.conv["id"],), daemon=True).start()
        # Windows that connected during startup have no conversation/model list yet: send them now.
        BUS.emit({"type": "settings", "settings": SETTINGS, "model": ANDERS.model})
        BUS.emit(ANDERS.conversation_event())
        BUS.emit({"type": "models", "models": ANDERS.list_models(), "current": ANDERS.model})
        threading.Thread(target=ANDERS.listener, daemon=True).start()
        threading.Thread(target=ANDERS.keep_model_loaded, daemon=True).start()
        threading.Thread(target=ANDERS.fact_worker, daemon=True).start()

    threading.Thread(target=boot, daemon=True).start()


@app.on_event("shutdown")
async def _shutdown() -> None:
    if ANDERS:
        ANDERS.stop_voice_proc()
        ANDERS.shutdown.set()
        ANDERS.stop_speaking.set()


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
