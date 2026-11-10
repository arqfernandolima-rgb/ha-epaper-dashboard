"""What the kitchen display shows (black and white, 800x480).

Page 1, the family's week: today's weather with what to wear, four rolling days plus the
weekend stacked in one column, who's away (all-day events as bars), trash and recycling,
birthdays, holidays and family events from the calendars. After 6 PM it's about tomorrow.
Exceptions (garage open, a package waiting, a room too hot or cold, the car low) sit at the bottom.

Page 2, the house: rooms, lights on, garage doors, the car, the shopping list.
"""
import datetime as dt
import math
import re

import view
from view import num, t12, wmo_icon

DAYS = 5                      # columns on page 1 (the weekend shares one)
EVENING_HOUR = 18             # after this, page 1 is about tomorrow
CAR_LOW = 25
# Room in a day column, in px (measured in Chromium from the template): under the day's header and
# weather there are 211 px for the chips row, the away bars and the events (rows shared by all days);
# a text line, the gap between events, a chip, an away bar; characters per line.
COL_PX, EV_PAD, LINE_PX, GAP_PX, CHIP_PX, BAR_PX = 220, 10, 19.5, 4, 23, 32
ALERT_CHIP_PX = 29            # the black "Out tonight" chip has padding
WEEKEND_PX = 237              # both halves of the weekend column, under their headers
CHARS = {"today": (21, 18), "day": (16, 13)}      # characters per line (no badge, with a badge)

# Words that make a timed event a family one (work meetings stay off the kitchen wall).
FAMILY = re.compile(r"\b(school|escola|bus|field ?trip|no school|early dismissal|half day|birthday|bday|"
                    r"anivers[aá]rio|dentist|dentista|doctor|m[eé]dico|pediatric\w*|orthodont\w*|recital|concert|"
                    r"practice|treino|game|jogo|tournament|cheer|band|chorus|choir|piano|soccer|futebol|"
                    r"basketball|swim\w*|dance|ballet|karate|scouts?|playdate|sleepover|babysitter|sitter|"
                    r"faxineira|cleaners?|cleaning|haircut|vet|buscar|levar)\b", re.I)
BDAY = re.compile(r"\b(birthday|bday|anivers[aá]rio)\b", re.I)
NO_SCHOOL = re.compile(r"\b(no school|early dismissal|half day|school closed|sem aula)\b", re.I)
CONNECT = re.compile(r"^\s*(?:(?:na|no|nas|nos|em|para|pra|in|at|to|for)\b|[-–—:|·])\s*", re.I)

WMO_TEXT = ((0, "Clear"), (2, "Partly cloudy"), (3, "Cloudy"), (48, "Fog"), (57, "Drizzle"), (67, "Rain"),
            (77, "Snow"), (82, "Showers"), (86, "Snow showers"), (99, "Storms"))


def wmo_text(code, day=True):
    if code is None:
        return ""
    if code == 0:
        return "Sunny" if day else "Clear"
    for top, text in WMO_TEXT:
        if code <= top:
            return text
    return ""


def snowy(code):
    return code is not None and (71 <= code <= 77 or code in (85, 86))


def hshort(d):
    """8a, 3p, 12p"""
    return f"{d.hour % 12 or 12}{'a' if d.hour < 12 else 'p'}"


def tshort(d):
    """8:15a, 3p"""
    h = d.hour % 12 or 12
    return (f"{h}:{d.minute:02d}" if d.minute else f"{h}") + ("a" if d.hour < 12 else "p")


# ---------------------------------------------------------------- people
class People:
    """'F=Fernando,Fer; M=Mi,Milena; T=Tom; E=Mimi,Emilia' -> initials found in event titles.
    The first person owns the calendars: their all-day events with no other name are theirs."""

    def __init__(self, spec):
        self.people = []
        for part in re.split(r"[;\n]", spec or ""):
            if "=" not in part:
                continue
            k, names = part.split("=", 1)
            k = k.strip()[:2].upper()
            ns = [n.strip() for n in names.split(",") if n.strip()]
            if k and ns:
                self.people.append((k, ns))
        self.owner = self.people[0][0] if self.people else ""
        pats = sorted(((k, n) for k, ns in self.people for n in ns), key=lambda x: -len(x[1]))
        # short names (Mi) only match with their capital letter; longer ones in any case
        self.pats = [(k, re.compile(r"(?<!\w)" + re.escape(n) + r"(?!\w)", 0 if len(n) <= 2 else re.I))
                     for k, n in pats]

    def find(self, title, skip_owner=False):
        for k, rx in self.pats:
            if skip_owner and k == self.owner:
                continue
            m = rx.search(title)
            if m:
                return k, m
        return "", None


