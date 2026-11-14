"""Computer control for Anders (GNOME on Ubuntu).

Deliberately a fixed set of actions — no free-form shell commands:

  immediate        volume, brightness, media players (MPRIS), open apps, dark mode,
                   do-not-disturb, system status
  after the reply  lock screen, suspend  (so Anders finishes talking first)
  needs YOU to     shut down, restart, log out, close an app
  confirm          -> the app asks; only your own "ja" / the Bekräfta button runs it
                      (the language model cannot confirm by itself), then a countdown
                      with Avbryt for power actions.
"""

from __future__ import annotations

import configparser
import difflib
import os
import re
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

APP_DIRS = [Path("/usr/share/applications"), Path("/usr/local/share/applications"),
            Path.home() / ".local/share/applications", Path("/var/lib/snapd/desktop/applications"),
            Path("/var/lib/flatpak/exports/share/applications"),
            Path.home() / ".local/share/flatpak/exports/share/applications"]

# spoken names -> desktop ids
APP_ALIASES = {"webbläsaren": "@browser", "webbläsare": "@browser", "browser": "@browser", "internet": "@browser",
               "terminalen": "org.gnome.Ptyxis", "terminal": "org.gnome.Ptyxis",
               "filer": "org.gnome.Nautilus", "filhanteraren": "org.gnome.Nautilus", "utforskaren": "org.gnome.Nautilus",
               "inställningar": "org.gnome.Settings", "inställningarna": "org.gnome.Settings",
               "miniräknaren": "org.gnome.Calculator", "kalkylatorn": "org.gnome.Calculator",
               "kameran": "org.gnome.Snapshot", "kamera": "org.gnome.Snapshot",
               "kalkylator": "org.gnome.Calculator", "kalkylatorn": "org.gnome.Calculator", "räknare": "org.gnome.Calculator",
               "miniräknare": "org.gnome.Calculator", "räknaren": "org.gnome.Calculator",
               "klockan": "org.gnome.clocks", "klockor": "org.gnome.clocks", "kalender": "org.gnome.Calendar",
               "kalendern": "org.gnome.Calendar", "kartor": "org.gnome.Maps", "väder": "org.gnome.Weather",
               "systemövervakaren": "org.gnome.SystemMonitor", "aktivitetshanteraren": "org.gnome.SystemMonitor",
               "programvara": "org.gnome.Software", "app center": "snap-store_snap-store",
               "textredigeraren": "org.gnome.TextEditor", "anteckningar": "org.gnome.TextEditor"}


def _run(cmd: list[str], timeout: float = 8) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout.strip()


# --------------------------------------------------------------------------- apps
class Apps:
    def __init__(self) -> None:
        self._apps: list[dict] | None = None
        self._loaded = 0.0

    def all(self) -> list[dict]:
        if self._apps is not None and time.time() - self._loaded < 300:
            return self._apps
        apps: dict[str, dict] = {}
        for d in APP_DIRS:
            for f in sorted(d.glob("*.desktop")) if d.is_dir() else []:
                cp = configparser.RawConfigParser(strict=False, interpolation=None)
                try:
                    cp.read(f, encoding="utf-8")
                    e = cp["Desktop Entry"]
                except Exception:
                    continue
                if e.get("Type", "Application") != "Application" or e.get("NoDisplay", "").lower() == "true" \
                        or e.get("Hidden", "").lower() == "true":
                    continue
                did = f.stem
                apps[did] = {"id": did, "name": e.get("Name[sv]") or e.get("Name", did), "name_en": e.get("Name", ""),
                             "exec": e.get("Exec", ""), "wmclass": e.get("StartupWMClass", ""),
                             "keywords": (e.get("Keywords[sv]", "") + ";" + e.get("Keywords", "")).lower()}
        self._apps, self._loaded = list(apps.values()), time.time()
        return self._apps

    def find(self, query: str) -> tuple[dict | None, list[str]]:
        q = (query or "").strip().lower()
        q = re.sub(r"^(programmet|appen|app|program)\s+", "", q)
        alias = APP_ALIASES.get(q)
        if alias == "@browser":
            alias = _run(["xdg-settings", "get", "default-web-browser"]).removesuffix(".desktop") or "firefox_firefox"
        apps = self.all()
        if alias:
            for a in apps:
                if a["id"] == alias or a["id"].endswith(alias):
                    return a, []
        def score(a):
            names = [a["name"].lower(), a["name_en"].lower(), a["id"].lower()]
            if q in names:
                return 3.0
            if any(n.startswith(q) for n in names):
                return 2.0
            if any(q in n for n in names) or q in a["keywords"]:
                return 1.5
            return max(difflib.SequenceMatcher(None, q, n).ratio() for n in names)
        ranked = sorted(apps, key=score, reverse=True)
        best = ranked[0] if ranked else None
        if best and score(best) >= 0.75:
            return best, []
        return None, [a["name"] for a in ranked[:5]]

    @staticmethod
    def process_names(app: dict) -> set[str]:
        names = set()
        exe = (app["exec"].split() or [""])
        tokens = [t for t in exe if not t.startswith("%") and "=" not in t]
        if tokens and tokens[0] in ("env",):
            tokens = tokens[1:]
        if tokens and tokens[0].endswith("flatpak") and "run" in tokens:
            names.add(tokens[-1].split(".")[-1].lower())
        if tokens:
            names.add(os.path.basename(tokens[0]).lower())
        if app["wmclass"]:
            names.add(app["wmclass"].lower())
        names.add(app["id"].split(".")[-1].split("_")[0].lower())
        return {n for n in names if n and n not in ("sh", "bash", "python3", "env")}


