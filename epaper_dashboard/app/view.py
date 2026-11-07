"""Turns raw data into exactly what the panel shows (the "view").

Everything here is already rounded and held per the refresh rules, so if two
views are equal the screen would look the same and we skip the redraw.
"""
import datetime as dt
import logging
import math

import sources

log = logging.getLogger("dash")

MINUS = "−"
DASH = "—"


class Sticky:
    """Holds a shown value until the new one is different enough to be worth a redraw."""

    def __init__(self):
        self.shown = {}                        # key -> (value, time shown, level)

    def get(self, key, value, now, changed=None, max_age=None, level=None):
        prev = self.shown.get(key)
        if prev is not None:
            pv, pt, pl = prev
            age = (now - pt).total_seconds()
            if value == pv:
                return pv
            keep = level == pl
            if keep and changed is not None:
                keep = not changed(pv, value)
            elif keep:
                keep = False                   # no rule: any change shows
            if keep and max_age is not None and age >= max_age:
                keep = False
            if keep:
                return pv
        self.shown[key] = (value, now, level)
        return value


# ---------------------------------------------------------------- formatting
def t12(t, suffix=True):
    h = t.hour % 12 or 12
    s = f"{h}:{t.minute:02d}"
    return f"{s} {'AM' if t.hour < 12 else 'PM'}" if suffix else s


def t_range(a, b):
    if (a.hour < 12) == (b.hour < 12) and a.date() == b.date():
        return f"{t12(a, False)}–{t12(b)}"
    return f"{t12(a)}–{t12(b)}"


def hour_short(t):
    return f"{t.hour % 12 or 12}{'A' if t.hour < 12 else 'P'}"


def round_to(t, minutes):
    base = t.replace(second=0, microsecond=0)
    m = round((base.minute + t.second / 60) / minutes) * minutes
    return base.replace(minute=0) + dt.timedelta(minutes=m)


def money(v):
    return f"{v:,.2f}"


def money0(v):
    return f"${v / 1e6:,.3f}M" if abs(v) >= 1e6 else f"${v:,.0f}"


def signed0(v):
    return f"+${v:,.0f}" if v >= 0 else f"{MINUS}${abs(v):,.0f}"


def signed(v):
    return f"+{v:,.2f}" if v >= 0 else f"{MINUS}{abs(v):,.2f}"


def num(state):
    try:
        v = float(state)
        return None if math.isnan(v) else v
    except (TypeError, ValueError):
        return None


def short_place(loc):
    l = loc.lower()
    for key, name in (("teams", "Teams"), ("zoom", "Zoom"), ("meet.google", "Meet"),
                      ("google meet", "Meet"), ("webex", "Webex")):
        if key in l:
            return name
    return loc.split(",")[0].split(";")[0].strip()[:28]


# ---------------------------------------------------------------- weather
def wmo_icon(code, day=True):
    if code is None:
        return "cloud"
    if code == 0:
        return "sun" if day else "moon"
    if code in (1, 2):
        return "cloud-sun" if day else "cloud-moon"
    if code in (45, 48):
        return "cloud-fog"
    if 51 <= code <= 57:
        return "cloud-drizzle"
    if code in (65, 67, 82):
        return "cloud-rain-wind"
    if 61 <= code <= 67 or 80 <= code <= 82:
        return "cloud-rain"
    if 71 <= code <= 77 or code in (85, 86):
        return "cloud-snow"
    if code == 95:
        return "cloud-lightning"
    if code in (96, 99):
        return "cloud-hail"
    return "cloud"


def aqi_level(v):
    return "poor" if v > 100 else "fair" if v > 50 else ""


def uv_level(v):
    return "poor" if v >= 8 else "fair" if v >= 6 else ""


