#!/usr/bin/python3
"""Native window for the Anders app (system Python: GTK 3 + WebKit2GTK 4.1)."""

import sys
import time
import urllib.request

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import GLib, Gtk, WebKit2  # noqa: E402

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8770/"
ICON = sys.argv[2] if len(sys.argv) > 2 else None

LOADING = """<html><body style="margin:0;height:100vh;display:grid;place-items:center;
background:#0b1020;color:#8a96ad;font-family:Cantarell,system-ui,sans-serif">
<div style="text-align:center"><div style="font-size:22px;color:#e8edf7;font-weight:700">Anders</div>
<div style="margin-top:8px">Startar…</div></div></body></html>"""


def main() -> None:
    GLib.set_prgname("anders")  # matches StartupWMClass in the .desktop file
    GLib.set_application_name("Anders")
    win = Gtk.Window(title="Anders")
    win.set_default_size(1180, 760)
    win.set_wmclass("anders", "Anders")
    if ICON:
        try:
            win.set_icon_from_file(ICON)
        except Exception:
            pass
    settings = WebKit2.Settings()
    settings.set_enable_developer_extras(False)
    view = WebKit2.WebView()
    view.set_settings(settings)
    view.set_background_color(_rgba("#0b1020"))
    view.load_html(LOADING, "about:blank")
    win.add(view)
    win.connect("destroy", Gtk.main_quit)
    win.show_all()

    def try_load():
        try:
            urllib.request.urlopen(URL, timeout=0.5)
            view.load_uri(URL)
            return False
        except Exception:
            return True  # keep polling until the backend is up

    GLib.timeout_add(400, try_load)
    Gtk.main()


def _rgba(hexcolor: str):
    from gi.repository import Gdk

    c = Gdk.RGBA()
    c.parse(hexcolor)
    return c


if __name__ == "__main__":
    main()
