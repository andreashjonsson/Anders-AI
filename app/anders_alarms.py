"""Timers, alarms and reminders for Anders.

Items are stored in ~/.openjarvis/anders/alarms.json (absolute due times), so they
survive a restart of the app. They only ring while the Anders app is running.

kinds:  timer     "sätt en timer på 10 minuter (för pastan)"
        alarm     "väck mig 06:30" (repeat: none | daily | weekdays | weekends)
        reminder  "påminn mig kl 17 om att ringa mamma"
"""

from __future__ import annotations

import datetime as dt
import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

STORE = Path(os.environ.get("ANDERS_ALARMS_FILE") or Path.home() / ".openjarvis" / "anders" / "alarms.json")
WEEKDAYS = ["måndag", "tisdag", "onsdag", "torsdag", "fredag", "lördag", "söndag"]
REPEATS = {"none", "daily", "weekdays", "weekends"}


def fmt_duration(seconds: float) -> str:
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h} timm{'e' if h == 1 else 'ar'}")
    if m:
        parts.append(f"{m} minut{'er' if m != 1 else ''}")
    if sec and not h:
        parts.append(f"{sec} sekund{'er' if sec != 1 else ''}")
    return " och ".join(parts) or "0 sekunder"


def fmt_when(ts: float) -> str:
    t = dt.datetime.fromtimestamp(ts)
    today = dt.date.today()
    if t.date() == today:
        day = "idag"
    elif t.date() == today + dt.timedelta(days=1):
        day = "imorgon"
    elif (t.date() - today).days < 7:
        day = "på " + WEEKDAYS[t.weekday()]
    else:
        day = f"{t.day}/{t.month}"
    return f"{day} kl {t:%H:%M}"


def _next_occurrence(hhmm: str, date: str | None, repeat: str, after: dt.datetime | None = None) -> dt.datetime:
    h, m = (int(x) for x in hhmm.replace(".", ":").split(":")[:2])
    now = after or dt.datetime.now()
    if date:
        t = dt.datetime.combine(dt.date.fromisoformat(date), dt.time(h, m))
        if t <= now and repeat == "none":
            raise ValueError("den tiden har redan passerat")
    else:
        t = now.replace(hour=h, minute=m, second=0, microsecond=0)
    while t <= now or (repeat == "weekdays" and t.weekday() >= 5) or (repeat == "weekends" and t.weekday() < 5):
        t += dt.timedelta(days=1)
    return t


class Scheduler:
    def __init__(self, on_fire: Callable[[dict], None], on_change: Callable[[list], None]) -> None:
        self.on_fire, self.on_change = on_fire, on_change
        self._lock = threading.RLock()
        self.items: list[dict] = self._load()
        self._stop = threading.Event()
        threading.Thread(target=self._loop, name="anders-alarms", daemon=True).start()

    # -- persistence --------------------------------------------------------------
    def _load(self) -> list[dict]:
        try:
            return json.loads(STORE.read_text(encoding="utf-8"))
        except Exception:
            return []

    def _save(self) -> None:
        STORE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STORE.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.items, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(STORE)

    def _changed(self) -> None:
        self._save()
        self.on_change(self.public())

    # -- API used by the tools ------------------------------------------------------
    def add_timer(self, seconds: float, label: str = "") -> dict:
        seconds = float(seconds)
        if not 1 <= seconds <= 24 * 3600:
            raise ValueError("en timer kan vara mellan 1 sekund och 24 timmar")
        item = {"id": uuid.uuid4().hex[:6], "kind": "timer", "label": label.strip(), "due": time.time() + seconds,
                "duration": seconds, "repeat": "none", "created": time.time()}
        with self._lock:
            self.items.append(item)
            self._changed()
        return item

    def add_alarm(self, time_hhmm: str, date: str | None = None, label: str = "", repeat: str = "none") -> dict:
        repeat = repeat if repeat in REPEATS else "none"
        due = _next_occurrence(time_hhmm, date, repeat)
        item = {"id": uuid.uuid4().hex[:6], "kind": "alarm", "label": label.strip(), "due": due.timestamp(),
                "time": f"{due:%H:%M}", "repeat": repeat, "created": time.time()}
        with self._lock:
            self.items.append(item)
            self._changed()
        return item

    def add_reminder(self, when_iso: str, text: str) -> dict:
        when = dt.datetime.fromisoformat(when_iso.strip().replace(" ", "T"))
        if when.tzinfo is not None:
            when = when.astimezone().replace(tzinfo=None)
        if when <= dt.datetime.now():
            raise ValueError("den tiden har redan passerat")
        item = {"id": uuid.uuid4().hex[:6], "kind": "reminder", "label": text.strip(), "due": when.timestamp(),
                "repeat": "none", "created": time.time()}
        with self._lock:
            self.items.append(item)
            self._changed()
        return item

    def cancel(self, ref: str = "", kind: str = "") -> list[dict]:
        """Cancel by id, by label text, or (no ref) the only/soonest item of a kind."""
        ref = (ref or "").strip().lower()
        with self._lock:
            pool = [i for i in self.items if not kind or i["kind"] == kind]
            if ref == "alla" or ref == "all":
                hits = pool
            elif ref:
                hits = [i for i in pool if i["id"] == ref or (i["label"] and ref in i["label"].lower())]
            else:
                hits = sorted(pool, key=lambda i: i["due"])[:1]
            for h in hits:
                self.items.remove(h)
            if hits:
                self._changed()
            return hits

    def snooze(self, item: dict, minutes: float = 9) -> None:
        with self._lock:
            copy = {**item, "id": uuid.uuid4().hex[:6], "kind": item["kind"], "due": time.time() + minutes * 60,
                    "repeat": "none", "snoozed": True}
            self.items.append(copy)
            self._changed()

    def public(self) -> list[dict]:
        with self._lock:
            return sorted(({k: v for k, v in i.items()} for i in self.items), key=lambda i: i["due"])

    def describe(self) -> list[dict]:
        out = []
        for i in self.public():
            d = {"id": i["id"], "typ": {"timer": "timer", "alarm": "larm", "reminder": "påminnelse"}[i["kind"]],
                 "etikett": i["label"] or None, "går_av": fmt_when(i["due"])}
            if i["kind"] == "timer":
                d["tid_kvar"] = fmt_duration(max(0, i["due"] - time.time()))
            if i.get("repeat", "none") != "none":
                d["upprepas"] = {"daily": "varje dag", "weekdays": "vardagar", "weekends": "helger"}[i["repeat"]]
            out.append(d)
        return out

    # -- loop -------------------------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            time.sleep(0.25)
            now = time.time()
            due = []
            with self._lock:
                for i in list(self.items):
                    if i["due"] <= now:
                        due.append(i)
                        self.items.remove(i)
                        if i.get("repeat", "none") != "none":  # schedule the next occurrence
                            nxt = _next_occurrence(i["time"], None, i["repeat"], after=dt.datetime.fromtimestamp(i["due"]))
                            self.items.append({**i, "due": nxt.timestamp()})
                if due:
                    self._changed()
            for i in due:
                if now - i["due"] > 6 * 3600:
                    continue  # missed while the app was closed for a long time: don't ring hours late
                try:
                    self.on_fire({**i, "late_s": max(0, now - i["due"])})
                except Exception:
                    pass

    def stop(self) -> None:
        self._stop.set()
