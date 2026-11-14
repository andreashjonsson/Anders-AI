"""Tools Anders can call: time, location, weather, web search, web pages, calculator.

All free and keyless:
  * Weather + place lookup: Open-Meteo (api.open-meteo.com / geocoding-api.open-meteo.com)
  * Location from IP (only when no home town is set): ipapi.co, name via OpenStreetMap Nominatim
  * Web search: OpenJarvis' web_search tool (You.com free tier, DuckDuckGo fallback)
  * Web pages: fetched directly, with OpenJarvis' SSRF guard (no local/internal addresses)
"""

from __future__ import annotations

import datetime as dt
import html
import json
import re
import threading
import time
from typing import Any, Callable

import httpx

# Wikimedia and MET Norway ask for an identifying User-Agent with a URL or contact.
UA = {"User-Agent": "Anders-assistant/1.0 (https://github.com/open-jarvis/OpenJarvis; local personal assistant)"}
_TIMEOUT = 12.0

WEEKDAYS = ["måndag", "tisdag", "onsdag", "torsdag", "fredag", "lördag", "söndag"]
MONTHS = ["januari", "februari", "mars", "april", "maj", "juni", "juli", "augusti",
          "september", "oktober", "november", "december"]

# WMO weather codes -> Swedish
WMO = {0: "klart", 1: "mestadels klart", 2: "halvklart", 3: "mulet", 45: "dimma", 48: "underkylande dimma",
       51: "lätt duggregn", 53: "duggregn", 55: "kraftigt duggregn", 56: "lätt underkylt duggregn",
       57: "underkylt duggregn", 61: "lätt regn", 63: "regn", 65: "kraftigt regn", 66: "lätt underkylt regn",
       67: "underkylt regn", 71: "lätt snöfall", 73: "snöfall", 75: "kraftigt snöfall", 77: "snökorn",
       80: "lätta regnskurar", 81: "regnskurar", 82: "kraftiga regnskurar", 85: "lätta snöbyar",
       86: "kraftiga snöbyar", 95: "åska", 96: "åska med lätt hagel", 99: "åska med hagel"}

# yr.no / MET Norway symbol codes -> Swedish (suffixes _day/_night/_polartwilight are stripped)
YR = {"clearsky": "klart", "fair": "mestadels klart", "partlycloudy": "halvklart", "cloudy": "mulet", "fog": "dimma",
      "lightrain": "lätt regn", "rain": "regn", "heavyrain": "kraftigt regn",
      "lightrainshowers": "lätta regnskurar", "rainshowers": "regnskurar", "heavyrainshowers": "kraftiga regnskurar",
      "lightsleet": "lätt snöblandat regn", "sleet": "snöblandat regn", "heavysleet": "kraftigt snöblandat regn",
      "lightsleetshowers": "lätta byar av snöblandat regn", "sleetshowers": "byar av snöblandat regn",
      "heavysleetshowers": "kraftiga byar av snöblandat regn",
      "lightsnow": "lätt snöfall", "snow": "snöfall", "heavysnow": "kraftigt snöfall",
      "lightsnowshowers": "lätta snöbyar", "snowshowers": "snöbyar", "heavysnowshowers": "kraftiga snöbyar"}


def yr_text(code: str) -> str:
    base = (code or "").split("_")[0]
    thunder = base.endswith("andthunder")
    base = base.replace("andthunder", "")
    txt = YR.get(base, base or "okänt")
    return txt + (" och åska" if thunder else "")


# --------------------------------------------------------------------------- schemas
TOOL_SPECS: list[dict] = [
    {"type": "function", "function": {
        "name": "get_time",
        "description": "Aktuellt datum, klockslag, veckodag och veckonummer (lokal tid). Använd vid frågor om tid/datum.",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "get_location",
        "description": "Var användaren befinner sig (hemort, eller ungefärlig plats från internetanslutningen).",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "get_weather",
        "description": "Väder från yr.no (MET Norway): just nu, kommande timmar och 7-dygnsprognos med veckodag och datum. Utan plats används användarens plats.",
        "parameters": {"type": "object", "properties": {
            "place": {"type": "string", "description": "Ort, t.ex. 'Karlstad' eller 'Oslo'. Tomt = användarens plats."}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "web_search",
        "description": "Sök på internet efter aktuell information: nyheter, öppettider, priser, fakta, händelser.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Sökfråga."},
            "max_results": {"type": "integer", "description": "Antal resultat (1-8), standard 5."}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "fetch_webpage",
        "description": "Hämta och läs texten på en webbsida (t.ex. en länk från web_search).",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string", "description": "Fullständig adress, börjar med https://"}},
            "required": ["url"]}}},
    {"type": "function", "function": {
        "name": "calculate",
        "description": "Räkna ut ett matematiskt uttryck exakt, t.ex. '23*19.5' eller '(1500-320)/12'.",
        "parameters": {"type": "object", "properties": {
            "expression": {"type": "string", "description": "Uttrycket som ska räknas ut."}},
            "required": ["expression"]}}},
    {"type": "function", "function": {
        "name": "remember_note",
        "description": "Spara något användaren uttryckligen ber dig komma ihåg (t.ex. 'kom ihåg att jag har tandläkartid på fredag').",
        "parameters": {"type": "object", "properties": {
            "note": {"type": "string", "description": "Kort mening i tredje person om Andreas, med exakt datum om möjligt, t.ex. 'Andreas har tandläkartid fredag 13 november kl 10.'"}},
            "required": ["note"]}}},
]