def strip_name(title, m):
    """'Mi na Florida' -> 'Florida', 'Buscar Mimi Art Class' -> 'Buscar Art Class'."""
    t = (title[:m.start()] + " " + title[m.end():]).strip()
    t = CONNECT.sub("", t)
    t = re.sub(r"\s{2,}", " ", t).strip(" -–—:|·,")
    return (t[:1].upper() + t[1:]) if t else title


def short_bday(title):
    """'Claudio Jose Pinto's Birthday' -> 'Claudio' (the gift icon says it's a birthday)."""
    name = BDAY.sub("", title).replace("'s", "").replace("’s", "").strip(" -–:'")
    return name.split()[0] if name.split() else "Birthday"


def cal_kind(eid):
    e = eid.lower()
    if "trash" in e or "garbage" in e or "lixo" in e:
        return "trash"
    if "recycl" in e or "reciclagem" in e:
        return "recycle"
    if "holiday" in e or "feriado" in e:
        return "holiday"
    if "birthday" in e or "anivers" in e:
        return "bday"
    return ""


def classify(e, people):
    """One calendar event -> what it is on the kitchen wall, or None to leave it off."""
    title, kind = e["title"], cal_kind(e.get("calendar", ""))
    if kind == "trash" or (e["all_day"] and re.search(r"\b(trash|garbage)\b", title, re.I)):
        return {"kind": "trash"}
    if kind == "recycle" or (e["all_day"] and re.search(r"\brecycl", title, re.I)):
        return {"kind": "recycle"}
    if kind == "holiday":
        return {"kind": "chip", "icon": "flag", "text": title}
    if kind == "bday" or (e["all_day"] and BDAY.search(title)):
        return {"kind": "bday", "text": short_bday(title)}
    if NO_SCHOOL.search(title):
        return {"kind": "chip", "icon": "school", "text": "No school" if e["all_day"] else title}
    if e["all_day"]:
        who, m = people.find(title)
        if e.get("recurring"):                       # e.g. the cleaner every Friday
            text = strip_name(title, m) if m else title
            return {"kind": "chip", "icon": "house", "text": text[:1].upper() + text[1:]}
        return {"kind": "away", "who": who or people.owner, "text": strip_name(title, m) if m else title}
    who, m = people.find(title, skip_owner=True)     # the owner's calendar is full of their own work
    if m:
        return {"kind": "event", "who": who, "text": strip_name(title, m)}
    if FAMILY.search(title):
        return {"kind": "event", "who": "", "text": title}
    return None


# ---------------------------------------------------------------- the five columns
def week_slots(today):
    """Today and the next days, Saturday and Sunday sharing a column; the coming weekend always shows
    (on a Monday it takes Friday's place)."""
    out, d = [], today
    while len(out) < DAYS:
        if d.weekday() == 5:
            out.append(("we", d, d + dt.timedelta(days=1)))
            d += dt.timedelta(days=2)
        else:
            out.append(("day", d))
            d += dt.timedelta(days=1)
    if today.weekday() != 6 and not any(s[0] == "we" for s in out):
        sat = today + dt.timedelta(days=5 - today.weekday())
        out[-1] = ("we", sat, sat + dt.timedelta(days=1))
    return out


def slot_of(slots, d):
    for i, s in enumerate(slots):
        if d in s[1:]:
            return i
    return None


# ---------------------------------------------------------------- weather
def _f(v, imperial):
    """Temperature in °F (the clothing rules are written in °F)."""
    if v is None:
        return None
    return v if imperial else v * 9 / 5 + 32


def wear(feels_f):
    if feels_f is None:
        return None
    if feels_f <= 25:
        return "Hats and mittens"
    if feels_f <= 38:
        return "Winter coat"
    if feels_f <= 50:
        return "Coat"
    if feels_f <= 62:
        return "Jacket"
    if feels_f <= 70:
        return "Sweater"
    if feels_f >= 88:
        return "Hot, bring water"
    return None


