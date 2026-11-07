"""Two small web servers.

:8090  the frame for the display (LAN). Only /frame.epd, with ETag / 304.
:8099  the status page shown in HA's sidebar (ingress). Only accepts the Supervisor.
"""
import datetime as dt
import hmac
import json
import logging
import os
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from jinja2 import Environment, FileSystemLoader, select_autoescape

import render
import view

log = logging.getLogger("dash")
INGRESS_PEER = "172.30.32.2"
_env = Environment(loader=FileSystemLoader(os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")),
                   autoescape=select_autoescape(["html"]))


class _Base(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "epaper-dashboard"

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.address_string(), fmt % args)

    def send(self, code, body=b"", ctype="text/plain; charset=utf-8", headers=None, head=False):
        self.send_response(code)
        if code != 304:
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body and not head and code != 304:
            self.wfile.write(body)

    @property
    def route(self):
        return urllib.parse.urlsplit(self.path).path


def frame_handler(app):
    class H(_Base):
        def _frame(self, head=False):
            now = app.now()
            if not head:
                try:
                    app.ensure_frame(now)            # in case the display is early or the loop is late
                except Exception as e:
                    log.warning("drawing on request failed: %s", e)
            with app.lock:
                frame, tag = app.frame, app.etag
            ip = self.client_address[0]
            hdr = {"X-Poll-In": str(app.poll_in(now))}
            if frame is None:
                app.note_poll(ip, 503)
                return self.send(503, b"no frame yet\n", headers={"Retry-After": "60", "X-Poll-In": "60"}, head=head)
            have = self.headers.get("If-None-Match")
            if have == tag:
                app.note_poll(ip, 304)
                return self.send(304, headers={"ETag": tag, **hdr})
            gap = now.timestamp() - app.last_new_served
            if have and gap < 175:                    # panel needs 3 min between refreshes
                app.note_poll(ip, 304)
                hdr["X-Poll-In"] = str(int(175 - gap) + 1)
                return self.send(304, headers={"ETag": have, **hdr})
            if not head:
                app.last_new_served = now.timestamp()
            app.note_poll(ip, 200)
            self.send(200, frame, "application/octet-stream", {"ETag": tag, "Cache-Control": "no-cache", **hdr}, head)

        def do_GET(self):
            if self.route == "/frame.epd":
                return self._frame()
            if self.route == "/":
                return self.send(200, b"E-paper dashboard. The display reads /frame.epd\n")
            self.send(404, b"not found\n")

        def do_POST(self):
            """The Clawdmeter daemon drops its latest Claude usage here (needs the push token)."""
            if self.route != "/claude":
                return self.send(404, b"not found\n")
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > 65536:
                return self.send(400, b"bad length\n")
            body = self.rfile.read(n)
            if not hmac.compare_digest(self.headers.get("X-Dashboard-Token", ""), app.push_token):
                app.claude_rejected = {"at": time.time(), "ip": self.client_address[0], "why": "wrong dashboard_token"}
                return self.send(403, b"bad token\n")
            try:
                ok = app.receive_claude(json.loads(body), self.client_address[0])
            except ValueError:
                ok = False
            if not ok:
                app.claude_rejected = {"at": time.time(), "ip": self.client_address[0], "why": "unexpected data"}
            self.send(204 if ok else 400, b"" if ok else b"expected {\"plans\": [...]}\n")

        def do_HEAD(self):
            if self.route == "/frame.epd":
                return self._frame(head=True)
            self.send(404, b"", head=True)
    return H


def _ago(ts):
    if not ts:
        return "never"
    s = int(time.time() - ts)
    return f"{s}s ago" if s < 90 else f"{s // 60} min ago" if s < 5400 else f"{s // 3600} h ago"


def status(app):
    tz = app.ctx.tz
    def t(iso):
        if not iso:
            return "never"
        d = dt.datetime.fromisoformat(iso).astimezone(tz)
        return d.strftime("%a ") + f"{d.hour % 12 or 12}:{d.minute:02d} {'AM' if d.hour < 12 else 'PM'}"
    srcs = []
    for s in app.sources.values():
        partial = s.data.get("errors") if isinstance(s.data, dict) and not s.error else None
        off = isinstance(s.data, dict) and s.data.get("configured") is False and not s.error
        srcs.append({"name": s.name, "ok": s.error is None and s.data is not None and not partial and not off,
                     "when": _ago(s.ok_at), "error": s.error or ("; ".join(partial) if partial else None),
                     "note": (s.hint or "not set up") if off else None})
    cl = app.claude or {}
    if cl:
        latest = max(r["at"] for r in cl.values())
        now = app.now()

        def plan(k, r):
            why = view.claude_hidden(r, now, app.opts)
            return ("Max" if k == "max" else "Enterprise") + f" {_ago(r['at'])}" + (f" (hidden: {why})" if why else "")
        names = ", ".join(plan(k, r) for k, r in cl.items())
        srcs.append({"name": "Claude (Clawdmeter daemon)", "ok": time.time() - latest < 1200, "when": names,
                     "error": None if time.time() - latest < 1200 else "no update in 20+ minutes",
                     "note": None})
    else:
        rej = getattr(app, "claude_rejected", None)
        srcs.append({"name": "Claude (Clawdmeter daemon)", "ok": False, "when": "",
                     "error": f"push from {rej['ip']} {_ago(rej['at'])} turned away: {rej['why']}" if rej else None,
                     "note": None if rej else "waiting for the daemon (settings below)"})
    d = app.ctx.discovered or {}
    labels = {"temperature": "Temp", "humidity": "Hum.", "voc": "VOC", "co2": "CO2"}

    def room(r):
        have = " · ".join(f"{labels[k]} {r[k]}" for k in labels if r.get(k))
        return f"{r['name']}: {have}" + ("" if r.get("co2") else " · no CO2 sensor in this area")
    pk = d.get("packages") or {}
    found = [
        ("Indoor air", [room(r) for r in d.get("rooms") or []]),
        ("3D printers", [f"{p['name']}: {p['id']}_* · " + (("temperatures: " + ", ".join(k.lower() for k in p["temps"]))
                         if p.get("temps") else "no temperature sensors found") for p in d.get("printers") or []]),
        ("Packages", [f"Nest alerts via {e}" for e in pk.get("notifications") or []]
                     + [v for k, v in pk.items() if k != "notifications" and v]),
        ("Health", [v for v in (d.get("health") or {}).values() if v]),
        ("Calendars", list(d.get("calendars") or [])),
        ("Backup weather", [d["weather"]] if d.get("weather") else []),
        ("Garage doors", list(d.get("garages") or [])),
    ]
    pf = app.sources["portfolio"].data or {}
    if pf.get("accounts"):
        found.append(("Brokerage accounts", [f"{a['name']} {a['mask']}".strip() for a in pf["accounts"]]))
    dl = app.sources["deliveries"].data or {}
    if dl.get("last"):
        found.append(("Last package alert today", [dl["last"]]))
    poll = app.last_poll
    return {
        "state": app.state, "drawn": t(app.drawn_at), "render_secs": app.render_secs,
        "render_error": app.render_error, "etag": app.etag, "colors": app.colors,
        "poll": ({"when": t(poll["at"]), "ip": poll["ip"],
                  "what": {200: "got a new frame", 304: "no change", 503: "no frame yet"}.get(poll["code"], poll["code"])}
                 if poll else None),
        "quiet": f"{app.opts.get('quiet_hours_start', 0)}:00 to {app.opts.get('quiet_hours_end', 6)}:00",
        "clock": app.clock_on(),
        "every": app.every(),
        "alerts_now": bool(app.opts.get("alerts_right_away", True)),
        "sources": srcs, "found": found,
        "stocks": ", ".join(s["symbol"] for s in app.opts.get("stocks", [])),
        "version": os.environ.get("DASH_VERSION", ""),
        "push_token": app.push_token,
    }


def ingress_handler(app):
    class I(_Base):
        def _ok(self):
            if app.dev or self.client_address[0] == INGRESS_PEER:
                return True
            self.send(403, b"forbidden\n")
            return False

        def do_GET(self):
            if not self._ok():
                return
            r = self.route.rstrip("/") or "/"
            if r == "/":
                html = _env.get_template("status.html").render(s=status(app))
                return self.send(200, html.encode(), "text/html; charset=utf-8")
            if r == "/status.json":
                return self.send(200, json.dumps(status(app), default=str).encode(), "application/json")
            if r in ("/preview.png", "/panel.png"):
                with app.lock:
                    img = app.sim_png if r == "/preview.png" else app.panel_png
                if not img:
                    return self.send(404, b"not drawn yet\n")
                return self.send(200, img, "image/png", {"Cache-Control": "no-store"})
            if r == "/frame.epd":
                with app.lock:
                    f = app.frame
                if not f:
                    return self.send(404, b"not drawn yet\n")
                return self.send(200, f, "application/octet-stream",
                                 {"Content-Disposition": 'attachment; filename="frame.epd"'})
            self.send(404, b"not found\n")

        def do_POST(self):
            if not self._ok():
                return
            n = int(self.headers.get("Content-Length") or 0)
            if n:
                self.rfile.read(n)
            if self.route.rstrip("/") == "/update":
                app.update_now.set()
                return self.send(204)
            self.send(404, b"not found\n")
    return I


def start(app):
    for port, handler in ((int(os.environ.get("DASH_FRAME_PORT", 8090)), frame_handler(app)),
                          (int(os.environ.get("DASH_INGRESS_PORT", 8099)), ingress_handler(app))):
        srv = ThreadingHTTPServer(("0.0.0.0", port), handler)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        log.info("listening on :%d", port)