def _fn(name: str, desc: str, props: dict | None = None, required: list | None = None) -> dict:
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {
        "type": "object", "properties": props or {}, "required": required or []}}}


TOOL_SPECS += [
    _fn("set_timer", "Starta en nedräkningstimer. Exempel: '10 minuter för pastan' -> minutes=10, label='pastan'.",
        {"minutes": {"type": "number", "description": "Antal minuter (kan vara decimaltal, t.ex. 0.5)."},
         "seconds": {"type": "integer", "description": "Extra sekunder (valfritt)."},
         "label": {"type": "string", "description": "Vad timern gäller (valfritt), t.ex. 'pastan'."}}, ["minutes"]),
    _fn("set_alarm", "Ställ ett larm/väckning på ett klockslag. Utan datum = nästa gång klockslaget inträffar.",
        {"time": {"type": "string", "description": "Klockslag HH:MM (24h), t.ex. '06:30'."},
         "date": {"type": "string", "description": "Datum YYYY-MM-DD om ett visst datum (valfritt)."},
         "repeat": {"type": "string", "enum": ["none", "daily", "weekdays", "weekends"], "description": "Upprepning."},
         "label": {"type": "string", "description": "Etikett (valfritt), t.ex. 'jobbet'."}}, ["time"]),
    _fn("set_reminder", "Påminn användaren om något vid en viss tidpunkt (räkna ut exakt datum och tid från 'Just nu').",
        {"when": {"type": "string", "description": "Tidpunkt YYYY-MM-DDTHH:MM i lokal tid."},
         "text": {"type": "string", "description": "Vad påminnelsen gäller, t.ex. 'ringa mamma'."}}, ["when", "text"]),
    _fn("list_timers", "Visa aktiva timers, larm och påminnelser (med tid kvar)."),
    _fn("cancel_timer", "Stäng av/ta bort timer, larm eller påminnelse.",
        {"ref": {"type": "string", "description": "Etikett eller id, 'alla' för alla, tomt = den närmaste."},
         "kind": {"type": "string", "enum": ["timer", "alarm", "reminder", ""], "description": "Typ (valfritt)."}}),
    _fn("list_add", "Lägg till saker på en lista, t.ex. inköpslistan eller att-göra-listan.",
        {"list": {"type": "string", "description": "Listans namn, t.ex. 'inköpslistan', 'att göra'."},
         "items": {"type": "array", "items": {"type": "string"}, "description": "Saker att lägga till."}}, ["list", "items"]),
    _fn("list_show", "Visa innehållet i en lista (eller vilka listor som finns om namn saknas).",
        {"list": {"type": "string", "description": "Listans namn (valfritt)."}}),
    _fn("list_remove", "Ta bort saker från en lista (eller töm den med items=['alla']).",
        {"list": {"type": "string"}, "items": {"type": "array", "items": {"type": "string"}}}, ["list", "items"]),
    _fn("electricity_price", "Elpriset (spotpris, öre/kWh) per timme för idag eller imorgon, med billigaste och dyraste timmarna.",
        {"day": {"type": "string", "enum": ["idag", "imorgon"]},
         "area": {"type": "string", "enum": ["SE1", "SE2", "SE3", "SE4"], "description": "Elområde, standard SE3 (Karlstad)."}}),
    _fn("swedish_day", "Svensk kalenderinfo för ett datum: namnsdag, helgdag, flaggdag, veckonummer.",
        {"date": {"type": "string", "description": "YYYY-MM-DD, tomt = idag."}}),
    _fn("news_headlines", "Senaste nyhetsrubrikerna från SVT Nyheter.",
        {"count": {"type": "integer", "description": "Antal rubriker (1-10), standard 5."}}),
    _fn("wikipedia", "Slå upp ett ämne, en person eller plats på Wikipedia (svenska först) och få en sammanfattning.",
        {"query": {"type": "string"}, "lang": {"type": "string", "description": "Språk, 'sv' (standard) eller 'en'."}}, ["query"]),
    _fn("convert_currency", "Växla belopp mellan valutor med dagens kurser (Europeiska centralbanken).",
        {"amount": {"type": "number"}, "from_currency": {"type": "string", "description": "T.ex. 'EUR'."},
         "to_currency": {"type": "string", "description": "T.ex. 'SEK'."}}, ["amount", "from_currency", "to_currency"]),
    _fn("open_url", "Öppna en webbsida i användarens webbläsare. Bara när användaren uttryckligen ber om det.",
        {"url": {"type": "string", "description": "https://…"}}, ["url"]),
]