class Weather:
    def __init__(self, w, tz):
        f = (w or {}).get("forecast") or {}
        self.ok = bool(f.get("daily"))
        self.imperial = bool((w or {}).get("imperial", True))
        self.cur = f.get("current") or {}
        h = f.get("hourly") or {}
        self.h = h
        self.times = [dt.datetime.fromisoformat(x).replace(tzinfo=tz) for x in h.get("time", [])]
        d = f.get("daily") or {}
        self.days = {}
        for i, x in enumerate(d.get("time", [])):
            self.days[dt.date.fromisoformat(x)] = {
                "code": d["weather_code"][i], "hi": d["temperature_2m_max"][i], "lo": d["temperature_2m_min"][i],
                "rain": (d.get("precipitation_probability_max") or [None] * (i + 1))[i]}

    def day(self, d):
        return self.days.get(d)

    def hour(self, t):
        try:
            k = self.times.index(t)
        except ValueError:
            return None
        g = lambda key: (self.h.get(key) or [None] * (k + 1))[k]     # noqa: E731
        return {"t": t, "temp": g("temperature_2m"), "feels": g("apparent_temperature") or g("temperature_2m"),
                "rain": g("precipitation_probability") or 0, "code": g("weather_code"), "day": g("is_day")}

    def hours(self, start, end):
        return [x for x in (self.hour(t) for t in self.times if start <= t < end) if x]


ADVICE_CHARS = 34             # one line under the date


def advice(wx, now, evening, tonight):
    """One line: what goes out tonight, what to wear, when to take an umbrella.
    Mornings look at the rest of today; after 6 PM at tomorrow 7 AM to 6 PM."""
    if evening:
        start = dt.datetime.combine((now + dt.timedelta(days=1)).date(), dt.time(7), now.tzinfo)
        hrs = wx.hours(start, start + dt.timedelta(hours=11))
    else:
        start = now.replace(minute=0, second=0, microsecond=0)
        end = now.replace(hour=19, minute=0, second=0, microsecond=0)
        hrs = wx.hours(start, end) if start < end else []
    wearing = []
    if hrs:
        feels = [_f(x["feels"], wx.imperial) for x in hrs if x["feels"] is not None]
        morning = [_f(x["feels"], wx.imperial) for x in hrs if x["t"].hour < 10 and x["feels"] is not None]
        coat = wear(min(feels)) if feels else None
        if coat:
            warms_up = morning and max(feels) - min(morning) >= 15
            wearing.append(("shirt", coat + (" this morning" if warms_up and not evening and now.hour < 11 else "")))
        wet = [x for x in hrs if (x["rain"] or 0) >= 50]
        if any(snowy(x["code"]) for x in hrs):
            wearing.append(("snowflake", "Snow boots"))
        elif wet:
            first = wet[0]["t"]
            later = first > start + dt.timedelta(hours=1)
            wearing.append(("umbrella", "Umbrella" + (f" after {t12(first)}" if later else "")))
    if not evening and len(wearing) < 2:            # nothing much today: the next notable day
        for k in range(1, 5):
            d = (now + dt.timedelta(days=k)).date()
            x = wx.day(d)
            if not x:
                continue
            name = "tomorrow" if k == 1 else d.strftime("%A")
            if snowy(x["code"]):
                wearing.append(("snowflake", f"Snow {name}"))
                break
            if (x["rain"] or 0) >= 60:
                wearing.append(("umbrella", f"Rain {name}"))
                break
    if evening and tonight:
        parts, lead = [("trash-2", f"{tonight[0]} tonight")] + wearing, ""
    elif evening:
        parts, lead = wearing, "Tomorrow: "
    else:
        parts, lead = wearing, ""
    if not parts:
        return None
    text = ""
    for _, p in parts:
        piece = p if not text and not lead else p[0].lower() + p[1:]
        cand = piece if not text else f"{text} · {piece}"
        if text and len(lead) + len(cand) > ADVICE_CHARS:
            break
        text = cand
    return {"icon": parts[0][0], "text": lead + text}