def build_weather(w, now, tz, tomorrow):
    f = w["forecast"]
    h, d = f["hourly"], f["daily"]
    times = [dt.datetime.fromisoformat(x).replace(tzinfo=tz) for x in h["time"]]
    hour_now = now.replace(minute=0, second=0, microsecond=0)
    i0 = next((i for i, t in enumerate(times) if t >= hour_now), 0)
    wind_unit = "mph" if w.get("imperial") else "km/h"

    def temp(v):
        return f"{round(v)}°"

    if not tomorrow:
        cur = f["current"]
        rain = max([x for x in h["precipitation_probability"][i0:i0 + 12] if x is not None] or [0])
        day = bool(cur.get("is_day", 1))
        out = {
            "label": "Weather", "icon": wmo_icon(cur["weather_code"], day),
            "temp": temp(cur["temperature_2m"]),
            "hilo": f"High {temp(d['temperature_2m_max'][0])} · Low {temp(d['temperature_2m_min'][0])}",
            "rain": f"Rain {round(rain)}%", "wind": f"Wind {round(cur['wind_speed_10m'])} {wind_unit}",
        }
        uv = h["uv_index"][i0] if day and i0 < len(h["uv_index"]) else None
        uv = round(uv) if uv is not None else None
        first = (now + dt.timedelta(minutes=30)).replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=1)
        slots = [first + dt.timedelta(hours=2 * k) for k in range(4)]
    else:
        out = {
            "label": "Tomorrow", "icon": wmo_icon(d["weather_code"][1]),
            "temp": temp(d["temperature_2m_max"][1]),
            "hilo": f"High {temp(d['temperature_2m_max'][1])} · Low {temp(d['temperature_2m_min'][1])}",
            "rain": f"Rain {round(d['precipitation_probability_max'][1] or 0)}%", "wind": "",
        }
        uv = d["uv_index_max"][1] if d.get("uv_index_max") else None
        uv = round(uv) if uv is not None else None
        base = (now + dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        slots = [base + dt.timedelta(hours=x) for x in (9, 12, 15, 18)]

    aq = (w.get("aq") or {}).get("current", {}).get("us_aqi")
    out["aqi"] = {"v": f"AQI {round(aq)}", "lvl": aqi_level(aq)} if aq is not None and not tomorrow else None
    out["uv"] = {"v": f"UV {uv}", "lvl": uv_level(uv)} if uv is not None else None
    hours = []
    for s in slots:
        try:
            k = times.index(s)
            hours.append({"t": hour_short(s), "v": temp(h["temperature_2m"][k])})
        except (ValueError, TypeError):
            pass
    out["hours"] = hours
    return out


# ---------------------------------------------------------------- calendar
def build_today(cal, now, tomorrow, max_later):
    day = (now + dt.timedelta(days=1)) if tomorrow else now
    out = {"day": day.strftime("%A"), "date": f"{day.strftime('%B')} {day.day}",
           "next": None, "later": [], "note": None}
    if cal is None:
        out["note"] = "Calendar loading"
        return out
    if not cal.get("calendars"):
        out["note"] = "No calendar connected"
        return out
    timed = []
    for e in cal["events"]:
        if e["all_day"]:
            continue
        timed.append(dict(e, s=dt.datetime.fromisoformat(e["start"]).astimezone(now.tzinfo),
                          e=dt.datetime.fromisoformat(e["end"]).astimezone(now.tzinfo)))
    if tomorrow:
        start_of = day.replace(hour=0, minute=0, second=0, microsecond=0)
        upcoming = [e for e in timed if e["s"] >= start_of]
    else:
        upcoming = [e for e in timed if e["s"] > now]
    nxt = upcoming[0] if upcoming and upcoming[0]["s"].date() == day.date() else None
    rest = upcoming[1:] if nxt else upcoming
    if nxt:
        place = short_place(nxt["location"]) if nxt["location"] else ""
        out["next"] = {
            "label": f"{'First' if tomorrow else 'Next'} · {t12(nxt['s'])}",
            "title": nxt["title"],
            "meta": t_range(nxt["s"], nxt["e"]) + (f" · {place}" if place else ""),
        }
    else:
        out["note"] = "Nothing else today" if not tomorrow else "Nothing scheduled"
    for e in rest[:max_later]:
        when = t12(e["s"]) if e["s"].date() == day.date() else f"{e['s'].strftime('%a')} {t12(e['s'], False)}"
        out["later"].append({"when": when, "title": e["title"]})
    return out


# ---------------------------------------------------------------- markets
def build_markets(mk, opts, now, sticky, hold=None):
    quotes = (mk or {}).get("quotes", {})
    now_ts = now.timestamp()
    status = "Closed"
    for q in quotes.values():
        o, c = q.get("open_ts"), q.get("close_ts")
        if o and c:
            if o <= now_ts < c:
                status = f"Open · closes {t12(dt.datetime.fromtimestamp(c, now.tzinfo)).replace(':00', '')}"
            elif now_ts < o and dt.datetime.fromtimestamp(o, now.tzinfo).date() == now.date():
                status = f"Opens {t12(dt.datetime.fromtimestamp(o, now.tzinfo))}"
            break
    if not quotes:
        status = "Quotes unavailable"
    rows = []
    for s in opts.get("stocks", []):
        sym = s["symbol"].upper()
        q = quotes.get(sym)
        name = s.get("name") or ""
        index = sym.startswith("^")
        row = {"sym": sym, "label": (name or sym.lstrip("^")) if index else sym, "name": "" if index else name,
               "price": DASH, "pct": "", "chg": "", "up": True, "series": [], "base": None, "slots": 0}
        if q and q.get("prev"):
            pct = (q["price"] - q["prev"]) / q["prev"] * 100
            slots = len(q["series"])
            if q.get("open_ts") and q.get("close_ts") and q["open_ts"] <= now_ts < q["close_ts"]:
                slots = max(slots, int((q["close_ts"] - q["open_ts"]) / 300))
            val = {"price": money(q["price"]), "pct": f"{abs(pct):.2f}%", "chg": signed(q["price"] - q["prev"]),
                   "up": pct >= 0, "series": [None if x is None else round(x, 2) for x in q["series"]], "base": round(q["prev"], 2),
                   "slots": slots, "p": round(pct, 2)}
            row.update({k: v for k, v in val.items() if k != "p"})
        rows.append(row)
    if hold and hold.get("configured") and hold.get("net_value") is not None:
        pf = (mk or {}).get("portfolio")
        row = {"sym": "PORTFOLIO", "label": "PORTFOLIO", "name": "", "pf": True, "price": money0(hold["net_value"]),
               "pct": "", "chg": "", "up": True, "series": [], "base": None, "slots": 0}
        if pf:
            pct = pf["change"] / pf["prev"] * 100 if pf["prev"] else 0.0
            slots = len(pf["series"])
            if pf["open_ts"] <= now_ts < pf["close_ts"]:
                slots = max(slots, int((pf["close_ts"] - pf["open_ts"]) / 300))
            row.update({"price": money0(pf["total"]), "pct": f"{abs(pct):.2f}%", "chg": signed0(pf["change"]),
                        "up": pf["change"] >= 0, "base": round(pf["prev"], 0), "slots": slots,
                        "series": [None if x is None else round(x, 0) for x in pf["series"]]})
        rows.append(row)
    if not rows:
        return None
    return {"status": status, "rows": rows}


# ---------------------------------------------------------------- home
def _state(states, eid):
    s = states.get(eid) if eid else None
    if not s or s.get("state") in (None, "unknown", "unavailable"):
        return None
    return s


def voc_level(v):
    return "poor" if v >= 2000 else "fair" if v >= 250 else ""


def hum_level(v):
    return "poor" if v < 25 or v > 70 else "fair" if v < 30 or v > 60 else ""


def co2_level(v):                       # Airthings bands: good < 800 ppm, fair < 1000, poor above
    return "poor" if v >= 1000 else "fair" if v >= 800 else ""


INDOOR = (("temperature", "Temp", 1.0, lambda v: f"{round(v)}°", None),
          ("humidity", "Hum.", 3.0, lambda v: f"{round(v)}%", hum_level),
          ("voc", "VOC", 25.0, lambda v: f"{round(v)}", voc_level),
          ("co2", "CO2", 50.0, lambda v: f"{round(v)}", co2_level))


def build_indoor(states, disc, now, sticky):
    found = (disc.get("rooms") or [])[:3]
    cols = [c for c in INDOOR if any(r.get(c[0]) for r in found)]     # no column nobody can fill
    rooms = []
    for room in found:
        cells = []
        for key, label, step, fmt, lvl in cols:
            if not room.get(key):
                cells.append({"k": label, "v": "", "lvl": "", "none": True})
                continue
            s = _state(states, room[key])
            v = num(s["state"]) if s else None
            if v is None:
                cells.append({"k": label, "v": DASH, "lvl": ""})
                continue
            level = lvl(v) if lvl else ""
            v = sticky.get(f"indoor:{room['name']}:{key}", v, now, level=level,
                           changed=lambda a, b, st=step: abs(a - b) >= st)
            cells.append({"k": label, "v": fmt(v), "lvl": lvl(v) if lvl else ""})
        if any(not c.get("none") for c in cells):
            rooms.append({"name": room["name"], "cells": cells})
    if not rooms:
        return None
    return {"area": rooms[0]["name"] if len(rooms) == 1 else "", "cols": [c[1] for c in cols], "rooms": rooms}


def build_packages(states, disc, dl=None, now=None):
    """One line at the bottom of Today: Nest package alerts (from the phone) and/or Mail and Packages."""
    p = disc.get("packages") or {}
    a = int(num((_state(states, p.get("arriving")) or {}).get("state")) or 0)
    d = int(num((_state(states, p.get("delivered")) or {}).get("state")) or 0)
    nest = None
    if dl and dl.get("configured") and now is not None and dl.get("day") == now.date().isoformat():
        ds = [dt.datetime.fromisoformat(x).astimezone(now.tzinfo) for x in dl.get("deliveries") or []]
        ps = [dt.datetime.fromisoformat(x).astimezone(now.tzinfo) for x in dl.get("pickups") or []]
        if ps and (not ds or ps[-1] > ds[-1]):
            nest = ("picked up", t12(ps[-1]), len(ds))
        elif ds:
            nest = ("delivered", t12(ds[-1]), len(ds))
    if nest:
        what, when, n = nest
        if a:
            return f"{a} arriving · {what} {when}"
        if what == "picked up":
            return f"Package picked up {when}"
        return f"Package delivered {when}" if n == 1 else f"{n} packages delivered · last {when}"
    if a and d:
        return f"{a} arriving · {d} delivered"
    if a:
        return f"{a} package{'s' if a > 1 else ''} arriving today"
    if d:
        return f"{d} package{'s' if d > 1 else ''} delivered"
    return None


def build_alerts(states, opts, disc=None, now=None):
    out, handled = [], set()
    for a in opts.get("alerts", []):
        handled.add(a.get("entity", ""))
        s = states.get(a.get("entity", ""))
        if s and str(s.get("state", "")).lower() == str(a.get("state", "")).lower():
            out.append(a.get("text") or a["entity"])
    # garage doors left open (no setup needed): after N minutes, with the time it opened
    if opts.get("garage_alerts", True) and now is not None:
        wait = dt.timedelta(minutes=int(opts.get("garage_alert_minutes", 10)))
        for eid in (disc or {}).get("garages", []):
            s = states.get(eid)
            if eid in handled or not s or s.get("state") != "open":
                continue
            since = dt.datetime.fromisoformat(s["last_changed"]).astimezone(now.tzinfo) if s.get("last_changed") else now
            if now - since < wait:
                continue
            name = s.get("attributes", {}).get("friendly_name") or "Garage door"
            if "garage" not in name.lower() and "gate" not in name.lower():
                name += " (garage)"
            day = "" if since.date() == now.date() else f"{since.strftime('%a')} "
            out.append(f"{name} open since {day}{t12(since)}")
    return out


def build_health(states, disc, opts, now, sticky):
    hd = disc.get("health") or {}
    steps = num((_state(states, hd.get("steps")) or {}).get("state"))
    sleep = num((_state(states, hd.get("sleep")) or {}).get("state"))
    rhr = num((_state(states, hd.get("rhr")) or {}).get("state"))
    if steps is None and sleep is None and rhr is None:
        return None
    out = {"steps": None, "pct": 0, "sleep": None, "rhr": None}
    if steps is not None:
        steps = sticky.get("health:steps", int(steps), now, changed=lambda a, b: abs(a - b) >= 500 or b < a)
        goal = max(1, int(opts.get("steps_goal") or 8000))
        out["steps"] = f"{steps:,}"
        out["pct"] = min(100, round(steps / goal * 100))
    if sleep is not None:
        unit = (_state(states, hd.get("sleep")) or {}).get("attributes", {}).get("unit_of_measurement", "min")
        mins = int(round(sleep * 60 if unit == "h" else sleep))
        out["sleep"] = f"{mins // 60}h {mins % 60:02d}m"
    if rhr is not None:
        out["rhr"] = f"{round(rhr)}"
    return out


def idle_hours(opts):
    try:
        return max(1, int(opts.get("hide_idle_hours", 3)))
    except (TypeError, ValueError):
        return 3


def build_printers(states, disc, now, opts):
    """Only printers that are doing something: printing, paused, or finished/failed within the idle window."""
    printers = disc.get("printers") or []
    cards, alerts = [], []
    for pr in printers:
        card, alert = build_printer(states, pr["id"], now, idle_hours(opts), pr.get("temps"))
        label = pr["name"] if len(printers) > 1 else "3D printer"
        if alert:
            alerts.append(f"{pr['name']} {alert[0].lower()}{alert[1:]}" if len(printers) > 1 else alert)
        if card:
            card["label"] = label
            cards.append(card)
    cards.sort(key=lambda c: c["pct"] is None)          # running ones first
    return cards, alerts


def printer_temps(states, p, known=None):
    """Nozzle / bed / chamber temperatures of one printer, in °C (the 3D-printing convention,
    whatever unit HA shows). `known` = {kind: [entity ids]} found on the printer's device;
    without it, sensors sharing the printer's prefix are used. Targets are skipped and with
    several nozzles (H2 series) the hottest one is the one printing. Nozzle and bed are held
    to 5° steps so small wiggles don't count as changes."""
    if known:
        cands = [(eid, kind) for kind, eids in known.items() for eid in eids]
    else:
        pre = "sensor." + p.split(".", 1)[-1] + "_"
        cands = [(eid, sources.temp_kind(eid)) for eid in states
                 if eid.startswith(pre) and eid.endswith("temperature")]
    hot = {}
    for eid, kind in cands:
        st = states.get(eid)
        v = num(st.get("state")) if kind and st else None
        if v is None:
            continue
        if str((st.get("attributes") or {}).get("unit_of_measurement", "")).upper().endswith("F"):
            v = (v - 32) * 5 / 9
        hot[kind] = max(hot.get(kind, v), v)
    out = []
    for kind in ("Nozzle", "Bed", "Chamber"):
        if kind in hot:
            v = round(hot[kind] / 5) * 5 if kind != "Chamber" else round(hot[kind])
            out.append({"k": kind, "v": f"{int(v)}°"})
    return out


def build_printer(states, p, now, idle_h=3, temps=None):
    st = _state(states, p + "_print_status")
    raw = (st or {}).get("state", "offline").lower()
    phase = {"running": "printing", "prepare": "preparing", "slicing": "preparing", "init": "preparing",
             "pause": "paused", "finish": "finished", "failed": "failed"}.get(raw, "idle")
    if phase in ("finished", "failed"):
        changed = dt.datetime.fromisoformat(st["last_changed"]).astimezone(now.tzinfo) if st.get("last_changed") else now
        if now - changed > dt.timedelta(hours=idle_h):
            phase = "idle"
    if phase == "idle":
        return None, None
    job = ""
    for suffix in ("_task_name", "_subtask_name", "_gcode_filename"):
        s = _state(states, p + suffix)
        if s and str(s.get("state", "")).strip() not in ("", "unknown", "unavailable"):
            job = s["state"].strip()
            # a file path only for the gcode file name; job titles can have slashes ("X1/P1/S, P2S, A1")
            if suffix == "_gcode_filename" or job.startswith("/"):
                job = job.rsplit("/", 1)[-1]
            for ext in (".gcode.3mf", ".3mf", ".gcode"):
                if job.lower().endswith(ext):
                    job = job[: -len(ext)]
            break
    out = {"phase": phase, "job": job, "pct": None, "pct_text": "", "line": "", "end": "", "temps": []}
    alert = f"Print failed{': ' + job if job else ''}" if phase == "failed" else None
    if phase in ("printing", "paused", "preparing"):
        out["temps"] = printer_temps(states, p, temps)
        prog = num((_state(states, p + "_print_progress") or {}).get("state")) or 0
        step = int(prog // 10 * 10)
        out["pct"], out["pct_text"] = step, f"{step}%"
        end = None
        e = _state(states, p + "_end_time")
        if e:
            try:
                end = dt.datetime.fromisoformat(e["state"]).astimezone(now.tzinfo)
            except ValueError:
                end = None
        if end is None:
            r = _state(states, p + "_remaining_time")
            mins = num(r["state"]) if r else None
            if mins is not None:
                unit = r.get("attributes", {}).get("unit_of_measurement", "min")
                mins = mins * 60 if unit == "h" else mins / 60 if unit == "s" else mins
                end = now + dt.timedelta(minutes=mins)
        if phase == "preparing":
            out["line"] = "Starting"
        elif phase == "paused":
            out["line"] = "Paused"
        elif end:
            day = "" if end.date() == now.date() else f"{end.strftime('%a')} "
            out["line"] = f"Done at {day}{t12(end)}"
            out["end"] = f"{day}{t12(end)}"                 # for the one-line version
    elif phase == "finished":
        out["line"] = "Finished"
    elif phase == "failed":
        out["line"] = "Failed"
    return out, alert


# ---------------------------------------------------------------- Claude (from the Clawdmeter daemon)
def use_level(v):
    return "poor" if v >= 90 else "fair" if v >= 75 else ""


def claude_hidden(rec, now, opts):
    """Why a plan is off the screen right now (None = shown)."""
    if now.timestamp() - rec["at"] > 6 * 3600:
        return "no update from the daemon in 6 h"           # daemon stopped long ago: hide rather than mislead
    used = rec.get("used", rec["at"])
    if now.timestamp() - used > idle_hours(opts) * 3600:
        return f"idle since {t12(dt.datetime.fromtimestamp(used, now.tzinfo))}"
    return None


def build_claude(cl, now, opts):
    plans = []
    for key in ("max", "enterprise"):
        rec = (cl or {}).get(key)
        if not rec or claude_hidden(rec, now, opts):
            continue
        age = now.timestamp() - rec["at"]
        p, at = rec["p"], dt.datetime.fromtimestamp(rec["at"], now.tzinfo)
        s = int(round(float(p.get("s") or 0)))
        if key == "max":
            w = int(round(float(p.get("w") or 0)))
            sub = ""
            if p.get("sr"):
                sub = "resets " + t12(round_to(at + dt.timedelta(minutes=float(p["sr"])), 5))
            bars = [{"k": "Session", "v": s, "lvl": use_level(s), "tick": None},
                    {"k": "Week", "v": w, "lvl": use_level(w), "tick": None}]
            name = opts.get("claude_personal_name") or "Max"
        else:
            tp = p.get("tp")
            sub = f"resets {p['rd']}" if p.get("rd") else ""
            bars = [{"k": "Budget", "v": s, "lvl": use_level(s),
                     "tick": max(0, min(100, int(tp))) if tp not in (None, "") else None}]
            name = "Enterprise"
        if age > 20 * 60:
            sub = f"as of {t12(at)}"
        plans.append({"name": name, "sub": sub, "bars": bars})
    return {"plans": plans} if plans else None


# ---------------------------------------------------------------- the whole screen
def _safe(name, fn, *args, default=None):
    """One broken widget (odd data from an API) shouldn't take the whole screen down."""
    try:
        return fn(*args)
    except Exception:
        log.exception("widget %s failed", name)
        return default


# The You column: Health always, then whatever is active. Heights (px) are measured from the
# template so a busy afternoon (two prints + both Claude plans) never runs off the bottom.
YOU_ROOM = 436                  # 480 - top/bottom padding
YOU_GAP = 28                    # rule between widgets (14 margin + 2 border + 12 padding)
YOU_H = {"health": 108, "health_steps": 66, "printer": 98, "printer_nojob": 72, "printer_done": 50,
         "printer_compact": 46, "printer_temps": 24, "claude_head": 19, "claude_plan": 29, "claude_bar": 25}


def _height(c):
    k = c["kind"]
    if k == "health":
        return YOU_H["health"] if (c.get("sleep") or c.get("rhr")) and c.get("steps") else YOU_H["health_steps"]
    if k == "printer":
        if c.get("compact"):
            return YOU_H["printer_compact"]
        if c["pct"] is None:
            return YOU_H["printer_done"]
        extra = YOU_H["printer_temps"] if c.get("temps") and not c.get("notemps") else 0
        return (YOU_H["printer"] if c.get("job") else YOU_H["printer_nojob"]) + extra
    if k == "claude":
        return YOU_H["claude_head"] + sum(YOU_H["claude_plan"] + YOU_H["claude_bar"] * len(p["bars"])
                                          for p in c["plans"])
    return 60


def fit_you(cards):
    def total():
        return sum(_height(c) for c in cards) + YOU_GAP * max(0, len(cards) - 1)
    if total() > YOU_ROOM:                       # first the printer temperatures go
        for c in cards:
            if c["kind"] == "printer":
                c["notemps"] = True
    if total() > YOU_ROOM:                       # then squeeze the printers to one line + bar
        for c in cards:
            if c["kind"] == "printer":
                c["compact"] = True
    while len(cards) > 1 and total() > YOU_ROOM:  # then drop from the bottom (lowest priority)
        cards.pop()
    return cards


def build(now, data, disc, opts, sticky, tomorrow):
    states = data.get("states") or {}
    alerts = _safe("alerts", build_alerts, states, opts, disc, now, default=[])
    printers, p_alerts = _safe("printers", build_printers, states, disc, now, opts, default=([], []))
    alerts += p_alerts
    packages = None if tomorrow else _safe("packages", build_packages, states, disc,   # deliveries are about today
                                           data.get("deliveries"), now)
    bottom = (1 if packages else 0) + len(alerts[:2])      # lines pinned to the bottom of Today
    max_later = 3 if bottom == 0 else 2 if bottom <= 2 else 1
    vm = {
        "today": _safe("today", build_today, data.get("calendar"), now, tomorrow, max_later)
                 or build_today(None, now, tomorrow, 0),
        "packages": packages,
        "alerts": alerts[:2],
        "weather": _safe("weather", build_weather, data["weather"], now, now.tzinfo, tomorrow)
                   if data.get("weather") else None,
        "indoor": _safe("indoor", build_indoor, states, disc, now, sticky),
        "markets": _safe("markets", build_markets, data.get("markets"), opts, now, sticky, data.get("portfolio")),
    }
    health = _safe("health", build_health, states, disc, opts, now, sticky)
    claude = _safe("claude", build_claude, data.get("claude"), now, opts)
    you = ([{"kind": "health", **health}] if health else []) + [{"kind": "printer", **p} for p in printers] \
        + ([{"kind": "claude", **claude}] if claude else [])
    vm["you"] = fit_you(you)
    vm["claude"] = claude                      # (kept for the status page and tests)
    return vm