import anders_system  # noqa: E402

TOOL_SPECS += anders_system.TOOL_SPECS

# Short Swedish/English status shown in the UI while a tool runs
STATUS = {
    "get_time": ("Kollar klockan…", "Checking the time…"),
    "get_location": ("Kollar var du är…", "Checking your location…"),
    "get_weather": ("Kollar vädret på yr…", "Checking the weather on yr…"),
    "web_search": ("Söker på nätet…", "Searching the web…"),
    "fetch_webpage": ("Läser webbsidan…", "Reading the page…"),
    "calculate": ("Räknar…", "Calculating…"),
    "remember_note": ("Sparar…", "Saving…"),
    "set_timer": ("Startar timer…", "Starting a timer…"),
    "set_alarm": ("Ställer larm…", "Setting an alarm…"),
    "set_reminder": ("Skapar påminnelse…", "Creating a reminder…"),
    "list_timers": ("Kollar timers…", "Checking timers…"),
    "cancel_timer": ("Stänger av…", "Cancelling…"),
    "list_add": ("Lägger till på listan…", "Adding to the list…"),
    "list_show": ("Läser listan…", "Reading the list…"),
    "list_remove": ("Tar bort från listan…", "Removing from the list…"),
    "electricity_price": ("Kollar elpriset…", "Checking electricity prices…"),
    "swedish_day": ("Kollar kalendern…", "Checking the calendar…"),
    "news_headlines": ("Hämtar nyheter…", "Fetching news…"),
    "wikipedia": ("Slår upp på Wikipedia…", "Looking it up on Wikipedia…"),
    "convert_currency": ("Räknar om valuta…", "Converting currency…"),
    "open_url": ("Öppnar webbsidan…", "Opening the page…"),
    **anders_system.STATUS,
}


# --------------------------------------------------------------------------- helpers
def now_text() -> str:
    n = dt.datetime.now().astimezone()
    week = n.isocalendar().week
    return (f"{WEEKDAYS[n.weekday()]} {n.day} {MONTHS[n.month - 1]} {n.year}, kl {n:%H:%M} "
            f"(vecka {week}, tidszon {n.tzname()})")


class Location:
    """Home town from settings, else approximate location from the IP address (cached)."""

    def __init__(self, home_getter: Callable[[], str]) -> None:
        self.home_getter = home_getter
        self._cache: dict[str, Any] = {}
        self._lock = threading.Lock()

    def geocode(self, place: str) -> dict | None:
        key = "geo:" + place.lower().strip()
        if key in self._cache:
            return self._cache[key]
        r = httpx.get("https://geocoding-api.open-meteo.com/v1/search",
                      params={"name": place, "count": 1, "language": "sv", "format": "json"},
                      headers=UA, timeout=_TIMEOUT, follow_redirects=True).json()
        res = (r.get("results") or [None])[0]
        if res:
            res = {"name": res["name"], "region": res.get("admin1", ""), "country": res.get("country", ""),
                   "lat": res["latitude"], "lon": res["longitude"], "source": "ort", "tz": res.get("timezone", "")}
        self._cache[key] = res
        return res

    def current(self) -> dict | None:
        home = (self.home_getter() or "").strip()
        if home:
            loc = self.geocode(home)
            if loc:
                return {**loc, "source": "hemort (inställningar)"}
        with self._lock:
            hit = self._cache.get("ip")
            if hit and time.time() - hit["_t"] < 3600:
                return hit
            g = httpx.get("https://ipapi.co/json/", headers=UA, timeout=_TIMEOUT, follow_redirects=True).json()
            if "latitude" not in g:
                return None
            loc = {"name": g.get("city", ""), "region": g.get("region", ""), "country": g.get("country_name", ""),
                   "lat": g["latitude"], "lon": g["longitude"], "tz": g.get("timezone", ""),
                   "source": "ungefärlig plats från internetanslutningen", "_t": time.time()}
            try:  # proper Swedish place name (ipapi returns e.g. 'Linkoeping')
                n = httpx.get("https://nominatim.openstreetmap.org/reverse",
                              params={"lat": loc["lat"], "lon": loc["lon"], "format": "json", "zoom": 10,
                                      "accept-language": "sv"}, headers=UA, timeout=_TIMEOUT, follow_redirects=True).json()
                a = n.get("address", {})
                loc["name"] = a.get("city") or a.get("town") or a.get("municipality") or loc["name"]
                loc["region"] = a.get("state") or loc["region"]
            except Exception:
                pass
            self._cache["ip"] = loc
            return loc