def hero(wx, now, evening):
    if not wx.ok:
        return None
    if evening:
        day = (now + dt.timedelta(days=1)).date()
        d = wx.day(day) or {}
        text = wmo_text(d.get("code"))
        lo = d.get("lo")
        if lo is not None:
            text += f", low {round(lo)}°"
        return {"label": "Tomorrow", "icon": wmo_icon(d.get("code")), "temp": round(d["hi"]) if d.get("hi") is not None else "–",
                "text": text}
    c = wx.cur
    day = bool(c.get("is_day", 1))
    text = wmo_text(c.get("weather_code"), day)
    hr = wx.hour(now.replace(minute=0, second=0, microsecond=0))
    if hr and hr["feels"] is not None and c.get("temperature_2m") is not None \
            and abs(hr["feels"] - c["temperature_2m"]) >= 5:
        text += f", feels {round(hr['feels'])}°"
    return {"label": "Now", "icon": wmo_icon(c.get("weather_code"), day),
            "temp": round(c["temperature_2m"]) if c.get("temperature_2m") is not None else "–", "text": text}


def hour_slots(wx, now, evening):
    if evening:
        base = dt.datetime.combine((now + dt.timedelta(days=1)).date(), dt.time(7), now.tzinfo)
        ts = [base + dt.timedelta(hours=3 * k) for k in range(4)]
    else:
        first = now.replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=1)
        ts = [first + dt.timedelta(hours=3 * k) for k in range(4)]
    out = []
    for t in ts:
        x = wx.hour(t)
        if not x or x["temp"] is None:
            continue
        out.append({"t": hshort(t), "icon": wmo_icon(x["code"], bool(x["day"] if x["day"] is not None else 1)),
                    "temp": round(x["temp"]), "rain": round(x["rain"]) if (x["rain"] or 0) >= 30 else None})
    return out


# ---------------------------------------------------------------- page 1
def _events(cal, people, tz):
    out = []
    for e in (cal or {}).get("events") or []:
        c = classify(e, people)
        if not c:
            continue
        s = dt.datetime.fromisoformat(e["start"]).astimezone(tz)
        en = dt.datetime.fromisoformat(e["end"]).astimezone(tz)
        out.append(dict(c, s=s, e=en, all_day=e["all_day"], recurring=e.get("recurring", False)))
    return out


def wrap_lines(text, width):
    """How many lines a greedy word wrap takes."""
    lines, cur = 1, 0
    for w in text.split():
        if cur == 0:
            cur = len(w)
        elif cur + 1 + len(w) <= width:
            cur += 1 + len(w)
        else:
            lines, cur = lines + 1, len(w)
    return lines


def event_px(e, width="day"):
    plain, badged = CHARS[width]
    return wrap_lines(f"{e['time']} {e['text']}", badged if e["who"] else plain) * LINE_PX + GAP_PX


def fit(events, room, width="day"):
    """Events in time order while they fit, keeping a line for '+N more' if some won't."""
    if room < LINE_PX:
        return [], 0
    used, shown = 0, []
    for i, e in enumerate(events):
        need = event_px(e, width)
        reserve = 0 if i == len(events) - 1 else LINE_PX
        if used + need - GAP_PX + reserve > room:
            return shown, len(events) - len(shown)
        shown.append(e)
        used += need
    return shown, 0