# --------------------------------------------------------------------------- control
class SystemControl:
    def __init__(self, request_confirm: Callable[[dict], None], after_reply: Callable[[Callable[[], None]], None]) -> None:
        self.request_confirm = request_confirm
        self.after_reply = after_reply
        self.apps = Apps()

    # -- power -------------------------------------------------------------------
    def power(self, action: str) -> dict:
        action = (action or "").lower()
        if action in ("lock", "lås"):
            self.after_reply(lambda: subprocess.Popen(
                ["gdbus", "call", "--session", "--dest", "org.gnome.ScreenSaver", "--object-path",
                 "/org/gnome/ScreenSaver", "--method", "org.gnome.ScreenSaver.Lock"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            return {"ok": True, "info": "skärmen låses när du har svarat klart"}
        if action in ("suspend", "sleep", "vila", "viloläge"):
            self.after_reply(lambda: subprocess.Popen(["systemctl", "suspend"], stdout=subprocess.DEVNULL,
                                                      stderr=subprocess.DEVNULL))
            return {"ok": True, "info": "datorn går i viloläge när du har svarat klart"}
        spec = {"shutdown": ("stänga av datorn", "Datorn stängs av", ["--power-off"], ["systemctl", "poweroff"]),
                "reboot": ("starta om datorn", "Datorn startas om", ["--reboot"], ["systemctl", "reboot"]),
                "logout": ("logga ut dig", "Du loggas ut", ["--logout"], None)}.get(action)
        if not spec:
            return {"error": f"okänd åtgärd: {action}"}
        label, countdown_text, gnome_flag, fallback = spec

        def run() -> None:
            # GNOME's own way first: apps get the chance to save / object (unsaved work).
            try:
                subprocess.run(["gnome-session-quit", *gnome_flag, "--no-prompt"], timeout=20, check=True)
            except Exception:
                if fallback:
                    subprocess.Popen(fallback)

        self.request_confirm({"kind": action, "label": label, "countdown": 10, "countdown_text": countdown_text, "run": run})
        return {"status": "väntar_på_bekräftelse",
                "instruktion": f"Fråga kort om användaren verkligen vill {label}. Det sker BARA om användaren själv "
                               "säger ja eller trycker Bekräfta – säg inte att det är gjort."}

    # -- apps ----------------------------------------------------------------------
    def open_app(self, name: str) -> dict:
        app, suggestions = self.apps.find(name)
        if not app:
            return {"error": f"hittade inget program som heter '{name}'", "kanske": suggestions}
        subprocess.Popen(["gtk-launch", app["id"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        return {"ok": True, "öppnade": app["name"]}

    def close_app(self, name: str) -> dict:
        import psutil

        app, suggestions = self.apps.find(name)
        if not app:
            return {"error": f"hittade inget program som heter '{name}'", "kanske": suggestions}
        wanted = self.apps.process_names(app)
        me = os.getpid()
        procs = []
        for p in psutil.process_iter(["pid", "name", "exe", "cmdline", "username"]):
            try:
                if p.info["username"] != os.environ.get("USER") or p.pid == me:
                    continue
                cmd0 = os.path.basename((p.info["cmdline"] or [""])[0]).lower()
                exe = os.path.basename(p.info["exe"] or "").lower()
                if (p.info["name"] or "").lower() in wanted or cmd0 in wanted or exe in wanted:
                    procs.append(p)
            except Exception:
                continue
        if not procs:
            return {"info": f"{app['name']} verkar inte vara igång"}
        top = [p for p in procs if p.ppid() not in {q.pid for q in procs}]

        def run() -> None:
            for p in top:
                try:
                    p.terminate()  # polite: like closing the window
                except Exception:
                    pass

        self.request_confirm({"kind": "close_app", "label": f"stänga {app['name']}", "countdown": 0,
                              "countdown_text": "", "run": run})
        return {"status": "väntar_på_bekräftelse",
                "instruktion": f"Fråga kort om användaren vill stänga {app['name']} (osparat arbete kan försvinna). "
                               "Det sker BARA om användaren själv säger ja eller trycker Bekräfta."}

    # -- sound & screen ---------------------------------------------------------------
    def volume(self, action: str = "get", percent: float | None = None) -> dict:
        sink = "@DEFAULT_AUDIO_SINK@"
        a = (action or "get").lower()
        if a == "set" and percent is not None:
            _run(["wpctl", "set-volume", "-l", "1.0", sink, f"{max(0, min(100, float(percent))) / 100:.2f}"])
        elif a in ("up", "down"):
            step = abs(float(percent or 10))
            _run(["wpctl", "set-volume", "-l", "1.0", sink, f"{step:.0f}%{'+' if a == 'up' else '-'}"])
        elif a in ("mute", "unmute"):
            _run(["wpctl", "set-mute", sink, "1" if a == "mute" else "0"])
        out = _run(["wpctl", "get-volume", sink])
        m = re.search(r"([\d.]+)", out)
        return {"volym_procent": round(float(m.group(1)) * 100) if m else None, "ljud_av": "MUTED" in out}

    def brightness(self, action: str = "get", percent: float | None = None) -> dict:
        base = next((p for p in Path("/sys/class/backlight").iterdir()), None) if Path("/sys/class/backlight").exists() else None
        if base is None:
            return {"error": "hittade ingen skärm med justerbar ljusstyrka"}
        mx = int((base / "max_brightness").read_text())
        cur = int((base / "brightness").read_text())
        a = (action or "get").lower()
        target = None
        if a == "set" and percent is not None:
            target = float(percent)
        elif a in ("up", "down"):
            target = cur / mx * 100 + (1 if a == "up" else -1) * abs(float(percent or 15))
        if target is not None:
            value = int(round(max(3, min(100, target)) / 100 * mx))  # never fully black
            _run(["busctl", "call", "org.freedesktop.login1", "/org/freedesktop/login1/session/auto",
                  "org.freedesktop.login1.Session", "SetBrightness", "ssu", "backlight", base.name, str(value)])
            cur = value
        return {"ljusstyrka_procent": round(cur / mx * 100)}

    # -- media (any MPRIS player: Spotify, Firefox/YouTube, …) -----------------------------
    def media(self, action: str = "status") -> dict:
        players = [l.split()[0] for l in _run(["busctl", "--user", "list", "--no-pager", "--no-legend"]).splitlines()
                   if l.split() and l.split()[0].startswith("org.mpris.MediaPlayer2.")]
        if not players:
            return {"info": "ingen mediaspelare är igång"}
        player = players[0]
        method = {"play_pause": "PlayPause", "play": "Play", "pause": "Pause", "stop": "Stop",
                  "next": "Next", "previous": "Previous"}.get((action or "status").lower())
        if method:
            _run(["busctl", "--user", "call", player, "/org/mpris/MediaPlayer2", "org.mpris.MediaPlayer2.Player", method])
            time.sleep(0.4)
        meta = _run(["gdbus", "call", "--session", "--dest", player, "--object-path", "/org/mpris/MediaPlayer2",
                     "--method", "org.freedesktop.DBus.Properties.Get", "org.mpris.MediaPlayer2.Player", "Metadata"])
        status = _run(["gdbus", "call", "--session", "--dest", player, "--object-path", "/org/mpris/MediaPlayer2",
                       "--method", "org.freedesktop.DBus.Properties.Get", "org.mpris.MediaPlayer2.Player", "PlaybackStatus"])
        title = re.search(r"'xesam:title': <'([^']*)'>", meta)
        artist = re.search(r"'xesam:artist': <\['([^']*)'", meta)
        return {"spelare": player.split(".")[-1], "status": {"Playing": "spelar", "Paused": "pausad",
                                                                 "Stopped": "stoppad"}.get(re.sub(r"\W", "", status.split("'")[1]) if "'" in status else "", status),
                "titel": title.group(1) if title else None, "artist": artist.group(1) if artist else None}

    # -- desktop settings ------------------------------------------------------------------
    def desktop_setting(self, setting: str, on: bool = True) -> dict:
        s = (setting or "").lower()
        if s in ("dark_mode", "mörkt läge", "mörkt"):
            _run(["gsettings", "set", "org.gnome.desktop.interface", "color-scheme", "prefer-dark" if on else "default"])
            return {"ok": True, "mörkt_läge": bool(on)}
        if s in ("do_not_disturb", "stör ej", "dnd"):
            _run(["gsettings", "set", "org.gnome.desktop.notifications", "show-banners", "false" if on else "true"])
            return {"ok": True, "stör_ej": bool(on)}
        return {"error": f"okänd inställning: {setting}"}

    # -- status -------------------------------------------------------------------------------
    def status(self) -> dict:
        import psutil

        out: dict[str, Any] = {}
        b = psutil.sensors_battery()
        if b:
            out["batteri"] = {"procent": round(b.percent), "laddar": bool(b.power_plugged),
                              "tid_kvar_min": None if b.power_plugged or b.secsleft < 0 else round(b.secsleft / 60)}
        out["processor_procent"] = psutil.cpu_percent(interval=0.4)
        vm = psutil.virtual_memory()
        out["minne"] = {"använt_procent": vm.percent, "totalt_gb": round(vm.total / 1e9, 1)}
        du = shutil.disk_usage("/")
        out["disk"] = {"ledigt_gb": round(du.free / 1e9), "totalt_gb": round(du.total / 1e9)}
        up = time.time() - psutil.boot_time()
        out["igång_sedan"] = f"{int(up // 86400)} d {int(up % 86400 // 3600)} h {int(up % 3600 // 60)} min"
        try:
            ssid = next((l.split(":", 1)[1] for l in _run(["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"]).splitlines()
                         if l.startswith("yes:")), None)
            out["wifi"] = ssid
        except Exception:
            pass
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("10.255.255.255", 1))
            out["lokal_ip"] = s.getsockname()[0]
            s.close()
        except Exception:
            pass
        try:
            gpu = _run(["nvidia-smi", "--query-gpu=temperature.gpu,utilization.gpu,memory.used,memory.total",
                        "--format=csv,noheader,nounits"]).split(",")
            out["grafikkort"] = {"temp_c": int(gpu[0]), "användning_procent": int(gpu[1]),
                                 "minne_mb": f"{gpu[2].strip()}/{gpu[3].strip()}"}
        except Exception:
            pass
        return out


def _fn(name: str, desc: str, props: dict | None = None, required: list | None = None) -> dict:
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {
        "type": "object", "properties": props or {}, "required": required or []}}}


TOOL_SPECS = [
    _fn("computer_power", "Stäng av, starta om, logga ut, viloläge eller lås skärmen. Avstängning/omstart/utloggning "
        "kräver att användaren själv bekräftar.",
        {"action": {"type": "string", "enum": ["shutdown", "reboot", "logout", "suspend", "lock"]}}, ["action"]),
    _fn("computer_volume", "Datorns ljudvolym: visa, sätt, höj, sänk eller stäng av ljudet.",
        {"action": {"type": "string", "enum": ["get", "set", "up", "down", "mute", "unmute"]},
         "percent": {"type": "number", "description": "Nivå (set) eller steg (up/down) i procent."}}, ["action"]),
    _fn("computer_brightness", "Skärmens ljusstyrka: visa, sätt, höj eller sänk.",
        {"action": {"type": "string", "enum": ["get", "set", "up", "down"]},
         "percent": {"type": "number", "description": "Nivå (set) eller steg (up/down) i procent."}}, ["action"]),
    _fn("computer_media", "Styr musik/video som spelas (Spotify, webbläsare m.m.): spela/pausa, nästa, föregående, vad spelas.",
        {"action": {"type": "string", "enum": ["play_pause", "play", "pause", "next", "previous", "stop", "status"]}}, ["action"]),
    _fn("computer_open_app", "Öppna ett program på datorn, t.ex. 'Firefox', 'Spotify', 'terminalen', 'filer', 'inställningar'.",
        {"name": {"type": "string"}}, ["name"]),
    _fn("computer_close_app", "Stäng ett program som är igång (användaren måste bekräfta).",
        {"name": {"type": "string"}}, ["name"]),
    _fn("computer_setting", "Slå på/av mörkt läge eller stör ej (inga notiser).",
        {"setting": {"type": "string", "enum": ["dark_mode", "do_not_disturb"]}, "on": {"type": "boolean"}}, ["setting", "on"]),
    _fn("computer_status", "Datorns status: batteri, processor, minne, disk, wifi, IP, grafikkort, drifttid."),
]

STATUS = {"computer_power": ("Datorn…", "Computer…"), "computer_volume": ("Ändrar volymen…", "Changing volume…"),
          "computer_brightness": ("Ändrar ljusstyrkan…", "Changing brightness…"),
          "computer_media": ("Styr musiken…", "Controlling media…"), "computer_open_app": ("Öppnar programmet…", "Opening the app…"),
          "computer_close_app": ("Stänger programmet…", "Closing the app…"), "computer_setting": ("Ändrar inställning…", "Changing a setting…"),
          "computer_status": ("Kollar datorn…", "Checking the computer…")}