def _strip_html(raw: str) -> tuple[str, str]:
    title = html.unescape((re.search(r"<title[^>]*>(.*?)</title>", raw, re.S | re.I) or [None, ""])[1]).strip()
    raw = re.sub(r"(?is)<(script|style|noscript|svg|nav|footer|header|form|iframe)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h[1-6]|tr)>", "\n", raw)
    text = html.unescape(re.sub(r"<[^>]+>", " ", raw))
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return title, text.strip()


# --------------------------------------------------------------------------- executor
class Lists:
    """Simple named lists (shopping, to-do, …) in ~/.openjarvis/anders/lists.json."""

    SYNONYMS = {"inköpslista": "inköpslistan", "inköp": "inköpslistan", "handlingslista": "inköpslistan",
                "handlingslistan": "inköpslistan", "shoppinglista": "inköpslistan", "shopping list": "inköpslistan",
                "shopping": "inköpslistan", "todo": "att göra", "to do": "att göra", "to-do": "att göra",
                "att-göra": "att göra", "att göra-listan": "att göra", "att göra listan": "att göra", "attgöra": "att göra"}

    def __init__(self) -> None:
        import os
        from pathlib import Path

        self.path = Path(os.environ.get("ANDERS_LISTS_FILE") or Path.home() / ".openjarvis/anders/lists.json")
        self._lock = threading.Lock()

    def _name(self, name: str) -> str:
        n = (name or "").strip().lower()
        return self.SYNONYMS.get(n, n) or "inköpslistan"

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    @staticmethod
    def _items(items: Any) -> list[str]:
        if isinstance(items, str):
            items = re.split(r",|\boch\b|\band\b", items)
        return [str(i).strip() for i in (items or []) if str(i).strip()]

    def add(self, name: str, items: Any) -> dict:
        name, items = self._name(name), self._items(items)
        with self._lock:
            data = self._load()
            lst = data.setdefault(name, [])
            have = {x["item"].lower() for x in lst}
            added = [i for i in items if i.lower() not in have]
            lst.extend({"item": i, "added": time.time()} for i in added)
            self._save(data)
        return {"lista": name, "tillagt": added, "fanns_redan": [i for i in items if i not in added],
                "antal_nu": len(lst)}

    def show(self, name: str = "") -> dict:
        data = self._load()
        if not name:
            return {"listor": {k: len(v) for k, v in data.items()}} if data else {"info": "inga listor ännu"}
        name = self._name(name)
        return {"lista": name, "innehåll": [x["item"] for x in data.get(name, [])]}

    def remove(self, name: str, items: Any) -> dict:
        name, items = self._name(name), self._items(items)
        with self._lock:
            data = self._load()
            lst = data.get(name, [])
            if [i.lower() for i in items] in (["alla"], ["allt"], ["all"]):
                removed = [x["item"] for x in lst]
                data[name] = []
            else:
                want = {i.lower() for i in items}
                removed = [x["item"] for x in lst if x["item"].lower() in want]
                data[name] = [x for x in lst if x["item"].lower() not in want]
            self._save(data)
        return {"lista": name, "borttaget": removed, "kvar": [x["item"] for x in data.get(name, [])]}


def _day_symbol(day: dict) -> str:
    """Midday weather, unless the day has real precipitation: then its most common precipitation type."""
    if day["mm"] >= 1.0 and day["wet"]:
        return yr_text(max(set(day["wet"]), key=day["wet"].count))
    return yr_text(day["sym"][min(day["sym"])]) if day["sym"] else "okänt"