def build_week(app, now):
    data = {k: s.fresh() for k, s in app.sources.items()}
    tz, opts = now.tzinfo, app.opts
    people = People(opts.get("kitchen_people", ""))
    evening = now.hour >= int(opts.get("kitchen_evening_hour", EVENING_HOUR))
    wx = Weather(data.get("weather"), tz)
    today = now.date()
    slots = week_slots(today)
    evs = _events(data.get("calendar"), people, tz)

    # chips, bars and events per date
    per = {}                                         # date -> {"chips": [], "events": [], "trash": set()}
    def at(d):
        return per.setdefault(d, {"chips": [], "events": [], "bins": [], "seen": set()})
    bars = []
    for e in evs:
        d0 = e["s"].date()
        if e["kind"] in ("trash", "recycle"):
            if e["kind"] not in at(d0)["bins"]:
                at(d0)["bins"].append(e["kind"])
        elif e["kind"] == "bday":
            key = ("bday", e["text"].lower())
            if key not in at(d0)["seen"]:
                at(d0)["seen"].add(key)
                at(d0)["chips"].append({"icon": "gift", "text": e["text"]})
        elif e["kind"] == "chip":
            key = ("chip", e["text"].lower())
            if key not in at(d0)["seen"]:
                at(d0)["seen"].add(key)
                at(d0)["chips"].append({"icon": e["icon"], "text": e["text"]})
        elif e["kind"] == "away":
            last = (e["e"] - dt.timedelta(seconds=1)).date() if e["all_day"] else e["e"].date()
            bars.append({"who": e["who"], "text": e["text"], "first": d0, "last": max(d0, last)})
        elif e["kind"] == "event":
            if e["e"] <= now:                        # already over
                continue
            at(d0)["events"].append(e)

    def bins_text(bins):
        if not bins:
            return None
        if len(bins) == 2:
            return "Trash + recycling"
        return "Trash" if bins[0] == "trash" else "Recycling"

    tomorrow = today + dt.timedelta(days=1)
    tonight = []                                     # what goes out tonight (said in the advice line)
    if evening and bins_text(at(tomorrow)["bins"]):
        tonight.append(f"{bins_text(at(tomorrow)['bins'])} out")

    def day_items(d, near):
        """chips and events for one date; near = today or tomorrow (routine events show there only)."""
        p = per.get(d) or {"chips": [], "events": [], "bins": []}
        chips = []
        if d == today and evening and tonight:
            chips.append({"icon": "trash-2", "text": "Out tonight", "alert": True})
        else:
            for b in p["bins"]:                      # pickup day; black first thing in the morning
                chips.append({"icon": "recycle" if b == "recycle" else "trash-2",
                              "text": "Recycling" if b == "recycle" else "Trash",
                              "alert": d == today and not evening and now.hour < 9})
        chips += p["chips"]
        evlist = sorted((e for e in p["events"] if near or not e["recurring"]), key=lambda e: e["s"])
        return chips, [{"who": e["who"], "time": tshort(e["s"]), "text": e["text"]} for e in evlist]

    # bars: only across weekday columns (the weekend lists who's away instead); at most two rows of
    # them, anything more becomes a chip on its days
    vis = []
    for b in sorted(bars, key=lambda b: (b["first"], -(b["last"] - b["first"]).days)):
        idx = sorted({slot_of(slots, b["first"] + dt.timedelta(days=k))
                      for k in range((b["last"] - b["first"]).days + 1)} - {None})
        day_idx = [i for i in idx if slots[i][0] == "day"]
        if day_idx:
            vis.append({"start": day_idx[0] + 1, "span": day_idx[-1] - day_idx[0] + 1, "who": b["who"],
                        "text": b["text"], "more": b["last"] > slots[day_idx[-1]][-1], "src": b})
    vis, spill = _lanes(vis)
    for b in spill:
        for k in range(b["start"] - 1, b["start"] - 1 + b["span"]):
            at(slots[k][1])["chips"].insert(0, {"icon": "plane", "text": b["text"], "who": b["who"]})
    for b in vis:
        b.pop("src", None)
    lanes = max([b["row"] for b in vis] or [0])
    items = {s[1]: day_items(s[1], i < 2) for i, s in enumerate(slots) if s[0] == "day"}
    most = max([sum(ALERT_CHIP_PX if c.get("alert") else CHIP_PX for c in cs) for cs, _ in items.values()] or [0])
    # the chips row and the bars row are as tall as the busiest day's, in every column
    room_day = COL_PX - 6 - max(0, most - 4) - 6 - max(0, lanes * BAR_PX - 4) - EV_PAD
    cols = []
    for i, s in enumerate(slots):
        if s[0] == "day":
            d = s[1]
            w = wx.day(d) or {}
            chips, events = items[d]
            shown, more = fit(events, room_day, "today" if i == 0 else "day")
            label = ("Tonight" if evening else "Today") if d == today else d.strftime("%a")
            cols.append({"kind": "day", "today": d == today, "label": label,
                         "num": "" if (d == today and evening) else str(d.day),
                         "icon": ("moon" if d == today and evening else wmo_icon(w.get("code"))),
                         "hi": round(w["hi"]) if w.get("hi") is not None else None,
                         "lo": round(w["lo"]) if w.get("lo") is not None else None,
                         "rain": round(w["rain"]) if (w.get("rain") or 0) >= 30 else None,
                         "chips": chips, "events": shown, "more": more})
        else:
            halves = []
            for d in s[1:]:
                if d < today:
                    continue
                w = wx.day(d) or {}
                chips, events = day_items(d, d <= tomorrow)
                for b in (b for b in bars if b["first"] <= d <= b["last"]):
                    chips.insert(0, {"icon": "plane", "text": b["text"], "who": b["who"]})
                halves.append({"label": ("Today" if d == today else d.strftime("%a")), "num": str(d.day),
                               "today": d == today, "icon": wmo_icon(w.get("code")) if w else None,
                               "hi": round(w["hi"]) if w.get("hi") is not None else None,
                               "rain": round(w["rain"]) if (w.get("rain") or 0) >= 50 else None,
                               "chips": chips, "all": events})
            # share the column: the busier day gets what the quieter one doesn't need
            need = [len(h["chips"]) * CHIP_PX + sum(event_px(e) for e in h["all"]) for h in halves]
            for k, h in enumerate(halves):
                room_k = WEEKEND_PX if len(halves) == 1 else max(WEEKEND_PX / 2, WEEKEND_PX - need[1 - k])
                h["events"], h["more"] = fit(h.pop("all"), room_k - len(h["chips"]) * CHIP_PX)
            cols.append({"kind": "we", "days": halves})
    return {
        "page": "week", "evening": evening,
        "day": now.strftime("%A"), "date": f"{now.strftime('%B')} {now.day}",
        "advice": advice(wx, now, evening, tonight) if wx.ok else None,
        "hero": hero(wx, now, evening), "hours": hour_slots(wx, now, evening) if wx.ok else [],
        "cols": cols, "bars": vis, "foot": footer(app, data, now),
    }


MAX_LANES = 2


def _lanes(bars):
    """Give each away bar a row (overlapping ones stack, others share). Bars past MAX_LANES rows
    are returned separately (they become chips)."""
    rows, spill = [], []
    for b in bars:
        for n, r in enumerate(rows):
            if all(b["start"] >= x["start"] + x["span"] or x["start"] >= b["start"] + b["span"] for x in r):
                r.append(b)
                b["row"] = n + 1
                break
        else:
            if len(rows) < MAX_LANES:
                rows.append([b])
                b["row"] = len(rows)
            else:
                spill.append(b)
    return [b for r in rows for b in r], spill


# ---------------------------------------------------------------- the bottom row: exceptions
def _short_door(name):
    n = re.sub(r"\b(garage\s+door|garage|door)\b", "", name or "", flags=re.I)
    return re.sub(r"\s{2,}", " ", n).strip() or "Garage"


def footer(app, data, now):
    states = data.get("states") or {}
    disc = app.ctx.discovered or {}
    opts = app.opts
    items = []
    # garage doors
    open_doors = []
    for eid in disc.get("garages", []):
        s = states.get(eid) or {}
        if s.get("state") in ("open", "opening"):
            since = dt.datetime.fromisoformat(s["last_changed"]).astimezone(now.tzinfo) if s.get("last_changed") else now
            open_doors.append((_short_door(s.get("attributes", {}).get("friendly_name")), since))
    if open_doors:
        name, since = min(open_doors, key=lambda x: x[1])
        mins = int((now - since).total_seconds() // 60)
        dur = f"{mins} min" if mins < 90 else f"{mins // 60} h"
        more = f" +{len(open_doors) - 1}" if len(open_doors) > 1 else ""
        door = name if re.search(r"\b(door|gate)$", name, re.I) else f"{name} door"
        # black only once it's been open a while (someone may just be out there)
        late = mins >= int(opts.get("garage_alert_minutes", 10))
        items.append({"icon": "warehouse", "text": f"{door} open {dur}{more}", "alert": late})
    elif disc.get("garages"):
        items.append({"icon": "warehouse", "text": "All closed", "alert": False})
    # rooms too hot or cold
    lo, hi = float(opts.get("kitchen_temp_low", 62)), float(opts.get("kitchen_temp_high", 80))
    for room in disc.get("rooms") or []:
        s = states.get(room.get("temperature") or "") or {}
        v = num(s.get("state"))
        if v is not None and not lo <= v <= hi:
            items.append({"icon": "thermometer", "text": f"{room['name']} {round(v)}°", "alert": True})
    # the car, when it needs attention
    for v in disc.get("vehicles") or []:
        car = view._safe("vehicle", view.vehicle_info, states, v, now)
        if car and car["attention"]:
            items.append({"icon": "car", "text": f"{car['name']} {car['soc']}%"
                          + (" · plug in" if not car["plugged"] and car["soc"] < CAR_LOW else ""), "alert": True})
    # packages waiting (gone once someone picks them up)
    pk = view._safe("packages", view.build_packages, states, disc, data.get("deliveries"), now)
    if pk and "picked up" not in pk:
        pk = re.sub(r"^(\d+) packages delivered · last ", r"\1 delivered · ", pk).replace("Package delivered", "Delivered")
        items.append({"icon": "package-check", "text": pk, "alert": False})
    alerts = [x for x in items if x["alert"]]
    calm = [x for x in items if not x["alert"]]
    return (alerts + calm)[:3]


# ---------------------------------------------------------------- page 2
PRINTER_LIGHT = re.compile(r"chamber_light|heatbed_light|_printer_", re.I)


def build_house(app, now):
    data = {k: s.fresh() for k, s in app.sources.items()}
    states = data.get("states") or {}
    disc = app.ctx.discovered or {}
    wx = Weather(data.get("weather"), now.tzinfo)
    rooms = []
    for room in (disc.get("rooms") or [])[:2]:
        def val(k):
            s = states.get(room.get(k) or "") or {}
            return num(s.get("state"))
        t, h, co2, voc = val("temperature"), val("humidity"), val("co2"), val("voc")
        if t is None:
            continue
        air = []
        if co2 is not None:
            air.append({"k": "CO₂", "v": round(co2)})
        if voc is not None:
            air.append({"k": "VOC", "v": round(voc)})
        verdict, alert = "Air good", False
        if co2 is not None and co2 >= 1000:
            verdict, alert = "Open a window", True
        elif voc is not None and voc >= 2000:
            verdict, alert = "VOC high", True
        elif (co2 is not None and co2 >= 800) or (voc is not None and voc >= 250):
            verdict = "Air fair"
        rooms.append({"name": room["name"], "t": round(t), "h": round(h) if h is not None else "–",
                      "air": air, "verdict": verdict, "alert": alert})
    lights = sorted((s.get("attributes", {}).get("friendly_name") or e) for e, s in states.items()
                    if e.startswith("light.") and s.get("state") == "on" and not PRINTER_LIGHT.search(e))
    garage = []
    for eid in disc.get("garages", []):
        s = states.get(eid) or {}
        op = s.get("state") in ("open", "opening")
        dur = None
        if op and s.get("last_changed"):
            mins = int((now - dt.datetime.fromisoformat(s["last_changed"]).astimezone(now.tzinfo)).total_seconds() // 60)
            dur = f"{mins}m" if mins < 90 else f"{mins // 60}h"
        garage.append({"name": _short_door(s.get("attributes", {}).get("friendly_name")), "open": dur or ("now" if op else None)})
    car = None
    for v in disc.get("vehicles") or []:
        c = _car_detail(states, v, now)
        if c:
            car = c
            break
    out = None
    if wx.ok and wx.cur.get("temperature_2m") is not None:
        day = bool(wx.cur.get("is_day", 1))
        out = {"icon": wmo_icon(wx.cur.get("weather_code"), day), "temp": round(wx.cur["temperature_2m"]),
               "text": wmo_text(wx.cur.get("weather_code"), day).lower()}
    shop = (data.get("shopping") or {}).get("items")
    return {"page": "house", "out": out, "rooms": rooms, "lights": lights, "garage": garage, "car": car,
            "shopping": ", ".join(shop[:6]) + (f", +{len(shop) - 6}" if len(shop) > 6 else "") if shop else
            ("Nothing on the list" if shop is not None else "Shopping list not found")}


def _car_detail(states, v, now):
    c = view._safe("vehicle", view.vehicle_info, states, v, now)
    if not c:
        return None
    line = c["status"] or ("Plugged in" if c["plugged"] else "Not plugged in")
    return {"name": c["name"], "soc": c["soc"], "limit": c["tick"], "range": c["range"] or "",
            "charging": c["charging"], "line": line, "low": c["attention"]}