class Tools:
    def __init__(self, home_getter: Callable[[], str], remember: Callable[[str], None], scheduler: Any = None,
                 system: Any = None) -> None:
        self.system = system
        self.loc = Location(home_getter)
        self.remember = remember
        self.scheduler = scheduler
        self.lists = Lists()
        self._web = None

    def run(self, name: str, args: dict) -> str:
        try:
            fn = getattr(self, "t_" + name)
        except AttributeError:
            return json.dumps({"error": f"okänt verktyg: {name}"})
        try:
            out = fn(**(args or {}))
        except TypeError as exc:
            out = {"error": f"fel argument: {exc}"}
        except httpx.HTTPError as exc:
            out = {"error": f"nätverksfel: {exc.__class__.__name__}"}
        except Exception as exc:  # noqa: BLE001 — a tool must never crash the turn
            out = {"error": str(exc)[:300]}
        return out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)

    # -- tools ------------------------------------------------------------------
    def t_get_time(self) -> dict:
        return {"nu": now_text()}

    def t_get_location(self) -> dict:
        loc = self.loc.current()
        if not loc:
            return {"error": "kunde inte avgöra platsen"}
        return {"plats": ", ".join(x for x in (loc["name"], loc["region"], loc["country"]) if x),
                "källa": loc["source"], "lat": round(loc["lat"], 3), "lon": round(loc["lon"], 3)}

    _yr_cache: dict = {}

    def _yr(self, lat: float, lon: float) -> dict:
        """MET Norway Locationforecast (the data behind yr.no), cached until its Expires header."""
        from email.utils import parsedate_to_datetime

        key = (round(lat, 4), round(lon, 4))  # MET asks for at most 4 decimals
        hit = self._yr_cache.get(key)
        if hit and time.time() < hit["expires"]:
            return hit["data"]
        r = httpx.get("https://api.met.no/weatherapi/locationforecast/2.0/complete",
                      params={"lat": key[0], "lon": key[1]}, headers=UA, timeout=_TIMEOUT, follow_redirects=True)
        r.raise_for_status()
        try:
            exp = parsedate_to_datetime(r.headers["expires"]).timestamp()
        except Exception:
            exp = time.time() + 600
        data = r.json()
        self._yr_cache[key] = {"data": data, "expires": exp}
        return data

    def t_get_weather(self, place: str = "", days: int = 7, **_ignored) -> dict:
        loc = self.loc.geocode(place) if (place or "").strip() else self.loc.current()
        if not loc:
            return {"error": f"hittade ingen plats som heter '{place}'" if place else "okänd plats"}
        try:
            return self._weather_yr(loc)
        except Exception:
            out = self._weather_openmeteo(loc)  # backup if yr.no is unreachable
            out["obs"] = "yr.no svarade inte, data från Open-Meteo"
            return out

    def _weather_yr(self, loc: dict) -> dict:
        from zoneinfo import ZoneInfo

        try:
            tz = ZoneInfo(loc.get("tz") or "Europe/Stockholm")
        except Exception:
            tz = ZoneInfo("Europe/Stockholm")
        ts = self._yr(loc["lat"], loc["lon"])["properties"]["timeseries"]
        now = dt.datetime.now(tz)
        first = ts[0]["data"]
        inst, n1 = first["instant"]["details"], first.get("next_1_hours", {})
        out = {"plats": ", ".join(x for x in (loc["name"], loc["region"]) if x), "källa": "yr.no (MET Norway)",
               "just_nu": {"väder": yr_text(n1.get("summary", {}).get("symbol_code", "")),
                           "temperatur_c": inst.get("air_temperature"), "vind_m_s": inst.get("wind_speed"),
                           "byar_m_s": inst.get("wind_speed_of_gust"), "luftfuktighet_procent": inst.get("relative_humidity"),
                           "nederbörd_nästa_timme_mm": n1.get("details", {}).get("precipitation_amount")},
               "kommande_timmar": [], "prognos": []}
        days: dict = {}
        for e in ts:
            t = dt.datetime.fromisoformat(e["time"].replace("Z", "+00:00")).astimezone(tz)
            d, det = e["data"], e["data"]["instant"]["details"]
            hourly = "next_1_hours" in d
            nxt = d.get("next_1_hours") or d.get("next_6_hours") or {}
            nd = nxt.get("details", {})
            if hourly and len(out["kommande_timmar"]) < 12 and t >= now - dt.timedelta(minutes=59):
                out["kommande_timmar"].append({"kl": f"{t:%H}", "väder": yr_text(nxt.get("summary", {}).get("symbol_code", "")),
                                               "temp_c": det.get("air_temperature"), "nederbörd_mm": nd.get("precipitation_amount"),
                                               "regnrisk_procent": nd.get("probability_of_precipitation")})
            day = days.setdefault(t.date(), {"temps": [], "mm": 0.0, "prob": 0.0, "wind": 0.0, "gust": 0.0, "sym": {}, "wet": [], "last6": None})
            day["temps"].append(det.get("air_temperature"))
            day["wind"] = max(day["wind"], det.get("wind_speed") or 0)
            day["gust"] = max(day["gust"], det.get("wind_speed_of_gust") or 0)
            # precipitation: hourly steps use next_1_hours; the 6-hourly part uses next_6_hours (no double counting)
            if hourly or (day["last6"] is None or t >= day["last6"]):
                day["mm"] += nd.get("precipitation_amount") or 0.0
                if not hourly:
                    day["last6"] = t + dt.timedelta(hours=6)
            day["prob"] = max(day["prob"], nd.get("probability_of_precipitation") or 0.0)
            if nxt.get("summary"):
                code = nxt["summary"]["symbol_code"]
                day["sym"][abs(t.hour - 13)] = code  # representative: closest to midday
                if any(w in code for w in ("rain", "sleet", "snow")):
                    day["wet"].append(code.split("_")[0])
        for i, (date, day) in enumerate(sorted(days.items())[:7]):
            temps = [x for x in day["temps"] if x is not None]
            label = "idag" if date == now.date() else "imorgon" if date == now.date() + dt.timedelta(days=1) else WEEKDAYS[date.weekday()]
            out["prognos"].append({"dag": f"{label} ({date.isoformat()})",
                                   "väder": _day_symbol(day),
                                   "max_c": max(temps) if temps else None, "min_c": min(temps) if temps else None,
                                   "nederbörd_mm": round(day["mm"], 1), "regnrisk_procent": round(day["prob"]),
                                   "max_vind_m_s": day["wind"], "max_byar_m_s": day["gust"] or None})
        return out

    def _weather_openmeteo(self, loc: dict, days: int = 7) -> dict:
        w = httpx.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": loc["lat"], "longitude": loc["lon"], "timezone": "auto", "forecast_days": days,
            "wind_speed_unit": "ms",
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,weather_code,wind_speed_10m,wind_gusts_10m",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max,wind_speed_10m_max,sunrise,sunset",
        }, headers=UA, timeout=_TIMEOUT, follow_redirects=True).json()
        c, d = w.get("current", {}), w.get("daily", {})
        out = {"plats": ", ".join(x for x in (loc["name"], loc["region"]) if x),
               "just_nu": {"väder": WMO.get(c.get("weather_code"), "okänt"), "temperatur_c": c.get("temperature_2m"),
                           "känns_som_c": c.get("apparent_temperature"), "vind_m_s": c.get("wind_speed_10m"),
                           "byar_m_s": c.get("wind_gusts_10m"), "nederbörd_mm": c.get("precipitation"),
                           "luftfuktighet_procent": c.get("relative_humidity_2m")},
               "prognos": []}
        for i, day in enumerate(d.get("time", [])):
            wd = WEEKDAYS[dt.date.fromisoformat(day).weekday()]
            out["prognos"].append({
                "dag": ("idag" if i == 0 else "imorgon" if i == 1 else wd) + f" ({day})",
                "väder": WMO.get(d["weather_code"][i], "okänt"),
                "max_c": d["temperature_2m_max"][i], "min_c": d["temperature_2m_min"][i],
                "nederbörd_mm": d["precipitation_sum"][i], "regnrisk_procent": d["precipitation_probability_max"][i],
                "max_vind_m_s": d["wind_speed_10m_max"][i],
                "soluppgång": d["sunrise"][i][-5:], "solnedgång": d["sunset"][i][-5:]})
        out["källa"] = "Open-Meteo"
        return out

    def t_web_search(self, query: str, max_results: int = 5) -> str:
        if self._web is None:
            from openjarvis.tools.web_search import WebSearchTool

            self._web = WebSearchTool()
        r = self._web.execute(query=query, max_results=max(1, min(int(max_results or 5), 8)))
        text = (r.content or "").strip() if r.success else f"Sökningen misslyckades: {r.content}"
        return text[:7000] or "Inga resultat."

    def t_fetch_webpage(self, url: str) -> dict:
        from openjarvis.security.ssrf import check_ssrf

        url = (url or "").strip()
        if not re.match(r"^https?://", url):
            return {"error": "adressen måste börja med http:// eller https://"}
        problem = check_ssrf(url)
        if problem:
            return {"error": f"adressen är blockerad av säkerhetsskäl: {problem}"}
        r = httpx.get(url, headers={**UA, "Accept-Language": "sv,en;q=0.8"}, timeout=15.0, follow_redirects=True)
        ctype = r.headers.get("content-type", "")
        if "html" not in ctype and "text" not in ctype and "json" not in ctype:
            return {"error": f"kan inte läsa den här typen av innehåll ({ctype or 'okänt'})"}
        title, text = _strip_html(r.text) if "html" in ctype else ("", r.text)
        return {"url": str(r.url), "titel": title, "text": text[:6000],
                "förkortad": len(text) > 6000}

    def t_calculate(self, expression: str) -> dict:
        from openjarvis.tools.calculator import safe_eval

        value = safe_eval(str(expression).replace(",", ".").replace("×", "*").replace("÷", "/"))
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        return {"uttryck": expression, "resultat": value}

    def t_remember_note(self, note: str) -> dict:
        self.remember(note)
        return {"sparat": note}

    # -- timers, alarms, reminders ---------------------------------------------------
    def t_set_timer(self, minutes: float = 0, seconds: int = 0, label: str = "", **_):
        from anders_alarms import fmt_duration, fmt_when

        total = float(minutes or 0) * 60 + float(seconds or 0)
        it = self.scheduler.add_timer(total, label or "")
        return {"ok": True, "timer": fmt_duration(total), "etikett": it["label"] or None, "klar": fmt_when(it["due"])}

    def t_set_alarm(self, time: str, date: str = "", repeat: str = "none", label: str = "", **_):
        from anders_alarms import fmt_when

        it = self.scheduler.add_alarm(time, date or None, label or "", repeat or "none")
        return {"ok": True, "larm": fmt_when(it["due"]), "upprepas": it["repeat"], "etikett": it["label"] or None}

    def t_set_reminder(self, when: str, text: str, **_):
        from anders_alarms import fmt_when

        it = self.scheduler.add_reminder(when, text)
        return {"ok": True, "påminnelse": it["label"], "när": fmt_when(it["due"])}

    def t_list_timers(self, **_):
        items = self.scheduler.describe()
        return {"aktiva": items} if items else {"aktiva": [], "info": "inga aktiva timers, larm eller påminnelser"}

    def t_cancel_timer(self, ref: str = "", kind: str = "", **_):
        hits = self.scheduler.cancel(ref or "", kind or "")
        if not hits:
            return {"ok": False, "info": "hittade inget att stänga av"}
        return {"ok": True, "avstängt": [h["label"] or h["kind"] for h in hits]}

    # -- lists ------------------------------------------------------------------------------
    def t_list_add(self, list: str, items: Any, **_):  # noqa: A002
        return self.lists.add(list, items)

    def t_list_show(self, list: str = "", **_):  # noqa: A002
        return self.lists.show(list)

    def t_list_remove(self, list: str, items: Any, **_):  # noqa: A002
        return self.lists.remove(list, items)

    # -- Swedish everyday info ------------------------------------------------------------
    def t_electricity_price(self, day: str = "idag", area: str = "SE3", **_):
        d = dt.date.today() + dt.timedelta(days=1 if (day or "").startswith("imorg") or day == "tomorrow" else 0)
        area = (area or "SE3").upper()
        r = httpx.get(f"https://www.elprisetjustnu.se/api/v1/prices/{d:%Y}/{d:%m-%d}_{area}.json", headers=UA, timeout=_TIMEOUT, follow_redirects=True)
        if r.status_code == 404:
            return {"info": "morgondagens priser publiceras runt kl 13 dagen innan" if d > dt.date.today() else "inga priser hittades"}
        r.raise_for_status()
        hours: dict[int, list] = {}
        for row in r.json():
            h = int(row["time_start"][11:13])
            hours.setdefault(h, []).append(row["SEK_per_kWh"] * 100)  # 15-min prices -> öre/kWh per hour
        per_hour = {h: round(sum(v) / len(v), 1) for h, v in sorted(hours.items())}
        cheap = sorted(per_hour, key=per_hour.get)[:3]
        dear = sorted(per_hour, key=per_hour.get, reverse=True)[:3]
        now_h = dt.datetime.now().hour
        out = {"datum": d.isoformat(), "elområde": area, "enhet": "öre/kWh, spotpris exkl. moms, nätavgift och skatt",
               "snitt": round(sum(per_hour.values()) / len(per_hour), 1),
               "billigast": [f"kl {h:02d}-{h + 1:02d}: {per_hour[h]}" for h in sorted(cheap)],
               "dyrast": [f"kl {h:02d}-{h + 1:02d}: {per_hour[h]}" for h in sorted(dear)],
               "per_timme": {f"{h:02d}": p for h, p in per_hour.items()}}
        if d == dt.date.today() and now_h in per_hour:
            out["just_nu"] = per_hour[now_h]
        return out

    def t_swedish_day(self, date: str = "", **_):
        d = dt.date.fromisoformat(date) if date else dt.date.today()
        r = httpx.get(f"https://sholiday.faboul.se/dagar/v2.1/{d:%Y/%m/%d}", headers=UA, timeout=_TIMEOUT, follow_redirects=True).json()
        day = (r.get("dagar") or [{}])[0]
        return {"datum": d.isoformat(), "veckodag": day.get("veckodag"), "vecka": day.get("vecka"),
                "namnsdag": day.get("namnsdag") or [], "röd_dag": day.get("röd dag") == "Ja",
                "helgdag": day.get("helgdag"), "flaggdag": day.get("flaggdag") or None,
                "arbetsfri": day.get("arbetsfri dag") == "Ja"}

    def t_news_headlines(self, count: int = 5, **_):
        import xml.etree.ElementTree as ET

        r = httpx.get("https://www.svt.se/nyheter/rss.xml", headers=UA, timeout=_TIMEOUT, follow_redirects=True)
        items = ET.fromstring(r.content).findall("./channel/item")[: max(1, min(int(count or 5), 10))]
        return {"källa": "SVT Nyheter", "rubriker": [
            {"rubrik": (i.findtext("title") or "").strip(),
             "ingress": re.sub(r"<[^>]+>", "", i.findtext("description") or "").strip()[:220],
             "länk": i.findtext("link")} for i in items]}

    def t_wikipedia(self, query: str, lang: str = "sv", **_):
        lang = (lang or "sv")[:2]
        for lg in dict.fromkeys([lang, "sv", "en"]):
            s = httpx.get(f"https://{lg}.wikipedia.org/w/api.php", params={"action": "query", "list": "search",
                          "srsearch": query, "format": "json", "srlimit": 1}, headers=UA, timeout=_TIMEOUT, follow_redirects=True).json()
            hits = s.get("query", {}).get("search", [])
            if not hits:
                continue
            title = hits[0]["title"]
            r = httpx.get(f"https://{lg}.wikipedia.org/api/rest_v1/page/summary/{title.replace(' ', '_')}",
                          headers=UA, timeout=_TIMEOUT, follow_redirects=True).json()
            return {"titel": r.get("title", title), "sammanfattning": (r.get("extract") or "")[:1800],
                    "språk": lg, "källa": f"Wikipedia ({lg})"}
        return {"info": f"hittade inget på Wikipedia om '{query}'"}

    def t_convert_currency(self, amount: float, from_currency: str, to_currency: str, **_):
        f, t = from_currency.strip().upper()[:3], to_currency.strip().upper()[:3]
        if f == t:
            return {"resultat": amount}
        r = httpx.get("https://api.frankfurter.app/latest", params={"amount": amount, "from": f, "to": t},
                      headers=UA, timeout=_TIMEOUT, follow_redirects=True)
        if r.status_code != 200:
            return {"error": f"okänd valuta: {f} eller {t}"}
        j = r.json()
        return {"belopp": amount, "från": f, "till": t, "resultat": round(j["rates"][t], 2),
                "kursdatum": j.get("date"), "källa": "Europeiska centralbanken (via Frankfurter)"}

    # -- computer control (anders_system) -----------------------------------------------
    def t_computer_power(self, action: str, **_):
        return self.system.power(action)

    def t_computer_volume(self, action: str = "get", percent: float | None = None, **_):
        return self.system.volume(action, percent)

    def t_computer_brightness(self, action: str = "get", percent: float | None = None, **_):
        return self.system.brightness(action, percent)

    def t_computer_media(self, action: str = "status", **_):
        return self.system.media(action)

    def t_computer_open_app(self, name: str, **_):
        return self.system.open_app(name)

    def t_computer_close_app(self, name: str, **_):
        return self.system.close_app(name)

    def t_computer_setting(self, setting: str, on: bool = True, **_):
        return self.system.desktop_setting(setting, on)

    def t_computer_status(self, **_):
        return self.system.status()

    def t_open_url(self, url: str, **_):
        import subprocess

        url = (url or "").strip()
        if not re.match(r"^https?://[^\s]+$", url):
            return {"error": "bara http(s)-adresser kan öppnas"}
        subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return {"ok": True, "öppnade": url}
